#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compila el launcher como .exe onefile (sin runtime embebido) y arma el ZIP
portable usando UNICAMENTE las reglas de release_rules.json.

El ZIP lo arma release_zip.py (lógica compartida con build.py). Uso:
    python build_exe.py          # solo compila el .exe (recomendado)
    python build_exe.py --zip    # compila y ademas arma el ZIP portable

El ZIP NO se genera por defecto: primero se prueba el .exe y recien con la
aprobacion del usuario se corre con --zip.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from release_zip import load_rules, make_release_zip

# Dependencias del bundle del launcher (sin cryptography/keyring/win32).
HIDDEN_IMPORTS = [
    "tkinter",
    "tkinter.ttk",
    "pystray",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageTk",
    "ctypes",
    "ctypes.wintypes",
]

EXCLUDES = [
    "matplotlib",
    "numpy",
    "scipy",
    "pandas",
    "jupyter",
    "notebook",
    "IPython",
    "pytest",
]


def build_spec(project_root: Path) -> Path:
    """Genera un spec onefile: el exe NO embebe runtime/ (vive aparte)."""
    src_dir = project_root / "src"
    ico_path = str(project_root / "ytremote.ico")
    hidden = ", ".join(f"'{h}'" for h in HIDDEN_IMPORTS)
    excludes = ", ".join(f"'{e}'" for e in EXCLUDES)

    spec_content = f"""# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['src/launcher.py'],
    pathex=[r'{project_root}', r'{src_dir}'],
    binaries=[],
    datas=[],
    hiddenimports=[
        {hidden}
    ],
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[
        {excludes}
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ytremote',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Sin consola (ventana GUI)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=r'{ico_path}',  # Icono del .exe (ytremote.ico)
)
"""
    spec_path = project_root / "launcher.spec"
    spec_path.write_text(spec_content, encoding="utf-8")
    return spec_path


def run_pyinstaller(project_root: Path, spec_path: Path) -> Path:
    exe = project_root / "dist" / "ytremote.exe"
    if exe.exists():
        exe.unlink()

    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(spec_path)],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    if result.returncode != 0:
        print(f"ERROR en PyInstaller:\n{result.stderr}")
        sys.exit(1)
    if not exe.exists():
        print(f"ERROR: No se genero {exe}")
        sys.exit(1)
    return exe


def main():
    project_root = Path(__file__).resolve().parent
    dist_dir = project_root / "dist"
    build_dir = project_root / "build"

    rules = load_rules(project_root)

    # Limpiar builds anteriores
    for d in (dist_dir, build_dir):
        if d.exists():
            shutil.rmtree(d)

    spec_path = build_spec(project_root)
    print("Ejecutando PyInstaller (onefile, sin runtime embebido)...")
    exe = run_pyinstaller(project_root, spec_path)
    print(f"[OK] Ejecutable generado: {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")

    if "--zip" in sys.argv:
        make_release_zip(project_root, rules, exe_file=exe)
    else:
        print("")
        print("Zip NO generado (por defecto). Cuando el .exe este probado:")
        print("   python build_exe.py --zip")


if __name__ == "__main__":
    main()