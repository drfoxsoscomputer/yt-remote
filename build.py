#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py — Empaquetado portable de YT-Remote (PyInstaller onedir)
─────────────────────────────────────────────────────────────────
Genera dist/ytremote/ytremote.exe con:
  - templates/ y static/ junto al exe (para Flask + pywebview)
  - ytremote.ico embebido + tray
  - splash.png nativo (si arranque > 1.5s)
  - ytremote.manifest con dpiAware (evita salto de re-escala)
  - runtime python portable en _internal/

Uso:
    python build.py          # solo compila el .exe
    python build.py --zip    # compila y arma ZIP portable (tras probar exe)
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
DIST_DIR = PROJECT_ROOT / "dist" / "ytremote"
BUILD_DIR = PROJECT_ROOT / "build"
SPEC_PATH = PROJECT_ROOT / "launcher.spec"

PYTHON = sys.executable

# Archivos/dirs que van JUNTO al exe (BASE_DIR portable los lee ahí)
DATAS = [
    ("templates", "templates"),
    ("static", "static"),
    ("ytremote.ico", "."),
    ("ytremote.manifest", "."),
    # WebView2 runtimes: necesarias para interop_dll_path() en modo frozen (PyInstaller)
    # Sin estos, el exe falla con "Main window failed to start" al crear la ventana WebView2
    ("D:\laragon\bin\python\python-3.13\Lib\site-packages\webview\lib\runtimes", "webview/lib/runtimes"),
]

# Hidden imports necesarios para el launcher web
HIDDEN_IMPORTS = [
    "tkinter", "tkinter.ttk",
    "pystray", "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageTk",
    "ctypes", "ctypes.wintypes",
    "webview", "flask", "flask.json", "werkzeug",
    "bot_process", "security", "config", "persistence", "queue_manager",
    "player", "bot", "roles", "search",
]

EXCLUDES = [
    "matplotlib", "numpy", "scipy", "pandas",
    "jupyter", "notebook", "IPython", "pytest",
]


def run_pyinstaller():
    """Corre PyInstaller onedir para main_launcher.py -> dist/ytremote/ytremote.exe."""
    # Limpiar builds anteriores
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d)

    cmd = [
        PYTHON, "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--clean",
        "--name", "ytremote",
        "--distpath", str(PROJECT_ROOT / "dist"),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--icon", str(PROJECT_ROOT / "ytremote.ico"),
        "--manifest", str(PROJECT_ROOT / "ytremote.manifest"),
        "--windowed",  # Sin consola (GUI)
        "--paths", str(SRC_DIR),  # Para que PyInstaller encuentre módulos en src/
    ]

    # Splash nativo (QUITADO: conflicto con WebView2)
    # No se usa splash; el HTML + JS propio maneja el tiempo de carga.

    for h in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", h]

    for src, dst in DATAS:
        src_path = PROJECT_ROOT / src
        if src_path.exists():
            cmd += ["--add-data", f"{src_path}{os.pathsep}{dst}"]
        else:
            print(f"ADVERTENCIA: {src_path} no existe, se omite en --add-data")

    cmd.append(str(PROJECT_ROOT / "src" / "main_launcher.py"))

    print(f"\n=== PyInstaller: ytremote (onedir) ===")
    print(f"Comando: {' '.join(cmd)}")

    # Reintento ante bloqueos de antivirus (como AlbionHelper)
    for intento in range(1, 4):
        resultado = subprocess.run(cmd, cwd=PROJECT_ROOT)
        if resultado.returncode == 0:
            break
        print(f"intento {intento} falló (exit {resultado.returncode}); "
              + ("reintento..." if intento < 3 else "abandonamos."))
        import time
        time.sleep(6)
    else:
        raise SystemExit("PyInstaller falló para ytremote tras 3 intentos")

    exe = DIST_DIR / "ytremote.exe"
    if not exe.exists():
        raise SystemExit(f"ERROR: No se generó {exe}")

    print(f"\n[OK] Ejecutable generado: {exe} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    return exe


def make_zip(exe: Path):
    """Arma ZIP portable con la carpeta completa dist/ytremote/."""
    version = "1.0.0"
    zip_name = f"yt-remote-{version}-portable.zip"
    zip_path = PROJECT_ROOT / zip_name

    print(f"\nCreando {zip_name}...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Recorrer toda la carpeta dist/ytremote/
        for root, _, files in os.walk(DIST_DIR):
            for file in files:
                file_path = Path(root) / file
                rel = file_path.relative_to(PROJECT_ROOT / "dist")
                zf.write(file_path, rel.as_posix())

    print(f"[OK] Release creado: {zip_path}")
    print(f"   Tamaño: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print("   Pruebalo antes de distribuir.")
    return zip_path


def main():
    exe = run_pyinstaller()

    if "--zip" in sys.argv:
        make_zip(exe)
    else:
        print("")
        print("Zip NO generado (por defecto). Cuando el .exe esté probado:")
        print("   python build.py --zip")


if __name__ == "__main__":
    main()