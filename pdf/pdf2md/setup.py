#!/usr/bin/env python3

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"

PACKAGES = [
    "pyyaml",
    "PyMuPDF",
    "pymupdf4llm",
    "marker-pdf",
]


def run(*args):
    subprocess.run(args, check=True)


def main():
    if not shutil.which("uv"):
        raise SystemExit(
            "uv is required. Install it with:\n"
            "  sudo pacman -S uv"
        )

    run("uv", "python", "install", "3.12")
    run("uv", "venv", "--python", "3.12", str(VENV))
    run("uv", "pip", "install", "--python", str(VENV / "bin" / "python"), *PACKAGES)

    print("\nSetup complete.")
    print("Activate with:")
    print("  source .venv/bin/activate")
    print("\nThen run:")
    print("  python get_text.py")


if __name__ == "__main__":
    main()
