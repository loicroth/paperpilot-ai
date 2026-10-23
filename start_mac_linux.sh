#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if command -v python3 >/dev/null 2>&1; then
    python3 launcher.py
elif command -v python >/dev/null 2>&1; then
    python launcher.py
else
    echo "ERREUR : Python 3.11 ou plus récent est requis."
    echo "Installez-le depuis https://www.python.org/downloads/ puis relancez ce fichier."
    exit 1
fi
