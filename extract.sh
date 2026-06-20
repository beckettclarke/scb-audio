#!/bin/bash
#
# Generic SimCity BuildIt / Marmalade .group.bin extractor.
#
# Usage:
#   ./extract.sh <file.group.bin>            # output to <file>/ next to input
#   ./extract.sh <file.group.bin> <output>   # custom output dir
#   ./extract.sh                             # batch mode: process input/*.group.bin
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

HELPER="$SCRIPT_DIR/lzham_codec/final_decode"
LIB="$SCRIPT_DIR/lzham_codec/build/lzhamdll/liblzhamdll.dylib"

# Build the helper if missing
if [ ! -x "$HELPER" ]; then
    echo "Building LZHAM helper from source..."
    (
        cd "$SCRIPT_DIR/lzham_codec"
        if [ ! -f "build/lzhamdll/liblzhamdll.dylib" ] && [ ! -f "build/lzhamdll/liblzhamdll.so" ]; then
            mkdir -p build && cd build
            cmake .. -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1 || {
                echo "  cmake failed — install cmake, or place a prebuilt liblzhamdll in build/lzhamdll/"
                exit 1
            }
            make -j lzhamdll >/dev/null 2>&1 || make -j >/dev/null 2>&1
        fi
        clang++ -O2 final_decode.cpp -I. -Lbuild/lzhamdll -llzhamdll -o final_decode
    )
fi

# Make liblzhamdll discoverable at runtime
if [ -f "$LIB" ]; then
    export DYLD_LIBRARY_PATH="$(dirname "$LIB"):${DYLD_LIBRARY_PATH:-}"
elif [ -f "$SCRIPT_DIR/lzham_codec/build/lzhamdll/liblzhamdll.so" ]; then
    export LD_LIBRARY_PATH="$SCRIPT_DIR/lzham_codec/build/lzhamdll:${LD_LIBRARY_PATH:-}"
fi

echo ""
echo "=============================================="
echo " SimCity BuildIt / Marmalade Group Extractor"
echo "=============================================="

if [ $# -ge 1 ]; then
    # Single-file mode
    echo " Input: $1"
    [ $# -ge 2 ] && echo " Output: $2"
    echo "=============================================="
    echo ""
    python3 "$SCRIPT_DIR/group_extract.py" "$@"
else
    # Batch mode: process every .group.bin in input/
    INPUT_DIR="$SCRIPT_DIR/input"
    if [ ! -d "$INPUT_DIR" ] || [ -z "$(ls -A "$INPUT_DIR"/*.group.bin 2>/dev/null)" ]; then
        echo " No .group.bin files found in $INPUT_DIR/"
        echo " Either drop files there and re-run, or pass a file path:"
        echo "   ./extract.sh /path/to/file.group.bin"
        exit 1
    fi
    echo " Batch mode: processing all .group.bin in $INPUT_DIR/"
    echo "=============================================="
    echo ""
    for f in "$INPUT_DIR"/*.group.bin; do
        python3 "$SCRIPT_DIR/group_extract.py" "$f"
    done
fi
