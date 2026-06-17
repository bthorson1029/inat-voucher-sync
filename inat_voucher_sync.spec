# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for a standalone Windows build of Voucher Sync.

Build locally:
    pip install -r requirements.txt pyinstaller
    pyinstaller --noconfirm inat_voucher_sync.spec

Produces a single windowed executable at dist/VoucherSync.exe with the OCR
engine (RapidOCR + its ONNX models and the onnxruntime native libraries)
bundled — so end users need no Python, no pip, and no Tesseract.
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Bundle the OCR engine, its ONNX model/config data files, and the onnxruntime
# native runtime so OCR works offline with no install step. collect_all pulls
# data + binaries + submodules for each package (the lazy `from
# rapidocr_onnxruntime import RapidOCR` inside the app wouldn't be enough on
# its own to drag in the model files).
for pkg in ("rapidocr_onnxruntime", "onnxruntime"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["inat_voucher_sync.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VoucherSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
