#!/usr/bin/env python3
"""
Verification helper — checks extracted OGG files are valid and reports stats.
Uses ffprobe if available, else falls back to a lightweight Vorbis header check.

Usage:
    ./verify.py [ogg_dir]
"""
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path


def check_vorbis_header(data):
    """Return (ok, info_dict) for an OGG file's bytes."""
    if data[:4] != b'OggS':
        return False, {'error': 'no OggS magic'}
    if len(data) < 35:
        return False, {'error': 'too small'}
    # First packet starts after the page header + segment table
    nseg = data[26]
    pkt_off = 27 + nseg
    if pkt_off + 7 > len(data):
        return False, {'error': 'no packet'}
    pkt = data[pkt_off:pkt_off + 30]
    if pkt[:7] != b'\x01vorbis':
        return False, {'error': 'not vorbis (no identification packet)'}
    channels = pkt[11]
    sample_rate = struct.unpack_from('<I', pkt, 12)[0]
    return True, {'channels': channels, 'sample_rate': sample_rate}


def main():
    ogg_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('output/ogg')
    if not ogg_dir.exists():
        print(f"Directory not found: {ogg_dir}")
        sys.exit(1)

    files = sorted(ogg_dir.glob('*.ogg'))
    print(f"Checking {len(files)} files in {ogg_dir}\n")

    use_ffprobe = shutil.which('ffprobe') is not None
    if not use_ffprobe:
        print("(ffprobe not available — using header-only check)\n")

    ok = bad = 0
    total_bytes = 0
    total_duration = 0.0
    for f in files:
        data = f.read_bytes()
        total_bytes += len(data)
        if use_ffprobe:
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'error',
                     '-show_entries', 'format=duration:stream=channels,sample_rate',
                     '-of', 'default=noprint_wrappers=1', str(f)],
                    capture_output=True, text=True, check=True
                )
                ok += 1
                for line in r.stdout.splitlines():
                    if line.startswith('duration='):
                        try: total_duration += float(line.split('=')[1])
                        except ValueError: pass
            except subprocess.CalledProcessError:
                bad += 1
                print(f"  CORRUPT: {f.name}")
        else:
            valid, info = check_vorbis_header(data)
            if valid:
                ok += 1
            else:
                bad += 1
                print(f"  CORRUPT ({info.get('error')}): {f.name}")

    print(f"\n{'─' * 50}")
    print(f"Valid:    {ok}")
    print(f"Corrupt:  {bad}")
    print(f"Total:    {ok + bad}")
    print(f"Size:     {total_bytes / 1024 / 1024:.1f} MB")
    if use_ffprobe and total_duration > 0:
        m, s = divmod(total_duration, 60)
        print(f"Duration: {int(m)}m {s:.1f}s ({total_duration:.1f}s)")
    print(f"{'─' * 50}")
    return 0 if bad == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
