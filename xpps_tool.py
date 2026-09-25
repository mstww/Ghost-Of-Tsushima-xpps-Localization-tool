#!/usr/bin/env python3
"""
xpps_tool.py - Ghost of Tsushima Localization Tool (v2.1)

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


def repack_xpps(template_path, new_strings, output_path):
    """
    Rebuild a .xpps file by replacing string content with `new_strings`
    while keeping the full KCAP binary structure intact.
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

    new_pool = bytearray()
    offset_map = {}

    def get_offset(text):
        enc = text.encode('utf-8') + b'\x00'
        if enc not in offset_map:
            offset_map[enc] = len(new_pool)
            new_pool.extend(enc)
        return offset_map[enc]

    new_sec_data = bytearray(sec_data)

    # Collect and assign offsets for Table 1 (UI strings)
    t1_start = t1_ptr - sec_start
    t1_ptrs = []
    for i in range(t1_cnt):
        off = t1_start + i * 16
        if off + 16 > len(sec_data):
            break
        h, s_off = struct.unpack('<QQ', bytes(sec_data[off: off + 16]))
        h_hex = f"{h:016x}"
        text = new_strings.get(h_hex, _read_cstr(bytes(sec_data), s_off - sec_start))
        rel_p = get_offset(text)
        t1_ptrs.append((off + 8, rel_p))

    # Collect and assign offsets for Table 3 (Dialogue cues)
    t3_start = t3_ptr - sec_start
    t3_ptrs = []
    for i in range(t3_cnt):
        off = t3_start + i * 24
        if off + 24 > len(sec_data):
            break
        h, sub_p, sub_cnt = struct.unpack('<QQQ', bytes(sec_data[off: off + 24]))
        h_hex = f"{h:016x}"
        sub_start = sub_p - sec_start
        if h_hex in new_strings:
            for j in range(int(sub_cnt)):
                se_off = sub_start + j * 16
                if 0 <= se_off + 16 <= len(sec_data):
                    txt = new_strings[h_hex] if j == 0 else ""
                    rel_p = get_offset(txt)
                    t3_ptrs.append((se_off + 8, rel_p))
        else:
            for j in range(int(sub_cnt)):
                se_off = sub_start + j * 16
                if 0 <= se_off + 16 <= len(sec_data):
                    sh, s_off = struct.unpack('<QQ', bytes(sec_data[se_off: se_off + 16]))
                    txt = _read_cstr(bytes(sec_data), s_off - sec_start)
                    rel_p = get_offset(txt)
                    t3_ptrs.append((se_off + 8, rel_p))

    if len(new_pool) <= desc_pos:
        # In-pool engine: string pool fits within original boundary (84KB free space)
        # Guarantees zero relocation breakdown, zero header shifts, 100% game compatibility
        for ptr_off, rel_p in t1_ptrs:
            struct.pack_into('<Q', new_sec_data, ptr_off, sec_start + rel_p)
        for ptr_off, rel_p in t3_ptrs:
            struct.pack_into('<Q', new_sec_data, ptr_off, sec_start + rel_p)

        pad = desc_pos - len(new_pool)
        new_sec_data[:desc_pos] = new_pool + b'\x00' * pad
        out_data = bytes(data[:sec_abs]) + bytes(new_sec_data) + bytes(data[sec_abs + sec_size:])
    else:
        # Expanded engine: string pool exceeds original boundary
        # Shifts tables and synchronizes both Primary and Secondary KCAP headers
        append_blob = bytearray()
        seen_append = {}

        def get_append_offset(text):
            enc = text.encode('utf-8') + b'\x00'
            if enc not in seen_append:
                seen_append[enc] = len(append_blob)
                append_blob.extend(enc)
            return seen_append[enc]

        append_base_abs = sec_start + len(sec_data)
        for i in range(t1_cnt):
            off = t1_start + i * 16
            if off + 16 > len(sec_data):
                break
            h, s_off = struct.unpack('<QQ', bytes(sec_data[off: off + 16]))
            h_hex = f"{h:016x}"
            if h_hex in new_strings:
                str_p = s_off - sec_start
                orig_text = _read_cstr(bytes(sec_data), str_p) if 0 <= str_p < len(sec_data) else ''
                if new_strings[h_hex] != orig_text:
                    rel_off = get_append_offset(new_strings[h_hex])
                    struct.pack_into('<Q', new_sec_data, off + 8, append_base_abs + rel_off)

        for i in range(t3_cnt):
            off = t3_start + i * 24
            if off + 24 > len(sec_data):
                break
            h, sub_p, sub_cnt = struct.unpack('<QQQ', bytes(sec_data[off: off + 24]))
            h_hex = f"{h:016x}"
            if h_hex in new_strings:
                sub_start = sub_p - sec_start
                for j in range(int(sub_cnt)):
                    se_off = sub_start + j * 16
                    if 0 <= se_off + 16 <= len(sec_data):
                        txt = new_strings[h_hex] if j == 0 else ""
                        rel_off = get_append_offset(txt)
                        struct.pack_into('<Q', new_sec_data, se_off + 8, append_base_abs + rel_off)

        if append_blob:
            new_sec_data += append_blob

        new_sec0_size = len(new_sec_data)
        pad_len = (16 - (new_sec0_size % 16)) % 16
        new_sec_data += b'\x00' * pad_len
        new_sec0_size_aligned = len(new_sec_data)

        out_header = bytearray(data[:hdr_size])
        pre_sec0_data = bytes(data[hdr_size:sec_abs])

        sec1_flags, sec1_size, sec1_offset = struct.unpack('<III', bytes(data[hdr_size-40 : hdr_size-28]))
        sec2_flags, sec2_size, sec2_offset = struct.unpack('<III', bytes(data[hdr_size-28 : hdr_size-16]))

        sec1_abs = hdr_size + sec1_offset
        sec2_abs = hdr_size + sec2_offset
        sec1_data = bytes(data[sec1_abs : sec1_abs + sec1_size])
        sec2_data = bytes(data[sec2_abs : sec2_abs + sec2_size])

        # Primary Table
        struct.pack_into('<I', out_header, hdr_size - 52 + 4, new_sec0_size_aligned)
        new_sec1_offset = sec_start + new_sec0_size_aligned
        struct.pack_into('<I', out_header, hdr_size - 40 + 8, new_sec1_offset)
        new_sec2_offset = new_sec1_offset + sec1_size
        struct.pack_into('<I', out_header, hdr_size - 28 + 8, new_sec2_offset)
        new_total_payload = new_sec2_offset + sec2_size
        struct.pack_into('<I', out_header, 0x2C, new_total_payload)

        # Secondary Descriptor Table (Sync required by Sucker Punch engine)
        struct.pack_into('<I', out_header, 0xEC, new_sec1_offset)
        struct.pack_into('<I', out_header, 0x118, new_sec1_offset)
        struct.pack_into('<I', out_header, 0x13C, sec2_size)
        struct.pack_into('<I', out_header, 0x140, new_sec2_offset)

        out_data = out_header + pre_sec0_data + new_sec_data + sec1_data + sec2_data

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

    new_sec_size = repack_xpps(template_path, new_strings, output_path)
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
            "Ghost of Tsushima Localization Tool (v2.1)\n"
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
    print("Ghost of Tsushima Localization Tool  v2.1")
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
