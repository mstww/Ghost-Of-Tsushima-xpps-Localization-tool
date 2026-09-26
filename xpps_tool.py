#!/usr/bin/env python3
"""
xpps_tool.py - Ghost of Tsushima Localization Tool (v3.0)

Extracts game text from KCAP .xpps localization files (lang_*_text.xpps) into
editable JSON and rebuilds game-ready .xpps files from translated JSON.

Commands
--------
  extract  -- Extract localization text from .xpps file(s) into editable JSON
  repack   -- Rebuild a .xpps file from translated JSON using the original as template
  verify   -- Structurally check a repacked .xpps against its original template
  to-list  -- Turn joined multi-line subtitles in a JSON into per-line lists
  diff     -- Compare two localization files and report translation progress

v3.0 rewrite of the repack engine
---------------------------------
Earlier versions patched the file with hard-coded offsets. The KNLI relocation
table was decoded incorrectly (the reloc count was treated as a pointer count,
so padding and the " DIC" footer were read as relocations and written back as
garbage fix-ups), the root object of the text table was not moved, and the
KNLI footer size was hard-coded to 208 bytes (wrong for ar/ja/zh files). Any
translation that did not fit in the old string pool therefore produced a
broken file.

The new engine is layout-preserving:
  * The original string pool is kept byte-for-byte; new/changed strings are
    appended after it, so every untouched pointer stays valid.
  * Everything after the pool (tables, font data, KNLI) is shifted by one
    4 KiB-aligned delta, and every pointer is rewritten from the KNLI
    relocation list (decoded and re-encoded exactly; round-trip is checked).
  * Header section sizes/offsets and the KNLI " DIC" footer are updated.
  * Repacking with no changes reproduces the template bit-for-bit.
  * After every repack the output is verified structurally against the
    template (every relocation, every non-pointer byte, every string).
"""

import os
import sys
import struct
import json
import re
import argparse
import time

if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

VERSION = '3.0'

LANG_MAP = {
    'lang_arabic_text.xpps':    'ar',
    'lang_brazilian_text.xpps': 'pt-br',
    'lang_british_text.xpps':   'en-gb',
    'lang_chinese_s_text.xpps': 'zh-cn',
    'lang_chinese_text.xpps':   'zh-tw',
    'lang_croatian_text.xpps':  'hr',
    'lang_czech_text.xpps':     'cs',
    'lang_danish_text.xpps':    'da',
    'lang_dutch_text.xpps':     'nl',
    'lang_english_text.xpps':   'en',
    'lang_finnish_text.xpps':   'fi',
    'lang_french_text.xpps':    'fr',
    'lang_german_text.xpps':    'de',
    'lang_greek_text.xpps':     'el',
    'lang_hungarian_text.xpps': 'hu',
    'lang_italian_text.xpps':   'it',
    'lang_japanese_text.xpps':  'ja',
    'lang_korean_text.xpps':    'ko',
    'lang_latino_text.xpps':    'es-mx',
    'lang_norwegian_text.xpps': 'no',
    'lang_polish_text.xpps':    'pl',
    'lang_portuguese_text.xpps': 'pt',
    'lang_russian_text.xpps':   'ru',
    'lang_spanish_text.xpps':   'es',
    'lang_swedish_text.xpps':   'sv',
    'lang_thai_text.xpps':      'th',
    'lang_turkish_text.xpps':   'tr',
}

PUA_PATTERN = re.compile(r'[-]')

KNLI_STREAM_OFF = 0x1c      # relocation word stream starts inside the KNLI header
POOL_ALIGN = 0x1000         # everything after the string pool moves by a multiple of this


class XppsError(Exception):
    pass


def strip_pua(text):
    """Strip Unicode Private Use Area gamepad button icons and collapse spaces."""
    cleaned = PUA_PATTERN.sub('', text)
    return re.sub(r'[ \t]+', ' ', cleaned).strip()


def _read_cstr(data, offset):
    end = data.find(b'\x00', offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode('utf-8', errors='replace')


def _u64(buf, off):
    return struct.unpack_from('<Q', buf, off)[0]


# ---------------------------------------------------------------------------
# KNLI relocation stream
# ---------------------------------------------------------------------------
#   0xC000 | p   -> select page p (page = 0x8000 dwords = 128 KiB)
#   0x8000 | n   -> n more relocations, each 8 bytes after the previous one
#   w < 0x8000   -> relocation at byte offset (page * 0x8000 + w) * 4
# Every relocation is a 64-bit pointer, relative to the payload start.
# header.count == number_of_words_from_0x1c - header.u6 (true for all 27 languages)

def decode_relocs(words):
    """Returns list of ops: ('rel', loc) or ('run', n, [locs])."""
    ops = []
    page = 0
    last = None
    for w in words:
        if (w & 0xC000) == 0xC000:
            page = w & 0x3FFF
        elif w & 0x8000:
            n = w & 0x3FFF
            if last is None:
                raise XppsError('KNLI: run op before any relocation')
            locs = [last + 8 * (k + 1) for k in range(n)]
            ops.append(('run', n, locs))
            last = locs[-1] if locs else last
        else:
            last = (page * 0x8000 + w) * 4
            ops.append(('rel', last))
    return ops


def encode_relocs(ops):
    words = []
    cur_page = None
    for op in ops:
        if op[0] == 'rel':
            loc = op[1]
            if loc % 4:
                raise XppsError(f'KNLI: unaligned relocation 0x{loc:x}')
            dw = loc // 4
            p, w = divmod(dw, 0x8000)
            if p > 0x3FFF:
                raise XppsError('KNLI: relocation beyond 2 GiB')
            if p != cur_page:
                words.append(0xC000 | p)
                cur_page = p
            words.append(w)
        else:
            words.append(0x8000 | op[1])
    return words


def reloc_locations(ops):
    out = []
    for op in ops:
        if op[0] == 'rel':
            out.append(op[1])
        else:
            out.extend(op[2])
    return out


# ---------------------------------------------------------------------------
# Container parser
# ---------------------------------------------------------------------------

def find_master_descriptor(sec_data, sec_start):
    """Master descriptor: [t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt]."""
    n = len(sec_data) - 48
    for strict in (True, False):
        for p in range(0, n, 8):
            t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt = struct.unpack_from('<6Q', sec_data, p)
            if not (t1_cnt > 0 and t2_cnt > 0 and t3_cnt > 0):
                continue
            if strict and t1_ptr not in (p + sec_start + 72, p + sec_start + 48):
                continue
            if not strict and not (t1_cnt > 1000 and t3_cnt > 1000):
                continue
            if t2_ptr == t1_ptr + t1_cnt * 16 and t3_ptr == t2_ptr + t2_cnt * 24:
                return p, t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt
    return None


class Xpps:
    def __init__(self, data, name='<xpps>'):
        self.data = bytes(data)
        self.name = name
        d = self.data
        if len(d) < 512 or d[:4] != b'KCAP':
            raise XppsError(f'{name}: not a KCAP .xpps file')
        H = struct.unpack_from('<I', d, 0x28)[0]
        self.H = H
        self.payload_size = struct.unpack_from('<I', d, 0x2c)[0]
        if H + self.payload_size != len(d):
            raise XppsError(f'{name}: payload size mismatch')
        self.P = d[H:]
        # section triplets (flags, size, offset) for text / font-data / KNLI
        self.trip_off = {'s6': H - 52, 's7': H - 40, 's8': H - 28}
        self.s6_flags, self.s6_size, self.s6 = struct.unpack_from('<III', d, H - 52)
        self.s7_flags, self.s7_size, self.s7 = struct.unpack_from('<III', d, H - 40)
        self.s8_flags, self.s8_size, self.s8 = struct.unpack_from('<III', d, H - 28)
        if self.s6 + self.s6_size != self.s7 or self.s7 + self.s7_size != self.s8 \
                or self.s8 + self.s8_size != self.payload_size:
            raise XppsError(f'{name}: unexpected section layout')
        self.S = self.P[self.s6:self.s6 + self.s6_size]

        res = find_master_descriptor(self.S, self.s6)
        if res is None:
            raise XppsError(f'{name}: master localization descriptor not found')
        (self.desc_pos, self.t1_ptr, self.t1_cnt, self.t2_ptr, self.t2_cnt,
         self.t3_ptr, self.t3_cnt) = res
        self._parse_knli()
        self._parse_tables()

    # -- KNLI --------------------------------------------------------------
    def _parse_knli(self):
        K = self.P[self.s8:self.s8 + self.s8_size]
        self.K = K
        if K[:4] != b'KNLI':
            raise XppsError(f'{self.name}: KNLI magic not found')
        (_, self.k_comp, _, _, self.k_count, self.k_u5, self.k_u6) = struct.unpack_from('<7I', K, 0)
        n_words = self.k_count + self.k_u6
        end = KNLI_STREAM_OFF + 2 * n_words
        self.k_words = list(struct.unpack_from(f'<{n_words}H', K, KNLI_STREAM_OFF))
        dic_off = (end + 7) & ~7
        if K[dic_off:dic_off + 4] != b' DIC' or self.k_comp != dic_off - 8:
            raise XppsError(f'{self.name}: KNLI layout not recognised')
        self.k_dic = K[dic_off:]
        self.k_ops = decode_relocs(self.k_words)
        if encode_relocs(self.k_ops) != self.k_words:
            raise XppsError(f'{self.name}: KNLI stream does not round-trip')
        self.relocs = reloc_locations(self.k_ops)

    # -- text tables -------------------------------------------------------
    def _parse_tables(self):
        S, base = self.S, self.s6
        self.t1 = []     # (hash, ptr_loc_payload, str_off_s6)
        o = self.t1_ptr - base
        for i in range(self.t1_cnt):
            h, v = struct.unpack_from('<QQ', S, o + 16 * i)
            self.t1.append((h, self.t1_ptr + 16 * i + 8, v - base))
        self.t3 = []     # (hash, [(sub_hash, ptr_loc_payload, str_off_s6), ...])
        o = self.t3_ptr - base
        for i in range(self.t3_cnt):
            h, sp, sc = struct.unpack_from('<QQQ', S, o + 24 * i)
            parts = []
            for j in range(int(sc)):
                sh, v = struct.unpack_from('<QQ', S, sp - base + 16 * j)
                parts.append((sh, sp + 16 * j + 8, v - base))
            self.t3.append((h, parts))
        # end of the string pool / start of the tables block
        str_offs = [s for _, _, s in self.t1] + [s for _, ps in self.t3 for _, _, s in ps]
        pool_end = 0
        for s in str_offs:
            if 0 <= s < self.desc_pos:
                e = S.index(b'\x00', s) + 1
                if e > pool_end:
                    pool_end = e
        # the first object after the strings (e.g. the root object that owns the
        # descriptor) marks the beginning of the table block
        tab = self.desc_pos
        for loc in self.relocs:
            v = _u64(self.P, loc)
            if self.s6 + pool_end <= v < self.s6 + self.desc_pos:
                tab = min(tab, v - self.s6)
        if any(S[pool_end:tab]):
            raise XppsError(f'{self.name}: unexpected data between string pool and tables')
        self.pool_end = pool_end
        self.tab_start = tab

    def text(self, off):
        return _read_cstr(self.S, off)

    def strings(self, clean_pua=False, parts_as_list=False):
        out = {}
        for h, _, s in self.t1:
            t = self.text(s)
            out[f'{h:016x}'] = strip_pua(t) if clean_pua else t
        for h, parts in self.t3:
            ps = [self.text(s).strip() for _, _, s in parts]
            if clean_pua:
                ps = [strip_pua(p) for p in ps]
            if parts_as_list and len(parts) > 1:
                out[f'{h:016x}'] = ps
            else:
                out[f'{h:016x}'] = ' '.join(p for p in ps if p)
        return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_xpps(file_path, clean_pua=False, parts_as_list=False):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f'File not found: {file_path}')
    with open(file_path, 'rb') as f:
        x = Xpps(f.read(), file_path)
    return x.strings(clean_pua=clean_pua, parts_as_list=parts_as_list)


# ---------------------------------------------------------------------------
# Repack
# ---------------------------------------------------------------------------

_WORD_SPLIT = re.compile(r'[ \t\r\n]+')          # NBSP stays inside a word
_PUNCT_END = ('.', '!', '?', '\u2026', ',', ';', ':', '"', '\u201d', '\u00bb', ')')


def _words(text):
    return [w for w in _WORD_SPLIT.split(text) if w]


def _split_proportional(text, orig_parts):
    """Fallback for a completely new text: cut into len(orig_parts) lines, following
    the length ratio of the original lines and preferring cuts after punctuation."""
    n = len(orig_parts)
    words = _words(text)
    if len(words) <= n:
        return words + [''] * (n - len(words))
    ends, pos = [], 0                     # char position after each word
    for w in words:
        pos += len(w) + 1
        ends.append(pos)
    total = ends[-1]
    weights = [max(1, len(p)) for p in orig_parts]
    wsum = float(sum(weights))
    cuts, acc, prev = [], 0.0, -1         # cut after word index
    for k in range(n - 1):
        acc += weights[k] / wsum * total
        lo = prev + 1                             # at least one word per line
        hi = len(words) - (n - 1 - k) - 1         # leave one word for every later line
        window = 0.35 * total / n
        best, best_score = None, None
        for i in range(lo, hi + 1):
            d = abs(ends[i] - acc)
            if d > window and best is not None:
                continue
            score = d - (window * 0.8 if words[i].endswith(_PUNCT_END) else 0)
            if best_score is None or score < best_score:
                best, best_score = i, score
        cuts.append(best)
        prev = best
    out, start = [], 0
    for c in cuts + [len(words) - 1]:
        out.append(' '.join(words[start:c + 1]))
        start = c + 1
    return out


def _split_parts(text, orig_parts):
    """Distribute an edited, space-joined multi-line subtitle back onto its timed lines.

    Each line of a Table-3 entry has its own on-screen time window (start/end are
    encoded in the line's sub-hash), so the text must be split where the original
    lines were split. Words that still match the original are aligned to their
    original line (difflib); inserted words stay with the preceding line. A text
    with little in common with the original falls back to a proportional split
    that prefers cuts after punctuation."""
    import difflib
    new_w = _words(text)
    orig_w, owner = [], []
    for i, p in enumerate(orig_parts):
        for w in _words(p):
            orig_w.append(w)
            owner.append(i)
    if not new_w:
        return [''] * len(orig_parts)
    sm = difflib.SequenceMatcher(None, orig_w, new_w, autojunk=False)
    matched = sum(bl.size for bl in sm.get_matching_blocks())
    if not orig_w or matched < 0.5 * len(orig_w):
        return _split_proportional(text, orig_parts)
    assign = [None] * len(new_w)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(j2 - j1):
                assign[j1 + k] = owner[i1 + k]
        elif tag == 'replace':
            for k in range(j2 - j1):
                assign[j1 + k] = owner[i1 + (k * (i2 - i1)) // (j2 - j1)]
    # inserted words: attach to the previous word's line (or the next one at the start)
    cur = next((a for a in assign if a is not None), 0)
    for j in range(len(new_w)):
        if assign[j] is None:
            assign[j] = cur
        else:
            cur = max(cur, assign[j])      # keep lines in order
            assign[j] = cur
    out = [[] for _ in orig_parts]
    for w, a in zip(new_w, assign):
        out[a].append(w)
    return [' '.join(ws) for ws in out]


def _plan_changes(x, new_strings, allow_blank=False):
    """Returns ({ptr_loc_payload: new_text}, [kept_blank_keys]).

    Table-1 strings that are empty or whitespace-only in the original ('', ' ', '\\n' ...)
    are layout pieces the game glues between other texts (e.g. between a subtitle and
    the next line). Changing them makes text appear in places it should not, so they are
    kept unless allow_blank=True."""
    changes = {}
    kept_blank = []
    for h, loc, s in x.t1:
        k = f'{h:016x}'
        if k not in new_strings:
            continue
        v = new_strings[k]
        if isinstance(v, list):
            v = ' '.join(v)
        orig = x.text(s)
        if v != orig:
            if not allow_blank and orig.strip() == '':
                kept_blank.append(k)
                continue
            changes[loc] = v
    for h, parts in x.t3:
        k = f'{h:016x}'
        if k not in new_strings:
            continue
        v = new_strings[k]
        orig = [x.text(s).strip() for _, _, s in parts]
        n = len(parts)
        if isinstance(v, list):
            if len(v) != n:
                if n == 1:
                    v = ' '.join(v)
                else:
                    v = ' '.join(v)
            else:
                pieces = [str(p) for p in v]
                for (sh, loc, s), new, old in zip(parts, pieces, orig):
                    if new.strip() != old:
                        changes[loc] = new
                continue
        if v == ' '.join(p for p in orig if p):
            continue                      # unchanged
        if n == 1:
            changes[parts[0][1]] = v
            continue
        lines = v.split('\n')
        if len(lines) == n:
            pieces = [l.strip() for l in lines]
        else:
            pieces = _split_parts(v, orig)
        for (sh, loc, s), new, old in zip(parts, pieces, orig):
            if new != old:
                changes[loc] = new
    return changes, kept_blank


def repack_bytes(template_bytes, new_strings, name='<template>', allow_blank=False):
    x = Xpps(template_bytes, name)
    changes, kept_blank = _plan_changes(x, new_strings, allow_blank)
    if not changes:
        return bytes(template_bytes), x, {'kept_blank': kept_blank} if kept_blank else {}

    S = x.S
    # ---- 1. new string blob appended after the original pool ---------------
    existing = {}
    for _, _, s in x.t1:
        existing.setdefault(x.text(s), s)
    for _, ps in x.t3:
        for _, _, s in ps:
            existing.setdefault(x.text(s), s)
    blob = bytearray()
    new_ptr = {}                 # ptr_loc -> new s6-relative string offset
    added = {}
    for loc in sorted(changes):
        t = changes[loc]
        if t in existing:
            new_ptr[loc] = existing[t]
            continue
        if t not in added:
            added[t] = x.tab_start + len(blob)
            blob += t.encode('utf-8') + b'\x00'
        new_ptr[loc] = added[t]
    delta = (len(blob) + POOL_ALIGN - 1) // POOL_ALIGN * POOL_ALIGN
    blob += b'\x00' * (delta - len(blob))

    s6, s6_end = x.s6, x.s6 + x.s6_size

    def mapv(v):
        # payload offset in template -> payload offset in output
        if v < s6 + x.tab_start:
            return v                  # headers and original string pool do not move
        return v + delta

    # ---- 2. build the new payload -------------------------------------------
    P = x.P
    new_P = bytearray()
    new_P += P[:s6 + x.tab_start]
    new_P += blob
    new_P += P[s6 + x.tab_start:x.s8]          # tables + font data (shifted)
    new_s8 = x.s8 + delta

    # ---- 3. rewrite every relocated pointer ------------------------------
    for loc in x.relocs:
        nloc = mapv(loc)
        v = _u64(P, loc)
        if loc in new_ptr:
            nv = s6 + new_ptr[loc]
        else:
            nv = mapv(v)
        struct.pack_into('<Q', new_P, nloc, nv)
    missing = set(new_ptr) - set(x.relocs)
    if missing:
        raise XppsError(f'{len(missing)} string pointers are not in the relocation table')

    # ---- 4. KNLI ------------------------------------------------------------
    new_ops = []
    for op in x.k_ops:
        if op[0] == 'rel':
            new_ops.append(('rel', mapv(op[1])))
        else:
            new_ops.append(op)
    words = encode_relocs(new_ops)
    stream = struct.pack(f'<{len(words)}H', *words)
    end = KNLI_STREAM_OFF + len(stream)
    dic_off = (end + 7) & ~7
    K = bytearray(x.K[:KNLI_STREAM_OFF])
    K += stream
    K += b'\x00' * (dic_off - end)
    dic = bytearray(x.k_dic)
    # " DIC" footer: [magic, size, count(u64), count x (ptr+0x10, type_hash), ...]
    n_dic = _u64(dic, 8)
    for i in range(n_dic):
        o = 16 + 16 * i
        v = _u64(dic, o)
        struct.pack_into('<Q', dic, o, mapv(v - 0x10) + 0x10)
    K += dic
    struct.pack_into('<I', K, 4, dic_off - 8)
    struct.pack_into('<I', K, 16, len(words) - x.k_u6)
    new_P += K

    # ---- 5. header ------------------------------------------------------------
    hdr = bytearray(x.data[:x.H])
    old = {'s7': x.s7, 's8': x.s8, 'k': x.s8_size}
    new = {'s7': x.s7 + delta, 's8': new_s8, 'k': len(K)}
    struct.pack_into('<I', hdr, 0x2c, len(new_P))
    struct.pack_into('<III', hdr, x.trip_off['s6'], x.s6_flags, x.s6_size + delta, x.s6)
    struct.pack_into('<III', hdr, x.trip_off['s7'], x.s7_flags, x.s7_size, new['s7'])
    struct.pack_into('<III', hdr, x.trip_off['s8'], x.s8_flags, new['k'], new['s8'])
    # memory-segment table in the upper header repeats s7 offset, KNLI size/offset
    for off in range(0x30, x.H - 64, 4):
        v = struct.unpack_from('<I', hdr, off)[0]
        for key in ('s7', 's8', 'k'):
            if v == old[key] and old[key] != new[key]:
                struct.pack_into('<I', hdr, off, new[key])
                break
    out = bytes(hdr) + bytes(new_P)
    info = {'changed_pointers': len(changes), 'appended_bytes': len(blob), 'delta': delta,
            'kept_blank': kept_blank}
    return out, x, info


def verify_repack(template_bytes, out_bytes, new_strings=None):
    """Structural check of an output file against its template. Returns list of problems."""
    probs = []
    a = Xpps(template_bytes, 'template')
    try:
        b = Xpps(out_bytes, 'output')
    except XppsError as e:
        return [f'output does not parse: {e}']
    delta = b.s7 - a.s7
    if delta < 0 or delta % 16:
        probs.append(f'bad section delta {delta}')
    for k in ('s6', 's7_size', 't1_cnt', 't2_cnt', 't3_cnt', 'k_u5', 'k_u6'):
        if getattr(a, k) != getattr(b, k):
            probs.append(f'{k} differs')
    if b.desc_pos != a.desc_pos + delta:
        probs.append('descriptor moved unexpectedly')
    if b.s6_size != a.s6_size + delta or b.s8 != a.s8 + delta:
        probs.append('section sizes inconsistent')
    if b.P[b.s7:b.s7 + b.s7_size] != a.P[a.s7:a.s7 + a.s7_size]:
        probs.append('font/texture section changed')
    if len(a.relocs) != len(b.relocs):
        probs.append('relocation count differs')
        return probs

    def mapv(v):
        return v if v < a.s6 + a.tab_start else v + delta
    str_locs = {loc for _, loc, _ in a.t1} | {loc for _, ps in a.t3 for _, loc, _ in ps}
    bad = 0
    for la, lb in zip(a.relocs, b.relocs):
        if mapv(la) != lb:
            bad += 1
            continue
        va, vb = _u64(a.P, la), _u64(b.P, lb)
        if la in str_locs:
            if not (b.s6 <= vb < b.s6 + b.tab_start) or (vb - b.s6 and b.S[vb - b.s6 - 1] != 0):
                bad += 1
        elif mapv(va) != vb:
            bad += 1
    if bad:
        probs.append(f'{bad} relocated pointers are wrong')
    # every non-pointer byte of headers + tables must be unchanged
    ma = bytearray(a.P[:a.s6]) + bytearray(a.P[a.s6 + a.tab_start:a.s8])
    mb = bytearray(b.P[:b.s6]) + bytearray(b.P[b.s6 + b.tab_start:b.s8])
    if len(ma) != len(mb):
        probs.append('table block size differs')
    else:
        def to_idx(loc, x):
            return loc if loc < x.s6 else loc - (x.s6 + x.tab_start) + x.s6
        for loc in a.relocs:
            i = to_idx(loc, a)
            ma[i:i + 8] = b'\0' * 8
        for loc in b.relocs:
            i = to_idx(loc, b)
            mb[i:i + 8] = b'\0' * 8
        if ma != mb:
            n = sum(1 for p, q in zip(ma, mb) if p != q)
            probs.append(f'{n} non-pointer bytes in tables differ')
    dic_a, dic_b = a.k_dic, b.k_dic
    if len(dic_a) != len(dic_b):
        probs.append('DIC footer size differs')
    else:
        for i in range(_u64(dic_b, 8)):
            o = 16 + 16 * i
            if _u64(dic_b, o) != mapv(_u64(dic_a, o) - 0x10) + 0x10 or dic_a[o + 8:o + 16] != dic_b[o + 8:o + 16]:
                probs.append('DIC footer pointers are wrong')
                break
        n = _u64(dic_a, 8)
        if dic_a[16 + 16 * n:] != dic_b[16 + 16 * n:] or dic_a[:16] != dic_b[:16]:
            probs.append('DIC footer header/trailer changed')
    # header: only size/offset fields may change
    ha, hb = a.data[:a.H], b.data[:b.H]
    changed = [o for o in range(0, a.H, 4) if ha[o:o + 4] != hb[o:o + 4]]
    for o in changed:
        va, vb = struct.unpack_from('<I', ha, o)[0], struct.unpack_from('<I', hb, o)[0]
        if vb - va not in (delta, b.s8_size - a.s8_size, len(b.P) - len(a.P)):
            probs.append(f'unexpected header change at 0x{o:x}')
    if new_strings is not None:
        got = b.strings()
        want = a.strings()
        for k, v in new_strings.items():
            if k in want:
                want[k] = ' '.join(p for p in (s.strip() for s in v) if p) if isinstance(v, list) else v
        # multi-part entries are stored split; compare with whitespace normalised
        norm = lambda s: ' '.join(s.split())
        diff = [k for k in want if norm(want[k]) != norm(got.get(k, ''))]
        if diff:
            probs.append(f'{len(diff)} strings do not read back as expected (e.g. {diff[0]})')
    return probs


def repack_xpps(template_path, new_strings, output_path, verify=True, allow_blank=False):
    with open(template_path, 'rb') as f:
        tpl = f.read()
    out, x, info = repack_bytes(tpl, new_strings, template_path, allow_blank)
    expected = {k: v for k, v in new_strings.items() if k not in set(info.get('kept_blank', ()))}
    probs = verify_repack(tpl, out, expected) if verify else []
    if probs:
        raise XppsError('verification failed, output NOT written:\n  - ' + '\n  - '.join(probs))
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(out)
    return len(out), info


def _load_strings(file_path):
    if not os.path.exists(file_path):
        raise FileNotFoundError(f'File not found: {file_path}')
    with open(file_path, 'rb') as f:
        magic = f.read(4)
    if magic == b'KCAP':
        return extract_xpps(file_path)
    with open(file_path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f'Expected JSON object in {file_path}')
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _write_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def cmd_extract(args):
    kw = dict(clean_pua=args.clean_pua, parts_as_list=args.parts_as_list)
    if os.path.isfile(args.input):
        base = os.path.basename(args.input)
        lang = LANG_MAP.get(base, 'extracted')
        print(f'Extracting: {args.input}  (language: {lang})')
        strings = extract_xpps(args.input, **kw)
        print(f'  [+] {len(strings)} strings extracted')
        if args.output and args.output.lower().endswith('.json'):
            out = args.output
        else:
            out = os.path.join(args.output or 'translations', lang, 'strings.json')
        _write_json(out, strings)
        print(f'  [>] Saved -> {out}')
    elif os.path.isdir(args.input):
        found = [(LANG_MAP.get(f, os.path.splitext(f)[0]), os.path.join(args.input, f))
                 for f in sorted(os.listdir(args.input)) if f.lower().endswith('_text.xpps')]
        if not found:
            print(f'Error: no *_text.xpps files in {args.input}')
            sys.exit(1)
        out_dir = args.output or 'translations'
        for code, fp in found:
            t0 = time.time()
            strings = extract_xpps(fp, **kw)
            out = os.path.join(out_dir, code, 'strings.json')
            _write_json(out, strings)
            print(f'  [>] {os.path.basename(fp):<27} {len(strings):>6} strings -> {out} ({time.time()-t0:.1f}s)')
        print(f'\n[OK] Batch extraction complete: {out_dir}')
    else:
        print(f"Error: '{args.input}' is neither a file nor a directory.")
        sys.exit(1)


def cmd_repack(args):
    if not os.path.exists(args.strings_file):
        print(f'Error: translation file not found: {args.strings_file}')
        sys.exit(1)
    if not os.path.exists(args.template):
        print(f'Error: template file not found: {args.template}')
        sys.exit(1)
    output = args.output or '{0}_modded{1}'.format(*os.path.splitext(args.template))
    if os.path.abspath(output) == os.path.abspath(args.template):
        print('Error: output must not overwrite the template (keep the original as template).')
        sys.exit(1)
    print(f'Loading translations: {args.strings_file}')
    new_strings = _load_strings(args.strings_file)
    print(f'  Strings:     {len(new_strings):,}')
    print(f'  Template:    {args.template}')
    print(f'  Destination: {output}')
    t0 = time.time()
    try:
        size, info = repack_xpps(args.template, new_strings, output, verify=not args.no_verify,
                                 allow_blank=args.allow_blank)
    except XppsError as e:
        print(f'\n[ERROR] {e}')
        sys.exit(2)
    kept = info.get('kept_blank', [])
    if kept:
        print(f"\n  [!] {len(kept)} empty/whitespace-only layout strings were left unchanged "
              f"(e.g. {kept[0]}).\n      The game joins them between other texts; use --allow-blank to edit them anyway.")
    if not info.get('changed_pointers'):
        print('\n[OK] No strings differ from the template; output is identical to the template.')
    else:
        print(f"\n  Changed string pointers: {info['changed_pointers']:,}")
        print(f"  Added text:              {info['appended_bytes']:,} bytes")
    print(f'[OK] Written {output}  ({size:,} bytes, {time.time()-t0:.1f}s)')
    if not args.no_verify:
        print('[OK] Structural verification passed (relocations, tables, KNLI, all strings).')


def cmd_verify(args):
    with open(args.template, 'rb') as f:
        a = f.read()
    with open(args.file, 'rb') as f:
        b = f.read()
    ns = _load_strings(args.strings) if args.strings else None
    probs = verify_repack(a, b, ns)
    if probs:
        print('[FAIL]')
        for p in probs:
            print('  -', p)
        sys.exit(2)
    print('[OK] File is structurally consistent with the template.')


def cmd_to_list(args):
    """Convert a JSON with space-joined subtitles into per-line lists, split exactly
    like `repack` would split them, so every timed line can be checked by hand."""
    with open(args.template, 'rb') as f:
        x = Xpps(f.read(), args.template)
    data = _load_strings(args.strings_file)
    n = 0
    for h, parts in x.t3:
        k = f'{h:016x}'
        if len(parts) < 2 or k not in data or isinstance(data[k], list):
            continue
        orig = [x.text(s).strip() for _, _, s in parts]
        v = data[k]
        lines = v.split('\n')
        data[k] = [l.strip() for l in lines] if len(lines) == len(parts) else _split_parts(v, orig)
        n += 1
    out = args.output or os.path.splitext(args.strings_file)[0] + '_lines.json'
    _write_json(out, data)
    print(f'[OK] {n} multi-line subtitles converted to lists -> {out}')


def cmd_diff(args):
    orig = _load_strings(args.file1)
    trans = _load_strings(args.file2)
    as_s = lambda v: ' '.join(v) if isinstance(v, list) else v
    common = set(orig) & set(trans)
    modified = {k: (as_s(orig[k]), as_s(trans[k])) for k in common if as_s(orig[k]) != as_s(trans[k])}
    pct = len(modified) / len(orig) * 100 if orig else 0.0
    print('=' * 50)
    print(f'  Original strings:   {len(orig):>8,}')
    print(f'  Translated strings: {len(trans):>8,}')
    print(f'  Modified:           {len(modified):>8,}  ({pct:.2f}%)')
    print(f'  Identical:          {len(common) - len(modified):>8,}')
    if set(orig) - set(trans):
        print(f'  Missing in translated: {len(set(orig) - set(trans)):,}')
    if set(trans) - set(orig):
        print(f'  Unknown keys (ignored on repack): {len(set(trans) - set(orig)):,}')
    for k, (o, t) in list(modified.items())[:5]:
        print(f'  [{k}]\n    - {o[:70]}\n    + {t[:70]}')
    if args.output:
        _write_json(args.output, {k: trans[k] for k in modified})
        print(f'\n[OK] Exported {len(modified)} modified strings to {args.output}')


def build_parser():
    p = argparse.ArgumentParser(prog='xpps_tool',
                                description=f'Ghost of Tsushima Localization Tool v{VERSION}')
    sub = p.add_subparsers(dest='command', metavar='<command>')

    e = sub.add_parser('extract', help='Extract text from .xpps file(s) into JSON')
    e.add_argument('input', help='.xpps file or folder with lang_*_text.xpps files')
    e.add_argument('-o', '--output', help='output .json (single file) or directory')
    e.add_argument('--clean-pua', action='store_true', help='strip controller glyphs (do not repack such output)')
    e.add_argument('--parts-as-list', action='store_true',
                   help='write multi-part subtitles as JSON lists (one item per subtitle line)')

    r = sub.add_parser('repack', help='Rebuild a .xpps from translated JSON')
    r.add_argument('strings_file', help='translated strings.json')
    r.add_argument('-t', '--template', required=True, help='ORIGINAL game .xpps used as template')
    r.add_argument('-o', '--output', help='output path (default: <template>_modded.xpps)')
    r.add_argument('--no-verify', action='store_true', help='skip the structural verification')
    r.add_argument('--allow-blank', action='store_true',
                   help='also change strings that are empty/whitespace in the original (layout pieces)')
    r.add_argument('--force-expand', action='store_true', help=argparse.SUPPRESS)  # v2 compatibility

    v = sub.add_parser('verify', help='Check a repacked .xpps against the original template')
    v.add_argument('file', help='repacked .xpps')
    v.add_argument('-t', '--template', required=True, help='original .xpps')
    v.add_argument('-s', '--strings', help='optional strings.json the file was built from')

    tl = sub.add_parser('to-list', help='Split joined multi-line subtitles in a JSON into per-line lists')
    tl.add_argument('strings_file', help='translated strings.json')
    tl.add_argument('-t', '--template', required=True, help='original .xpps')
    tl.add_argument('-o', '--output', help='output json (default: <name>_lines.json)')

    d = sub.add_parser('diff', help='Compare two localization files')
    d.add_argument('file1')
    d.add_argument('file2')
    d.add_argument('-o', '--output', help='export modified strings as JSON')
    return p


def main():
    print('=' * 70)
    print(f'Ghost of Tsushima Localization Tool  v{VERSION}')
    print('=' * 70)
    if len(sys.argv) == 2 and sys.argv[1].lower().endswith('.xpps') and os.path.isfile(sys.argv[1]):
        fp = sys.argv[1]
        out = os.path.splitext(fp)[0] + '_strings.json'
        strings = extract_xpps(fp)
        _write_json(out, strings)
        print(f'[OK] Extracted {len(strings)} strings -> {out}')
        return
    parser = build_parser()
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()
    {'extract': cmd_extract, 'repack': cmd_repack, 'verify': cmd_verify,
     'to-list': cmd_to_list, 'diff': cmd_diff}.get(args.command, lambda a: parser.print_help())(args)


if __name__ == '__main__':
    main()
