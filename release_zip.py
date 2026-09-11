#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
release_zip.py — Ensamblado compartido del ZIP portable de YT-Remote.

La lógica que arma el ZIP vive aquí para que ambos builders la reutilicen:
  - build_exe.py (onefile):          exe + runtime allowlist + src/ + docs
  - build.py (onedir, con _internal): _internal + exe + runtime allowlist + src/ + docs

Las reglas (version, allowlist de site-packages, include_top_files, folders)
son la fuente única y viven en release_rules.json. Sin la allowlist, el ZIP
cargaría pip, pytest, customtkinter y otra basura de desarrollo que el bot
no usa.
"""

import json
import os
import sys
import zipfile
from pathlib import Path

ZIP_DEFLATED = zipfile.ZIP_DEFLATED
RELEASE_RULES = "release_rules.json"


def load_rules(project_root: Path) -> dict:
    """Carga release_rules.json (fuente única de reglas del ZIP)."""
    rules_path = project_root / RELEASE_RULES
    if not rules_path.exists():
        print(f"ERROR: No se encuentra {RELEASE_RULES}")
        sys.exit(1)
    with open(rules_path, encoding="utf-8") as f:
        return json.load(f)


def _matches_any(path: str, globs) -> bool:
    import fnmatch

    norm = path.replace("\\", "/")
    for pattern in globs:
        if fnmatch.fnmatch(norm, pattern):
            return True
    return False


def _walk_tree(zf, tree: Path, base_rel: Path, skip_dirs=frozenset(), globs=()):
    """Zipea un árbol respetando exclusiones (rutas relativas a la raíz)."""
    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for file in files:
            file_path = Path(root) / file
            rel = file_path.relative_to(base_rel).as_posix()
            if _matches_any(rel, globs):
                continue
            zf.write(file_path, rel)


def _add_runtime(zf, runtime_dir: Path, rules: dict):
    """Copia runtime/ con allowlist: mpv y python_core completos, pero
    site-packages SOLO con los paquetes permitidos por release_rules.json."""
    always_exclude = rules.get("always_exclude_globs", [])
    rel_root = runtime_dir.parent

    mpv_dir = runtime_dir / "mpv"
    if rules.get("mpv") == "include_all" and mpv_dir.exists():
        _walk_tree(zf, mpv_dir, rel_root, globs=always_exclude)

    python_dir = runtime_dir / "python"
    if rules.get("python_core") == "include_all" and python_dir.exists():
        python_core_files = set()
        for item in python_dir.iterdir():
            if item.is_file():
                python_core_files.add(item.name)

        for name in ("python313._pth", "python.exe", "python313.dll", "python313.zip"):
            p = python_dir / name
            if p.exists():
                zf.write(p, p.relative_to(rel_root).as_posix())

        for sub in ("platlib", "DLLs", "Lib", "Scripts"):
            sub_dir = python_dir / sub
            if sub_dir.exists():
                for root, _, files in os.walk(sub_dir):
                    for file in files:
                        file_path = Path(root) / file
                        rel = file_path.relative_to(rel_root).as_posix()
                        if not _matches_any(rel, always_exclude) and "site-packages" not in rel:
                            zf.write(file_path, rel)

        for extra in python_core_files - {"python313._pth", "python.exe", "python313.dll", "python313.zip"}:
            p = python_dir / extra
            if p.is_file():
                rel = p.relative_to(rel_root).as_posix()
                if not _matches_any(rel, always_exclude):
                    zf.write(p, rel)

    site_packages = python_dir / "Lib" / "site-packages"
    if site_packages.exists():
        allow = set(rules.get("site_packages_allowlist", []))
        extra_loose = set(rules.get("site_packages_extra_loose_files", []))
        for item in site_packages.iterdir():
            name = item.name
            if item.is_dir():
                base_name = name.split("-")[0].split(".")[0]
                included = base_name in allow
            else:
                included = name in allow or name in extra_loose
            if not included:
                continue
            if item.is_dir():
                _walk_tree(zf, item, rel_root, globs=always_exclude)
            else:
                zf.write(item, item.relative_to(rel_root).as_posix())


def add_directory_rules(zf, base: Path, folders_rules: dict):
    """Agrega src/ y data/ respetando folders.* de release_rules.json."""
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


def make_release_zip(project_root: Path, rules: dict, dist_dir: Path | None = None, exe_file: Path | None = None) -> Path:
    """Arma el ZIP portable con el nombre de la versión de release_rules.json.

    dist_dir:  modo onedir (todo el árbol del bundle: exe + _internal; saltea data/).
    exe_file:  modo onefile (solo el ejecutable).
    En ambos casos se agregan runtime/ (allowlist), src/, data/ y los top_files
    (config.json, README.md, GUIA.txt).
    """
    if dist_dir is None and exe_file is None:
        raise ValueError("Se necesita dist_dir (onedir) o exe_file (onefile)")

    version = str(rules.get("version", "0.0.0"))
    zip_name = rules.get("zip_name_template", "yt-remote-{version}-portable.zip").format(version=version)
    zip_path = project_root / zip_name
    exe_name = rules.get("exe_name", "ytremote.exe")

    print(f"Creando {zip_name}...")
    with zipfile.ZipFile(zip_path, "w", ZIP_DEFLATED) as zf:
        if dist_dir is not None:
            _walk_tree(zf, dist_dir, dist_dir, skip_dirs={"data"})
        elif exe_file is not None:
            zf.write(exe_file, exe_name)

        for top_file in rules.get("include_top_files", []):
            if top_file == exe_name:
                continue  # ya viene del dist_dir (onedir) o del exe_file (onefile)
            file_path = project_root / top_file
            if file_path.exists() and file_path.is_file():
                zf.write(file_path, file_path.name)

        add_directory_rules(zf, project_root, rules.get("folders", {}))

    print(f"[OK] Release creado: {zip_path}")
    print(f"   Tamaño: {zip_path.stat().st_size / 1024 / 1024:.1f} MB")
    print("   Probalo antes de distribuir.")
    return zip_path