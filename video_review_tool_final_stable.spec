# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

project_dir = Path(SPECPATH)
script_path = project_dir / "video_review_app_final_stable.py"
icon_path = project_dir / "app_logo.ico"
png_logo = project_dir / "app_logo.png"

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "imageio_ffmpeg",
]

datas = []
if png_logo.exists():
    datas.append((str(png_logo), "."))

datas += collect_data_files("imageio_ffmpeg")
binaries = collect_dynamic_libs("PySide6") + collect_dynamic_libs("imageio_ffmpeg")

a = Analysis(
    [str(script_path)],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="视频审核工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="视频审核工具稳定版",
)
