# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

asset_datas = [('assets', 'assets')] if Path('assets').is_dir() else []

a = Analysis(
    ['arknights_pixel_autofill.py'],
    pathex=[],
    binaries=[],
    datas=asset_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow can optionally integrate with NumPy, but this application does not
    # use that path.  Excluding it keeps the executable smaller and avoids
    # collecting unrelated scientific packages from a system Python install.
    excludes=['numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Arknights-Pixel-Autofill',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['arknights_pixel.ico'],
)
