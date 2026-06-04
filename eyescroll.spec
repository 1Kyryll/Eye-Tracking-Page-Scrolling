# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for Eye Scroll (one-file; macOS wraps it as a .app).
# Build:  pyinstaller eyescroll.spec
# The build step must place `face_landmarker.task` next to this spec first
# (the CI workflow downloads it).

import os
import sys
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("mediapipe", "cv2", "pyautogui"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Bundle the model so the app works offline on first launch.
_model = os.path.join(os.getcwd(), "face_landmarker.task")
if os.path.exists(_model):
    datas.append((_model, "."))

# Make sure the right pystray backend is pulled in for this OS.
if sys.platform.startswith("win"):
    hiddenimports += ["pystray._win32"]
elif sys.platform == "darwin":
    hiddenimports += ["pystray._darwin"]
else:
    hiddenimports += ["pystray._xorg"]

a = Analysis(
    ["eyescroll_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
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
    name="EyeScroll",
    debug=False,
    strip=False,
    upx=False,
    console=False,            # no terminal window — it's a tray app
    disable_windowed_traceback=False,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="EyeScroll.app",
        icon=None,
        bundle_identifier="com.eyescroll.app",
        info_plist={
            "NSCameraUsageDescription":
                "Eye Scroll uses the camera to track your gaze for scrolling.",
            "LSUIElement": True,   # agent app: no Dock icon, tray only
            "CFBundleName": "EyeScroll",
        },
    )
