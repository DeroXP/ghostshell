# -*- mode: python ; coding: utf-8 -*-
# GhostShell PyInstaller Build Spec
# Usage: pyinstaller build.spec

import os

block_cipher = None
base_path = os.path.dirname(os.path.abspath(SPEC))

# v3.2 — bundle vgamepad's ViGEmClient.dll so the Gamepad Mapper's
# virtual-controller mode works inside the PyInstaller bundle.  We pull
# the package directory dynamically so the path is correct regardless of
# whether vgamepad is installed system-wide or in a venv.
_vgamepad_datas = []
try:
    import vgamepad as _vg
    _vg_dir = os.path.dirname(_vg.__file__)
    _vg_win = os.path.join(_vg_dir, 'win')
    if os.path.isdir(_vg_win):
        for _root, _dirs, _files in os.walk(_vg_win):
            for _f in _files:
                _full = os.path.join(_root, _f)
                _rel  = os.path.relpath(_full, _vg_dir)
                _vgamepad_datas.append((_full, os.path.join('vgamepad', os.path.dirname(_rel))))
except Exception:
    pass


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


# v3.2.2 — the standalone updater (GhostShellUpdater.exe) is NO LONGER
# bundled inside GhostShell.exe.  GhostShell auto-downloads it from the
# Railway release server on first run, into %APPDATA%/GhostShell/updater/.
# Why: lets the updater iterate independently of GhostShell's version,
# and keeps GhostShell.exe small.  See updater_helper.spec + core/updater.py
# (ensure_updater_installed_async).


a = Analysis(
    ['app.py'],
    pathex=[base_path],
    binaries=_pyaudio_binaries,
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
        ('assets', 'assets'),
        # v3.4 — bundle curated recoil presets file alongside its module
        ('core/recoil_presets.json', 'core'),
    ] + _vgamepad_datas,
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
        # v3.2 — virtual gamepad / ViGEmBus integration
        'vgamepad',
        # v3.6 — recoil pattern capture from gameplay
        # numpy: FFT-based phase correlation for camera-motion tracking
        # mss: fast multi-monitor screen capture (~5 ms/frame vs PIL ImageGrab's ~30-50 ms)
        'numpy',
        'numpy.fft',
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
    # v3.6 — numpy is needed for the recoil-capture feature.  pandas /
    # matplotlib stay excluded since we don't use them and they'd add
    # another ~150 MB.
    excludes=['tkinter', 'matplotlib', 'pandas'],
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
