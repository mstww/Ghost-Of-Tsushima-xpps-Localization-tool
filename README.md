# Ghost of Tsushima Localization Tool (`xpps_tool`)

![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Languages](https://img.shields.io/badge/supported%20languages-27-orange)
![Release](https://img.shields.io/badge/release-v2.2-brightgreen)

A dedicated game localization, translation, and modding utility for **Ghost of Tsushima Director's Cut**.
Extract game text from proprietary KCAP `.xpps` binary localization files into easily editable JSON, translate strings, inspect changes with built-in diffing, and repack them back into game-ready `.xpps` files.

---

## Important Prerequisite: Extracting Game Files

In a standard Ghost of Tsushima PC installation, `.xpps` localization files are **not loose files** in the game folder. They are packed inside the game's `.psarc` archive files located in:
`<GameDirectory>/cache_pc/psarc/` (specifically archive files starting with `l`, such as language audio/text archives).

### How to get the `.xpps` files:
1. Download **GoTExtractor** by Glumboi from Nexus Mods:  
   👉 **[GoTExtractor on Nexus Mods (Mod #65)](https://www.nexusmods.com/ghostoftsushima/mods/65)**
2. Use GoTExtractor to unpack the target `.psarc` archive from `cache_pc/psarc/` (gapack_misc_l.psarc).
3. Once unpacked, you will have the raw `.xpps` files (e.g. `lang_english_text.xpps`, `lang_turkish_text.xpps`, etc.).
4. Use this tool (**`xpps_tool`**) to extract, edit, translate, and repack the `.xpps` text files.
5. Finally, use GoTExtractor to repack the modified `.xpps` back into `.psarc` or place it in your game's mod directory.

---

## Table of Contents

- [Features](#features)
- [Installation](#installation)
  - [Option A: Standalone Executable (No Python Required)](#option-a-standalone-executable-no-python-required)
  - [Option B: Python Script](#option-b-python-script)
- [Complete Modding & Translation Workflow](#complete-modding--translation-workflow)
  - [Step 0: Unpack `.psarc` with GoTExtractor](#step-0-unpack-psarc-with-gotextractor)
  - [Step 1: Extract Strings to JSON with `xpps_tool`](#step-1-extract-strings-to-json-with-xpps_tool)
  - [Step 2: Translate & Edit](#step-2-translate--edit)
  - [Step 3: Track Translation Progress (`diff`)](#step-3-track-translation-progress-diff)
  - [Step 4: Repack into `.xpps`](#step-4-repack-into-xpps)
  - [Step 5: Pack Back into Game](#step-5-pack-back-into-game)
- [CLI Reference](#cli-reference)
  - [1. `extract`](#1-extract)
  - [2. `repack`](#2-repack)
  - [3. `diff`](#3-diff)
- [Supported Languages (27 Languages)](#supported-languages-27-languages)
- [KCAP & KNLI Relocation Architecture](#kcap--knli-relocation-architecture)
- [Türkçe Yerelleştirme ve Çeviri Rehberi](#türkçe-yerelleştirme-ve-çeviri-rehberi)
- [License](#license)

---

## Features

- **Extract Game Text**: Unpacks 32,000+ UI, menu, quest, subtitle, and dialogue strings from proprietary KCAP `.xpps` files into clean, readable JSON format.
- **Dual-Engine Binary Repacker**:
  - **Duplicate-Slot In-Pool Engine**: Reclaims unused duplicate slots (85KB+ slack space) for instant, surgical translation edits with 99.996% identical binary layout.
  - **Dynamic Relocation & KNLI Re-encoding Engine (v2.2)**: Solves the notorious relocation breakdown! Dynamically resizes the string pool, shifts internal descriptors and tables, recalculates Section 7/8 boundaries, and re-encodes the Sucker Punch Section 8 KNLI relocation bytecode from scratch. Supports arbitrary text length and **2x, 5x, 10x file size expansion** without crashing or memory corruption.
- **Built-in Verification**: Automatically re-extracts and verifies repacked files against templates to ensure 100% round-trip binary integrity.
- **Translation Progress & Diff Tool**: Compare translation files against the original game text, calculate translation percentages, and view changed lines.
- **Drag & Drop Support**: Drag a `.xpps` file onto `xpps_tool.exe` to instantly extract its text.
- **Standalone Release**: Includes a pre-compiled Windows `.exe` (`xpps_tool.exe`) — no Python or environment setup needed.
- **Pure Python**: Zero external dependencies when running from source (`os`, `struct`, `json`, `re`, `argparse`).
- **Clean PUA Glyphs Option**: Option to strip gamepad button icons (Square, Cross, Circle) for clean text editing.

---

## Installation

### Option A: Standalone Executable (No Python Required)
1. Download `xpps_tool.exe` from the repository root or [Releases](../../releases).
2. Place `xpps_tool.exe` in your working folder.
3. Open PowerShell or Command Prompt and run:
   ```cmd
   xpps_tool.exe --help
   ```

### Option B: Python Script
Requires Python 3.8 or higher.
```bash
git clone https://github.com/mstww/Ghost-Of-Tsushima-xpps-Localization-tool.git
cd Ghost-Of-Tsushima-xpps-Localization-tool

# Standard library only — no pip install needed!
python xpps_tool.py --help
```

---

## Complete Modding & Translation Workflow

```
[Ghost of Tsushima Game Files]
       │
       ▼
 [cache_pc/psarc/gapack_misc_l.psarc]
       │
       │  (Unpack with GoTExtractor - NexusMods #65)
       ▼
 [lang_*_text.xpps]
       │
       │  (Extract with xpps_tool extract)
       ▼
 [translation.json]  ◄── (Translate in VS Code / Notepad++)
       │
       │  (Check with xpps_tool diff)
       ▼
 [lang_*_text.xpps]  (Repack with xpps_tool repack)
       │
       │  (Repack with GoTExtractor or place in mods folder)
       ▼
[Ready to Play in Game!]
```

### Step 0: Unpack `.psarc` with GoTExtractor
1. Install [GoTExtractor (Nexus Mods #65)](https://www.nexusmods.com/ghostoftsushima/mods/65).
2. Open GoTExtractor and extract the language archive (e.g. archive starting with `l` in `cache_pc/psarc/`).
3. You will obtain the `.xpps` files (e.g. `lang_english_text.xpps`, `lang_turkish_text.xpps`).

### Step 1: Extract Strings to JSON with `xpps_tool`
Extract text from your target language file (e.g. English as a base, or Turkish):
```bash
# Extract to a single JSON file
xpps_tool.exe extract lang_english_text.xpps -o english_strings.json

# Or extract all 27 languages at once into organized folders
xpps_tool.exe extract <folder_with_xpps_files> -o translations/
```

### Step 2: Translate & Edit
Open `english_strings.json` in any text editor (VS Code, Notepad++, Sublime Text, etc.):
```json
{
  "00000002db95e0f0": "Cut",
  "00000002db9628be": "Eat",
  "00000002db962dcf": "End",
  "00000002db97e48c": "Point of Interest"
}
```
Edit the text values on the right. **Keep the 16-character hex keys unchanged**, as the game engine uses them to locate strings:
```json
{
  "00000002db95e0f0": "Kes",
  "00000002db9628be": "Ye",
  "00000002db962dcf": "Bitir",
  "00000002db97e48c": "Önemli Konum"
}
```

### Step 3: Track Translation Progress (`diff`)
Check how many strings have been translated and view sample changes:
```bash
xpps_tool.exe diff lang_english_text.xpps my_turkish_translation.json
```
Example output:
```
==================================================
Translation Progress & Statistics
==================================================
  Total original strings:     32,459
  Total translated strings:   32,459
  Modified / Translated:       4,250  (13.09%)
  Identical (untouched):      28,209

Sample Modified Strings (first 5):
  [00000002db95e0f0]
    - Original:   Cut
    + Translated: Kes
```

### Step 4: Repack into `.xpps`
Rebuild the game-ready `.xpps` file using the original file as a binary template:
```bash
xpps_tool.exe repack my_turkish_translation.json -t lang_english_text.xpps -o lang_turkish_text.xpps
```
The tool will automatically verify that the string count and binary invariants match the template.

### Step 5: Pack Back into Game
Repack the modified `.xpps` back into `.psarc` using **GoTExtractor**, or install it using your preferred Ghost of Tsushima mod manager.

---

## CLI Reference

### 1. `extract`
Extracts strings from a single `.xpps` file or a directory of `.xpps` files.

```bash
xpps_tool.exe extract <input> [-o OUTPUT] [--clean-pua]
```
- `<input>`: Path to a single `.xpps` file (e.g. `lang_turkish_text.xpps`) or a folder.
- `-o, --output`:
  - If given a path ending in `.json` (e.g. `-o tr.json`), writes directly to that file.
  - If given a directory path, extracts into `<output>/<lang>/strings.json`.
- `--clean-pua`: Removes gamepad button glyphs (Unicode Private Use Area icons `\ue000`–`\uf8ff`) for clean text reading.

### 2. `repack`
Rebuilds a game `.xpps` file from an edited translation JSON.

```bash
xpps_tool.exe repack <strings_file> -t <template> [-o OUTPUT] [--force-expand] [--no-verify]
```
- `<strings_file>`: Path to your translated `strings.json` file.
- `-t, --template`: Path to the original game `.xpps` file used as the layout template.
- `-o, --output`: Destination path (default: `<template>_modded.xpps`).
- `--force-expand`: Forces the Dynamic Relocation & KNLI Re-encoding Engine even if modifications fit into duplicate string slots.
- `--no-verify`: Skips automatic extraction verification after repacking.

> [!TIP]
> **Automatic Mode Selection**:
> If your changes are small (e.g. tweaking a few hundred strings or menus), `xpps_tool` automatically uses the **Duplicate-Slot In-Pool Engine**, keeping 99.996% of the original binary identical.
> If your changes exceed available slot space or you translate large chunks of text, `xpps_tool` **automatically switches to the Dynamic Relocation & KNLI Engine**, rebuilding all pointers and relocation tables with zero limits on length or file size!

### 3. `diff`
Compares two translation files or checks mod progress.

```bash
xpps_tool.exe diff <file1> <file2> [-o OUTPUT]
```
- `<file1>`: Base file (original `.xpps` or `strings.json`).
- `<file2>`: Translated file (edited `.xpps` or `strings.json`).
- `-o, --output`: Optional path to export all modified translation lines into a separate JSON file.

---

## Supported Languages (27 Languages)

| Code     | Language              | Default File Name            |
|----------|-----------------------|------------------------------|
| `ar`     | Arabic                | `lang_arabic_text.xpps`      |
| `pt-br`  | Portuguese (Brazil)   | `lang_brazilian_text.xpps`   |
| `en-gb`  | English (UK)          | `lang_british_text.xpps`     |
| `zh-cn`  | Chinese (Simplified)  | `lang_chinese_s_text.xpps`   |
| `zh-tw`  | Chinese (Traditional) | `lang_chinese_text.xpps`     |
| `hr`     | Croatian              | `lang_croatian_text.xpps`    |
| `cs`     | Czech                 | `lang_czech_text.xpps`       |
| `da`     | Danish                | `lang_danish_text.xpps`      |
| `nl`     | Dutch                 | `lang_dutch_text.xpps`       |
| `en`     | English (US)          | `lang_english_text.xpps`     |
| `fi`     | Finnish               | `lang_finnish_text.xpps`     |
| `fr`     | French                | `lang_french_text.xpps`      |
| `de`     | German                | `lang_german_text.xpps`      |
| `el`     | Greek                 | `lang_greek_text.xpps`       |
| `hu`     | Hungarian             | `lang_hungarian_text.xpps`   |
| `it`     | Italian               | `lang_italian_text.xpps`     |
| `ja`     | Japanese              | `lang_japanese_text.xpps`    |
| `ko`     | Korean                | `lang_korean_text.xpps`      |
| `es-mx`  | Spanish (Latin America)| `lang_latino_text.xpps`     |
| `no`     | Norwegian             | `lang_norwegian_text.xpps`   |
| `pl`     | Polish                | `lang_polish_text.xpps`      |
| `pt`     | Portuguese (Portugal) | `lang_portuguese_text.xpps`  |
| `ru`     | Russian               | `lang_russian_text.xpps`     |
| `es`     | Spanish (Spain)       | `lang_spanish_text.xpps`     |
| `sv`     | Swedish               | `lang_swedish_text.xpps`     |
| `th`     | Thai                  | `lang_thai_text.xpps`        |
| `tr`     | Turkish               | `lang_turkish_text.xpps`     |

---

## KCAP & KNLI Relocation Architecture

Ghost of Tsushima uses Sucker Punch Productions' proprietary **KCAP** archive format with **KNLI** (Kernel Linker Information) relocation bytecode for memory rebasing:

```
[KCAP Container Header] (0x00 - 0x1E4)
  0x00: Magic ASCII "KCAP"
  0x28: Header Size (0x1E4 = 484 bytes)
  0x2C: Total Payload Size (uint32)
  0xC0: Secondary Section Table (Offsets & Sizes for Sec 7 & 8)
  0x168: Primary Section Table (12 bytes per section descriptor: flags, size, offset)

[KCAP Payload Layout]
  Sections 0 - 5: System and Sub-archive Descriptors
  Section 6: Text & Localization Section (Strings, Descriptors, Tables 1, 2, 3)
  Section 7: Language Font & Glyph Raster Data (13,631,568 bytes)
  Section 8: KNLI 64-bit Pointer Relocation Bytecode Table (127+ KB)

[Section 6 Layout]
  0x000000 .. desc_pos: Contiguous UTF-8 String Pool
  desc_pos: Master Localization Descriptor [T1_ptr, T1_cnt, T2_ptr, T2_cnt, T3_ptr, T3_cnt]
  Table 1: UI & Menu Strings (16 bytes: uint64 hash, uint64 str_ptr)
  Table 2: Engine Audio/Event Mappings (24 bytes: uint64 hash, uint64 sub_ptr, uint64 count)
  Table 3: Dialogue & Subtitle Cues (24 bytes: uint64 hash, uint64 sub_ptr, uint64 count)
    Table 3 Sub-entries: Array of (16 bytes: uint64 sub_hash, uint64 str_ptr)
  Tail: Format Descriptors & Engine Type Registrations
```

### Why Naive Repacking Broke Games & How v2.2 Solves It
1. **Engine Bounds Invariant**: The engine validates that every string pointer falls strictly inside `0 .. desc_pos`. In v2.2, all strings are positioned within this boundary and `desc_pos` dynamically expands.
2. **KNLI Relocation Bytecode**: When loaded into RAM, the engine traverses the KNLI stream and rebases every 64-bit pointer (`*(uint64_t*)(base + offset) += base`). If string expansion moves tables without updating KNLI, the engine rebases random memory, resulting in instant crashes.
3. **15-Bit Window Relocation Encoding**: `xpps_tool` v2.2 includes the world's first complete encoder/decoder for KNLI bytecode, recalculating all 63,400+ relocation pointers so that file size can grow to **2x, 5x, or 10x** seamlessly without crashing.

---

## Türkçe Yerelleştirme ve Çeviri Rehberi

Ghost of Tsushima Director's Cut için kendi Türkçe yamanızı yapmak veya mevcut çevirileri düzenlemek için tam adımlar:

### 1. Ön Hazırlık (.psarc Dosyalarını Açma)
`.xpps` dil dosyaları oyunun `cache_pc/psarc/` klasöründeki `l` harfiyle başlayan `.psarc` arşivlerinin içindedir.
1. Nexus Mods'tan **GoTExtractor** aracını indirin:  
   👉 **[GoTExtractor (Nexus Mods #65)](https://www.nexusmods.com/ghostoftsushima/mods/65)**
2. GoTExtractor ile `cache_pc/psarc/` içindeki dil arşivini (örneğin `gapack_misc_l.psarc`) dışarı çıkarın.
3. Böylece `lang_turkish_text.xpps` dosyasını elde edeceksiniz.

### 2. Metinleri JSON Olarak Dışa Aktarma
```powershell
.\xpps_tool.exe extract lang_turkish_text.xpps -o turkce_ceviri.json
```
Bu komut 32.461 satırlık oyun metnini düzenlenebilir `turkce_ceviri.json` dosyasına çıkarır.

### 3. Çeviriyi Düzenleme (Sınırsız Karakter ve Uzunluk!)
JSON dosyasını VS Code veya Notepad++ ile açın. Sol taraftaki 16 haneli hex kodlarına dokunmadan sağ taraftaki metinleri dilediğiniz gibi uzatın ve düzenleyin:
```json
"8bc3029abeb1b9a2": "Başlamak için  düğmesine bas"
```
> [!NOTE]
> `xpps_tool` v2.2 ile artık **metin uzunluğu veya karakter sınırı tamamen kaldırılmıştır**. Çeviriniz orijinalden 2 kat, 5 kat daha uzun olsa bile Dinamik Relokasyon Motoru sayesinde oyun çökmeden sorunsuz yüklenir.

### 4. Çeviri İlerlemesini Kontrol Etme
```powershell
.\xpps_tool.exe diff lang_turkish_text.xpps turkce_ceviri.json
```
Bu komut kaç satırın çevrildiğini, yüzde kaç tamamlandığını ve örnek çevirileri gösterir.

### 5. Oyuna Uygun Hale Getirme (Repack)
```powershell
.\xpps_tool.exe repack turkce_ceviri.json -t lang_turkish_text.xpps -o lang_turkish_text.xpps
```

### 6. Oyuna Yükleme
Ürettiğiniz `lang_turkish_text.xpps` dosyasını GoTExtractor ile tekrar `.psarc` içine paketleyin veya oyunun mod klasörüne ekleyin.

---

## License

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.
Open source and free for the modding community.
