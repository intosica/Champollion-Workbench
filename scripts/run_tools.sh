#!/bin/bash
export WINEPREFIX="$HOME/.wine64"
export WINEDEBUG="-all"

echo "=== Running Champollion Version Check ==="
wine "/home/montel/Documents/Champollion.v1.3.2/Champollion.exe" --version

echo -e "\n=== Running Caprica Help Check ==="
wine "/home/montel/Documents/Caprica.v0.3.0/Caprica.exe" --help | head -n 10
