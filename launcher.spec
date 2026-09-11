# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['src/launcher.py'],
    pathex=[r'D:\laragon\www\yt-remote', r'D:\laragon\www\yt-remote\src'],
    binaries=[],
    datas=[],
    hiddenimports=[
        'tkinter', 'tkinter.ttk', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageTk', 'ctypes', 'ctypes.wintypes'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'scipy', 'pandas', 'jupyter', 'notebook', 'IPython', 'pytest'
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
    icon=r'D:\laragon\www\yt-remote\ytremote.ico',  # Icono del .exe (ytremote.ico)
)
