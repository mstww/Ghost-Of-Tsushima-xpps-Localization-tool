#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ghost of Tsushima DIRECTOR'S CUT - Font Tool (GUI + CLI)
Preview, export / import, extend and replace the SDF fonts stored in the KCAP
localisation packs (lang_*_text.xpps, found in gapack_misc_l.psarc).

  got_font_tool                 -> graphical interface
  got_font_tool <file.xpps>     -> graphical interface with that file
  got_font_tool <command> ...   -> command line (got_font_tool --help)

Font layout (little endian; pointers are payload offsets rebased through KNLI):
  font header (0x40 bytes)
    +0x00 u64 name hash        +0x08 u32 id
    +0x0C f32 1/size           +0x10 u16 size   +0x12 u16 spread (SDF padding, virtual px)
    +0x14 f32 1/Vw  +0x18 f32 1/Vh  (virtual atlas size)          +0x1C f32 1.0
    +0x28 u64 -> glyph table   +0x30 u32 glyph count        +0x38 u64 -> texture desc + 0x10
  glyph (24 bytes): u16 cp, u16 x, u16 y, u16 w, u16 h, s16 xoff, s16 yoff, u16 adv, u64 -> kern record
  kern record (16 bytes): u64 -> pairs, u32 count, u32 pad ; pair (4 bytes): u16 cp2, s16 amount
  texture desc (0x60): +0x28 u16 w, u16 h, +0x2f u8 mips, +0x30 u32 size, +0x34 u32 format,
                       +0x38 u64 -> pixels (payload offset, second KNLI list)
  texture: R8 signed distance field (128 = edge), full mip chain, rows stored bottom-up.
  Glyph boxes are in virtual pixels with a top-left origin once the texture is flipped upright.

Growing a font appends new tables to the end of the main (text) block, rebuilds the texture
block when an atlas has to get bigger, shifts the KNLI block and re-encodes the relocations.
"""

import sys
import os
import json
import math
import struct
import argparse

VERSION = "1.0"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

R8_FORMAT = 0x00492001
MAX_TEX = 8192

# ----------------------------------------------------------------------------
# KNLI relocation stream
#   0xC000 | p   select page p (page = 0x8000 dwords), next word is the first relocation
#   0x8000 | n   n more relocations, each 8 bytes after the previous one
#   w < 0x8000   relocation at byte offset (page * 0x8000 + w) * 4
# ----------------------------------------------------------------------------

def knli_decode(words):
    out, hi, pos, i = [], 0, 0, 0
    n = len(words)
    while i < n:
        x = words[i]
        t = x >> 14
        if t == 3:
            hi = x & 0x3fff
            pos = ((hi << 15) | words[i + 1]) * 4
            out.append(pos)
            i += 2
        elif t == 2:
            for _ in range(x & 0x3fff):
                pos += 8
                out.append(pos)
            i += 1
        else:
            pos = ((hi << 15) | x) * 4
            out.append(pos)
            i += 1
    return out


def knli_encode(positions):
    """Inverse of knli_decode; reproduces the game's own encoding word for word."""
    w, hi, i, n = [], None, 0, len(positions)
    while i < n:
        p = positions[i]
        if p % 4:
            raise ValueError(f"relocation {p:#x} is not 4-byte aligned")
        q = p // 4
        if hi is None or (q >> 15) != hi:
            hi = q >> 15
            w += [0xc000 | hi, q & 0x7fff]
        else:
            w.append(q & 0x7fff)
        i += 1
        r = 0
        while i + r < n and positions[i + r] == p + 8 * (r + 1) and r < 0x3fff:
            r += 1
        if r >= 2:
            w.append(0x8000 | r)
            i += r
    return w


# ----------------------------------------------------------------------------
# data model
# ----------------------------------------------------------------------------

class Glyph:
    __slots__ = ('cp', 'x', 'y', 'w', 'h', 'xoff', 'yoff', 'adv', 'kern', 'img', 'sub')

    def __init__(self, cp, x, y, w, h, xoff, yoff, adv, kern=None, img=None):
        self.cp, self.x, self.y, self.w, self.h = cp, x, y, w, h
        self.xoff, self.yoff, self.adv = xoff, yoff, adv
        self.kern = kern if kern is not None else []   # [(cp2, amount), ...]  (cp2 may repeat)
        self.img = img          # texel crop (numpy uint8) while repacking
        self.sub = (0, 0)       # sub-texel offset of the box inside img, virtual px


class Font:
    def __init__(self, pack, index, hofs):
        self.pack, self.index, self.hofs = pack, index, hofs   # hofs = payload offset of header
        d = pack.data
        a = pack.DO + hofs
        self.hash = d[a:a + 8].hex()
        self.id = struct.unpack_from('<I', d, a + 8)[0]
        self.size, self.spread = struct.unpack_from('<HH', d, a + 0x10)
        ivw, ivh = struct.unpack_from('<ff', d, a + 0x14)
        self.vw, self.vh = int(round(1.0 / ivw)), int(round(1.0 / ivh))
        self.glyph_ptr = struct.unpack_from('<Q', d, a + 0x28)[0]
        self.count = struct.unpack_from('<I', d, a + 0x30)[0]
        tp = struct.unpack_from('<Q', d, a + 0x38)[0]
        self.tex_desc = tp - 0x10
        t = pack.DO + self.tex_desc
        self.tex_w, self.tex_h = struct.unpack_from('<HH', d, t + 0x28)
        self.tex_mips = d[t + 0x2f]
        self.tex_size, self.tex_fmt = struct.unpack_from('<II', d, t + 0x30)
        self.tex_off = struct.unpack_from('<Q', d, t + 0x38)[0]
        self.otw, self.oth = self.tex_w, self.tex_h     # size stored in the file
        self.glyphs = self._read_glyphs()
        self.new_texture = None            # upright image waiting to be written (any size)

    @property
    def editable(self):
        return self.tex_fmt == R8_FORMAT

    @property
    def fx(self):
        return self.vw / self.tex_w

    @property
    def fy(self):
        return self.vh / self.tex_h

    def _read_glyphs(self):
        d, DO = self.pack.data, self.pack.DO
        out = []
        for i in range(self.count):
            cp, x, y, w, h, xo, yo, adv, kp = struct.unpack_from('<HHHHHhhHQ', d, DO + self.glyph_ptr + i * 24)
            kern = []
            if kp:
                pp, n = struct.unpack_from('<QI', d, DO + kp)
                for j in range(n):
                    kern.append(struct.unpack_from('<Hh', d, DO + pp + j * 4))
            out.append(Glyph(cp, x, y, w, h, xo, yo, adv, kern))
        return out

    def read_texture(self):
        import numpy as np
        if self.new_texture is not None:
            return self.new_texture.copy()
        d = self.pack.data
        a = self.pack.DO + self.tex_off
        n = self.tex_w * self.tex_h
        img = np.frombuffer(bytes(d[a:a + n]), np.uint8).reshape(self.tex_h, self.tex_w)
        return img[::-1].copy()   # upright

    def set_texture(self, img):
        """Queue a new atlas (may be a different size; the virtual size keeps the texel density)."""
        import numpy as np
        img = np.ascontiguousarray(img, np.uint8)
        h, w = img.shape
        for v in (w, h):
            if v & (v - 1) or v > MAX_TEX:
                raise ValueError(f"texture size {w}x{h} must be powers of two <= {MAX_TEX}")
        self.new_texture = img
        self.tex_w, self.tex_h = w, h

    def set_virtual(self, vw, vh):
        struct.pack_into('<ff', self.pack.data, self.pack.DO + self.hofs + 0x14, 1.0 / vw, 1.0 / vh)
        self.vw, self.vh = vw, vh


def mip_chain(img):
    import numpy as np
    out = [img.astype(np.uint8)]
    cur = img.astype(np.float32)
    while cur.shape[0] > 1 or cur.shape[1] > 1:
        h, w = cur.shape
        nh, nw = max(1, h // 2), max(1, w // 2)
        cur = cur.reshape(nh, h // nh, nw, w // nw).mean(axis=(1, 3))
        out.append(np.clip(np.round(cur), 0, 255).astype(np.uint8))
    return out


def texture_blob(img):
    chain = mip_chain(img)
    return b''.join(m[::-1].tobytes() for m in chain), len(chain)


# ----------------------------------------------------------------------------
# KCAP container
# ----------------------------------------------------------------------------

class Pack:
    def __init__(self, path):
        self.path = path
        with open(path, 'rb') as f:
            self.data = bytearray(f.read())
        d = self.data
        if d[:4] != b'KCAP':
            raise ValueError(f"{path}: not a KCAP/xpps file")
        self.DO, self.DS = struct.unpack_from('<II', d, 0x28)
        if self.DO + self.DS != len(d):
            raise ValueError(f"{path}: payload size mismatch")
        self._parse_blocks()
        self._parse_knli()
        self.fonts = self._find_fonts()
        self.extra = bytearray()      # bytes appended to the end of the main block
        self.new_relocs = []          # payload offsets of new pointers (inside self.extra)
        self.fresh = set()            # pointer locations whose value is already final

    # block table: (size, offset) pairs, laid out back to back up to the payload end
    def _parse_blocks(self):
        d, DO, DS = self.data, self.DO, self.DS
        best = None
        for s in range(0xb8, DO - 11, 4):
            size, off = struct.unpack_from('<II', d, s)
            if off != 0 or size == 0 or size > DS:
                continue
            blocks, p, prev_end = [], s, 0
            while p + 8 <= DO:
                size, off = struct.unpack_from('<II', d, p)
                if off < prev_end or off - prev_end > 0x10000 or size > DS:
                    break
                blocks.append([size, off, p])
                prev_end = off + size
                p += 12
                if prev_end == DS:
                    if best is None or len(blocks) > len(best):
                        best = list(blocks)
                    break
        if not best:
            raise ValueError("could not locate the KCAP block table")
        self.blocks = best
        self.knli_block = next((b for b in best if d[DO + b[1]:DO + b[1] + 4] == b'KNLI'), None)
        if self.knli_block is None:
            raise ValueError("no KNLI relocation block")

    def _parse_knli(self):
        d = self.data
        K = self.DO + self.knli_block[1]
        size, nsub = struct.unpack_from('<II', d, K + 4)
        subs = [[None, struct.unpack_from('<I', d, K + 16)[0]]]
        p = K + 20
        for _ in range(1, nsub):
            t, c = struct.unpack_from('<II', d, p)
            subs.append([t, c])
            p += 8
        self.knli_subs = []
        for t, c in subs:
            words = list(struct.unpack_from('<%dH' % c, d, p))
            p += 2 * c
            self.knli_subs.append([t, knli_decode(words)])
        if ((p - K + 7) & ~7) != size + 8:
            raise ValueError("unexpected KNLI layout")
        self.knli_tail = bytes(d[K + size + 8:self.DO + self.knli_block[1] + self.knli_block[0]])
        self.relocs = set(self.knli_subs[0][1])

    def _find_fonts(self):
        """Font headers: relocated glyph pointer (+0x28) and texture pointer (+0x38),
        power-of-two virtual size at +0x14/+0x18 and a sorted glyph table."""
        d, DO, DS = self.data, self.DO, self.DS
        fonts = []
        for p in sorted(self.relocs):
            h = p - 0x28
            if h < 0 or (h + 0x38) not in self.relocs or h + 0x40 > DS:
                continue
            a = DO + h
            ivw, ivh = struct.unpack_from('<ff', d, a + 0x14)
            if not (0 < ivw < 1 and 0 < ivh < 1):
                continue
            vw, vh = 1.0 / ivw, 1.0 / ivh
            if any(abs(v - round(v)) > 1e-3 or int(round(v)) & (int(round(v)) - 1) or v < 64 for v in (vw, vh)):
                continue
            size, spread = struct.unpack_from('<HH', d, a + 0x10)
            gp = struct.unpack_from('<Q', d, a + 0x28)[0]
            cnt = struct.unpack_from('<I', d, a + 0x30)[0]
            tp = struct.unpack_from('<Q', d, a + 0x38)[0]
            if size == 0 or not 0 < cnt < 0x10000 or gp + cnt * 24 > DS or tp < 0x10 or tp + 0x50 > DS:
                continue
            cps = [struct.unpack_from('<H', d, DO + gp + i * 24)[0] for i in range(min(cnt, 64))]
            if cps != sorted(cps):
                continue
            try:
                fonts.append(Font(self, len(fonts), h))
            except Exception:
                pass
        return fonts

    @property
    def main_block(self):
        h = self.fonts[0].hofs
        return next(b for b in self.blocks if b[1] <= h < b[1] + b[0])

    # ------------------------------------------------------------------
    def alloc(self, nbytes, align=8):
        """Reserve bytes at the end of the main block; returns their payload offset."""
        while len(self.extra) % align:
            self.extra.append(0)
        mb = self.main_block
        off = mb[1] + mb[0] + len(self.extra)
        self.extra += b'\x00' * nbytes
        return off

    def get(self, off, fmt):
        mb = self.main_block
        end = mb[1] + mb[0]
        if off >= end:
            return struct.unpack_from(fmt, self.extra, off - end)
        return struct.unpack_from(fmt, self.data, self.DO + off)

    def put(self, off, fmt, *vals):
        mb = self.main_block
        end = mb[1] + mb[0]
        if off >= end:
            struct.pack_into(fmt, self.extra, off - end, *vals)
        else:
            struct.pack_into(fmt, self.data, self.DO + off, *vals)

    def write_font_tables(self, font, glyphs):
        """Write glyph + kerning tables; in place when the table layout is unchanged."""
        glyphs = sorted(glyphs, key=lambda g: g.cp)
        cps = [g.cp for g in glyphs]
        if len(set(cps)) != len(cps):
            raise ValueError("duplicate code points in glyph list")
        for g in glyphs:
            if not 0 <= g.cp <= 0xffff:
                raise ValueError(f"code point U+{g.cp:X} is outside the BMP (not supported by the game)")
            if not (0 <= g.x and g.x + g.w <= font.vw and 0 <= g.y and g.y + g.h <= font.vh):
                raise ValueError(f"glyph U+{g.cp:04X} box is outside the {font.vw}x{font.vh} atlas")
        same = (len(glyphs) == font.count and [g.cp for g in font.glyphs] == cps and
                [[k[0] for k in a.kern] for a in font.glyphs] == [[k[0] for k in b.kern] for b in glyphs])
        d, DO = self.data, self.DO
        if same:
            for i, g in enumerate(glyphs):
                a = font.glyph_ptr + i * 24
                kp = self.get(a + 16, '<Q')[0]
                self.put(a, '<HHHHHhhH', g.cp, g.x, g.y, g.w, g.h, g.xoff, g.yoff, g.adv)
                if kp:
                    pp, n = self.get(kp, '<QI')
                    for j, (c2, amt) in enumerate(g.kern):
                        self.put(pp + j * 4, '<Hh', c2, clamp16(amt))
            font.glyphs = glyphs
            return 'in-place'
        gt = self.alloc(len(glyphs) * 24, 16)
        kerned = [g for g in glyphs if g.kern]
        kr = self.alloc(len(kerned) * 16, 8) if kerned else 0
        npairs = sum(len(g.kern) for g in kerned)
        kp_base = self.alloc(npairs * 4, 4) if npairs else 0
        ki = pi = 0
        for i, g in enumerate(glyphs):
            a = gt + i * 24
            rec = 0
            if g.kern:
                rec = kr + ki * 16
                ki += 1
                g.kern = sorted(g.kern, key=lambda k: k[0])      # stable sort keeps duplicate order
                pairs_off = kp_base + pi * 4
                for j, (c2, amt) in enumerate(g.kern):
                    self.put(pairs_off + j * 4, '<Hh', c2, clamp16(amt))
                self.put(rec, '<QI', pairs_off, len(g.kern))
                pi += len(g.kern)
                self.new_relocs += [rec, a + 16]
            self.put(a, '<HHHHHhhHQ', g.cp, g.x, g.y, g.w, g.h, g.xoff, g.yoff, g.adv, rec)
        struct.pack_into('<Q', d, DO + font.hofs + 0x28, gt)
        struct.pack_into('<I', d, DO + font.hofs + 0x30, len(glyphs))
        self.fresh.add(font.hofs + 0x28)
        font.glyph_ptr, font.count, font.glyphs = gt, len(glyphs), glyphs
        return 'relocated'

    # ------------------------------------------------------------------
    def save(self, out_path):
        """Write the pack; works on copies, so editing can continue afterwards."""
        d, DO, DS = bytearray(self.data), self.DO, self.DS
        extra = bytearray(self.extra)
        mb, kb = self.main_block, self.knli_block
        ins = mb[1] + mb[0]
        if kb[1] + kb[0] != DS:
            raise RuntimeError("KNLI block is not the last block - unsupported layout")
        # texture descriptors = targets of the second relocation list (desc + 0x38)
        tex_ptr_locs = [p for t, l in self.knli_subs[1:] for p in l]
        descs = {}
        for p in tex_ptr_locs:
            descs[p - 0x38] = struct.unpack_from('<Q', d, DO + p)[0]
        dirty = {f.tex_desc: f for f in self.fonts if f.new_texture is not None}
        tb = None
        if descs:
            anyoff = next(iter(descs.values()))
            tb = next((b for b in self.blocks if b[1] <= anyoff < b[1] + b[0]), None)
        resized = False
        for desc, f in dirty.items():
            img = f.new_texture
            if img.shape != (f.oth, f.otw):
                resized = True
            else:   # same size: write in place
                blob, mips = texture_blob(img)
                if len(blob) != f.tex_size:
                    raise RuntimeError("mip chain size mismatch")
                d[DO + f.tex_off:DO + f.tex_off + len(blob)] = blob
        # --- new texture block (only when an atlas changed size)
        new_tex_block, new_pix = None, {}
        if resized:
            if tb is None or tb[1] != ins:
                raise RuntimeError("texture block does not follow the main block - unsupported layout")
            order = sorted(descs.items(), key=lambda kv: kv[1])
            blk = bytearray()
            for desc, pix in order:
                while len(blk) % 16:
                    blk.append(0)
                size = struct.unpack_from('<I', d, DO + desc + 0x30)[0]
                if desc in dirty and dirty[desc].new_texture.shape != (dirty[desc].oth, dirty[desc].otw):
                    f = dirty[desc]
                    blob, mips = texture_blob(f.new_texture)
                    th, tw = f.new_texture.shape
                    struct.pack_into('<HH', d, DO + desc + 0x28, tw, th)
                    d[DO + desc + 0x2f] = mips
                    struct.pack_into('<I', d, DO + desc + 0x30, len(blob))
                else:
                    blob = d[DO + pix:DO + pix + size]
                new_pix[pix] = (len(blk), size)
                blk += blob
            while len(blk) % 16:
                blk.append(0)
            new_tex_block = blk
        # --- deltas
        while (len(extra) or new_tex_block is not None) and len(extra) % 0x1000:
            extra.append(0)                 # like xpps_tool v3.0: shift by 4 KiB multiples
        d1 = len(extra)
        d2 = (len(new_tex_block) - tb[0]) if new_tex_block is not None else 0
        if not d1 and not d2 and not self.new_relocs:
            _atomic_write(out_path, d)
            return
        tb_off, tb_end = (tb[1], tb[1] + tb[0]) if tb else (None, None)
        pix_sorted = sorted(new_pix.items())

        def mapv(v):
            if v < ins:
                return v
            if new_tex_block is not None and tb_off <= v < tb_end:
                for old, (rel, size) in reversed(pix_sorted):
                    if old <= v:
                        return tb_off + d1 + rel + (v - old)
                return v + d1
            if tb_end is not None and v >= tb_end:
                return v + d1 + d2
            return v + d1

        # --- payload
        payload = bytearray(d[DO:DO + ins]) + extra
        if new_tex_block is not None:
            payload += new_tex_block
            payload += d[DO + tb_end:DO + kb[1]]
        else:
            payload += d[DO + ins:DO + kb[1]]
        # pointers (all positions are below the insertion point)
        for t, lst in self.knli_subs:
            for p in lst:
                if p >= ins:
                    raise RuntimeError("relocation inside a moved block - unsupported layout")
                if p in self.fresh:
                    continue
                v = struct.unpack_from('<Q', payload, p)[0]
                if v < DS:
                    struct.pack_into('<Q', payload, p, mapv(v))
        # KNLI
        subs = []
        for si, (t, lst) in enumerate(self.knli_subs):
            nl = list(lst)
            if si == 0 and self.new_relocs:
                nl = sorted(set(nl) | set(self.new_relocs))
            subs.append((t, nl))
        words = [knli_encode(l) for t, l in subs]
        body = bytearray(b'KNLI') + struct.pack('<III', 0, len(subs), 0) + struct.pack('<I', len(words[0]))
        for (t, l), w in zip(subs[1:], words[1:]):
            body += struct.pack('<II', t, len(w))
        for w in words:
            body += struct.pack('<%dH' % len(w), *w)
        while len(body) % 8:
            body.append(0)
        struct.pack_into('<I', body, 4, len(body) - 8)
        tail = bytearray(self.knli_tail)
        if tail[:4] == b' DIC':
            n = struct.unpack_from('<Q', tail, 8)[0]
            for i in range(n):
                o = 16 + i * 16
                v = struct.unpack_from('<Q', tail, o)[0]
                struct.pack_into('<Q', tail, o, mapv(v - 0x10) + 0x10)
        knli = body + tail
        while len(knli) % 8:
            knli.append(0)
        new_kb_off = len(payload)
        payload += knli
        # header
        hdr = bytearray(d[:DO])
        struct.pack_into('<I', hdr, 0x2c, len(payload))
        repl = {}
        for b in self.blocks:
            size, off, pos = b
            nsize, noff = size, off
            if b is mb:
                nsize = size + d1
            elif b is kb:
                noff, nsize = new_kb_off, len(knli)
            elif tb is not None and b is tb:
                noff = off + d1
                nsize = len(new_tex_block) if new_tex_block is not None else size
            elif off >= ins:
                noff = mapv(off)
            struct.pack_into('<II', hdr, pos, nsize, noff)
            if b is not mb:
                if noff != off:
                    repl[off] = noff
                if nsize != size:
                    repl[size] = nsize
        # memory-segment records above the block table repeat these offsets / sizes
        for p in range(0x30, self.blocks[0][2], 4):
            v = struct.unpack_from('<I', hdr, p)[0]
            if v in repl and v > 0x100:
                struct.pack_into('<I', hdr, p, repl[v])
        _atomic_write(out_path, hdr + payload)


def _atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fp:
        fp.write(data)
    os.replace(tmp, path)


def clamp16(v):
    return max(-32768, min(32767, int(round(v))))


# ----------------------------------------------------------------------------
# verification
# ----------------------------------------------------------------------------

def verify(path, quiet=False):
    p = Pack(path)
    errs = []
    rel = p.knli_subs[0][1]
    for off in rel:
        if off + 8 > p.DS:
            errs.append(f"relocation {off:#x} outside payload")
            continue
        v = struct.unpack_from('<Q', p.data, p.DO + off)[0]
        if v >= p.DS:
            errs.append(f"pointer at {off:#x} -> {v:#x} outside payload")
    for t, lst in p.knli_subs[1:]:
        for off in lst:
            v = struct.unpack_from('<Q', p.data, p.DO + off)[0]
            desc = off - 0x38
            size = struct.unpack_from('<I', p.data, p.DO + desc + 0x30)[0]
            if v + size > p.DS:
                errs.append(f"texture pointer at {off:#x} -> {v:#x} outside payload")
    rs = p.relocs
    for f in p.fonts:
        if (f.hofs + 0x28) not in rs:
            errs.append(f"font {f.index}: glyph pointer not relocated")
        cps = [g.cp for g in f.glyphs]
        if cps != sorted(cps) or len(set(cps)) != len(cps):
            errs.append(f"font {f.index}: glyph table not sorted/unique")
        for i in range(f.count):
            a = f.glyph_ptr + i * 24 + 16
            kp = struct.unpack_from('<Q', p.data, p.DO + a)[0]
            if kp and (a not in rs or kp not in rs):
                errs.append(f"font {f.index}: kerning pointer of glyph {i} not relocated")
                break
        for g in f.glyphs:
            if g.x + g.w > f.vw or g.y + g.h > f.vh:
                errs.append(f"font {f.index}: glyph U+{g.cp:04X} outside atlas")
                break
        if f.editable:
            n = sum((max(1, f.tex_w >> i)) * (max(1, f.tex_h >> i)) for i in range(f.tex_mips))
            if n != f.tex_size:
                errs.append(f"font {f.index}: texture size field inconsistent")
    if not quiet:
        print(f"[verify] {os.path.basename(path)}: {len(p.fonts)} fonts, {len(rel)} relocations, "
              f"{'OK' if not errs else str(len(errs)) + ' problem(s)'}")
        for e in errs[:20]:
            print("   -", e)
    return not errs


# ----------------------------------------------------------------------------
# export / import
# ----------------------------------------------------------------------------

def font_basename(f):
    return f"font{f.index}_{f.hash}"


def glyph_char(cp):
    c = chr(cp)
    return c if c.isprintable() and not (0xe000 <= cp <= 0xf8ff) and cp != 0xffff else ''


def export_font(f, outdir):
    from PIL import Image
    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, font_basename(f))
    meta = {
        "tool": f"got_font_tool {VERSION}",
        "font_index": f.index, "hash": f.hash, "id": f.id,
        "size": f.size, "spread": f.spread,
        "virtual_width": f.vw, "virtual_height": f.vh,
        "texture_width": f.tex_w, "texture_height": f.tex_h, "editable": f.editable,
        "note": "x/y/w/h/xoff/yoff/adv are virtual pixels (texture px * virtual/texture), top-left origin. "
                "kern: [[second_codepoint, amount], ...]. The PNG may be enlarged (power of two); "
                "then scale virtual_width/height by the same factor.",
        "glyphs": [],
    }
    for g in f.glyphs:
        meta["glyphs"].append({
            "cp": g.cp, "hex": f"U+{g.cp:04X}", "char": glyph_char(g.cp),
            "x": g.x, "y": g.y, "w": g.w, "h": g.h,
            "xoff": g.xoff, "yoff": g.yoff, "adv": g.adv,
            "kern": [[int(c2), int(a)] for c2, a in g.kern],
        })
    with open(base + '.json', 'w', encoding='utf-8') as fp:
        json.dump(meta, fp, ensure_ascii=False, indent=1)
    if f.editable:
        Image.fromarray(f.read_texture(), 'L').save(base + '.png')
    else:
        a = f.pack.DO + f.tex_off
        with open(base + f'_{f.tex_w}x{f.tex_h}_fmt{f.tex_fmt:08x}.bin', 'wb') as fp:
            fp.write(f.pack.data[a:a + f.tex_size])
    return base


def parse_kern(k):
    if not k:
        return []
    if isinstance(k, dict):
        return [(int(a), int(b)) for a, b in k.items()]
    return [(int(a), int(b)) for a, b in k]


def load_font_json(path):
    with open(path, 'r', encoding='utf-8-sig') as fp:
        meta = json.load(fp)
    glyphs = []
    for e in meta["glyphs"]:
        cp = e.get("cp")
        if cp is None:
            cp = int(e["hex"][2:], 16) if e.get("hex") else ord(e["char"])
        glyphs.append(Glyph(int(cp), int(e["x"]), int(e["y"]), int(e["w"]), int(e["h"]),
                            int(e["xoff"]), int(e["yoff"]), int(e["adv"]), parse_kern(e.get("kern"))))
    return meta, glyphs


def import_font(pack, f, json_path, png_path=None):
    import numpy as np
    from PIL import Image
    meta, glyphs = load_font_json(json_path)
    vw = int(meta.get("virtual_width", meta.get("virtual_size", f.vw)))
    vh = int(meta.get("virtual_height", meta.get("virtual_size", f.vh)))
    notes = []
    if (vw, vh) != (f.vw, f.vh):
        f.set_virtual(vw, vh)
        notes.append(f"virtual {vw}x{vh}")
    notes.insert(0, pack.write_font_tables(f, glyphs))
    if png_path and os.path.isfile(png_path) and f.editable:
        img = np.array(Image.open(png_path).convert('L'), np.uint8)
        if img.shape != (f.tex_h, f.tex_w) or not np.array_equal(img, f.read_texture()):
            f.set_texture(img)
            notes.append(f"texture {img.shape[1]}x{img.shape[0]}")
    return ', '.join(notes)


# ----------------------------------------------------------------------------
# TTF rendering (SDF)
# ----------------------------------------------------------------------------

SDF_SLOPE = 1.11 * 128.0   # value = 128 + distance * SDF_SLOPE / spread  (measured on the game fonts)
REF_CHARS = 'HIEFTLNMKZXxzvwnmu'


def estimate_baseline(font):
    gl = {g.cp: g for g in font.glyphs}
    vals = [gl[ord(c)].yoff + gl[ord(c)].h - font.spread for c in REF_CHARS if ord(c) in gl]
    if not vals:
        vals = [g.yoff + g.h - font.spread for g in font.glyphs if g.h > 2 * font.spread]
    vals.sort()
    return vals[len(vals) // 2] if vals else int(font.size * 0.8)


class TTFRenderer:
    def __init__(self, ttf_path, font, scale=1.0, ppem=None, baseline_shift=0):
        from PIL import ImageFont
        self.path, self.font = ttf_path, font
        self.spread = font.spread
        self.ss = 2 if font.size < 400 else 1
        self.baseline = estimate_baseline(font) + baseline_shift
        self.ppem = (ppem or self.calibrate()) * scale
        self.pil = ImageFont.truetype(ttf_path, max(1, int(round(self.ppem * self.ss))))
        try:
            from fontTools.ttLib import TTFont
            self.tt = TTFont(ttf_path, fontNumber=0, lazy=True)
            self.cmap = self.tt.getBestCmap() or {}
            self.upem = self.tt['head'].unitsPerEm
        except Exception:
            self.tt, self.cmap, self.upem = None, None, None

    def has(self, cp):
        return True if self.cmap is None else cp in self.cmap

    def ink_box(self, pil, ch):
        """Exact ink box (x0, y0, x1, y1) relative to the pen origin on the baseline, or None."""
        import numpy as np
        from PIL import Image, ImageDraw
        bb = pil.getbbox(ch, anchor='ls')           # horizontal extent = advance box, not ink
        if bb is None:
            return None
        m = int(pil.size * 0.6) + 4
        w, h = bb[2] - bb[0] + 2 * m, bb[3] - bb[1] + 2 * m
        ox, oy = m - bb[0], m - bb[1]
        im = Image.new('L', (w, h), 0)
        ImageDraw.Draw(im).text((ox, oy), ch, font=pil, fill=255, anchor='ls')
        a = np.array(im)
        ys = np.nonzero(a.max(axis=1))[0]
        if not len(ys):
            return None
        xs = np.nonzero(a.max(axis=0))[0]
        return xs[0] - ox, ys[0] - oy, xs[-1] + 1 - ox, ys[-1] + 1 - oy

    def calibrate(self):
        """px/em so the TTF ink height matches the game font (median over reference glyphs)."""
        from PIL import ImageFont
        probe = ImageFont.truetype(self.path, 200)
        gl = {g.cp: g for g in self.font.glyphs}
        ratios = []
        for ch in REF_CHARS:
            g = gl.get(ord(ch))
            bb = self.ink_box(probe, ch) if g else None
            if not bb or bb[3] - bb[1] <= 0:
                continue
            ratios.append((g.h - 2 * self.spread) / (bb[3] - bb[1]) * 200)
        if not ratios:
            return float(self.font.size) * 0.8
        ratios.sort()
        return ratios[len(ratios) // 2]

    def render(self, cp):
        """Glyph with metrics (virtual px) and .img = SDF crop at the font's texel density."""
        import numpy as np
        from PIL import Image, ImageDraw
        f = self.font.fx
        ch = chr(cp)
        s, R = self.spread, self.ss
        adv = int(round(self.pil.getlength(ch) / R))
        bb = self.ink_box(self.pil, ch)
        if bb is None:
            w = h = int(math.ceil(2 * s / f) * f)
            g = Glyph(cp, 0, 0, w, h, -s, self.baseline - s, adv)
            g.img = np.zeros((int(round(h / f)), int(round(w / f))), np.uint8)
            return g
        il = math.floor(bb[0] / R)
        it = math.floor(bb[1] / R)            # negative = above the baseline
        iw = math.ceil(bb[2] / R) - il
        ih = math.ceil(bb[3] / R) - it
        w = int(math.ceil((iw + 2 * s) / f) * f)
        h = int(math.ceil((ih + 2 * s) / f) * f)
        im = Image.new('L', (w * R, h * R), 0)
        ImageDraw.Draw(im).text(((s - il) * R, (s - it) * R), ch, font=self.pil, fill=255, anchor='ls')
        sdf = signed_distance(np.array(im) >= 128) / R       # virtual px
        val = np.clip(128.0 + sdf * (SDF_SLOPE / s), 0, 255)
        g = Glyph(cp, 0, 0, w, h, il - s, self.baseline + it - s, adv)
        g.img = np.clip(np.round(resample(val, int(round(w / f)), int(round(h / f)))), 0, 255).astype(np.uint8)
        return g

    def kerning(self, cps):
        """{(cp1, cp2): amount in virtual px} from GPOS pair adjustments and the kern table."""
        if self.tt is None:
            return {}
        cps = set(cps)
        g2c = {}
        for cp, gn in self.cmap.items():
            if cp in cps:
                g2c.setdefault(gn, []).append(cp)
        res = {}
        scale = self.ppem / self.upem

        def add(g1, g2, v):
            if v:
                for c1 in g2c.get(g1, ()):
                    for c2 in g2c.get(g2, ()):
                        res.setdefault((c1, c2), v * scale)
        try:
            if 'GPOS' in self.tt and self.tt['GPOS'].table.LookupList:
                for lk in self.tt['GPOS'].table.LookupList.Lookup:
                    for st in lk.SubTable:
                        typ = lk.LookupType
                        if typ == 9:
                            typ, st = st.ExtensionLookupType, st.ExtSubTable
                        if typ != 2:
                            continue
                        cov = st.Coverage.glyphs
                        if st.Format == 1:
                            for gi, ps in zip(cov, st.PairSet):
                                if gi in g2c:
                                    for pv in ps.PairValueRecord:
                                        add(gi, pv.SecondGlyph, getattr(pv.Value1, 'XAdvance', 0) if pv.Value1 else 0)
                        elif st.Format == 2:
                            cd1 = st.ClassDef1.classDefs if st.ClassDef1 else {}
                            cd2 = st.ClassDef2.classDefs if st.ClassDef2 else {}
                            for gi in cov:
                                if gi not in g2c:
                                    continue
                                row = st.Class1Record[cd1.get(gi, 0)].Class2Record
                                for g2 in g2c:
                                    v1 = row[cd2.get(g2, 0)].Value1
                                    add(gi, g2, getattr(v1, 'XAdvance', 0) if v1 else 0)
            if 'kern' in self.tt:
                for kt in self.tt['kern'].kernTables:
                    for (g1, g2), v in getattr(kt, 'kernTable', {}).items():
                        add(g1, g2, v)
        except Exception as ex:
            print(f"    [!] could not read kerning: {ex}")
        return {k: int(round(v)) for k, v in res.items() if int(round(v))}


def signed_distance(mask):
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    if not mask.any():
        return np.full(mask.shape, -1e3)
    inside = distance_transform_edt(mask)
    outside = distance_transform_edt(~mask)
    return np.where(mask, inside - 0.5, -(outside - 0.5))


def resample(a, tw, th):
    import numpy as np
    h, w = a.shape
    if h % th == 0 and w % tw == 0:
        return a.reshape(th, h // th, tw, w // tw).mean(axis=(1, 3))
    from PIL import Image
    return np.array(Image.fromarray(a.astype(np.float32), 'F').resize((tw, th), Image.BOX))


# ----------------------------------------------------------------------------
# atlas packing
# ----------------------------------------------------------------------------

def crop_existing(font, atlas, g):
    """Cut an existing glyph out of the atlas, remembering its sub-texel offset."""
    fx, fy = font.fx, font.fy
    x0, y0 = int(g.x // fx), int(g.y // fy)
    x1, y1 = int(math.ceil((g.x + g.w) / fx)), int(math.ceil((g.y + g.h) / fy))
    g.img = atlas[y0:y1, x0:x1].copy()
    g.sub = (g.x - int(round(x0 * fx)), g.y - int(round(y0 * fy)))


class Skyline:
    """Bottom-left skyline bin packer (texel units, `gap` texels between glyphs)."""

    def __init__(self, w, h, gap=1):
        self.w, self.h, self.gap = w, h, gap
        self.sky = [(0, 0, w)]      # segments (x, y, width), sorted by x, covering [0, w)

    def _top(self, i, ww):
        y, rem = 0, ww
        while rem > 0:
            x, sy, sw = self.sky[i]
            y = max(y, sy)
            rem -= sw
            i += 1
        return y

    def place(self, w, h):
        best = None
        for i, (x, _, _) in enumerate(self.sky):
            if x + w > self.w:
                break
            ww = min(w + self.gap, self.w - x)
            y = self._top(i, ww)
            if y + h > self.h:
                continue
            key = (y + h, x)
            if best is None or key < best[0]:
                best = (key, x, y, ww)
        if best is None:
            return None
        _, x, y, ww = best
        end = x + ww
        out, inserted = [], False
        for sx, sy, sw in self.sky:
            se = sx + sw
            if se <= x or sx >= end:
                out.append((sx, sy, sw))
                continue
            if sx < x:
                out.append((sx, sy, x - sx))
            if not inserted:
                out.append((x, y + h + self.gap, ww))
                inserted = True
            if se > end:
                out.append((end, sy, se - end))
        merged = []
        for seg in out:
            if merged and merged[-1][1] == seg[1] and merged[-1][0] + merged[-1][2] == seg[0]:
                merged[-1] = (merged[-1][0], seg[1], merged[-1][2] + seg[2])
            else:
                merged.append(seg)
        self.sky = merged
        return x, y


def pack_atlas(font, glyphs, tex_w, tex_h, gap=1):
    """Place every glyph (all must have .img) into a tex_w x tex_h atlas. Returns image or None."""
    import numpy as np
    sk = Skyline(tex_w, tex_h, gap)
    canvas = np.zeros((tex_h, tex_w), np.uint8)
    fx, fy = font.fx, font.fy          # texel density stays the same when the atlas grows
    vw, vh = int(round(tex_w * fx)), int(round(tex_h * fy))
    for g in sorted(glyphs, key=lambda g: (-g.img.shape[0], -g.img.shape[1], g.cp)):
        th, tw = g.img.shape
        pos = sk.place(tw, th)
        if pos is None:
            return None
        x, y = pos
        canvas[y:y + th, x:x + tw] = g.img
        g.x = int(round(x * fx)) + g.sub[0]
        g.y = int(round(y * fy)) + g.sub[1]
        g.x, g.y = min(g.x, vw - g.w), min(g.y, vh - g.h)
    return canvas


def build_atlas(font, glyphs, grow='rect', max_tex=MAX_TEX):
    """Repack glyphs; grows the texture (and virtual size, keeping texel density) when full."""
    tw, th = font.tex_w, font.tex_h
    while True:
        img = pack_atlas(font, glyphs, tw, th)
        if img is not None:
            return img, tw, th
        if tw >= max_tex and th >= max_tex:
            raise RuntimeError("glyphs do not fit even into a %dx%d atlas" % (max_tex, max_tex))
        if grow == 'rect':
            if th <= tw and th < max_tex:
                th *= 2
            else:
                tw *= 2
        else:
            tw, th = min(tw * 2, max_tex), min(th * 2, max_tex)
        print(f"    [i] atlas full - growing texture to {tw}x{th}")


def apply_atlas(font, glyphs, img, tw, th, pack):
    if (tw, th) != (font.tex_w, font.tex_h):
        font.set_virtual(int(round(tw * font.fx)), int(round(th * font.fy)))
    font.set_texture(img)
    return pack.write_font_tables(font, glyphs)


# ----------------------------------------------------------------------------
# preview renderer
# ----------------------------------------------------------------------------

def glyph_alpha(font, atlas, g, scale, soft=None):
    """Coverage (0..1 float array) of a glyph box drawn at `scale` screen px per virtual px."""
    import numpy as np
    from PIL import Image
    th, tw = atlas.shape
    fx, fy = font.vw / tw, font.vh / th
    crop = atlas[int(g.y / fy):int(math.ceil((g.y + g.h) / fy)), int(g.x / fx):int(math.ceil((g.x + g.w) / fx))]
    dw, dh = max(1, int(round(g.w * scale))), max(1, int(round(g.h * scale)))
    if not crop.size:
        return np.zeros((dh, dw), np.float32)
    im = np.array(Image.fromarray(crop.astype(np.float32), 'F').resize((dw, dh), Image.BILINEAR))
    if soft is None:
        soft = max(1.0, 8.0 / (scale * fx))
    return np.clip((im - 128.0) / soft + 0.5, 0, 1)


def measure_line(font, gl, line):
    w, prev = 0, None
    for c in line:
        g = gl.get(ord(c)) or gl.get(0xffff)
        if g is None:
            continue
        if prev is not None and prev.kern:
            w += next((a for c2, a in prev.kern if c2 == g.cp), 0)
        w += g.adv
        prev = g
    return w


def text_image(font, text, height=96, fg=(0, 0, 0), bg=(255, 255, 255), margin=None, uppercase=False,
               atlas=None, boxes=False):
    """Render text with the game font (like the game does: SDF threshold) -> PIL RGB image."""
    import numpy as np
    from PIL import Image
    if uppercase:
        text = text.upper()
    atlas = font.read_texture() if atlas is None else atlas
    gl = {g.cp: g for g in font.glyphs}
    lines = text.split('\n') or ['']
    scale = height / font.size
    margin = height // 2 if margin is None else margin
    line_h = font.size * 1.25 * scale
    W = int(max([measure_line(font, gl, ln) for ln in lines] + [1]) * scale + 2 * margin)
    H = int(len(lines) * line_h + 2 * margin)
    out = np.zeros((H, W), np.float32)
    box = np.zeros((H, W), np.float32) if boxes else None
    ry = margin
    for ln in lines:
        pen = margin / scale
        prev = None
        for c in ln:
            g = gl.get(ord(c)) or gl.get(0xffff)
            if g is None:
                continue
            if prev is not None and prev.kern:
                pen += next((a for c2, a in prev.kern if c2 == g.cp), 0)
            x0 = int(round((pen + g.xoff) * scale))
            y0 = int(round(ry + g.yoff * scale))
            if g.w > 2 * font.spread:
                alpha = glyph_alpha(font, atlas, g, scale)
                dh, dw = alpha.shape
                xs, ys, xe, ye = max(0, x0), max(0, y0), min(W, x0 + dw), min(H, y0 + dh)
                if xe > xs and ye > ys:
                    out[ys:ye, xs:xe] = np.maximum(out[ys:ye, xs:xe], alpha[ys - y0:ye - y0, xs - x0:xe - x0])
                if boxes:
                    x1, y1 = min(W - 1, x0 + dw - 1), min(H - 1, y0 + dh - 1)
                    if 0 <= x0 < W and 0 <= y0 < H:
                        box[y0, max(0, x0):x1] = 1
                        box[y1, max(0, x0):x1] = 1
                        box[max(0, y0):y1, x0] = 1
                        box[max(0, y0):y1, x1] = 1
            pen += g.adv
            prev = g
        ry += line_h
    fg, bg = np.array(fg, np.float32), np.array(bg, np.float32)
    rgb = bg[None, None, :] * (1 - out[..., None]) + fg[None, None, :] * out[..., None]
    if boxes:
        rgb = rgb * (1 - 0.6 * box[..., None]) + np.array([230, 80, 60], np.float32) * 0.6 * box[..., None]
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), 'RGB')


def render_preview(font, text, out_png, height=96):
    text_image(font, text, height).save(out_png)


def glyph_thumb(font, atlas, g, size):
    """Glyph box scaled to fit a size x size square -> float coverage array (size, size)."""
    import numpy as np
    out = np.zeros((size, size), np.float32)
    if g.w <= 0 or g.h <= 0:
        return out
    scale = (size - 2) / max(g.w, g.h)
    a = glyph_alpha(font, atlas, g, scale)
    h, w = a.shape
    h, w = min(h, size), min(w, size)
    y0, x0 = (size - h) // 2, (size - w) // 2
    out[y0:y0 + h, x0:x0 + w] = a[:h, :w]
    return out


def font_role(f):
    """Best guess of what a font is used for (from its size and charset)."""
    if not f.editable:
        return 'icons'
    if f.count > 1000:
        return 'cjk'
    if f.size >= 900:
        return 'menu'          # main menu (caps style), e.g. 1252 / 1185 / 1049
    if f.size >= 400:
        return 'title'
    if f.size <= 131:
        return 'body'
    return 'ui'


# ----------------------------------------------------------------------------
# operations (shared by the CLI and the GUI; they only change the Pack in memory)
# ----------------------------------------------------------------------------

def chars_to_cps(text):
    import unicodedata
    out = set()
    for c in set(text):
        if not 0x20 <= ord(c) <= 0xffff:
            continue
        # control/format chars (ZWSP, BOM...) are not drawn; PUA = button icons (icon font)
        if unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Co'):
            continue
        out.add(ord(c))
    return sorted(out)


def text_from_json(path):
    """All text of an xpps_tool strings.json (dict or list, values may be lists of lines)."""
    with open(path, 'r', encoding='utf-8-sig') as fp:
        data = json.load(fp)
    vals = data.values() if isinstance(data, dict) else data
    out = []
    for v in vals:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            out += [x for x in v if isinstance(x, str)]
    return '\n'.join(out)


def missing_cps(font, cps):
    have = {g.cp for g in font.glyphs}
    return [c for c in cps if c not in have]


def op_add(pack, fonts, ttf, cps, kern=False, scale=1.0, baseline_shift=0, grow='rect', replace_existing=False):
    """Render glyphs from a TTF/OTF into the given fonts. Returns number of glyphs added."""
    total = 0
    for f in fonts:
        if not f.editable:
            continue
        todo = list(cps) if replace_existing else missing_cps(f, cps)
        if not todo:
            print(f"[=] font {f.index}: all characters present")
            continue
        r = TTFRenderer(ttf, f, scale=scale or 1.0, baseline_shift=baseline_shift or 0)
        new = [r.render(cp) for cp in todo if r.has(cp)]
        lost = [cp for cp in todo if not r.has(cp)]
        if lost:
            print(f"    [!] {os.path.basename(ttf)} has no glyph for: " + ''.join(chr(c) for c in lost))
        if not new:
            continue
        newset = {g.cp for g in new}
        atlas = f.read_texture()
        keep = [g for g in f.glyphs if g.cp not in newset]
        for g in keep:
            crop_existing(f, atlas, g)
        if kern:
            byc = {g.cp: g for g in keep + new}
            nk = 0
            for (c1, c2), v in r.kerning(list(byc)).items():
                if (c1 in newset or c2 in newset) and not any(k[0] == c2 for k in byc[c1].kern):
                    byc[c1].kern.append((c2, v))
                    nk += 1
            print(f"    kerning pairs added: {nk}")
        img, tw, th = build_atlas(f, keep + new, grow=grow)
        mode = apply_atlas(f, keep + new, img, tw, th, pack)
        print(f"[+] font {f.index}: +{len(new)} glyphs ({''.join(chr(g.cp) for g in new)[:60]}), "
              f"px/em {r.ppem:.1f}, texture {tw}x{th}, tables {mode}")
        total += len(new)
    return total


def op_replace(pack, fonts, ttf, extra_cps=(), kern=True, scale=1.0, baseline_shift=0, grow='rect'):
    """Rebuild whole fonts from a TTF/OTF (keeps the charset; glyphs the TTF lacks stay original)."""
    n = 0
    for f in fonts:
        if not f.editable:
            continue
        r = TTFRenderer(ttf, f, scale=scale or 1.0, baseline_shift=baseline_shift or 0)
        atlas = f.read_texture()
        old = {g.cp: g for g in f.glyphs}
        glyphs, kept = [], []
        for cp in sorted(set(old) | set(extra_cps)):
            if r.has(cp) and cp != 0xffff:
                glyphs.append(r.render(cp))
            elif cp in old:
                crop_existing(f, atlas, old[cp])
                glyphs.append(old[cp])
                kept.append(cp)
        if kept:
            print(f"    [i] font {f.index}: kept {len(kept)} original glyph(s) the TTF does not have: " +
                  ''.join(glyph_char(c) or '?' for c in kept[:60]))
        byc = {g.cp: g for g in glyphs}
        for g in glyphs:
            if g.cp not in kept:
                g.kern = []
        if kern:
            kp = r.kerning(list(byc))
            for (c1, c2), v in sorted(kp.items()):
                if c1 not in kept:
                    byc[c1].kern.append((c2, v))
            print(f"    kerning pairs: {len(kp)}")
        img, tw, th = build_atlas(f, glyphs, grow=grow)
        mode = apply_atlas(f, glyphs, img, tw, th, pack)
        print(f"[+] font {f.index}: {len(glyphs)} glyphs from {os.path.basename(ttf)}, "
              f"px/em {r.ppem:.1f}, texture {tw}x{th}, tables {mode}")
        n += 1
    return n


def image_glyph(font, cp, mask_img, height=1.2, drop=0.1, bearing=0.08):
    """
    Glyph from a picture (logo/icon). mask_img: PIL image; alpha is used when present,
    otherwise brightness (white = ink). height/drop/bearing are fractions of the cap height:
    ink height, how far it goes below the baseline, and the left/right side bearing.
    """
    import numpy as np
    from PIL import Image
    s, f = font.spread, font.fx
    gl = {g.cp: g for g in font.glyphs}
    capref = [gl[ord(c)].h - 2 * s for c in 'HIE' if ord(c) in gl]
    cap = sorted(capref)[len(capref) // 2] if capref else font.size * 0.7
    base = estimate_baseline(font)
    im = mask_img
    if im.mode in ('RGBA', 'LA') and im.getchannel('A').getextrema()[0] < 255:
        a = im.getchannel('A')
    else:
        a = im.convert('L')
    bbox = a.point(lambda v: 255 if v >= 128 else 0).getbbox()
    if not bbox:
        raise ValueError("image has no ink (white or opaque pixels)")
    a = a.crop(bbox)
    ih = max(1, int(round(cap * height)))
    iw = max(1, int(round(a.width * ih / a.height)))
    R = 4                                        # supersampling for a clean edge
    m = np.array(a.resize((iw * R, ih * R), Image.LANCZOS)) >= 128
    w = int(math.ceil((iw + 2 * s) / f) * f)
    h = int(math.ceil((ih + 2 * s) / f) * f)
    canvas = np.zeros((h * R, w * R), bool)
    canvas[s * R:s * R + ih * R, s * R:s * R + iw * R] = m
    sdf = signed_distance(canvas) / R
    val = np.clip(128.0 + sdf * (SDF_SLOPE / s), 0, 255)
    lsb = int(round(cap * bearing))
    top = base + int(round(cap * drop)) - ih          # ink top, from the line top
    g = Glyph(cp, 0, 0, w, h, lsb - s, top - s, iw + 2 * lsb)
    g.img = np.clip(np.round(resample(val, int(round(w / f)), int(round(h / f)))), 0, 255).astype(np.uint8)
    return g


def op_addimg(pack, fonts, cp, image, height=1.2, drop=0.1, bearing=0.08, invert=False, grow='rect'):
    """Add (or overwrite) one glyph made from an image in the given fonts."""
    from PIL import Image, ImageOps
    if not 0x20 < cp <= 0xffff:
        raise ValueError("code point must be in the BMP (U+0021..U+FFFF)")
    img = Image.open(image) if isinstance(image, str) else image
    if invert:
        img = ImageOps.invert(img.convert('L'))
    n = 0
    for f in fonts:
        if not f.editable:
            continue
        g = image_glyph(f, cp, img, height, drop, bearing)
        atlas = f.read_texture()
        keep = [x for x in f.glyphs if x.cp != cp]
        for x in keep:
            crop_existing(f, atlas, x)
        img_atlas, tw, th = build_atlas(f, keep + [g], grow=grow)
        mode = apply_atlas(f, keep + [g], img_atlas, tw, th, pack)
        print(f"[+] font {f.index}: U+{cp:04X} from image ({g.w}x{g.h} virtual px), "
              f"texture {tw}x{th}, tables {mode}")
        n += 1
    return n


def op_remove(pack, font, cps):
    """Remove glyphs (and kerning pairs pointing at them) from a font."""
    cps = set(cps)
    keep = [g for g in font.glyphs if g.cp not in cps]
    if len(keep) == len(font.glyphs):
        return 0
    for g in keep:
        g.kern = [k for k in g.kern if k[0] not in cps]
    n = len(font.glyphs) - len(keep)
    mode = pack.write_font_tables(font, keep)
    print(f"[-] font {font.index}: removed {n} glyph(s), tables {mode}")
    return n


def op_set_metrics(pack, font, cp, adv=None, xoff=None, yoff=None):
    """Change the spacing of one glyph (in place)."""
    glyphs = list(font.glyphs)
    g = next((x for x in glyphs if x.cp == cp), None)
    if g is None:
        raise ValueError(f"U+{cp:04X} is not in font {font.index}")
    if adv is not None:
        g.adv = int(adv)
    if xoff is not None:
        g.xoff = int(xoff)
    if yoff is not None:
        g.yoff = int(yoff)
    return pack.write_font_tables(font, glyphs)


def parse_cp(v):
    v = v.strip()
    if v[:2].upper() in ('U+', '0X'):
        return int(v[2:], 16)
    if len(v) == 1:
        return ord(v)
    return int(v, 16)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def pick_fonts(pack, sel, editable_only=True):
    if sel is None or sel == 'all':
        return [f for f in pack.fonts if f.editable or not editable_only]
    out = []
    for s in str(sel).split(','):
        s = s.strip()
        m = [f for f in pack.fonts if str(f.index) == s or f.hash.startswith(s.lower())]
        if not m:
            raise SystemExit(f"font '{s}' not found (use 'list')")
        out += m
    return out


def parse_chars(args):
    chars = ''
    if getattr(args, 'chars', None):
        chars += args.chars
    if getattr(args, 'charfile', None):
        with open(args.charfile, 'r', encoding='utf-8-sig') as fp:
            chars += fp.read()
    if getattr(args, 'from_json', None):
        chars += text_from_json(args.from_json)
    return chars_to_cps(chars)


def default_out(path):
    b, e = os.path.splitext(path)
    return b + '_font' + e


def finish(pack, out):
    pack.save(out)
    print(f"[+] saved {out} ({os.path.getsize(out):,} bytes)")
    if not verify(out):
        raise SystemExit(1)


def write_previews(args, fonts):
    if getattr(args, 'preview', None):
        for f in fonts:
            pv = os.path.splitext(args.output or default_out(args.xpps))[0] + f'_font{f.index}_preview.png'
            render_preview(f, args.preview, pv)
            print(f"    preview: {pv}")


def cmd_list(args):
    p = Pack(args.xpps)
    print(f"{os.path.basename(args.xpps)}: payload {p.DS:,} bytes, {len(p.blocks)} blocks, "
          f"{sum(len(l) for t, l in p.knli_subs):,} relocations")
    print(f"{'#':<3}{'hash':<18}{'role':<7}{'size':>6}{'spread':>7}{'virtual':>12}{'texture':>11}{'glyphs':>7}  range")
    for f in p.fonts:
        cps = [g.cp for g in f.glyphs]
        note = '' if f.editable else '  (button icons, block compressed - not editable)'
        print(f"{f.index:<3}{f.hash:<18}{font_role(f):<7}{f.size:>6}{f.spread:>7}{f'{f.vw}x{f.vh}':>12}"
              f"{f'{f.tex_w}x{f.tex_h}':>11}{f.count:>7}  U+{min(cps):04X}..U+{max(cps):04X}{note}")
    want = parse_chars(args)
    if want:
        for f in p.fonts:
            if f.editable:
                miss = missing_cps(f, want)
                print(f"  font {f.index}: missing {len(miss)}" + (": " + ''.join(chr(c) for c in miss[:300]) if miss else ''))


def cmd_export(args):
    p = Pack(args.xpps)
    out = args.output or os.path.splitext(args.xpps)[0] + '_fonts'
    for f in pick_fonts(p, args.font, editable_only=False):
        base = export_font(f, out)
        print(f"[+] font {f.index} -> {base}.json ({f.count} glyphs)")


def cmd_import(args):
    p = Pack(args.xpps)
    n = 0
    for f in p.fonts:
        j = os.path.join(args.fontdir, font_basename(f) + '.json')
        if os.path.isfile(j):
            mode = import_font(p, f, j, os.path.join(args.fontdir, font_basename(f) + '.png'))
            print(f"[+] font {f.index}: {mode}")
            n += 1
    if not n:
        raise SystemExit("no fontN_<hash>.json matching this file in " + args.fontdir)
    finish(p, args.output or default_out(args.xpps))


def cmd_add(args):
    p = Pack(args.xpps)
    want = parse_chars(args)
    if not want:
        raise SystemExit("no characters given (--chars / --charfile / --from-json)")
    fonts = pick_fonts(p, args.font)
    if op_add(p, fonts, args.ttf, want, args.kern, args.scale, args.baseline_shift, args.grow,
              args.replace_existing):
        write_previews(args, fonts)
        finish(p, args.output or default_out(args.xpps))


def cmd_replace(args):
    p = Pack(args.xpps)
    fonts = pick_fonts(p, args.font)
    op_replace(p, fonts, args.ttf, parse_chars(args), not args.no_kern, args.scale, args.baseline_shift, args.grow)
    write_previews(args, fonts)
    finish(p, args.output or default_out(args.xpps))


def cmd_addimg(args):
    p = Pack(args.xpps)
    fonts = pick_fonts(p, args.font)
    op_addimg(p, fonts, parse_cp(args.cp), args.image, args.height, args.drop, args.bearing, args.invert, args.grow)
    write_previews(args, fonts)
    finish(p, args.output or default_out(args.xpps))


def cmd_remove(args):
    p = Pack(args.xpps)
    cps = parse_chars(args)
    for f in pick_fonts(p, args.font):
        op_remove(p, f, cps)
    finish(p, args.output or default_out(args.xpps))


def cmd_preview(args):
    p = Pack(args.xpps)
    fonts = pick_fonts(p, args.font)
    for f in fonts:
        out = args.output or os.path.splitext(args.xpps)[0] + '_preview.png'
        if len(fonts) > 1:
            out = os.path.splitext(out)[0] + f'_font{f.index}.png'
        text_image(f, args.text.replace('\\n', '\n'), args.height, uppercase=args.upper).save(out)
        print(f"[+] {out}")


def cmd_verify(args):
    sys.exit(0 if verify(args.xpps) else 1)


def cmd_gui(args):
    run_gui(getattr(args, 'xpps', None))


def build_parser():
    ap = argparse.ArgumentParser(
        prog='got_font_tool',
        description=f"Ghost of Tsushima DIRECTOR'S CUT - Font Tool v{VERSION} (lang_*_text.xpps SDF fonts). "
                    "Run without arguments to open the GUI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  got_font_tool                                   (opens the GUI)
  got_font_tool gui     lang_turkish_text.xpps
  got_font_tool list    lang_turkish_text.xpps --from-json my_translation.json
  got_font_tool export  lang_turkish_text.xpps fonts_out
  got_font_tool import  lang_turkish_text.xpps fonts_out -o lang_turkish_text_new.xpps
  got_font_tool add     lang_turkish_text.xpps --ttf myfont.ttf --from-json my_translation.json --kern
  got_font_tool replace lang_turkish_text.xpps --font 2 --ttf title.otf --preview "Hayalet"
  got_font_tool addimg  lang_turkish_text.xpps --image logo.png --cp U+2605 --preview "Logo ★ test"
  got_font_tool remove  lang_turkish_text.xpps --font 0 --chars "★"
  got_font_tool preview lang_turkish_text.xpps --font 0 --text "Çağrı Işığı Ötüşü"
  got_font_tool verify  lang_turkish_text_new.xpps
""")
    sp = ap.add_subparsers(dest='cmd')

    a = sp.add_parser('gui', help='open the graphical interface (default without arguments)')
    a.add_argument('xpps', nargs='?')
    a.set_defaults(fn=cmd_gui)

    def char_args(a):
        a.add_argument('--chars', help='characters')
        a.add_argument('--charfile', help='UTF-8 text file with characters')
        a.add_argument('--from-json', help='every character used in an xpps_tool strings.json')

    a = sp.add_parser('list', help='list fonts in an .xpps (and missing characters)')
    a.add_argument('xpps')
    char_args(a)
    a.set_defaults(fn=cmd_list)

    a = sp.add_parser('export', help='export fonts as PNG atlas + JSON metrics')
    a.add_argument('xpps')
    a.add_argument('output', nargs='?')
    a.add_argument('--font', help="font index/hash list, e.g. 0,2 (default: all)")
    a.set_defaults(fn=cmd_export)

    a = sp.add_parser('import', help='import edited PNG/JSON (glyphs may be added or removed)')
    a.add_argument('xpps')
    a.add_argument('fontdir')
    a.add_argument('-o', '--output')
    a.set_defaults(fn=cmd_import)

    for name, fn, hlp in (('add', cmd_add, 'add glyphs rendered from a TTF/OTF'),
                          ('replace', cmd_replace, 'rebuild whole font(s) from a TTF/OTF')):
        a = sp.add_parser(name, help=hlp)
        a.add_argument('xpps')
        a.add_argument('--ttf', required=True, help='TTF/OTF font file')
        a.add_argument('--font', default='all', help="font index/hash list (default: all editable)")
        char_args(a)
        a.add_argument('--scale', type=float, help='size multiplier for the TTF glyphs (default: auto-match)')
        a.add_argument('--baseline-shift', type=int, help='move new glyphs down (+) / up (-) in virtual px')
        a.add_argument('--grow', choices=['rect', 'square'], default='rect',
                       help='how to enlarge a full atlas: rect = one side at a time (default, like the '
                            "game's own 2048x4096 CJK atlases), square = both sides")
        a.add_argument('--preview', help='also write a preview PNG of this text')
        a.add_argument('-o', '--output')
        if name == 'add':
            a.add_argument('--kern', action='store_true', help='add TTF kerning pairs that involve the new glyphs')
            a.add_argument('--replace-existing', action='store_true', help='re-render the given chars even if present')
        else:
            a.add_argument('--no-kern', action='store_true', help='keep no kerning (default: import from the TTF)')
        a.set_defaults(fn=fn)

    a = sp.add_parser('addimg', help='add a glyph made from an image (logo/icon) at a code point')
    a.add_argument('xpps')
    a.add_argument('--image', required=True, help='PNG: alpha channel or white-on-black is the shape')
    a.add_argument('--cp', required=True, help='code point, e.g. U+2605 or the character itself')
    a.add_argument('--font', default='all')
    a.add_argument('--height', type=float, default=1.2, help='ink height as a multiple of the cap height (1.2)')
    a.add_argument('--drop', type=float, default=0.1, help='part below the baseline, x cap height (0.1)')
    a.add_argument('--bearing', type=float, default=0.08, help='side spacing, x cap height (0.08)')
    a.add_argument('--invert', action='store_true', help='black-on-white image')
    a.add_argument('--grow', choices=['rect', 'square'], default='rect')
    a.add_argument('--preview')
    a.add_argument('-o', '--output')
    a.set_defaults(fn=cmd_addimg)

    a = sp.add_parser('remove', help='remove glyphs from font(s)')
    a.add_argument('xpps')
    a.add_argument('--font', default='all')
    char_args(a)
    a.add_argument('-o', '--output')
    a.set_defaults(fn=cmd_remove)

    a = sp.add_parser('preview', help='render text with a font to PNG (no game needed)')
    a.add_argument('xpps')
    a.add_argument('--font', default='all')
    a.add_argument('--text', default='Ghost of Tsushima - Çağ ğüşıöç ĞÜŞİÖÇ 0123456789')
    a.add_argument('--height', type=int, default=96)
    a.add_argument('--upper', action='store_true', help='upper-case the text (main menu style)')
    a.add_argument('-o', '--output')
    a.set_defaults(fn=cmd_preview)

    a = sp.add_parser('verify', help='check relocations / fonts of an .xpps')
    a.add_argument('xpps')
    a.set_defaults(fn=cmd_verify)
    return ap


def main():
    argv = sys.argv[1:]
    if not argv or (len(argv) == 1 and os.path.isfile(argv[0])):
        # no arguments (double click) or a dropped file -> GUI
        run_gui(argv[0] if argv else None)
        return
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, 'fn', None):
        ap.print_help()
        return
    try:
        args.fn(args)
    except (ValueError, RuntimeError) as ex:
        print(f"[ERROR] {ex}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------
# GUI (tkinter)
# ----------------------------------------------------------------------------

I18N = {
    'en': {
        'title': "Ghost of Tsushima Font Tool", 'file': "File", 'open': "Open .xpps…", 'save': "Save",
        'save_as': "Save as…", 'revert': "Revert (reload from disk)", 'undo': "Undo", 'export': "Export fonts (PNG + JSON)…",
        'import': "Import fonts from folder…", 'exit': "Exit", 'fonts_menu': "Fonts", 'add': "Add glyphs from TTF/OTF…",
        'replace': "Replace font with TTF/OTF…", 'addimg': "Add image glyph (logo/icon)…", 'verify': "Verify file",
        'language': "Language", 'help': "Help", 'howto': "How to use", 'about': "About",
        'fonts': "Fonts in this file", 'col_role': "Used for", 'col_size': "Size", 'col_glyphs': "Glyphs",
        'col_atlas': "Atlas", 'no_file': "Open a lang_*_text.xpps (File > Open, or drop it on the .exe).",
        'tab_preview': "Preview", 'tab_glyphs': "Glyphs", 'tab_atlas': "Atlas", 'tab_cover': "Missing characters",
        'tab_log': "Log", 'sample': "Sample text:", 'load_json': "Load strings.json…", 'search': "Search:",
        'height': "Size:", 'dark': "Dark background", 'upper': "UPPER CASE (main menu)", 'boxes': "Glyph boxes",
        'all_fonts': "All fonts", 'missing_in_text': "Missing in this font:", 'none': "none",
        'page': "Page", 'prev': "◀", 'next': "▶", 'glyph': "Glyph", 'advance': "Advance", 'xoff': "X offset",
        'yoff': "Y offset", 'apply': "Apply", 'delete': "Delete glyph", 'kerning': "Kerning pairs",
        'box': "Box (virtual px)", 'fit': "Fit", 'zoom': "Zoom:", 'check': "Check", 'load_txt': "Load .txt…",
        'cover_hint': "Paste your translation (or load strings.json / .txt) and press Check.",
        'add_missing': "Add missing characters from TTF…", 'col_font': "Font", 'col_missing': "Missing",
        'col_chars': "Characters", 'ttf': "TTF/OTF font:", 'browse': "Browse…", 'chars': "Characters:",
        'extra_chars': "Extra characters:", 'target_fonts': "Target fonts:", 'kern': "Import kerning from the font",
        'scale': "Size multiplier:", 'baseline': "Baseline shift (px):", 'replace_existing': "Re-render characters that already exist",
        'grow': "When the atlas is full:", 'grow_rect': "grow one side (smaller file)", 'grow_square': "grow both sides",
        'ok': "OK", 'cancel': "Cancel", 'image': "Image (PNG):", 'codepoint': "Character / code point:",
        'img_height': "Height (× cap height):", 'img_drop': "Below baseline (× cap height):",
        'img_bearing': "Side spacing (× cap height):", 'invert': "Black shape on white background",
        'busy': "Working…", 'ready': "Ready", 'unsaved': "You have unsaved changes. Continue?",
        'saved': "Saved and verified:", 'verify_ok': "Verification passed.", 'verify_bad': "Verification found problems (see Log).",
        'err': "Error", 'no_font_sel': "Select a font first.", 'no_ttf': "Choose a TTF/OTF file.",
        'exported': "Fonts exported to:", 'imported': "Imported fonts:", 'none_imported': "No matching fontN_<hash>.json in that folder.",
        'role_body': "Text", 'role_ui': "UI", 'role_title': "Title", 'role_menu': "Main menu (caps)",
        'role_cjk': "CJK text", 'role_icons': "Button icons", 'strings_loaded': "strings loaded",
        'not_editable': "This font (button icons) is block compressed and cannot be edited.",
        'confirm_delete': "Delete this glyph from the font?", 'undo_empty': "Nothing to undo.",
        'howto_text': (
            "1. Unpack gapack_misc_l.psarc with got_psarc_tool and open lang_<language>_text.xpps.\n"
            "2. Preview: type text or load your xpps_tool strings.json and click a line to see it in the game font.\n"
            "3. Missing characters: paste/load your translation and press Check to see which letters each font lacks.\n"
            "4. Add glyphs from TTF/OTF: pick a font file (e.g. from C:\\Windows\\Fonts); size and baseline are matched automatically.\n"
            "5. Replace font: rebuild a whole font from a TTF/OTF (e.g. a different title font).\n"
            "6. Glyphs tab: inspect any glyph, fix its spacing (advance / offsets) or delete it.\n"
            "7. Add image glyph: put a logo or icon on any character (e.g. ★ U+2605) and use it in your text.\n"
            "8. Save as… writes and verifies the .xpps; repack the archive with got_psarc_tool.\n\n"
            "Edits stay in memory until you save; Undo and Revert are available."),
    },
    'tr': {
        'title': "Ghost of Tsushima Font Aracı", 'file': "Dosya", 'open': ".xpps aç…", 'save': "Kaydet",
        'save_as': "Farklı kaydet…", 'revert': "Geri dön (diskten yeniden yükle)", 'undo': "Geri al",
        'export': "Fontları dışa aktar (PNG + JSON)…", 'import': "Klasörden font içe aktar…", 'exit': "Çıkış",
        'fonts_menu': "Fontlar", 'add': "TTF/OTF'den glif ekle…", 'replace': "Fontu TTF/OTF ile değiştir…",
        'addimg': "Resimden glif ekle (logo/ikon)…", 'verify': "Dosyayı doğrula", 'language': "Dil", 'help': "Yardım",
        'howto': "Nasıl kullanılır", 'about': "Hakkında", 'fonts': "Bu dosyadaki fontlar", 'col_role': "Kullanım",
        'col_size': "Boyut", 'col_glyphs': "Glif", 'col_atlas': "Atlas",
        'no_file': "Bir lang_*_text.xpps açın (Dosya > Aç ya da dosyayı .exe'nin üzerine bırakın).",
        'tab_preview': "Önizleme", 'tab_glyphs': "Glifler", 'tab_atlas': "Atlas", 'tab_cover': "Eksik karakterler",
        'tab_log': "Kayıt", 'sample': "Örnek metin:", 'load_json': "strings.json yükle…", 'search': "Ara:",
        'height': "Boyut:", 'dark': "Koyu arka plan", 'upper': "BÜYÜK HARF (ana menü)", 'boxes': "Glif kutuları",
        'all_fonts': "Tüm fontlar", 'missing_in_text': "Bu fontta eksik:", 'none': "yok",
        'page': "Sayfa", 'prev': "◀", 'next': "▶", 'glyph': "Glif", 'advance': "İlerleme (advance)",
        'xoff': "X kayması", 'yoff': "Y kayması", 'apply': "Uygula", 'delete': "Glifi sil", 'kerning': "Kerning çiftleri",
        'box': "Kutu (sanal px)", 'fit': "Sığdır", 'zoom': "Yakınlık:", 'check': "Kontrol et", 'load_txt': ".txt yükle…",
        'cover_hint': "Çevirinizi yapıştırın (ya da strings.json / .txt yükleyin) ve Kontrol et'e basın.",
        'add_missing': "Eksik karakterleri TTF'den ekle…", 'col_font': "Font", 'col_missing': "Eksik",
        'col_chars': "Karakterler", 'ttf': "TTF/OTF font:", 'browse': "Gözat…", 'chars': "Karakterler:",
        'extra_chars': "Ek karakterler:", 'target_fonts': "Hedef fontlar:", 'kern': "Fonttan kerning al",
        'scale': "Boyut çarpanı:", 'baseline': "Taban çizgisi kaydırma (px):",
        'replace_existing': "Var olan karakterleri de yeniden çiz", 'grow': "Atlas dolduğunda:",
        'grow_rect': "tek yönde büyüt (küçük dosya)", 'grow_square': "iki yönde büyüt", 'ok': "Tamam", 'cancel': "İptal",
        'image': "Resim (PNG):", 'codepoint': "Karakter / kod noktası:", 'img_height': "Yükseklik (× büyük harf):",
        'img_drop': "Taban çizgisi altı (× büyük harf):", 'img_bearing': "Yan boşluk (× büyük harf):",
        'invert': "Beyaz zemin üzerinde siyah şekil", 'busy': "Çalışıyor…", 'ready': "Hazır",
        'unsaved': "Kaydedilmemiş değişiklikler var. Devam edilsin mi?", 'saved': "Kaydedildi ve doğrulandı:",
        'verify_ok': "Doğrulama başarılı.", 'verify_bad': "Doğrulama sorun buldu (Kayıt sekmesine bakın).",
        'err': "Hata", 'no_font_sel': "Önce bir font seçin.", 'no_ttf': "Bir TTF/OTF dosyası seçin.",
        'exported': "Fontlar dışa aktarıldı:", 'imported': "İçe aktarılan fontlar:",
        'none_imported': "Bu klasörde eşleşen fontN_<hash>.json yok.", 'role_body': "Metin", 'role_ui': "Arayüz",
        'role_title': "Başlık", 'role_menu': "Ana menü (büyük harf)", 'role_cjk': "CJK metin",
        'role_icons': "Buton ikonları", 'strings_loaded': "metin yüklendi",
        'not_editable': "Bu font (buton ikonları) sıkıştırılmış dokudadır, düzenlenemez.",
        'confirm_delete': "Bu glif fonttan silinsin mi?", 'undo_empty': "Geri alınacak işlem yok.",
        'howto_text': (
            "1. gapack_misc_l.psarc'ı got_psarc_tool ile açın ve lang_<dil>_text.xpps dosyasını açın.\n"
            "2. Önizleme: metin yazın ya da xpps_tool strings.json dosyanızı yükleyip bir satıra tıklayın; oyun fontuyla görünür.\n"
            "3. Eksik karakterler: çevirinizi yapıştırın/yükleyin, Kontrol et'e basın; her fontta eksik harfler listelenir.\n"
            "4. TTF/OTF'den glif ekle: bir font dosyası seçin (örn. C:\\Windows\\Fonts); boyut ve taban çizgisi otomatik eşlenir.\n"
            "5. Fontu değiştir: bir fontun tamamını başka bir TTF/OTF'den yeniden oluşturun (örn. başlık fontu).\n"
            "6. Glifler sekmesi: herhangi bir glifi inceleyin, aralığını (ilerleme / kaymalar) düzeltin veya silin.\n"
            "7. Resimden glif: bir logo/ikonu herhangi bir karaktere (örn. ★ U+2605) koyup metinde kullanın.\n"
            "8. Farklı kaydet… .xpps'i yazar ve doğrular; arşivi got_psarc_tool ile paketleyin.\n\n"
            "Değişiklikler kaydedene kadar bellekte durur; Geri al ve Geri dön kullanılabilir."),
    },
}

SAMPLES = [
    ("Türkçe", "Pijamalı hasta yağız şoföre çabucak güvendi.\nPİJAMALI HASTA YAĞIZ ŞOFÖRE ÇABUCAK GÜVENDİ."),
    ("English", "The quick brown fox jumps over the lazy dog.\nTHE QUICK BROWN FOX 0123456789 !?%&"),
    ("Deutsch", "Falsches Üben von Xylophonmusik quält jeden größeren Zwerg."),
    ("Français", "Portez ce vieux whisky au juge blond qui fume. Œuvre, déjà, çà."),
    ("Español", "El veloz murciélago hindú comía feliz cardillo y kiwi. ¿Sí? ¡Ñandú!"),
    ("Português", "À noite, vovô Kowalsky vê o ímã cair no pé do pinguim queixoso."),
    ("Italiano", "Quel vituperabile xenofobo zelante assaggia il whisky ed esclama: alleluja!"),
    ("Polski", "Zażółć gęślą jaźń. Pchnąć w tę łódź jeża lub ośm skrzyń fig."),
    ("Čeština", "Příliš žluťoučký kůň úpěl ďábelské ódy."),
    ("Magyar", "Árvíztűrő tükörfúrógép."),
    ("Română", "Înjurând pițigăiat, zoofobul comandă vexat whisky și tequila."),
    ("Русский", "Съешь же ещё этих мягких французских булок, да выпей чаю."),
    ("Українська", "Чуєш їх, доцю, га? Кумедна ж ти, прощайся без ґольфів!"),
    ("Ελληνικά", "Ξεσκεπάζω την ψυχοφθόρα βδελυγμία."),
    ("Tiếng Việt", "Tiếng Việt có dấu: ă â đ ê ô ơ ư ạ ả ấ ầ ẩ ẫ ậ."),
    ("Nordisk", "Høj bly gom vandt fræk sexquiz på wc. Åsa, Ödön, Æble."),
    ("Ghost of Tsushima", "Jin Sakai — Lord Shimura, Yuna, Khotun Khan. Tsushima, 1274."),
]

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _TK_OK = True
except Exception:          # pragma: no cover
    tk = None
    _TK_OK = False


def _hide_console_if_owned():
    """When the .exe is double-clicked, Windows opens a console just for it - hide that one."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        arr = (ctypes.c_uint * 4)()
        if k32.GetConsoleProcessList(arr, 4) <= 1:
            hwnd = k32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


class _QueueWriter:
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(('log', s))

    def flush(self):
        pass


def run_gui(path=None):
    if not _TK_OK:
        print("tkinter is not available - use the command line (got_font_tool --help)")
        return
    _hide_console_if_owned()
    app = FontToolApp(path)
    app.mainloop()


if _TK_OK:
    class FontToolApp(tk.Tk):
        def __init__(self, path=None):
            super().__init__()
            import queue
            self.lang = 'en'            # Language menu switches to Turkish
            self.q = queue.Queue()
            self.pack = None
            self.path = None
            self.dirty = False
            self.busy = False
            self.undo_stack = []
            self.strings = {}
            self.atlas_cache = {}
            self._render_job = None
            self._imgs = {}
            self.geometry("1280x820")
            self.minsize(980, 640)
            try:
                self.state('zoomed')     # maximised on Windows
            except tk.TclError:
                pass
            try:
                ttk.Style().theme_use('vista' if os.name == 'nt' else 'clam')
            except Exception:
                pass
            self._build()
            self.after(100, self._poll)
            self.protocol("WM_DELETE_WINDOW", self._quit)
            if path:
                self.after(50, lambda: self.open_file(path))

        # ------------------------------------------------------------------ helpers
        def T(self, k):
            return I18N[self.lang].get(k, I18N['en'].get(k, k))

        def role(self, f):
            return self.T('role_' + font_role(f))

        def cur_font(self):
            sel = self.tree.selection() if self.pack else ()
            if not sel:
                return None
            return self.pack.fonts[int(sel[0])]

        def atlas(self, f):
            key = (f.index, id(f.new_texture))
            if key not in self.atlas_cache:
                self.atlas_cache = {k: v for k, v in self.atlas_cache.items() if k[0] != f.index}
                self.atlas_cache[key] = f.read_texture()
            return self.atlas_cache[key]

        def photo(self, key, pil_img):
            from PIL import ImageTk
            ph = ImageTk.PhotoImage(pil_img)
            self._imgs[key] = ph
            return ph

        # ------------------------------------------------------------------ layout
        def _build(self):
            for w in self.winfo_children():
                w.destroy()
            self.title(self.T('title') + (f" - {os.path.basename(self.path)}" if self.path else '') +
                       (' *' if self.dirty else ''))
            self._build_menu()
            bar = ttk.Frame(self, padding=(6, 4))
            bar.pack(fill='x')
            for key, cmd in (('open', self.ask_open), ('save_as', self.save_as), ('undo', self.undo),
                             ('add', self.dlg_add), ('replace', self.dlg_replace), ('addimg', self.dlg_image),
                             ('export', self.export), ('import', self.import_dir), ('verify', self.verify_now)):
                ttk.Button(bar, text=self.T(key).rstrip('…'), command=cmd).pack(side='left', padx=2)

            main = ttk.PanedWindow(self, orient='horizontal')
            main.pack(fill='both', expand=True, padx=6, pady=(0, 4))
            left = ttk.Frame(main, padding=4)
            main.add(left, weight=1)
            ttk.Label(left, text=self.T('fonts'), font=('Segoe UI', 10, 'bold')).pack(anchor='w')
            cols = ('role', 'size', 'glyphs', 'atlas')
            self.tree = ttk.Treeview(left, columns=cols, show='tree headings', height=10, selectmode='browse')
            self.tree.heading('#0', text='#')
            self.tree.column('#0', width=40, stretch=False)
            for c, k, w in (('role', 'col_role', 150), ('size', 'col_size', 55), ('glyphs', 'col_glyphs', 55),
                            ('atlas', 'col_atlas', 90)):
                self.tree.heading(c, text=self.T(k))
                self.tree.column(c, width=w, anchor='w' if c == 'role' else 'center')
            self.tree.pack(fill='x')
            self.tree.bind('<<TreeviewSelect>>', lambda e: self.on_font_select())
            self.info = ttk.Label(left, text=self.T('no_file'), wraplength=380, justify='left')
            self.info.pack(fill='x', pady=6)

            right = ttk.Frame(main)
            main.add(right, weight=4)
            self.nb = ttk.Notebook(right)
            self.nb.pack(fill='both', expand=True)
            self._build_preview_tab()
            self._build_glyph_tab()
            self._build_atlas_tab()
            self._build_cover_tab()
            self._build_log_tab()
            self.nb.bind('<<NotebookTabChanged>>', lambda e: self.refresh_tab())

            sb = ttk.Frame(self, padding=(6, 2))
            sb.pack(fill='x')
            self.status = ttk.Label(sb, text=self.T('ready'))
            self.status.pack(side='left')
            self.prog = ttk.Progressbar(sb, mode='indeterminate', length=160)
            self.prog.pack(side='right')
            self.fill_fonts()

        def _build_menu(self):
            m = tk.Menu(self)
            fm = tk.Menu(m, tearoff=0)
            fm.add_command(label=self.T('open'), command=self.ask_open, accelerator='Ctrl+O')
            fm.add_command(label=self.T('save'), command=self.save, accelerator='Ctrl+S')
            fm.add_command(label=self.T('save_as'), command=self.save_as)
            fm.add_separator()
            fm.add_command(label=self.T('undo'), command=self.undo, accelerator='Ctrl+Z')
            fm.add_command(label=self.T('revert'), command=self.revert)
            fm.add_separator()
            fm.add_command(label=self.T('export'), command=self.export)
            fm.add_command(label=self.T('import'), command=self.import_dir)
            fm.add_separator()
            fm.add_command(label=self.T('exit'), command=self._quit)
            m.add_cascade(label=self.T('file'), menu=fm)
            om = tk.Menu(m, tearoff=0)
            om.add_command(label=self.T('add'), command=self.dlg_add)
            om.add_command(label=self.T('replace'), command=self.dlg_replace)
            om.add_command(label=self.T('addimg'), command=self.dlg_image)
            om.add_separator()
            om.add_command(label=self.T('verify'), command=self.verify_now)
            m.add_cascade(label=self.T('fonts_menu'), menu=om)
            lm = tk.Menu(m, tearoff=0)
            lm.add_command(label="English", command=lambda: self.set_lang('en'))
            lm.add_command(label="Türkçe", command=lambda: self.set_lang('tr'))
            m.add_cascade(label=self.T('language'), menu=lm)
            hm = tk.Menu(m, tearoff=0)
            hm.add_command(label=self.T('howto'), command=lambda: messagebox.showinfo(self.T('howto'), self.T('howto_text')))
            hm.add_command(label=self.T('about'), command=lambda: messagebox.showinfo(
                self.T('about'), f"got_font_tool v{VERSION}\nGhost of Tsushima DIRECTOR'S CUT SDF font editor\n"
                                 "github.com/mstww/Ghost-Of-Tsushima-xpps-Localization-tool"))
            m.add_cascade(label=self.T('help'), menu=hm)
            self.config(menu=m)
            self.bind_all('<Control-o>', lambda e: self.ask_open())
            self.bind_all('<Control-s>', lambda e: self.save())
            self.bind_all('<Control-z>', lambda e: None if isinstance(self.focus_get(), tk.Text) else self.undo())

        def _scroll_canvas(self, parent):
            fr = ttk.Frame(parent)
            cv = tk.Canvas(fr, background='#2b2b2b', highlightthickness=0)
            xs = ttk.Scrollbar(fr, orient='horizontal', command=cv.xview)
            ys = ttk.Scrollbar(fr, orient='vertical', command=cv.yview)
            cv.configure(xscrollcommand=xs.set, yscrollcommand=ys.set)
            cv.grid(row=0, column=0, sticky='nsew')
            ys.grid(row=0, column=1, sticky='ns')
            xs.grid(row=1, column=0, sticky='ew')
            fr.rowconfigure(0, weight=1)
            fr.columnconfigure(0, weight=1)
            cv.bind('<MouseWheel>', lambda e: cv.yview_scroll(int(-e.delta / 120), 'units'))
            return fr, cv

        def _build_preview_tab(self):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=self.T('tab_preview'))
            top = ttk.Frame(tab)
            top.pack(fill='x')
            ttk.Label(top, text=self.T('sample')).pack(side='left')
            self.sample_cb = ttk.Combobox(top, values=[s[0] for s in SAMPLES], state='readonly', width=18)
            self.sample_cb.pack(side='left', padx=4)
            self.sample_cb.bind('<<ComboboxSelected>>', lambda e: self.set_text(SAMPLES[self.sample_cb.current()][1]))
            ttk.Button(top, text=self.T('load_json'), command=self.load_strings).pack(side='left', padx=8)
            ttk.Label(top, text=self.T('search')).pack(side='left')
            self.str_search = tk.StringVar()
            e = ttk.Entry(top, textvariable=self.str_search, width=28)
            e.pack(side='left', padx=4)
            self.str_search.trace_add('write', lambda *a: self.filter_strings())
            self.str_count = ttk.Label(top, text='')
            self.str_count.pack(side='left', padx=6)

            body = ttk.PanedWindow(tab, orient='vertical')
            body.pack(fill='both', expand=True, pady=4)
            upper = ttk.Frame(body)
            body.add(upper, weight=1)
            self.str_list = tk.Listbox(upper, height=5, font=('Segoe UI', 9), activestyle='none')
            self.str_list.pack(side='left', fill='both', expand=True)
            self.str_list.bind('<<ListboxSelect>>', lambda e: self.pick_string())
            sl = ttk.Scrollbar(upper, command=self.str_list.yview)
            sl.pack(side='left', fill='y')
            self.str_list.configure(yscrollcommand=sl.set)
            self.text = tk.Text(upper, height=5, width=50, font=('Segoe UI', 11), wrap='word', undo=True)
            self.text.pack(side='left', fill='both', expand=True, padx=(6, 0))
            self.text.insert('1.0', SAMPLES[0][1] if self.lang == 'tr' else SAMPLES[1][1])
            self.text.bind('<KeyRelease>', lambda e: self.schedule_preview())

            opts = ttk.Frame(tab)
            opts.pack(fill='x')
            ttk.Label(opts, text=self.T('height')).pack(side='left')
            self.pv_height = tk.IntVar(value=64)
            ttk.Spinbox(opts, from_=16, to=240, increment=8, textvariable=self.pv_height, width=5,
                        command=self.schedule_preview).pack(side='left', padx=4)
            self.pv_dark = tk.BooleanVar(value=True)
            self.pv_upper = tk.BooleanVar(value=False)
            self.pv_boxes = tk.BooleanVar(value=False)
            self.pv_all = tk.BooleanVar(value=False)
            for var, k in ((self.pv_dark, 'dark'), (self.pv_upper, 'upper'), (self.pv_boxes, 'boxes'),
                           (self.pv_all, 'all_fonts')):
                ttk.Checkbutton(opts, text=self.T(k), variable=var, command=self.schedule_preview).pack(side='left', padx=6)
            self.pv_missing = ttk.Label(tab, text='', foreground='#c0392b', wraplength=900, justify='left')
            self.pv_missing.pack(fill='x')
            fr, self.pv_canvas = self._scroll_canvas(body)
            body.add(fr, weight=4)

        def _build_glyph_tab(self):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=self.T('tab_glyphs'))
            top = ttk.Frame(tab)
            top.pack(fill='x')
            ttk.Label(top, text=self.T('search')).pack(side='left')
            self.g_search = tk.StringVar()
            ttk.Entry(top, textvariable=self.g_search, width=24).pack(side='left', padx=4)
            self.g_search.trace_add('write', lambda *a: (setattr(self, 'g_page', 0), self.draw_glyph_page()))
            ttk.Button(top, text=self.T('prev'), width=3, command=lambda: self.glyph_page(-1)).pack(side='left', padx=(12, 2))
            self.g_page_lbl = ttk.Label(top, text='')
            self.g_page_lbl.pack(side='left')
            ttk.Button(top, text=self.T('next'), width=3, command=lambda: self.glyph_page(1)).pack(side='left', padx=2)
            self.g_page = 0
            self.g_sel = None
            pw = ttk.PanedWindow(tab, orient='horizontal')
            pw.pack(fill='both', expand=True, pady=4)
            fr, self.g_canvas = self._scroll_canvas(pw)
            pw.add(fr, weight=3)
            self.g_canvas.bind('<Button-1>', self.on_glyph_click)
            insp = ttk.Frame(pw, padding=6)
            pw.add(insp, weight=2)
            self.g_big = tk.Label(insp, background='#202020')
            self.g_big.pack(fill='x')
            self.g_info = ttk.Label(insp, text='', justify='left', font=('Consolas', 9))
            self.g_info.pack(fill='x', pady=4)
            form = ttk.Frame(insp)
            form.pack(fill='x')
            self.g_adv, self.g_xoff, self.g_yoff = tk.IntVar(), tk.IntVar(), tk.IntVar()
            for r, (k, v) in enumerate((('advance', self.g_adv), ('xoff', self.g_xoff), ('yoff', self.g_yoff))):
                ttk.Label(form, text=self.T(k)).grid(row=r, column=0, sticky='w')
                ttk.Spinbox(form, from_=-4000, to=8000, textvariable=v, width=8).grid(row=r, column=1, sticky='w', pady=1)
            btns = ttk.Frame(insp)
            btns.pack(fill='x', pady=4)
            ttk.Button(btns, text=self.T('apply'), command=self.apply_metrics).pack(side='left')
            ttk.Button(btns, text=self.T('delete'), command=self.delete_glyph).pack(side='left', padx=6)
            self.g_ctx = tk.Label(insp, background='#202020')
            self.g_ctx.pack(fill='x', pady=4)
            ttk.Label(insp, text=self.T('kerning')).pack(anchor='w')
            self.g_kern = tk.Listbox(insp, height=8, font=('Consolas', 9))
            self.g_kern.pack(fill='both', expand=True)

        def _build_atlas_tab(self):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=self.T('tab_atlas'))
            top = ttk.Frame(tab)
            top.pack(fill='x')
            ttk.Label(top, text=self.T('zoom')).pack(side='left')
            self.a_zoom = ttk.Combobox(top, values=[self.T('fit'), '25%', '50%', '100%'], state='readonly', width=8)
            self.a_zoom.current(0)
            self.a_zoom.pack(side='left', padx=4)
            self.a_zoom.bind('<<ComboboxSelected>>', lambda e: self.draw_atlas())
            self.a_boxes = tk.BooleanVar(value=False)
            ttk.Checkbutton(top, text=self.T('boxes'), variable=self.a_boxes, command=self.draw_atlas).pack(side='left', padx=8)
            self.a_info = ttk.Label(top, text='')
            self.a_info.pack(side='left', padx=8)
            fr, self.a_canvas = self._scroll_canvas(tab)
            fr.pack(fill='both', expand=True, pady=4)
            self.a_canvas.bind('<Button-1>', self.on_atlas_click)
            self.a_canvas.bind('<Configure>', lambda e: self.a_zoom.current() == 0 and self._atlas_resize())

        def _build_cover_tab(self):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=self.T('tab_cover'))
            ttk.Label(tab, text=self.T('cover_hint')).pack(anchor='w')
            top = ttk.Frame(tab)
            top.pack(fill='x', pady=4)
            ttk.Button(top, text=self.T('load_json'), command=lambda: self.cover_load('json')).pack(side='left')
            ttk.Button(top, text=self.T('load_txt'), command=lambda: self.cover_load('txt')).pack(side='left', padx=4)
            ttk.Button(top, text=self.T('check'), command=self.cover_check).pack(side='left', padx=12)
            ttk.Button(top, text=self.T('add_missing'), command=self.cover_add).pack(side='left')
            self.c_text = tk.Text(tab, height=8, font=('Segoe UI', 10), wrap='word')
            self.c_text.pack(fill='x')
            cols = ('role', 'missing', 'chars')
            self.c_tree = ttk.Treeview(tab, columns=cols, show='tree headings', height=8)
            self.c_tree.heading('#0', text=self.T('col_font'))
            self.c_tree.column('#0', width=60, stretch=False)
            for c, k, w in (('role', 'col_role', 160), ('missing', 'col_missing', 70), ('chars', 'col_chars', 700)):
                self.c_tree.heading(c, text=self.T(k))
                self.c_tree.column(c, width=w, anchor='w')
            self.c_tree.pack(fill='both', expand=True, pady=4)
            self.c_missing = {}

        def _build_log_tab(self):
            tab = ttk.Frame(self.nb, padding=6)
            self.nb.add(tab, text=self.T('tab_log'))
            self.log = tk.Text(tab, font=('Consolas', 9), wrap='none', state='disabled', background='#1e1e1e',
                               foreground='#d4d4d4')
            ys = ttk.Scrollbar(tab, command=self.log.yview)
            self.log.configure(yscrollcommand=ys.set)
            self.log.pack(side='left', fill='both', expand=True)
            ys.pack(side='left', fill='y')

        def set_lang(self, lang):
            text = self.text.get('1.0', 'end-1c') if hasattr(self, 'text') else ''
            self.lang = lang
            self._build()
            self.set_text(text)
            self.filter_strings()

        # ------------------------------------------------------------------ file handling
        def _quit(self):
            if self.dirty and not messagebox.askyesno(self.T('title'), self.T('unsaved')):
                return
            self.destroy()

        def ask_open(self):
            if self.busy:
                return
            if self.dirty and not messagebox.askyesno(self.T('title'), self.T('unsaved')):
                return
            p = filedialog.askopenfilename(filetypes=[("Ghost of Tsushima xpps", "*.xpps"), ("*", "*.*")])
            if p:
                self.open_file(p)

        def open_file(self, p):
            try:
                pack = Pack(p)
            except Exception as ex:
                messagebox.showerror(self.T('err'), str(ex))
                return
            if not pack.fonts:
                messagebox.showerror(self.T('err'), "No fonts in this file (fonts are in lang_*_text.xpps).")
                return
            self.pack, self.path, self.dirty = pack, p, False
            self.undo_stack.clear()
            self.atlas_cache.clear()
            self.logw(f"[open] {p}\n")
            self.update_title()
            self.fill_fonts()

        def update_title(self):
            self.title(self.T('title') + (f" - {os.path.basename(self.path)}" if self.path else '') +
                       (' *' if self.dirty else ''))

        def fill_fonts(self, keep_sel=True):
            if not hasattr(self, 'tree'):
                return
            prev = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            if not self.pack:
                return
            for f in self.pack.fonts:
                self.tree.insert('', 'end', iid=str(f.index), text=str(f.index),
                                 values=(self.role(f), f.size, f.count, f"{f.tex_w}×{f.tex_h}"))
            sel = prev[0] if keep_sel and prev and self.tree.exists(prev[0]) else str(
                next((f.index for f in self.pack.fonts if font_role(f) == 'body'), 0))
            self.tree.selection_set(sel)
            self.on_font_select()

        def on_font_select(self):
            f = self.cur_font()
            if not f:
                return
            cps = [g.cp for g in f.glyphs]
            txt = (f"#{f.index}  {self.role(f)}\nhash {f.hash}\n{self.T('col_size')} {f.size}, spread {f.spread}\n"
                   f"virtual {f.vw}×{f.vh}, texture {f.tex_w}×{f.tex_h}\n{self.T('col_glyphs')}: {f.count} "
                   f"(U+{min(cps):04X}–U+{max(cps):04X}), kerning: {sum(len(g.kern) for g in f.glyphs)}")
            if not f.editable:
                txt += "\n\n" + self.T('not_editable')
            self.info.configure(text=txt)
            self.g_page, self.g_sel = 0, None
            self.refresh_tab()

        def refresh_tab(self):
            if not self.pack:
                return
            try:
                i = self.nb.index(self.nb.select())
            except Exception:
                return
            if i == 0:
                self.render_preview()
            elif i == 1:
                self.draw_glyph_page()
            elif i == 2:
                self.draw_atlas()

        def snapshot(self):
            import copy
            self.undo_stack.append(copy.deepcopy(self.pack))
            del self.undo_stack[:-4]

        def undo(self):
            if self.busy:
                return
            if not self.undo_stack:
                self.status.configure(text=self.T('undo_empty'))
                return
            self.pack = self.undo_stack.pop()
            self.dirty = True
            self.atlas_cache.clear()
            self.logw("[undo]\n")
            self.update_title()
            self.fill_fonts()

        def revert(self):
            if self.path and not self.busy:
                self.open_file(self.path)

        def save(self):
            if self.path:
                self._save_to(self.path)

        def save_as(self):
            if not self.pack or self.busy:
                return
            p = filedialog.asksaveasfilename(defaultextension='.xpps', initialfile=os.path.basename(self.path),
                                             initialdir=os.path.dirname(self.path),
                                             filetypes=[("Ghost of Tsushima xpps", "*.xpps")])
            if p:
                self._save_to(p)

        def _save_to(self, p):
            if not self.pack or self.busy:
                return

            def job():
                self.pack.save(p)
                ok = verify(p)
                return ok, p
            self.run_bg(job, self._saved)

        def _saved(self, res):
            ok, p = res
            if ok:
                self.path, self.dirty = p, False
                self.pack = Pack(p)
                self.atlas_cache.clear()
                self.update_title()
                self.fill_fonts()
                messagebox.showinfo(self.T('title'), f"{self.T('saved')}\n{p}")
            else:
                messagebox.showerror(self.T('err'), self.T('verify_bad'))

        def verify_now(self):
            if not self.pack or self.busy:
                return
            import tempfile

            def job():
                tmp = os.path.join(tempfile.gettempdir(), 'got_font_tool_verify.xpps')
                self.pack.save(tmp)
                ok = verify(tmp)
                os.remove(tmp)
                return ok
            self.run_bg(job, lambda ok: messagebox.showinfo(self.T('verify'), self.T('verify_ok')) if ok
                        else messagebox.showerror(self.T('verify'), self.T('verify_bad')))

        def export(self):
            if not self.pack:
                return
            d = filedialog.askdirectory(initialdir=os.path.dirname(self.path))
            if not d:
                return

            def job():
                for f in self.pack.fonts:
                    export_font(f, d)
                    print(f"[+] font {f.index} -> {font_basename(f)}.json/.png")
                return d
            self.run_bg(job, lambda r: messagebox.showinfo(self.T('export'), f"{self.T('exported')}\n{r}"))

        def import_dir(self):
            if not self.pack or self.busy:
                return
            d = filedialog.askdirectory(initialdir=os.path.dirname(self.path))
            if not d:
                return
            self.snapshot()

            def job():
                done = []
                for f in self.pack.fonts:
                    j = os.path.join(d, font_basename(f) + '.json')
                    if os.path.isfile(j):
                        print(f"[+] font {f.index}: " + import_font(self.pack, f, j, os.path.join(d, font_basename(f) + '.png')))
                        done.append(f.index)
                return done

            def after(done):
                if not done:
                    self.undo_stack.pop()
                    messagebox.showwarning(self.T('import'), self.T('none_imported'))
                else:
                    self.changed()
                    messagebox.showinfo(self.T('import'), f"{self.T('imported')} {done}")
            self.run_bg(job, after)

        # ------------------------------------------------------------------ background jobs
        def run_bg(self, fn, done=None):
            import threading
            if self.busy:
                return
            self.busy = True
            self.status.configure(text=self.T('busy'))
            self.prog.start(12)
            self.configure(cursor='watch')

            def worker():
                old = sys.stdout
                sys.stdout = _QueueWriter(self.q)
                try:
                    res = fn()
                    self.q.put(('done', (done, res)))
                except Exception as ex:
                    import traceback
                    self.q.put(('log', traceback.format_exc()))
                    self.q.put(('error', str(ex)))
                finally:
                    sys.stdout = old
            threading.Thread(target=worker, daemon=True).start()

        def _poll(self):
            try:
                while True:
                    kind, val = self.q.get_nowait()
                    if kind == 'log':
                        self.logw(val)
                    elif kind in ('done', 'error'):
                        self.busy = False
                        self.prog.stop()
                        self.configure(cursor='')
                        self.status.configure(text=self.T('ready'))
                        if kind == 'error':
                            if self.undo_stack and getattr(self, '_pending_undo', False):
                                self.pack = self.undo_stack.pop()
                            messagebox.showerror(self.T('err'), val)
                        else:
                            cb, res = val
                            if cb:
                                cb(res)
                        self._pending_undo = False
            except Exception:
                pass
            self.after(100, self._poll)

        def logw(self, s):
            self.log.configure(state='normal')
            self.log.insert('end', s)
            self.log.see('end')
            self.log.configure(state='disabled')
            last = s.strip().splitlines()[-1] if s.strip() else ''
            if last:
                self.status.configure(text=last[:140])

        def changed(self):
            self.dirty = True
            self.atlas_cache.clear()
            self.update_title()
            self.fill_fonts()
            if self.c_missing:
                self.cover_check()

        def run_op(self, fn):
            """Run an editing operation in the background with an undo snapshot."""
            if not self.pack or self.busy:
                return
            self.snapshot()
            self._pending_undo = True
            self.run_bg(fn, lambda r: self.changed())

        # ------------------------------------------------------------------ preview tab
        def set_text(self, s):
            self.text.delete('1.0', 'end')
            self.text.insert('1.0', s)
            self.schedule_preview()

        def schedule_preview(self):
            if self._render_job:
                self.after_cancel(self._render_job)
            self._render_job = self.after(200, self.render_preview)

        def render_preview(self):
            self._render_job = None
            if not self.pack or self.busy:
                return
            from PIL import Image, ImageDraw
            txt = self.text.get('1.0', 'end-1c')
            h = max(8, min(400, int(self.pv_height.get() or 64)))
            dark = self.pv_dark.get()
            fg, bg = ((235, 230, 220), (32, 36, 38)) if dark else ((0, 0, 0), (255, 255, 255))
            fonts = [f for f in self.pack.fonts if f.editable] if self.pv_all.get() else [self.cur_font()]
            fonts = [f for f in fonts if f and f.editable]
            if not fonts:
                self.pv_canvas.delete('all')
                self.pv_canvas.create_text(20, 20, anchor='nw', fill='#dddddd', text=self.T('not_editable'))
                self.pv_missing.configure(text='')
                return
            cps = chars_to_cps(txt.upper() if self.pv_upper.get() else txt)
            imgs, miss_lines = [], []
            for f in fonts:
                im = text_image(f, txt, h, fg, bg, uppercase=self.pv_upper.get(), atlas=self.atlas(f),
                                boxes=self.pv_boxes.get())
                if self.pv_all.get():
                    lab = Image.new('RGB', (im.width, 18), bg)
                    ImageDraw.Draw(lab).text((6, 3), f"#{f.index} {self.role(f)} ({f.size})", fill=(140, 150, 160))
                    both = Image.new('RGB', (im.width, im.height + 18), bg)
                    both.paste(lab, (0, 0))
                    both.paste(im, (0, 18))
                    im = both
                imgs.append(im)
                m = missing_cps(f, cps) if f.editable else []
                if m:
                    miss_lines.append(f"#{f.index} {self.role(f)}: " + ' '.join(chr(c) for c in m))
            if not imgs:
                return
            W = max(i.width for i in imgs)
            out = Image.new('RGB', (W, sum(i.height for i in imgs)), bg)
            y = 0
            for i in imgs:
                out.paste(i, (0, y))
                y += i.height
            ph = self.photo('pv', out)
            self.pv_canvas.delete('all')
            self.pv_canvas.configure(background='#%02x%02x%02x' % bg)
            self.pv_canvas.create_image(0, 0, image=ph, anchor='nw')
            self.pv_canvas.configure(scrollregion=(0, 0, out.width, out.height))
            self.pv_missing.configure(text=(self.T('missing_in_text') + ' ' + ('  |  '.join(miss_lines)))
                                      if miss_lines else '')

        def load_strings(self):
            p = filedialog.askopenfilename(filetypes=[("strings.json", "*.json"), ("*", "*.*")])
            if not p:
                return
            try:
                with open(p, 'r', encoding='utf-8-sig') as fp:
                    data = json.load(fp)
            except Exception as ex:
                messagebox.showerror(self.T('err'), str(ex))
                return
            if isinstance(data, list):
                data = {str(i): v for i, v in enumerate(data)}
            self.strings = {k: ('\n'.join(v) if isinstance(v, list) else str(v)) for k, v in data.items()}
            self.logw(f"[strings] {p}: {len(self.strings)}\n")
            self.filter_strings()

        def filter_strings(self):
            if not hasattr(self, 'str_list'):
                return
            q = self.str_search.get().lower().strip()
            self.str_list.delete(0, 'end')
            self._str_keys = []
            n = 0
            for k, v in self.strings.items():
                if q and q not in v.lower() and q not in k.lower():
                    continue
                n += 1
                if len(self._str_keys) < 2000:
                    self._str_keys.append(k)
                    self.str_list.insert('end', v.replace('\n', ' ⏎ ')[:200])
            self.str_count.configure(text=f"{n} / {len(self.strings)} {self.T('strings_loaded')}" if self.strings else '')

        def pick_string(self):
            sel = self.str_list.curselection()
            if sel:
                self.set_text(self.strings[self._str_keys[sel[0]]])

        # ------------------------------------------------------------------ glyph tab
        COLS, ROWS, CELL = 12, 7, 64

        def glyph_list(self):
            f = self.cur_font()
            if not f:
                return []
            q = self.g_search.get().strip()
            if not q:
                return f.glyphs
            want = set()
            for tok in q.replace(',', ' ').split():
                if tok[:2].upper() == 'U+':
                    try:
                        want.add(int(tok[2:], 16))
                        continue
                    except ValueError:
                        pass
                want |= {ord(c) for c in tok}
            return [g for g in f.glyphs if g.cp in want]

        def glyph_page(self, d):
            n = max(1, math.ceil(len(self.glyph_list()) / (self.COLS * self.ROWS)))
            self.g_page = max(0, min(n - 1, self.g_page + d))
            self.draw_glyph_page()

        def draw_glyph_page(self):
            import numpy as np
            from PIL import Image, ImageDraw
            f = self.cur_font()
            if not f or self.busy:
                return
            gl = self.glyph_list()
            per = self.COLS * self.ROWS
            pages = max(1, math.ceil(len(gl) / per))
            self.g_page = min(self.g_page, pages - 1)
            self.g_page_lbl.configure(text=f"{self.T('page')} {self.g_page + 1}/{pages}  ({len(gl)})")
            items = gl[self.g_page * per:(self.g_page + 1) * per]
            C, lab = self.CELL, 14
            img = Image.new('RGB', (self.COLS * C, self.ROWS * (C + lab)), (32, 32, 32))
            dr = ImageDraw.Draw(img)
            if f.editable:
                atlas = self.atlas(f)
            for i, g in enumerate(items):
                cx, cy = (i % self.COLS) * C, (i // self.COLS) * (C + lab)
                sel = self.g_sel is not None and g.cp == self.g_sel
                dr.rectangle((cx, cy, cx + C - 1, cy + C + lab - 1), fill=(70, 90, 130) if sel else (44, 44, 44),
                             outline=(60, 60, 60))
                if f.editable:
                    a = glyph_thumb(f, atlas, g, C - 6)
                    tile = Image.fromarray((a * 235).astype(np.uint8), 'L')
                    img.paste((235, 232, 220), (cx + 3, cy + 3, cx + C - 3, cy + C - 3), tile)
                dr.text((cx + 3, cy + C), f"{g.cp:04X}", fill=(150, 150, 150))
            self._g_items = items
            ph = self.photo('grid', img)
            self.g_canvas.delete('all')
            self.g_canvas.create_image(0, 0, image=ph, anchor='nw')
            self.g_canvas.configure(scrollregion=(0, 0, img.width, img.height))
            self.show_glyph()

        def on_glyph_click(self, e):
            x, y = self.g_canvas.canvasx(e.x), self.g_canvas.canvasy(e.y)
            i = int(y // (self.CELL + 14)) * self.COLS + int(x // self.CELL)
            if 0 <= x < self.COLS * self.CELL and 0 <= i < len(getattr(self, '_g_items', [])):
                self.g_sel = self._g_items[i].cp
                self.draw_glyph_page()

        def show_glyph(self):
            import numpy as np
            import unicodedata
            from PIL import Image
            f = self.cur_font()
            g = next((x for x in f.glyphs if x.cp == self.g_sel), None) if f and self.g_sel is not None else None
            if not g:
                self.g_big.configure(image='')
                self.g_ctx.configure(image='')
                self.g_info.configure(text='')
                self.g_kern.delete(0, 'end')
                return
            try:
                name = unicodedata.name(chr(g.cp))
            except ValueError:
                name = '-'
            self.g_info.configure(text=(f"U+{g.cp:04X}  {glyph_char(g.cp)}  {name}\n"
                                        f"{self.T('box')}: x {g.x}  y {g.y}  w {g.w}  h {g.h}\n"
                                        f"xoff {g.xoff}  yoff {g.yoff}  adv {g.adv}"))
            self.g_adv.set(g.adv)
            self.g_xoff.set(g.xoff)
            self.g_yoff.set(g.yoff)
            if f.editable:
                a = glyph_thumb(f, self.atlas(f), g, 200)
                big = Image.fromarray((a * 240).astype(np.uint8), 'L').convert('RGB')
                self.g_big.configure(image=self.photo('big', big))
                c = chr(g.cp)
                ctx = text_image(f, f"H{c}H n{c}n {c}{c}{c}", 48, (235, 230, 220), (32, 32, 32), margin=10,
                                 atlas=self.atlas(f))
                if ctx.width > 460:
                    ctx = ctx.resize((460, int(ctx.height * 460 / ctx.width)))
                self.g_ctx.configure(image=self.photo('ctx', ctx))
            self.g_kern.delete(0, 'end')
            for c2, amt in g.kern[:500]:
                self.g_kern.insert('end', f"{glyph_char(g.cp)}{glyph_char(c2)}   U+{c2:04X}   {amt:+d}")

        def apply_metrics(self):
            f = self.cur_font()
            if not f or self.g_sel is None or not f.editable:
                return
            cp, adv, xo, yo = self.g_sel, self.g_adv.get(), self.g_xoff.get(), self.g_yoff.get()
            self.run_op(lambda: print(f"[~] font {f.index} U+{cp:04X}: "
                                      + op_set_metrics(self.pack, f, cp, adv, xo, yo)))

        def delete_glyph(self):
            f = self.cur_font()
            if not f or self.g_sel is None or not f.editable:
                return
            if not messagebox.askyesno(self.T('delete'), self.T('confirm_delete')):
                return
            cp = self.g_sel
            self.g_sel = None
            self.run_op(lambda: op_remove(self.pack, f, [cp]))

        # ------------------------------------------------------------------ atlas tab
        def _atlas_resize(self):
            if getattr(self, '_atlas_job', None):
                self.after_cancel(self._atlas_job)
            self._atlas_job = self.after(150, self.draw_atlas)

        def draw_atlas(self):
            from PIL import Image, ImageDraw
            self._atlas_job = None
            f = self.cur_font()
            if not f or self.busy:
                return
            if not f.editable:
                self.a_canvas.delete('all')
                self.a_info.configure(text=self.T('not_editable'))
                return
            atlas = self.atlas(f)
            th, tw = atlas.shape
            z = self.a_zoom.current()
            if z == 0:
                cw, ch = max(100, self.a_canvas.winfo_width()), max(100, self.a_canvas.winfo_height())
                s = min(cw / tw, ch / th, 1.0)
            else:
                s = (0.25, 0.5, 1.0)[z - 1]
            img = Image.fromarray(atlas, 'L')
            if s != 1.0:
                img = img.resize((max(1, int(tw * s)), max(1, int(th * s))), Image.BILINEAR)
            img = img.convert('RGB')
            if self.a_boxes.get():
                dr = ImageDraw.Draw(img)
                k = s * tw / f.vw
                for g in f.glyphs:
                    dr.rectangle((g.x * k, g.y * k, (g.x + g.w) * k, (g.y + g.h) * k), outline=(220, 90, 60))
            self._a_scale = s
            ph = self.photo('atlas', img)
            self.a_canvas.delete('all')
            self.a_canvas.create_image(0, 0, image=ph, anchor='nw')
            self.a_canvas.configure(scrollregion=(0, 0, img.width, img.height))
            self.a_info.configure(text=f"texture {tw}×{th}  virtual {f.vw}×{f.vh}  "
                                       f"{self.T('col_glyphs')}: {f.count}")

        def on_atlas_click(self, e):
            f = self.cur_font()
            if not f or not f.editable:
                return
            s = getattr(self, '_a_scale', 1.0)
            vx = self.a_canvas.canvasx(e.x) / s * f.fx
            vy = self.a_canvas.canvasy(e.y) / s * f.fy
            for g in f.glyphs:
                if g.x <= vx < g.x + g.w and g.y <= vy < g.y + g.h:
                    self.g_sel = g.cp
                    self.g_search.set(glyph_char(g.cp) or f"U+{g.cp:04X}")
                    self.nb.select(1)
                    return

        # ------------------------------------------------------------------ coverage tab
        def cover_load(self, kind):
            if kind == 'json':
                p = filedialog.askopenfilename(filetypes=[("strings.json", "*.json")])
                if p:
                    try:
                        t = text_from_json(p)
                    except Exception as ex:
                        messagebox.showerror(self.T('err'), str(ex))
                        return
            else:
                p = filedialog.askopenfilename(filetypes=[("text", "*.txt"), ("*", "*.*")])
                if p:
                    with open(p, 'r', encoding='utf-8-sig', errors='replace') as fp:
                        t = fp.read()
            if p:
                self.c_text.delete('1.0', 'end')
                self.c_text.insert('1.0', t[:200000])
                self._cover_full = t
                self.cover_check()

        def cover_check(self):
            if not self.pack:
                return
            t = self.c_text.get('1.0', 'end-1c')
            full = getattr(self, '_cover_full', '')
            if full and full.startswith(t[:1000]):
                t = full
            cps = chars_to_cps(t)
            upper = chars_to_cps(t.upper())
            self.c_tree.delete(*self.c_tree.get_children())
            self.c_missing = {}
            for f in self.pack.fonts:
                if not f.editable:
                    continue
                m = missing_cps(f, upper if font_role(f) == 'menu' else cps)
                self.c_missing[f.index] = m
                self.c_tree.insert('', 'end', text=str(f.index), values=(
                    self.role(f), len(m), ' '.join(chr(c) for c in m[:400]) or self.T('none')))

        def cover_add(self):
            if not self.c_missing:
                self.cover_check()
            chars = sorted({c for m in self.c_missing.values() for c in m})
            fonts = [i for i, m in self.c_missing.items() if m]
            self.dlg_add(''.join(chr(c) for c in chars), fonts)

        # ------------------------------------------------------------------ dialogs
        def _font_checks(self, parent, preselect):
            fr = ttk.Frame(parent)
            vars_ = {}
            for f in self.pack.fonts:
                if not f.editable:
                    continue
                v = tk.BooleanVar(value=(f.index in preselect) if preselect is not None else True)
                ttk.Checkbutton(fr, text=f"#{f.index} {self.role(f)} ({f.size}, {f.count})", variable=v).pack(anchor='w')
                vars_[f.index] = v
            return fr, vars_

        def _dialog(self, title):
            d = tk.Toplevel(self)
            d.title(title)
            d.transient(self)
            d.grab_set()
            d.resizable(True, False)
            body = ttk.Frame(d, padding=10)
            body.pack(fill='both', expand=True)
            body.columnconfigure(1, weight=1)
            return d, body

        def _file_row(self, body, r, label, var, types, initialdir=None):
            ttk.Label(body, text=label).grid(row=r, column=0, sticky='w', pady=3)
            ttk.Entry(body, textvariable=var, width=60).grid(row=r, column=1, sticky='ew', pady=3)

            def br():
                p = filedialog.askopenfilename(filetypes=types, initialdir=initialdir)
                if p:
                    var.set(p)
            ttk.Button(body, text=self.T('browse'), command=br).grid(row=r, column=2, padx=4)

        def dlg_add(self, chars='', fonts=None, mode='add'):
            if not self.pack or self.busy:
                return
            d, body = self._dialog(self.T('add' if mode == 'add' else 'replace').rstrip('…'))
            ttf = tk.StringVar(value=getattr(self, '_last_ttf', ''))
            fdir = os.path.join(os.environ.get('WINDIR', 'C:\\Windows'), 'Fonts') if os.name == 'nt' else None
            self._file_row(body, 0, self.T('ttf'), ttf, [("Fonts", "*.ttf *.otf *.ttc"), ("*", "*.*")], fdir)
            ttk.Label(body, text=self.T('chars' if mode == 'add' else 'extra_chars')).grid(row=1, column=0, sticky='nw')
            ct = tk.Text(body, height=4, width=60, font=('Segoe UI', 11), wrap='char')
            ct.grid(row=1, column=1, columnspan=2, sticky='ew', pady=3)
            ct.insert('1.0', chars)
            ttk.Label(body, text=self.T('target_fonts')).grid(row=2, column=0, sticky='nw')
            fr, fvars = self._font_checks(body, fonts if fonts is not None else
                                          ([self.cur_font().index] if mode == 'replace' and self.cur_font() else None))
            fr.grid(row=2, column=1, columnspan=2, sticky='w')
            kern = tk.BooleanVar(value=True)
            ttk.Checkbutton(body, text=self.T('kern'), variable=kern).grid(row=3, column=1, sticky='w')
            rep = tk.BooleanVar(value=False)
            if mode == 'add':
                ttk.Checkbutton(body, text=self.T('replace_existing'), variable=rep).grid(row=4, column=1, sticky='w')
            sc, bs = tk.DoubleVar(value=1.0), tk.IntVar(value=0)
            ttk.Label(body, text=self.T('scale')).grid(row=5, column=0, sticky='w')
            ttk.Spinbox(body, from_=0.5, to=2.0, increment=0.05, textvariable=sc, width=8).grid(row=5, column=1, sticky='w')
            ttk.Label(body, text=self.T('baseline')).grid(row=6, column=0, sticky='w')
            ttk.Spinbox(body, from_=-500, to=500, increment=1, textvariable=bs, width=8).grid(row=6, column=1, sticky='w')
            ttk.Label(body, text=self.T('grow')).grid(row=7, column=0, sticky='w')
            grow = ttk.Combobox(body, values=[self.T('grow_rect'), self.T('grow_square')], state='readonly', width=30)
            grow.current(0)
            grow.grid(row=7, column=1, sticky='w')

            def ok():
                if not os.path.isfile(ttf.get()):
                    messagebox.showerror(self.T('err'), self.T('no_ttf'), parent=d)
                    return
                sel = [self.pack.fonts[i] for i, v in fvars.items() if v.get()]
                if not sel:
                    messagebox.showerror(self.T('err'), self.T('no_font_sel'), parent=d)
                    return
                cps = chars_to_cps(ct.get('1.0', 'end-1c'))
                if mode == 'add' and not cps:
                    return
                self._last_ttf = ttf.get()
                g = 'rect' if grow.current() == 0 else 'square'
                args = dict(kern=kern.get(), scale=sc.get(), baseline_shift=bs.get(), grow=g)
                d.destroy()
                if mode == 'add':
                    self.run_op(lambda: op_add(self.pack, sel, self._last_ttf, cps, replace_existing=rep.get(), **args))
                else:
                    self.run_op(lambda: op_replace(self.pack, sel, self._last_ttf, cps, **args))
            bb = ttk.Frame(body)
            bb.grid(row=9, column=0, columnspan=3, sticky='e', pady=(10, 0))
            ttk.Button(bb, text=self.T('ok'), command=ok).pack(side='left', padx=4)
            ttk.Button(bb, text=self.T('cancel'), command=d.destroy).pack(side='left')

        def dlg_replace(self):
            self.dlg_add(mode='replace')

        def dlg_image(self):
            if not self.pack or self.busy:
                return
            d, body = self._dialog(self.T('addimg').rstrip('…'))
            img = tk.StringVar()
            self._file_row(body, 0, self.T('image'), img, [("PNG", "*.png"), ("*", "*.*")])
            cpv = tk.StringVar(value='U+2605')
            ttk.Label(body, text=self.T('codepoint')).grid(row=1, column=0, sticky='w')
            ttk.Entry(body, textvariable=cpv, width=12).grid(row=1, column=1, sticky='w')
            ttk.Label(body, text=self.T('target_fonts')).grid(row=2, column=0, sticky='nw')
            fr, fvars = self._font_checks(body, None)
            fr.grid(row=2, column=1, sticky='w')
            hv, dv, bv = tk.DoubleVar(value=1.2), tk.DoubleVar(value=0.1), tk.DoubleVar(value=0.08)
            for r, (k, v) in enumerate((('img_height', hv), ('img_drop', dv), ('img_bearing', bv)), start=3):
                ttk.Label(body, text=self.T(k)).grid(row=r, column=0, sticky='w')
                ttk.Spinbox(body, from_=-1, to=4, increment=0.05, textvariable=v, width=8).grid(row=r, column=1, sticky='w')
            inv = tk.BooleanVar(value=False)
            ttk.Checkbutton(body, text=self.T('invert'), variable=inv).grid(row=6, column=1, sticky='w')

            def ok():
                if not os.path.isfile(img.get()):
                    return
                try:
                    cp = parse_cp(cpv.get())
                except ValueError as ex:
                    messagebox.showerror(self.T('err'), str(ex), parent=d)
                    return
                sel = [self.pack.fonts[i] for i, v in fvars.items() if v.get()]
                if not sel:
                    return
                path, h, dr, be, iv = img.get(), hv.get(), dv.get(), bv.get(), inv.get()
                d.destroy()
                self.g_sel = cp
                self.run_op(lambda: op_addimg(self.pack, sel, cp, path, h, dr, be, iv))
            bb = ttk.Frame(body)
            bb.grid(row=8, column=0, columnspan=3, sticky='e', pady=(10, 0))
            ttk.Button(bb, text=self.T('ok'), command=ok).pack(side='left', padx=4)
            ttk.Button(bb, text=self.T('cancel'), command=d.destroy).pack(side='left')


if __name__ == '__main__':
    main()
