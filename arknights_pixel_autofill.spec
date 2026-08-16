# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

asset_datas = [('arknights_pixel.ico', '.')]
transparent_logo = Path('assets/tracer_logo_transparent.png')
if transparent_logo.is_file():
    asset_datas.append((str(transparent_logo), 'assets'))

a = Analysis(
    ['arknights_pixel_qt.py'],
    pathex=[],
    binaries=[],
    datas=asset_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow can optionally integrate with NumPy, which is unused here.
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
    name='Arknights-Pixel-Autofill-v1.4.0',
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
    uac_admin=True,
)
