#!/bin/bash
set -e

export WINEPREFIX="$HOME/.wine64"
export WINEDEBUG="-all"

BASE_DIR="/home/montel/Documents/Champollion workbench/files"
CHAMPOLLION="/home/montel/Documents/Champollion.v1.3.2/Champollion.exe"
CAPRICA="/home/montel/Documents/Caprica.v0.3.0/Caprica.exe"

PEX_DIR="$BASE_DIR/pex"
PSC_DIR="$BASE_DIR/psc"
COMPILED_DIR="$BASE_DIR/compiled"

mkdir -p "$PEX_DIR" "$PSC_DIR" "$COMPILED_DIR"

echo "=== Decompiling .pex files with Champollion ==="
if compgen -G "$PEX_DIR/*.pex" > /dev/null; then
    wine "$CHAMPOLLION" "$PEX_DIR" -o "$PSC_DIR"
else
    echo "No .pex files found in $PEX_DIR"
fi

echo -e "\n=== Recompiling .psc files with Caprica ==="
if compgen -G "$PSC_DIR/*.psc" > /dev/null; then
    wine "$CAPRICA" "$PSC_DIR" -g skyrim -i "$PSC_DIR" -o "$COMPILED_DIR" --parallel-compile
else
    echo "No .psc files found in $PSC_DIR"
fi

echo -e "\nProcess complete."
