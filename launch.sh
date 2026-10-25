#!/bin/bash
cd "/home/montel/Documents/Champollion workbench/files"
if command -v conda &> /dev/null; then
    eval "$(conda shell.bash hook)"
    conda deactivate
fi
source venv/bin/activate
python3 champollion_gui.py
