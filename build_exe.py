#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compila el launcher como .exe onefile (sin runtime embebido) y arma el ZIP
portable usando UNICAMENTE las reglas de release_rules.json.

Uso:
    python build_exe.py          # solo compila el .exe (recomendado)
    python build_exe.py --zip    # compila y ademas arma el ZIP portable

El ZIP NO se genera por defecto: primero se prueba el .exe y recien con la
aprobacion del usuario se corre con --zip.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ZIP_DEFLATED = zipfile.ZIP_DEFLATED

RELEASE_RULES = "release_rules.json"

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


def load_rules(project_root: Path) -> dict:
    rules_path = project_root / RELEASE_RULES
    if not rules_path.exists():
        print(f"ERROR: No se encuentra {RELEASE_RULES}")
        sys.exit(1)
    with open(rules_path, encoding="utf-8") as f:
        return json.load(f)


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


def _matches_any(path: str, globs) -> bool:
    import fnmatch

    norm = path.replace("\\", "/")
    for pattern in globs:
        if fnmatch.fnmatch(norm, pattern):
            return True
    return False


def add_directory_rules(zf, base: Path, folders_rules: dict):
    """Agrega carpetas al zip respetando folders.* de release_rules.json."""
    for folder_name, rules in folders_rules.items():
        folder_path = base / folder_name
        if not folder_path.exists():
            continue

        if folder_name == "runtime":
            _add_runtime(zf, folder_path, rules)
            continue

        allowed = rules.get("allowed_files", [])
        for file in allowed:
            file_path = folder_path / file
            if file_path.exists() and file_path.is_file():
                if not _matches_any(str(file_path.relative_to(base)), rules.get("always_exclude_globs", [])):
                    zf.write(file_path, file_path.relative_to(base).as_posix())


def _add_runtime(zf, runtime_dir: Path, rules: dict):
    """Copia runtime/ con allowlist: mpv y python_core completos, pero
    site-packages SOLO con los paquetes permitidos."""
    mpv_dir = runtime_dir / "mpv"
    if rules.get("mpv") == "include_all" and mpv_dir.exists():
        for root, _, files in os.walk(mpv_dir):
            for file in files:
                file_path = Path(root) / file
                if not _matches_any(str(file_path.relative_to(runtime_dir.parent)), rules.get("always_exclude_globs", [])):
                    zf.write(file_path, file_path.relative_to(runtime_dir.parent).as_posix())

    python_dir = runtime_dir / "python"
    python_core_files = set()
    if rules.get("python_core") == "include_all" and python_dir.exists():
        # python313._pth, python.exe, python313.dll, platlib, scripts, y dlls
        for item in python_dir.iterdir():
            if item.is_file():
                python_core_files.add(item.name)
        for name in ("python313._pth", "python.exe", "python313.dll", "python313.zip"):
            p = python_dir / name
            if p.exists():
                zf.write(p, p.relative_to(runtime_dir.parent).as_posix())
        for sub in ("platlib", "DLLs", "Lib", "Scripts"):
            sub_dir = python_dir / sub
            if sub_dir.exists():
                for root, _, files in os.walk(sub_dir):
                    for file in files:
                        file_path = Path(root) / file
                        rel = file_path.relative_to(runtime_dir.parent).as_posix()
                        if not _matches_any(rel, rules.get("always_exclude_globs", [])):
                            if "site-packages" not in rel:
                                zf.write(file_path, rel)
        # Agregar archivos sueltos de python_core (no-enum por nombre)
        for extra in python_core_files - {"python313._pth", "python.exe", "python313.dll", "python313.zip"}:
            p = python_dir / extra
            if p.is_file():
                rel = p.relative_to(runtime_dir.parent).as_posix()
                if not _matches_any(rel, rules.get("always_exclude_globs", [])):
                    zf.write(p, rel)

    site_packages = python_dir / "Lib" / "site-packages"
    if site_packages.exists():
        allow = set(rules.get("site_packages_allowlist", []))
        extra_loose = set(rules.get("site_packages_extra_loose_files", []))
        for item in site_packages.iterdir():
            name = item.name
            included = False
            if item.is_dir():
                base_name = name.split("-")[0].split(".")[0]
                included = base_name in allow
            else:
                included = name in allow or name in extra_loose
            if not included:
                continue
            if item.is_dir():
                for root, _, files in os.walk(item):
                    for file in files:
                        file_path = Path(root) / file
                        rel = file_path.relative_to(runtime_dir.parent).as_posix()
                        if not _matches_any(rel, rules.get("always_exclude_globs", [])):
                            zf.write(file_path, rel)
            else:
                zf.write(item, item.relative_to(runtime_dir.parent).as_posix())


def make_zip(project_root: Path, rules: dict, exe: Path) -> Path:
    version = rules.get("version", "0.0.0")
    zip_name = rules.get("zip_name_template", "yt-remote-{version}-portable.zip").format(version=version)
    zip_path = project_root / zip_name

    print(f"Creando {zip_name}...")
    exe_name = rules.get("exe_name", "ytremote.exe")
    with zipfile.ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        for top_file in rules.get("include_top_files", []):
            if top_file == exe_name:
                # El .exe recien compilado vive en dist/
                zf.write(exe, exe_name)
                continue
            file_path = project_root / top_file
            if file_path.exists() and file_path.is_file():
                zf.write(file_path, file_path.name)

        add_directory_rules(zf, project_root, rules.get("folders", {}))

    print(f"[OK] Release creado: {zip_path}")
    print(f"   Tamanho: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print("   Probalo antes de distribuir.")
    return zip_path


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
        make_zip(project_root, rules, exe)
    else:
        print("")
        print("Zip NO generado (por defecto). Cuando el .exe este probado:")
        print("   python build_exe.py --zip")


if __name__ == "__main__":
    main()