"""Préparation locale reproductible, sans commande manuelle de programmation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent
ENV_DIR = ROOT / ".venv"
PYTHON = ENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STAMP = ENV_DIR / ".paperpilot-requirements.sha256"


def run(command: list[str], purpose: str) -> None:
    print(purpose, flush=True)
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"{purpose} Échec (code {result.returncode}).")


def main() -> int:
    if sys.version_info < (3, 11):
        print("ERREUR : installez Python 3.11 ou plus récent depuis python.org.")
        return 1
    os.chdir(ROOT)
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not PYTHON.exists():
        print("Préparation de l'environnement Python (premier lancement)…", flush=True)
        venv.EnvBuilder(with_pip=True).create(ENV_DIR)
    run([str(PYTHON), "-c", "import sys; assert sys.version_info >= (3,11), 'Python 3.11 minimum'"],
        "Vérification de Python…")
    probes = subprocess.run(
        [str(PYTHON), "-c", "import fastapi, uvicorn, httpx, socksio, dotenv, pydantic, filelock"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    installed_digest = STAMP.read_text().strip() if STAMP.exists() else ""
    if installed_digest != digest or probes.returncode:
        run([str(PYTHON), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(requirements)],
            "Installation des dépendances (connexion Internet requise)…")
        STAMP.write_text(digest + "\n")
    env_file = ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(ROOT / ".env.example", env_file)
        if os.name != "nt":
            env_file.chmod(0o600)
        print("Fichier .env créé. Sans clé OpenAI, le tableau de bord affiche la configuration à terminer.")
    run([str(PYTHON), "-m", "app.run", "--check"], "Vérification de la configuration…")
    print("\nOuverture du serveur local. Adresse par défaut : http://localhost:8000", flush=True)
    print("Gardez cette fenêtre ouverte. Ctrl+C arrête le processus et donc les protections virtuelles.\n", flush=True)
    run([str(PYTHON), "-m", "app.run"], "Démarrage de PaperPilot AI…")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nServeur arrêté. Les données sont conservées ; les stops virtuels ne sont plus surveillés.")
        raise SystemExit(0)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"\nERREUR : {exc}")
        print("Vérifiez la connexion Internet, les droits du dossier et le guide README_FR.md.")
        print("Sous Linux, si venv manque : installez le paquet python3-venv de votre distribution.")
        raise SystemExit(1)
