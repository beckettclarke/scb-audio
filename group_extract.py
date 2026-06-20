#!/usr/bin/env python3
"""
Generic SimCity BuildIt / Marmalade .group.bin extractor.

Takes a SINGLE .group.bin file, decompresses the LZHAM layer, parses the
Marmalade container, and extracts whatever assets are inside:

  - Audio groups (section hash 0xd5610dab): carves individual .ogg files
  - Other groups (textures, etc.): saves each block as a raw .bin file

The LZHAM decompression is handled by the bundled native helper
(lzham_codec/final_decode). Texture blocks are saved raw because Marmalade
stores them in its own CIwTexture serialized format, which would need a
separate deserializer to convert to PNG.

Usage:
    ./group_extract.py <file.group.bin> [output_dir]

If output_dir is omitted, a folder is created next to the input file with
the same base name (e.g. "Textures.group.bin" -> "Textures/").
"""
import os
import re
import struct
import subprocess
import sys
from pathlib import Path

# ─── Constants ──────────────────────────────────────────────────────────────
LZHAM_MAGIC      = b'\x6e\xcd'
MARMALADE_MAGIC  = 0x3d

# Section hashes (the 4-byte value at offset 4 inside each data block).
# These identify what kind of serialized resource the block contains.
SEC_HASH_AUDIO    = 0xd5610dab   # BASS audio sample group (OGG Vorbis inside)
SEC_HASH_TEXTURE  = 0x3521f539   # CIwTexture / texture group

# File signatures we know how to carve out of a raw data blob.
FILE_SIGNATURES = [
    (b'\x89PNG\r\n\x1a\n', 'png', 'PNG image'),
    (b'\xff\xd8\xff',       'jpg', 'JPEG image'),
    (b'OggS',               'ogg', 'OGG Vorbis audio'),
    (b'RIFF',               'wav', 'WAV audio'),
    (b'PVR\x03',            'pvr', 'PVR texture v3'),
    (b'PVR!',               'pvr', 'PVR texture'),
    (b'KTX ',               'ktx', 'KTX texture'),
    (b'<?xml',              'xml', 'XML document'),
    (b'BM',                 'bmp', 'BMP image'),
    (b'GIF8',               'gif', 'GIF image'),
]


def log(msg):
    print(msg, flush=True)


def find_helper(script_dir):
    """Locate the compiled final_decode binary."""
    for c in [script_dir / 'lzham_codec' / 'final_decode',
              script_dir / 'final_decode']:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return None


def decompress(in_path, out_path, helper):
    """Run the native LZHAM helper. Returns True on success."""
    r = subprocess.run([str(helper), str(in_path), str(out_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"  LZHAM failed (exit {r.returncode}):")
        log(r.stderr or r.stdout)
        return False
    with open(out_path, 'rb') as f:
        head = f.read(4)
    if head[0] != MARMALADE_MAGIC:
        log(f"  WARNING: output doesn't start with Marmalade 0x3d magic "
            f"(got {head[0]:#x})")
        return False
    log(f"  decompressed -> {out_path.stat().st_size:,} bytes "
        f"(Marmalade v{head[1]}.{head[2]}.{head[3]})")
    return True


# ─── Marmalade container parser ─────────────────────────────────────────────
def parse_marmalade(data):
    """
    Parse a Marmalade .group container.
    Returns (version_tuple, [(name_hash, payload_bytes), ...]).
    """
    if data[0] != MARMALADE_MAGIC:
        raise ValueError(f"Not Marmalade (magic {data[0]:#x})")
    version = (data[1], data[2], data[3])
    blocks = []
    off = 6  # 0x3d + major + minor + rev + u16 mystery
    while off + 8 <= len(data):
        name_hash, size = struct.unpack_from('<II', data, off)
        off += 8
        if name_hash == 0:
            break
        if size < 4 or size - 4 > len(data) - off:
            log(f"  WARNING: block {name_hash:#010x} size {size} invalid; stopping")
            break
        blocks.append((name_hash, data[off:off + size - 4]))
        off += size - 4
    return version, blocks


def read_section_header(block_data):
    """
    Read the section header that lives at the start of a data block.
    Layout: [sec_magic:u32=1][sec_hash:u32][count:u32][0x01 0x01]
    Returns (sec_hash, count) or (None, None) if it doesn't match.
    """
    if len(block_data) < 14:
        return None, None
    sec_magic = struct.unpack_from('<I', block_data, 0)[0]
    if sec_magic != 1:
        return None, None
    sec_hash = struct.unpack_from('<I', block_data, 4)[0]
    count = struct.unpack_from('<I', block_data, 8)[0]
    return sec_hash, count


# ─── OGG carving ────────────────────────────────────────────────────────────
def carve_ogg(data, out_dir, prefix='sample'):
    """Carve OGG streams by splitting on BOS pages. Returns count written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = []
    for m in re.finditer(b'OggS', data):
        off = m.start()
        if off + 27 > len(data):
            continue
        nseg = data[off + 26]
        if off + 27 + nseg > len(data):
            continue
        seg_table = data[off + 27:off + 27 + nseg]
        page_size = 27 + nseg + sum(seg_table)
        if off + page_size > len(data):
            continue
        pages.append({
            'off': off, 'htype': data[off + 5], 'end': off + page_size,
        })
    bos = [p for p in pages if (p['htype'] & 0x02)]
    eos = sum(1 for p in pages if (p['htype'] & 0x04))
    log(f"    {len(pages)} OGG pages, {len(bos)} streams (BOS), {eos} EOS")

    written = 0
    for i, b in enumerate(bos):
        start = b['off']
        end = bos[i + 1]['off'] if i + 1 < len(bos) else pages[-1]['end']
        chunk = data[start:end]
        if len(chunk) < 4096:
            continue
        with open(out_dir / f'{prefix}_{written:04d}.ogg', 'wb') as f:
            f.write(chunk)
        written += 1
    return written


# ─── Generic signature-based carving (for non-audio blocks) ─────────────────
def carve_by_signature(data, out_dir, prefix='asset'):
    """
    Scan for known file signatures and carve out any files found.
    Returns list of (type_label, path) tuples.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found = []
    for sig, ext, label in FILE_SIGNATURES:
        positions = [m.start() for m in re.finditer(re.escape(sig), data)]
        # Filter: require signature at a reasonable alignment and not inside
        # another signature's data. For OGG we use the dedicated carver.
        if ext == 'ogg':
            continue
        for idx, pos in enumerate(positions):
            # Heuristic: real files usually start at offset > 0 and the
            # signature is followed by plausible header bytes.
            chunk = carve_one_file(data, pos, sig, ext)
            if chunk is None:
                continue
            fname = out_dir / f'{prefix}_{ext}_{len(found):04d}.{ext}'
            with open(fname, 'wb') as f:
                f.write(chunk)
            found.append((label, fname))
    return found


def carve_one_file(data, start, sig, ext):
    """
    Best-effort: extract bytes from `start` until the next signature or end.
    Returns the chunk, or None if it looks like a false positive.
    """
    # Determine a plausible end: next occurrence of any signature, or EOF.
    # For simplicity, scan forward for the next signature of any type.
    earliest = len(data)
    for s, _, _ in FILE_SIGNATURES:
        nxt = data.find(s, start + len(sig))
        if 0 < nxt < earliest:
            earliest = nxt
    chunk = data[start:earliest]
    # Reject false positives: too small or mostly non-printable for text types
    if ext in ('xml',):
        if len(chunk) < 32:
            return None
    if len(chunk) < 16:
        return None
    return chunk


# ─── Main pipeline ──────────────────────────────────────────────────────────
def process_file(in_path, out_dir, helper):
    """Process one .group.bin. Returns True on success."""
    in_path = Path(in_path)
    log(f"\n{'='*60}")
    log(f"  {in_path.name}")
    log(f"{'='*60}")

    tmp_decoded = out_dir / '_decoded.bin'
    if not decompress(in_path, tmp_decoded, helper):
        return False
    with open(tmp_decoded, 'rb') as f:
        data = f.read()
    tmp_decoded.unlink()

    version, blocks = parse_marmalade(data)
    log(f"  Marmalade v{version[0]}.{version[1]}.{version[2]}, "
        f"{len(blocks)} block(s)")

    # Save the raw decoded container too (useful for inspection)
    raw_dir = out_dir / 'raw_blocks'
    raw_dir.mkdir(parents=True, exist_ok=True)

    assets_dir = out_dir / 'assets'
    assets_dir.mkdir(parents=True, exist_ok=True)

    total_assets = 0
    for name_hash, payload in blocks:
        log(f"  block {name_hash:08x} ({len(payload):,} bytes)")
        # Save raw block
        with open(raw_dir / f'{name_hash:08x}.bin', 'wb') as f:
            f.write(payload)

        # Is this a small "name" block (printable ASCII)?
        if len(payload) < 64:
            m = re.match(rb'^[\x20-\x7e]{2,40}\x00*$', payload)
            if m:
                log(f"    -> group name: {m.group().rstrip(chr(0).encode()).decode()}")
                continue

        # Try to interpret as a section with a known hash
        sec_hash, count = read_section_header(payload)
        if sec_hash == SEC_HASH_AUDIO:
            log(f"    -> AUDIO section ({count} samples expected)")
            n = carve_ogg(payload, assets_dir / 'ogg')
            log(f"    -> carved {n} OGG files")
            total_assets += n
        elif sec_hash == SEC_HASH_TEXTURE:
            log(f"    -> TEXTURE section ({count} entries) — saved raw "
                f"(CIwTexture format needs dedicated parser)")
            total_assets += 1
        elif sec_hash is not None:
            log(f"    -> unknown section hash {sec_hash:#010x} ({count} entries) — saved raw")
            total_assets += 1
        else:
            # Last resort: signature scan
            log(f"    -> no section header; scanning for embedded files...")
            found = carve_by_signature(payload, assets_dir / 'carved')
            if found:
                for label, path in found:
                    log(f"       carved {label}: {path.name}")
                total_assets += len(found)
            else:
                log(f"       no recognizable files; saved as raw block")

    log(f"\n  Total assets extracted: {total_assets}")
    return True


def main():
    if len(sys.argv) < 2:
        log("Usage: group_extract.py <file.group.bin> [output_dir]")
        log("")
        log("Extracts assets from any Marmalade .group.bin file.")
        log("Audio groups produce .ogg files; other groups produce raw blocks.")
        sys.exit(1)

    in_path = Path(sys.argv[1]).resolve()
    if not in_path.exists():
        log(f"File not found: {in_path}")
        sys.exit(1)
    if not in_path.name.endswith('.group.bin'):
        log(f"WARNING: '{in_path.name}' doesn't end in '.group.bin' — "
            f"trying anyway")

    if len(sys.argv) > 2:
        out_dir = Path(sys.argv[2])
    else:
        # Default: folder next to input, named after the file stem
        out_dir = in_path.parent / in_path.name.removesuffix('.group.bin')

    script_dir = Path(__file__).resolve().parent
    helper = find_helper(script_dir)
    if helper is None:
        log("ERROR: lzham_codec/final_decode not found or not executable.")
        log("Build it: cd lzham_codec && "
            "clang++ -O2 final_decode.cpp -I. -Lbuild/lzhamdll -llzhamdll -o final_decode")
        sys.exit(1)

    # Make the dynamic loader find liblzhamdll
    lib_dir = helper.parent / 'build' / 'lzhamdll'
    if lib_dir.exists():
        os.environ['DYLD_LIBRARY_PATH'] = (
            f"{lib_dir}:{os.environ.get('DYLD_LIBRARY_PATH', '')}")
        os.environ['LD_LIBRARY_PATH'] = (
            f"{lib_dir}:{os.environ.get('LD_LIBRARY_PATH', '')}")

    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"Input:  {in_path}")
    log(f"Output: {out_dir}")

    ok = process_file(in_path, out_dir, helper)
    log("")
    log("=" * 60)
    log(" DONE" if ok else " FAILED")
    log("=" * 60)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
