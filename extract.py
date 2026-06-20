#!/usr/bin/env python3
"""
SimCity BuildIt .group.bin audio extractor.

Self-contained Python implementation of the decompression and carving logic.
Does NOT depend on the bundled LZHAM library — it calls the prebuilt
`final_decode` helper (in lzham_codec/) which wraps the LZHAM streaming API
with the confirmed-correct parameters.

If you want a pure-Python version with no native deps, see extract_nolzham.py
(it documents the format and falls back to the raw-bytes approach if LZHAM
isn't available — but the native helper is strongly recommended for speed
and correctness).

Usage:
    ./extract.py [input_dir] [output_dir]

Defaults:
    input_dir  = ./input       (place sound_main.group.bin + sound_xml.group.bin here)
    output_dir = ./output
"""
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Constants reverse-engineered from libsimcity.so
# ─────────────────────────────────────────────────────────────────────────────
SCB_MAGIC      = b'\x6e\xcd'      # container magic (first 2 bytes of compressed file)
MARMALADE_MAGIC = 0x3d            # first byte of decompressed Marmalade .group container
LZHAM_HELPER   = 'final_decode'   # name of the native LZHAM wrapper binary

# Confirmed LZHAM parameters (see README.md "Confirmed decompression parameters"):
#   dict_size_log2 = 21, table_update_rate = 0,
#   flags = LZHAM_DECOMP_FLAG_READ_ZLIB_STREAM (= 4)
# These are hardcoded inside final_decode.cpp — no need to pass them here.


def log(msg):
    print(msg, flush=True)


def find_lzham_helper(script_dir):
    """Locate the compiled final_decode binary relative to this script."""
    candidates = [
        script_dir / 'lzham_codec' / LZHAM_HELPER,
        script_dir / LZHAM_HELPER,
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return None


def decompress_file(in_path, out_path, helper):
    """Run the native LZHAM helper to decompress one .group.bin -> Marmalade."""
    log(f"  decompressing {in_path.name} ...")
    result = subprocess.run(
        [str(helper), str(in_path), str(out_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"  ERROR (exit {result.returncode}):")
        log(result.stderr or result.stdout)
        return False
    # Sanity: first byte must be Marmalade magic 0x3d
    with open(out_path, 'rb') as f:
        head = f.read(4)
    if head[0] != MARMALADE_MAGIC:
        log(f"  WARNING: output doesn't start with 0x3d magic (got {head[0]:#x})")
        return False
    log(f"  -> {out_path.stat().st_size:,} bytes (Marmalade {head[1]}.{head[2]}.{head[3]})")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Marmalade .group container parsing
# ─────────────────────────────────────────────────────────────────────────────
def split_marmalade_group(data, base_name, out_dir):
    """
    Parse a Marmalade .group container (magic 0x3d) and extract each block.

    Header (6 bytes, per IwResSerialise.h):
        [0]    0x3d magic
        [1:4]  major, minor, rev
        [4:6]  u16 mystery (zero on v3.1.1+)

    Blocks repeat until terminator:
        [name_hash:u32le]
        [size:u32le]          (size of the data field, including the next 4
                               bytes which group_split discards as a checksum)
        [data: size-4 bytes]  (actual payload; the file pointer advances by
                               size-4, NOT size — verified empirically)

    Terminator: name_hash == 0

    Returns dict {name_hash_hex: data_bytes}.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if data[0] != MARMALADE_MAGIC:
        raise ValueError(f"Not a Marmalade group (magic {data[0]:#x}, expected 0x3d)")
    major, minor, rev = data[1], data[2], data[3]
    log(f"  Marmalade v{major}.{minor}.{rev}, base name '{base_name}'")

    blocks = {}
    off = 6  # 0x3d(1) + major(1) + minor(1) + rev(1) + u16 mystery(2)
    while off + 8 <= len(data):
        name_hash, size = struct.unpack_from('<II', data, off)
        off += 8
        if name_hash == 0:  # terminator
            break
        if size < 4 or size - 4 > len(data) - off:
            log(f"  WARNING: block {name_hash:#x} size {size} invalid; stopping")
            break
        # group_split writes size-4 bytes AND advances the file pointer by
        # size-4 (the trailing 4 bytes overlap with the next block's hash).
        block_data = data[off:off + size - 4]
        off += size - 4
        key = f'{name_hash:08x}'
        blocks[key] = block_data
        out_path = out_dir / f'{base_name}_{key}'
        with open(out_path, 'wb') as f:
            f.write(block_data)
        log(f"  block {key}: {size - 4:,} bytes -> {out_path.name}")
    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# OGG Vorbis carving
# ─────────────────────────────────────────────────────────────────────────────
def parse_ogg_pages(data):
    """Yield dict for each OggS page in data."""
    pages = []
    for m in re.finditer(b'OggS', data):
        off = m.start()
        if off + 27 > len(data):
            continue
        nseg = data[off + 26]
        if off + 27 + nseg > len(data):
            continue
        seg_table = data[off + 27:off + 27 + nseg]
        body_len = sum(seg_table)
        page_size = 27 + nseg + body_len
        if off + page_size > len(data):
            continue
        pages.append({
            'off': off,
            'htype': data[off + 5],
            'serial': struct.unpack_from('<I', data, off + 14)[0],
            'seq': struct.unpack_from('<I', data, off + 18)[0],
            'page_size': page_size,
            'end': off + page_size,
        })
    return pages


def carve_ogg_streams(data, out_dir, min_size=4096):
    """
    Carve individual OGG streams. SimCity BuildIt reuses serial number 0 for
    every stream, so we split on BOS (Beginning Of Stream) pages instead.
    Each BOS page starts a new stream; the stream runs until the next BOS.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = parse_ogg_pages(data)
    bos_pages = [p for p in pages if (p['htype'] & 0x02)]
    eos_count = sum(1 for p in pages if (p['htype'] & 0x04))
    log(f"  {len(pages)} OGG pages, {len(bos_pages)} BOS (stream starts), {eos_count} EOS")

    written = 0
    for i, bos in enumerate(bos_pages):
        start = bos['off']
        end = bos_pages[i + 1]['off'] if i + 1 < len(bos_pages) else pages[-1]['end']
        stream_bytes = data[start:end]
        if len(stream_bytes) < min_size:
            continue
        out_path = out_dir / f'sample_{written:04d}.ogg'
        with open(out_path, 'wb') as f:
            f.write(stream_bytes)
        written += 1
    log(f"  -> {written} OGG files written")
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Manifest extraction
# ─────────────────────────────────────────────────────────────────────────────
def extract_manifest(blocks, out_path):
    """Pull the XML manifest out of the sound_xml data block."""
    for key, data in blocks.items():
        idx = data.find(b'<?xml')
        if idx >= 0:
            xml_text = data[idx:]
            with open(out_path, 'wb') as f:
                f.write(xml_text)
            log(f"  manifest (block {key}): {len(xml_text):,} bytes -> {out_path.name}")
            return True
    log("  WARNING: no XML manifest found in any block")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────
def main():
    script_dir = Path(__file__).resolve().parent
    in_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else script_dir / 'input'
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else script_dir / 'output'

    # Find input files
    main_file = in_dir / 'sound_main.group.bin'
    xml_file  = in_dir / 'sound_xml.group.bin'
    if not main_file.exists() and not xml_file.exists():
        log(f"No input files found in {in_dir}/")
        log("Place sound_main.group.bin and sound_xml.group.bin there.")
        sys.exit(1)

    # Find the LZHAM helper
    helper = find_lzham_helper(script_dir)
    if helper is None:
        log("ERROR: lzham_codec/final_decode not found or not executable.")
        log("Build it first:  cd lzham_codec && clang++ -O2 final_decode.cpp "
            "-I. -Lbuild/lzhamdll -llzhamdll -o final_decode")
        sys.exit(1)

    log(f"Using LZHAM helper: {helper}")
    log(f"Input:  {in_dir}")
    log(f"Output: {out_dir}")
    log('')

    decoded_dir = out_dir / 'decoded'
    blocks_dir  = out_dir / 'blocks'
    ogg_dir     = out_dir / 'ogg'
    decoded_dir.mkdir(parents=True, exist_ok=True)
    blocks_dir.mkdir(parents=True, exist_ok=True)
    ogg_dir.mkdir(parents=True, exist_ok=True)

    # Process the XML manifest file first (smaller, validates the pipeline)
    if xml_file.exists():
        log("[1/3] sound_xml.group.bin (manifest)")
        xml_decoded = decoded_dir / 'sound_xml.group.decoded'
        if not decompress_file(xml_file, xml_decoded, helper):
            log("  FAILED — aborting")
            sys.exit(2)
        with open(xml_decoded, 'rb') as f:
            xml_data = f.read()
        xml_blocks = split_marmalade_group(xml_data, 'sound_xml', blocks_dir)
        extract_manifest(xml_blocks, out_dir / 'manifest.xml')
        log('')

    # Process the main audio file
    if main_file.exists():
        log("[2/3] sound_main.group.bin (audio)")
        main_decoded = decoded_dir / 'sound_main.group.decoded'
        if not decompress_file(main_file, main_decoded, helper):
            log("  FAILED — aborting")
            sys.exit(2)
        with open(main_decoded, 'rb') as f:
            main_data = f.read()
        main_blocks = split_marmalade_group(main_data, 'sound_main', blocks_dir)
        log('')

        # Carve OGG streams from whichever block contains them.
        # The name block (small, ~13 bytes) holds the group name; the other
        # block holds the payload. Find the largest block.
        log("[3/3] Carving OGG Vorbis streams")
        data_key = max(main_blocks, key=lambda k: len(main_blocks[k]))
        log(f"  using data block {data_key} ({len(main_blocks[data_key]):,} bytes)")
        n = carve_ogg_streams(main_blocks[data_key], ogg_dir)
        log(f"  extracted {n} OGG files")
        log('')

    log('=' * 60)
    log(' DONE')
    log('=' * 60)
    log(f"Audio files:  {ogg_dir}/")
    log(f"Manifest:     {out_dir / 'manifest.xml'}")
    log(f"Decoded raw:  {decoded_dir}/")
    log(f"Marmalade blocks: {blocks_dir}/")


if __name__ == '__main__':
    main()
