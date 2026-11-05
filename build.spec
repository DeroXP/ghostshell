# -*- mode: python ; coding: utf-8 -*-
# Vispora PyInstaller Build Spec
# Usage: pyinstaller build.spec

import os

block_cipher = None
base_path = os.path.dirname(os.path.abspath(SPEC))

# v3.3.0-beta.6 — bundle PyAudioWPatch's compiled extension + the
# PortAudio DLL.  Without this, the system-audio WASAPI loopback path
# falls back to "PyAudioWPatch not installed" and clips are silent for
# anyone who doesn't have a third-party loopback driver.
#
# PyInstaller's auto-detection finds the .pyd but on Windows misses
# the bundled portaudio_x64.dll (sometimes named portaudio.dll inside
# the package).  We sweep the package dir and grab every .pyd / .dll
# explicitly so it lands in the runtime binaries section.
_pyaudio_binaries = []
try:
    import pyaudiowpatch as _paw
    _paw_dir = os.path.dirname(_paw.__file__)
    for _root, _dirs, _files in os.walk(_paw_dir):
        for _f in _files:
            if _f.lower().endswith(('.dll', '.pyd')):
                _full = os.path.join(_root, _f)
                # Place at the same relative path under the bundled
                # pyaudiowpatch package so its dlopen finds them.
                _rel = os.path.relpath(_full, _paw_dir)
                _dest = os.path.join('pyaudiowpatch', os.path.dirname(_rel)) or 'pyaudiowpatch'
                _pyaudio_binaries.append((_full, _dest))
except Exception:
    pass


# Say what is missing.  The blocks above fail silently by design (the
# features are optional at run time), which is the wrong default for a build
# that is about to be handed to other people.  (PyAudioWPatch's compiled half
# is a top-level module in current releases, which PyInstaller finds on its
# own — so an empty sweep above does not mean it is absent.)
import importlib.util as _ilu
_optional = ("pyaudiowpatch", "mss", "obsws_python", "websocket", "pystray", "psutil")
_missing = [m for m in _optional if _ilu.find_spec(m) is None]
print(f"build.spec: optional packages missing on this machine: {', '.join(_missing) or 'none'}")
if _missing:
    print("build.spec: WARNING - the features that need them will not work in this build")


# v3.2.2 — the standalone updater (VisporaUpdater.exe) is NO LONGER
# bundled inside Vispora.exe.  Vispora auto-downloads it from the
# Railway release server on first run, into %APPDATA%/Vispora/updater/.
# Why: lets the updater iterate independently of Vispora's version,
# and keeps Vispora.exe small.  See updater_helper.spec + core/updater.py
# (ensure_updater_installed_async).


a = Analysis(
    ['app.py'],
    pathex=[base_path],
    binaries=_pyaudio_binaries,
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('assets', 'assets'),
    ],
    hiddenimports=[
        'flask',
        'cryptography',
        'cryptography.fernet',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.primitives.kdf.pbkdf2',
        'cryptography.hazmat.backends',
        'webview',
        'sqlite3',
        # v3 — system tray support
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        # mss: fast multi-monitor screen capture for screenshots
        # (~5 ms/frame vs PIL ImageGrab's ~30-50 ms)
        'mss',
        'mss.windows',
        # v3.3.0-beta.6 — WASAPI loopback bridge for native system-audio
        # capture (no third-party loopback driver required)
        'pyaudiowpatch',
        # v3.3.0-beta.15+ — OBS Studio backend (alternative to FFmpeg).
        # Bundled now so beta.16's WebSocket wiring lands with no
        # build-spec changes.
        'obsws_python',
        'websocket',          # websocket-client; obsws_python's transport
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pandas / matplotlib stay excluded: nothing uses them and they'd add
    # another ~150 MB.  numpy too: nothing in Vispora uses it, but Pillow's
    # type hints (PIL._typing) name it, which would pull the whole package in.
    excludes=['tkinter', 'matplotlib', 'pandas', 'numpy'],
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
    name='Vispora',
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
    uac_admin=True,
    icon=os.path.join(base_path, 'assets', 'vispora.ico'),
)
