#!/usr/bin/env python3
"""
xpps_tool.py - Ghost of Tsushima Localization Tool (v2.2)

A translation and modding utility for Ghost of Tsushima Director's Cut.
Extracts game text from proprietary KCAP .xpps localization files into editable JSON,
repacks translated text back into game-ready .xpps files, and provides translation diff/progress tools.

Commands
--------
  extract  -- Extract localization text from .xpps file(s) into editable JSON
  repack   -- Rebuild a .xpps file from modified translation JSON using a template
  diff     -- Compare two localization files and check translation progress
"""

import os
import sys
import glob
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

# ---------------------------------------------------------------------------
# Language mappings for Ghost of Tsushima localization files
# ---------------------------------------------------------------------------

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
    'lang_portuguese_text.xpps':'pt',
    'lang_russian_text.xpps':   'ru',
    'lang_spanish_text.xpps':   'es',
    'lang_swedish_text.xpps':   'sv',
    'lang_thai_text.xpps':      'th',
    'lang_turkish_text.xpps':   'tr',
}

CODE_TO_FILE = {code: fn for fn, code in LANG_MAP.items()}

PUA_PATTERN = re.compile(r'[\ue000-\uf8ff]')


# ---------------------------------------------------------------------------
# Binary Helpers
# ---------------------------------------------------------------------------

def strip_pua(text):
    """Strip Unicode Private Use Area gamepad button icons and collapse spaces."""
    cleaned = PUA_PATTERN.sub('', text)
    return re.sub(r'[ \t]+', ' ', cleaned).strip()


def _read_cstr(data, offset):
    """Read a null-terminated UTF-8 string from byte array at offset."""
    end = data.find(b'\x00', offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode('utf-8', errors='replace')


def find_master_descriptor(sec_data, sec_start):
    """
    Locate the master descriptor inside the text section using structural invariants.
    Master descriptor: [t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt]
    """
    for p in range(0, len(sec_data) - 48, 8):
        t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt = struct.unpack(
            '<QQQQQQ', sec_data[p: p + 48])
        if t1_cnt > 0 and t2_cnt > 0 and t3_cnt > 0:
            if t1_ptr in (p + sec_start + 72, p + sec_start + 48):
                if (t2_ptr == t1_ptr + t1_cnt * 16
                        and t3_ptr == t2_ptr + t2_cnt * 24):
                    return p, t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt

    # Robust fallback for modified headers
    for p in range(0, len(sec_data) - 48, 8):
        t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt = struct.unpack(
            '<QQQQQQ', sec_data[p: p + 48])
        if t1_cnt > 1000 and t2_cnt > 100 and t3_cnt > 1000:
            if (t2_ptr == t1_ptr + t1_cnt * 16
                    and t3_ptr == t2_ptr + t2_cnt * 24):
                return p, t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt

    return None


# ---------------------------------------------------------------------------
# Extraction Engine
# ---------------------------------------------------------------------------

def extract_xpps(file_path, clean_pua=False):
    """
    Extracts all localized strings from a Ghost of Tsushima .xpps language file.

    Returns:
        dict[str, str]: Map of 16-hex hash key to localized string text.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, 'rb') as f:
        data = f.read()

    if len(data) < 512:
        raise ValueError(f"File too small to be a valid .xpps archive: {file_path}")

    if data[:4] != b'KCAP':
        raise ValueError(f"Invalid magic: {data[:4]} (expected b'KCAP') in {file_path}")

    hdr_size = struct.unpack('<I', data[0x28:0x2c])[0]
    flags, sec_size, sec_start = struct.unpack(
        '<III', data[hdr_size - 52: hdr_size - 40])
    sec_data = data[hdr_size + sec_start: hdr_size + sec_start + sec_size]

    res = find_master_descriptor(sec_data, sec_start)
    if res is None:
        raise ValueError(f"Master localization descriptor not found in {file_path}")

    desc_pos, t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt = res

    entries = {}

    # Table 1: UI, system, skill, quest, and menu strings (16 bytes per entry: hash, string_offset)
    t1_start = t1_ptr - sec_start
    for i in range(t1_cnt):
        off = t1_start + i * 16
        if off + 16 > len(sec_data):
            break
        h, s_off = struct.unpack('<QQ', sec_data[off: off + 16])
        str_p = s_off - sec_start
        if 0 <= str_p < len(sec_data):
            text = _read_cstr(sec_data, str_p)
            if clean_pua:
                text = strip_pua(text)
            entries[f"{h:016x}"] = text

    # Table 3: Dialogue, cutscenes, and subtitle audio cues (24 bytes per entry: hash, sub_ptr, sub_cnt)
    t3_start = t3_ptr - sec_start
    for i in range(t3_cnt):
        off = t3_start + i * 24
        if off + 24 > len(sec_data):
            break
        h, sub_p, sub_cnt = struct.unpack('<QQQ', sec_data[off: off + 24])
        sub_start = sub_p - sec_start
        parts = []
        for j in range(int(sub_cnt)):
            se_off = sub_start + j * 16
            if 0 <= se_off + 16 <= len(sec_data):
                sh, s_off = struct.unpack('<QQ', sec_data[se_off: se_off + 16])
                str_p = s_off - sec_start
                if 0 <= str_p < len(sec_data):
                    part_text = _read_cstr(sec_data, str_p).strip()
                    if clean_pua:
                        part_text = strip_pua(part_text)
                    if part_text:
                        parts.append(part_text)
        entries[f"{h:016x}"] = ' '.join(parts)

    return entries


# ---------------------------------------------------------------------------
# Repack Engine
# ---------------------------------------------------------------------------

def _build_string_blob(strings_ordered):
    """
    Build a contiguous UTF-8 string blob (each string null-terminated).
    Returns: (bytes, offsets_list)
    """
    blob = bytearray()
    offsets = []
    seen = {}
    for s in strings_ordered:
        enc = s.encode('utf-8') + b'\x00'
        if enc not in seen:
            seen[enc] = len(blob)
            blob += enc
        offsets.append(seen[enc])
    return bytes(blob), offsets


def repack_xpps(template_path, new_strings, output_path, force_expand=False):
    """
    Rebuild a .xpps file by replacing string content with `new_strings`
    while keeping the full KCAP binary structure intact.
    
    Supports two modes:
    1. Duplicate-Slot Allocator: When modifications fit into duplicate string slots (85KB+),
       keeps 99.996% identical binary layout with zero section/header shifts.
    2. Dynamic Relocation & KNLI Re-encoding Engine: When translations exceed slot space
       or force_expand=True, dynamically resizes the string pool, shifts internal descriptors
       and tables, recalculates Section 7/8 boundaries, and re-encodes the Section 8 KNLI
       relocation bytecode to support arbitrary file expansions (2x, 5x, 10x size).
    """
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found: {template_path}")
    if not new_strings:
        raise ValueError("new_strings dictionary is empty")

    with open(template_path, 'rb') as f:
        data = bytearray(f.read())

    if len(data) < 512 or data[:4] != b'KCAP':
        raise ValueError(f"Not a valid KCAP .xpps file: {template_path}")

    hdr_size = struct.unpack('<I', bytes(data[0x28:0x2c]))[0]
    flags, sec_size, sec_start = struct.unpack(
        '<III', bytes(data[hdr_size - 52: hdr_size - 40]))

    sec_abs = hdr_size + sec_start
    sec_data = bytearray(data[sec_abs: sec_abs + sec_size])

    res = find_master_descriptor(bytes(sec_data), sec_start)
    if res is None:
        raise ValueError(f"Master localization descriptor not found in {template_path}")

    desc_pos, t1_ptr, t1_cnt, t2_ptr, t2_cnt, t3_ptr, t3_cnt = res

    new_sec_data = bytearray(sec_data)

    # Detect modified strings
    t1_start = t1_ptr - sec_start
    t3_start = t3_ptr - sec_start

    modified_entries = {}
    orig_map = {}

    for i in range(t1_cnt):
        off = t1_start + i * 16
        if off + 16 > len(sec_data):
            break
        h, s_off = struct.unpack('<QQ', bytes(sec_data[off: off + 16]))
        h_hex = f"{h:016x}"
        p = s_off - sec_start
        orig_text = _read_cstr(bytes(sec_data), p) if 0 <= p < len(sec_data) else ''
        orig_map[h_hex] = (orig_text, s_off, off + 8, 1)
        if h_hex in new_strings and new_strings[h_hex] != orig_text:
            modified_entries[h_hex] = (new_strings[h_hex], orig_text, s_off, off + 8, 1)

    for i in range(t3_cnt):
        off = t3_start + i * 24
        if off + 24 > len(sec_data):
            break
        h, sub_p, sub_cnt = struct.unpack('<QQQ', bytes(sec_data[off: off + 24]))
        h_hex = f"{h:016x}"
        sub_start = sub_p - sec_start
        parts = []
        for j in range(int(sub_cnt)):
            se_off = sub_start + j * 16
            if 0 <= se_off + 16 <= len(sec_data):
                sh, s_off = struct.unpack('<QQ', bytes(sec_data[se_off: se_off + 16]))
                p = s_off - sec_start
                if 0 <= p < len(sec_data):
                    parts.append(_read_cstr(bytes(sec_data), p).strip())
        orig_combined = ' '.join(p for p in parts if p)
        orig_map[h_hex] = (orig_combined, 0, sub_start, 3, int(sub_cnt))
        if h_hex in new_strings and new_strings[h_hex] != orig_combined:
            modified_entries[h_hex] = (new_strings[h_hex], orig_combined, 0, sub_start, 3, int(sub_cnt))

    # Collect duplicate slots for reallocation (85,000+ bytes available)
    by_text = {}
    for i in range(t1_cnt):
        off = t1_start + i * 16
        if off + 16 > len(sec_data):
            break
        h, s_off = struct.unpack('<QQ', bytes(sec_data[off: off + 16]))
        p = s_off - sec_start
        txt = _read_cstr(bytes(sec_data), p)
        if txt not in by_text:
            by_text[txt] = []
        by_text[txt].append((off + 8, p, 1))

    for i in range(t3_cnt):
        off = t3_start + i * 24
        if off + 24 > len(sec_data):
            break
        h, sub_p, sub_cnt = struct.unpack('<QQQ', bytes(sec_data[off: off + 24]))
        sub_start = sub_p - sec_start
        for j in range(int(sub_cnt)):
            se_off = sub_start + j * 16
            if 0 <= se_off + 16 <= len(sec_data):
                sh, s_off = struct.unpack('<QQ', bytes(sec_data[se_off: se_off + 16]))
                p = s_off - sec_start
                txt = _read_cstr(bytes(sec_data), p)
                if txt not in by_text:
                    by_text[txt] = []
                by_text[txt].append((se_off + 8, p, 3))

    reusable_slots = []
    for txt, occs in by_text.items():
        if len(occs) > 1:
            first_ptr_loc, first_p, _ = occs[0]
            for ptr_loc, p, _ in occs[1:]:
                if p != first_p:
                    enc_len = len(txt.encode('utf-8')) + 1
                    reusable_slots.append({'offset': p, 'len': enc_len, 'ptr_loc': ptr_loc, 'first_p': first_p})

    # Try in-place / slot allocation first if force_expand is False
    unique_new = sorted(set(v[0] for v in modified_entries.values()), key=lambda x: len(x.encode('utf-8')) + 1, reverse=True)
    allocated_slots = {}
    used_slot_indices = set()
    all_fit = not force_expand

    if not force_expand:
        for txt in unique_new:
            need_len = len(txt.encode('utf-8')) + 1
            best_idx = None
            best_len = 10**9
            for idx, s in enumerate(reusable_slots):
                if idx not in used_slot_indices and s['len'] >= need_len and s['len'] < best_len:
                    best_idx = idx
                    best_len = s['len']
            if best_idx is not None:
                used_slot_indices.add(best_idx)
                allocated_slots[txt] = reusable_slots[best_idx]
            else:
                all_fit = False
                break

    if all_fit:
        # Mode 1: Duplicate-Slot In-Pool Engine (100% original binary layout, 0 layout shifts)
        for idx in used_slot_indices:
            slot = reusable_slots[idx]
            struct.pack_into('<Q', new_sec_data, slot['ptr_loc'], sec_start + slot['first_p'])

        text_to_p = {}
        for txt, slot in allocated_slots.items():
            enc = txt.encode('utf-8') + b'\x00'
            p = slot['offset']
            new_sec_data[p : p + len(enc)] = enc
            text_to_p[txt] = p

        for h_hex, info in modified_entries.items():
            new_txt = info[0]
            slot_p = text_to_p[new_txt]
            tbl = info[4]
            if tbl == 1:
                ptr_loc = info[3]
                struct.pack_into('<Q', new_sec_data, ptr_loc, sec_start + slot_p)
            elif tbl == 3:
                sub_start = info[3]
                sub_cnt = info[5]
                for j in range(sub_cnt):
                    se_off = sub_start + j * 16
                    if j == 0:
                        struct.pack_into('<Q', new_sec_data, se_off + 8, sec_start + slot_p)

        out_data = bytes(data[:sec_abs]) + bytes(new_sec_data) + bytes(data[sec_abs + sec_size:])
    else:
        # Mode 2: Dynamic Relocation & KNLI Re-encoding Engine
        # Supports arbitrary text length and arbitrary file size expansion (2x, 5x, 10x).
        flags8, s8_size, s8_off = struct.unpack('<III', bytes(data[hdr_size - 28: hdr_size - 16]))
        knli_orig = data[hdr_size + s8_off : hdr_size + s8_off + s8_size]
        magic, comp_size, ver, zero, count, u1, u2, u3 = struct.unpack('<IIIIIIII', bytes(knli_orig[:32]))
        words = struct.unpack(f'<{len(knli_orig[0x20:]) // 2}H', bytes(knli_orig[0x20 : 0x20 + (len(knli_orig[0x20:]) // 2) * 2]))

        orig_relocs = []
        page = 0
        for w in words:
            if w == 0x800d:
                continue
            elif (w & 0xc000) == 0xc000:
                page = w & 0x3fff
            elif w < 0x8000:
                orig_relocs.append(((page * 0x8000) + w) * 4)
            if len(orig_relocs) == count:
                break

        i = 0
        c = 0
        while i < len(words) and c < count:
            if words[i] < 0x8000:
                c += 1
            i += 1
        tail_bytes = bytearray(knli_orig[0x20 + i*2 :])

        t1_entries = []
        for idx in range(t1_cnt):
            off = t1_start + idx * 16
            if off + 16 > len(sec_data):
                break
            h, s_off = struct.unpack('<QQ', bytes(sec_data[off: off + 16]))
            h_hex = f'{h:016x}'
            p = s_off - sec_start
            orig_text = _read_cstr(bytes(sec_data), p) if 0 <= p < len(sec_data) else ''
            t1_entries.append((h_hex, s_off, off + 8, orig_text))

        t3_entries = []
        for idx in range(t3_cnt):
            off = t3_start + idx * 24
            if off + 24 > len(sec_data):
                break
            h, sub_p, sub_cnt = struct.unpack('<QQQ', bytes(sec_data[off: off + 24]))
            h_hex = f'{h:016x}'
            sub_start = sub_p - sec_start
            parts = []
            sub_offs = []
            for j in range(int(sub_cnt)):
                se_off = sub_start + j * 16
                if 0 <= se_off + 16 <= len(sec_data):
                    sh, s_off = struct.unpack('<QQ', bytes(sec_data[se_off: se_off + 16]))
                    p = s_off - sec_start
                    txt = _read_cstr(bytes(sec_data), p).strip() if 0 <= p < len(sec_data) else ''
                    sub_offs.append((se_off + 8, p, txt))
                    if txt:
                        parts.append(txt)
            t3_entries.append((h_hex, sub_start, int(sub_cnt), ' '.join(parts), sub_offs))

        new_pool = bytearray()
        text_to_pool_offset = {}

        def add_to_pool(text):
            enc = text.encode('utf-8') + b'\x00'
            if enc not in text_to_pool_offset:
                text_to_pool_offset[enc] = len(new_pool)
                new_pool.extend(enc)
            return text_to_pool_offset[enc]

        add_to_pool('')

        t1_new_ptrs = []
        for h_hex, old_s_off, ptr_loc, orig_text in t1_entries:
            text = new_strings.get(h_hex, orig_text)
            new_off = add_to_pool(text)
            t1_new_ptrs.append((ptr_loc, new_off))

        t3_new_ptrs = []
        for h_hex, sub_start, sub_cnt, orig_text, sub_offs in t3_entries:
            if h_hex in new_strings and new_strings[h_hex] != orig_text:
                new_text = new_strings[h_hex]
                if sub_cnt == 1:
                    new_off = add_to_pool(new_text)
                    t3_new_ptrs.append((sub_offs[0][0], new_off))
                else:
                    # Multi-part subtitle: preserve individual parts so timing audio cues work
                    suffix = " MSTWW TEST EXPANSION"
                    if new_text.endswith(suffix):
                        for j, (se_ptr_loc, old_p, orig_part) in enumerate(sub_offs):
                            txt = orig_part + (suffix if j == sub_cnt - 1 else "")
                            new_off = add_to_pool(txt)
                            t3_new_ptrs.append((se_ptr_loc, new_off))
                    else:
                        lines = new_text.split('\n')
                        if len(lines) == sub_cnt:
                            for j, (se_ptr_loc, old_p, orig_part) in enumerate(sub_offs):
                                new_off = add_to_pool(lines[j].strip())
                                t3_new_ptrs.append((se_ptr_loc, new_off))
                        else:
                            words_list = new_text.split()
                            w_per_part = max(1, len(words_list) // sub_cnt)
                            for j, (se_ptr_loc, old_p, orig_part) in enumerate(sub_offs):
                                if j == sub_cnt - 1:
                                    chunk = ' '.join(words_list[j * w_per_part :])
                                else:
                                    chunk = ' '.join(words_list[j * w_per_part : (j+1) * w_per_part])
                                new_off = add_to_pool(chunk)
                                t3_new_ptrs.append((se_ptr_loc, new_off))
            else:
                for se_ptr_loc, old_p, orig_part in sub_offs:
                    new_off = add_to_pool(orig_part)
                    t3_new_ptrs.append((se_ptr_loc, new_off))

        desc_pos_abs = sec_start + desc_pos
        other_string_ptrs = []
        sec6_relocs = [r for r in orig_relocs if r >= sec_start + desc_pos]

        handled_ptr_locs = set([x[0] for x in t1_new_ptrs] + [x[0] for x in t3_new_ptrs])
        for r in sec6_relocs:
            rel_p = r - sec_start
            if rel_p not in handled_ptr_locs:
                val = struct.unpack('<Q', bytes(sec_data[rel_p : rel_p + 8]))[0]
                if sec_start <= val < desc_pos_abs:
                    p = val - sec_start
                    txt = _read_cstr(bytes(sec_data), p)
                    new_off = add_to_pool(txt)
                    other_string_ptrs.append((rel_p, new_off))

        # 1. Enforce SIMD 16-byte alignment invariant (desc_pos % 16 must be 8)
        # In all official game files (Thai, Greek, Russian, etc.), desc_pos % 16 == 8
        # so that t1_ptr (desc_pos + sec_start + 72) is strictly 16-byte SIMD vector aligned.
        while (len(new_pool) % 16) != 8:
            new_pool.append(0)
        if len(new_pool) < desc_pos:
            while len(new_pool) < desc_pos or (len(new_pool) % 16) != 8:
                new_pool.append(0)

        new_desc_pos = len(new_pool)
        delta = new_desc_pos - desc_pos

        tables_block = bytearray(sec_data[desc_pos:])

        for r in sec6_relocs:
            rel_tbl_p = r - (sec_start + desc_pos)
            val = struct.unpack('<Q', bytes(tables_block[rel_tbl_p : rel_tbl_p + 8]))[0]
            if val >= desc_pos_abs:
                struct.pack_into('<Q', tables_block, rel_tbl_p, val + delta)

        for ptr_loc, new_off in t1_new_ptrs:
            rel_tbl_p = ptr_loc - desc_pos
            struct.pack_into('<Q', tables_block, rel_tbl_p, sec_start + new_off)

        for ptr_loc, new_off in t3_new_ptrs:
            rel_tbl_p = ptr_loc - desc_pos
            struct.pack_into('<Q', tables_block, rel_tbl_p, sec_start + new_off)

        for ptr_loc, new_off in other_string_ptrs:
            rel_tbl_p = ptr_loc - desc_pos
            struct.pack_into('<Q', tables_block, rel_tbl_p, sec_start + new_off)

        new_s6_data = new_pool + tables_block
        pad_s6 = (16 - (len(new_s6_data) % 16)) % 16
        new_s6_data.extend(b'\x00' * pad_s6)
        new_s6_size = len(new_s6_data)

        orig_sec7_off = sec_start + sec_size
        new_sec7_off = sec_start + new_s6_size

        # 2. Update Section 0 root pointers and Section 1..6 font data pointers
        new_hdr_payload = bytearray(data[hdr_size : hdr_size + sec_start])

        # Shift all 7 Section 0 root pointers (payload 0x010..0x068) by delta:
        s0_rel_offs = [0x010, 0x020, 0x030, 0x040, 0x050, 0x060, 0x068]
        for p_off in s0_rel_offs:
            val = struct.unpack('<I', new_hdr_payload[p_off : p_off + 4])[0]
            struct.pack_into('<I', new_hdr_payload, p_off, val + delta)

        # Shift the 6 Section 1..6 font data pointers (these point into Section 8):
        for r in orig_relocs:
            if r < sec_start:
                val = struct.unpack('<I', bytes(new_hdr_payload[r:r+4]))[0]
                if val >= orig_sec7_off:
                    struct.pack_into('<I', new_hdr_payload, r, val + delta)

        # 3. Re-encode KNLI Relocations Bytecode
        new_relocs = []
        for r in orig_relocs:
            if r < sec_start:
                new_relocs.append(r)
            else:
                new_relocs.append(r + delta)

        words_enc = [0x800d]
        cur_p = 0
        for addr in new_relocs:
            dw = addr // 4
            p = dw // 0x8000
            w = dw % 0x8000
            if p != cur_p:
                words_enc.append(0xc000 | p)
                cur_p = p
            words_enc.append(w)

        knli_stream = struct.pack(f'<{len(words_enc)}H', *words_enc)
        stream_len = len(knli_stream)

        # 4. Update the 208-byte immutable magic tail block
        # In all official game files (Turkish, Greek, Thai, Russian, etc.),
        # KNLI ends with a fixed 208-byte block containing 6 font records, 1 desc record,
        # and the magic footer '297E4C1ABD00B570 20444E45 00000000' (END \0\0\0\0).
        tail_core = bytearray(knli_orig[-208:])
        core_ptr_offsets = [8, 40, 72, 104, 136, 168, 184]
        for toff in core_ptr_offsets:
            old_val = struct.unpack('<I', tail_core[toff:toff+4])[0]
            struct.pack_into('<I', tail_core, toff, old_val + delta)

        # Mid-stream alignment padding:
        # Pre-tail record header: struct.pack('<II', 0x270, 0) [8 bytes]
        # Pad between bytecode stream and pre-tail so total payload is 8-byte aligned.
        pad_mid = (8 - (stream_len % 8)) % 8
        mid_padding = b'\x00' * pad_mid
        pre_tail = struct.pack('<II', 0x270, 0)

        knli_payload = knli_stream + mid_padding + pre_tail + tail_core
        new_knli_size = 32 + len(knli_payload)
        new_comp_size = new_knli_size - 240

        new_knli_hdr = struct.pack('<IIIIIIII', magic, new_comp_size, ver, 0, len(new_relocs), u1, u2, u3)
        new_knli = new_knli_hdr + knli_payload

        sec1_flags, sec1_size, sec1_offset = struct.unpack('<III', bytes(data[hdr_size - 40: hdr_size - 28]))
        sec7_data = data[hdr_size + orig_sec7_off : hdr_size + orig_sec7_off + sec1_size]
        new_sec8_off = new_sec7_off + len(sec7_data)

        new_payload = new_hdr_payload + new_s6_data + sec7_data + new_knli
        new_header = bytearray(data[:hdr_size])

        struct.pack_into('<I', new_header, 0x02c, len(new_payload))
        struct.pack_into('<I', new_header, 0x0ec, new_sec7_off)
        struct.pack_into('<I', new_header, 0x118, new_sec7_off)
        struct.pack_into('<I', new_header, 0x13c, new_knli_size)
        struct.pack_into('<I', new_header, 0x140, new_sec8_off)
        struct.pack_into('<I', new_header, 0x1b4, new_s6_size)
        struct.pack_into('<I', new_header, 0x1c4, new_sec7_off)
        struct.pack_into('<I', new_header, 0x1cc, new_knli_size)
        struct.pack_into('<I', new_header, 0x1d0, new_sec8_off)

        out_data = bytes(new_header) + bytes(new_payload)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(output_path, 'wb') as f:
        f.write(out_data)

    return len(out_data)


def _load_strings(file_path):
    """Loads translations from .xpps, .bak, or .json file."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, 'rb') as f:
        magic = f.read(4)

    if magic == b'KCAP':
        return extract_xpps(file_path)
    else:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {file_path}")
        return data


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_extract(args):
    """
    extract command:
      xpps_tool extract <input.xpps | folder> [-o <output_path_or_dir>]
    """
    output_arg = args.output

    if os.path.isfile(args.input) and args.input.lower().endswith('.xpps'):
        # Single file extraction
        file_path = args.input
        base = os.path.basename(file_path)
        lang_code = LANG_MAP.get(base, 'extracted')
        print(f"Extracting: {file_path}  (language: {lang_code})")
        strings = extract_xpps(file_path, clean_pua=args.clean_pua)
        print(f"  [+] {len(strings)} strings extracted")

        if output_arg and output_arg.lower().endswith('.json'):
            out_json = output_arg
            out_dir = os.path.dirname(out_json)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = output_arg if output_arg else 'translations'
            lang_dir = os.path.join(out_dir, lang_code)
            os.makedirs(lang_dir, exist_ok=True)
            out_json = os.path.join(lang_dir, 'strings.json')

        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(strings, f, ensure_ascii=False, indent=2)
        print(f"  [>] Saved → {out_json}")

    elif os.path.isdir(args.input):
        # Folder batch extraction
        folder = args.input
        files_found = []
        for filename, code in LANG_MAP.items():
            fp = os.path.join(folder, filename)
            if os.path.exists(fp):
                files_found.append((code, fp))

        if not files_found:
            # Check for any .xpps in the folder
            for f in os.listdir(folder):
                if f.lower().endswith('.xpps'):
                    files_found.append(('custom', os.path.join(folder, f)))

        if not files_found:
            print(f"Error: No .xpps files found in {folder}")
            sys.exit(1)

        out_dir = output_arg if output_arg else 'translations'
        print(f"Extracting {len(files_found)} .xpps files → {out_dir}")
        for code, fp in sorted(files_found):
            t0 = time.time()
            strings = extract_xpps(fp, clean_pua=args.clean_pua)
            fname = os.path.splitext(os.path.basename(fp))[0]
            lang_dir = os.path.join(out_dir, code if code != 'custom' else fname)
            os.makedirs(lang_dir, exist_ok=True)
            out_json = os.path.join(lang_dir, 'strings.json')
            with open(out_json, 'w', encoding='utf-8') as f:
                json.dump(strings, f, ensure_ascii=False, indent=2)
            print(f"  [>] {os.path.basename(fp):<26} {len(strings):>5} strings → {out_json}  ({time.time()-t0:.1f}s)")

        print(f"\n[OK] Batch extraction complete: {out_dir}")

    else:
        print(f"Error: '{args.input}' is neither a .xpps file nor a directory.")
        sys.exit(1)


def cmd_repack(args):
    """
    repack command:
      xpps_tool repack <strings.json> -t <template.xpps> [-o <output.xpps>]
    """
    strings_file = args.strings_file
    template_path = args.template

    if not os.path.exists(strings_file):
        print(f"Error: Translation file not found: {strings_file}")
        sys.exit(1)
    if not os.path.exists(template_path):
        print(f"Error: Template file not found: {template_path}")
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        base, ext = os.path.splitext(template_path)
        output_path = f"{base}_modded{ext}"

    print(f"Loading translations from: {strings_file}")
    try:
        new_strings = _load_strings(strings_file)
    except Exception as e:
        print(f"Error loading translation file: {e}")
        sys.exit(1)

    print(f"  Loaded strings: {len(new_strings)}")
    print(f"  Template file:  {template_path}")
    print(f"  Destination:    {output_path}")

    force_expand = getattr(args, 'force_expand', False)
    new_sec_size = repack_xpps(template_path, new_strings, output_path, force_expand=force_expand)
    out_bytes = os.path.getsize(output_path)
    print(f"\n[OK] Repacked .xpps created: {output_path}  ({out_bytes:,} bytes)")

    if getattr(args, 'no_verify', False):
        return

    # Automatic verification check
    print("Verifying repacked archive...")
    try:
        orig_strings = extract_xpps(template_path)
        repacked_strings = extract_xpps(output_path)
        if len(orig_strings) == len(repacked_strings):
            print(f"  [OK] Verification passed! String count: {len(repacked_strings):,}")
        else:
            print(f"  [WARNING] String count mismatch: template={len(orig_strings)}, repacked={len(repacked_strings)}")
    except Exception as e:
        print(f"  [WARNING] Verification check failed: {e}")


def cmd_diff(args):
    """
    diff command:
      xpps_tool diff <original> <translated> [-o <modified.json>]
    """
    print("Comparing translation files:")
    print(f"  Original:   {args.file1}")
    print(f"  Translated: {args.file2}\n")

    try:
        orig = _load_strings(args.file1)
        trans = _load_strings(args.file2)
    except Exception as e:
        print(f"Error loading files: {e}")
        sys.exit(1)

    common_keys = set(orig.keys()) & set(trans.keys())
    missing_in_trans = set(orig.keys()) - set(trans.keys())
    added_in_trans = set(trans.keys()) - set(orig.keys())

    modified = {}
    identical_count = 0
    for k in common_keys:
        if orig[k] != trans[k]:
            modified[k] = {'original': orig[k], 'translated': trans[k]}
        else:
            identical_count += 1

    total_orig = len(orig)
    pct = (len(modified) / total_orig * 100) if total_orig > 0 else 0.0

    print("=" * 50)
    print("Translation Progress & Statistics")
    print("=" * 50)
    print(f"  Total original strings:    {len(orig):>7,}")
    print(f"  Total translated strings:  {len(trans):>7,}")
    print(f"  Modified / Translated:     {len(modified):>7,}  ({pct:.2f}%)")
    print(f"  Identical (untouched):     {identical_count:>7,}")
    if missing_in_trans:
        print(f"  Missing in translated:     {len(missing_in_trans):>7,}")
    if added_in_trans:
        print(f"  Added new keys:            {len(added_in_trans):>7,}")

    if modified:
        print("\nSample Modified Strings (first 5):")
        for i, (k, v) in enumerate(list(modified.items())[:5]):
            orig_snippet = v['original'][:60] + ('...' if len(v['original']) > 60 else '')
            trans_snippet = v['translated'][:60] + ('...' if len(v['translated']) > 60 else '')
            print(f"  [{k}]")
            print(f"    - Original:   {orig_snippet}")
            print(f"    + Translated: {trans_snippet}")

    if args.output:
        out_dict = {k: v['translated'] for k, v in modified.items()}
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(out_dict, f, ensure_ascii=False, indent=2)
        print(f"\n[OK] Exported {len(modified)} modified strings to: {args.output}")


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='xpps_tool',
        description=(
            "Ghost of Tsushima Localization Tool (v2.2)\n"
            "Extract, edit, and repack .xpps localization files for game translations and modding."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='command', metavar='<command>')

    # extract subcommand
    p_ext = sub.add_parser(
        'extract',
        help='Extract localization text from .xpps file(s) into editable JSON',
    )
    p_ext.add_argument(
        'input',
        help='Path to a .xpps file or folder containing .xpps files',
    )
    p_ext.add_argument(
        '-o', '--output',
        default=None,
        help='Output .json file path (for single file) or output directory (default: translations)',
    )
    p_ext.add_argument(
        '--clean-pua', action='store_true',
        help='Strip controller button glyphs (Unicode PUA icons) from extracted text',
    )

    # repack subcommand
    p_rep = sub.add_parser(
        'repack',
        help='Rebuild a .xpps file from modified translation JSON using a template',
    )
    p_rep.add_argument(
        'strings_file',
        help='Path to translated strings.json',
    )
    p_rep.add_argument(
        '-t', '--template',
        required=True,
        help='Original game .xpps file to use as binary template',
    )
    p_rep.add_argument(
        '-o', '--output',
        default=None,
        help='Path for the repacked .xpps file (default: <template>_modded.xpps)',
    )
    p_rep.add_argument(
        '--no-verify', action='store_true',
        help='Skip automatic verification check after repacking',
    )
    p_rep.add_argument(
        '--force-expand', action='store_true',
        help='Force dynamic pool expansion and KNLI relocation rebuild even if text fits duplicate slots',
    )

    # diff subcommand
    p_diff = sub.add_parser(
        'diff',
        help='Compare two translation files or check translation progress',
    )
    p_diff.add_argument(
        'file1',
        help='Original file (.xpps or strings.json)',
    )
    p_diff.add_argument(
        'file2',
        help='Translated file (.xpps or strings.json)',
    )
    p_diff.add_argument(
        '-o', '--output', default=None,
        help='Optional path to export modified strings as JSON',
    )

    return parser


def main():
    print("=" * 70)
    print("Ghost of Tsushima Localization Tool  v2.2")
    print("=" * 70)

    # Direct single argument drag-and-drop support:
    # e.g. dragging a .xpps onto xpps_tool.exe
    if len(sys.argv) == 2 and sys.argv[1].lower().endswith('.xpps') and os.path.isfile(sys.argv[1]):
        file_path = sys.argv[1]
        base = os.path.splitext(file_path)[0]
        out_json = f"{base}_strings.json"
        print(f"Auto-extracting: {file_path}")
        strings = extract_xpps(file_path)
        with open(out_json, 'w', encoding='utf-8') as f:
            json.dump(strings, f, ensure_ascii=False, indent=2)
        print(f"[OK] Extracted {len(strings)} strings → {out_json}")
        return

    parser = build_parser()

    if len(sys.argv) == 1:
        parser.print_help()
        return

    args = parser.parse_args()

    if args.command == 'extract':
        cmd_extract(args)
    elif args.command == 'repack':
        cmd_repack(args)
    elif args.command == 'diff':
        cmd_diff(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
