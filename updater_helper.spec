# -*- mode: python ; coding: utf-8 -*-
# GhostShell Updater build spec — produces dist/GhostShellUpdater.exe
#
# This is the STANDALONE updater app:
#   • Flask + pywebview backend (server.py)
#   • Install pipeline (installer.py)
#   • UI assets (templates/, static/)
#
# Hosted on the Railway release server at /download/updater.  GhostShell
# auto-fetches it on first run to %APPDATA%/GhostShell/updater/.
#
# Usage:  pyinstaller updater_helper.spec --noconfirm
import os

block_cipher = None
base_path = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(base_path, 'updater', 'main.py')],
    pathex=[base_path],
    binaries=[],
    datas=[
        # Ship UI assets alongside the binary.  PyInstaller extracts
        # them to sys._MEIPASS/updater/* at runtime; server.py's
        # _resource_dir() finds them there.
        (os.path.join(base_path, 'updater', 'templates'), 'updater/templates'),
        (os.path.join(base_path, 'updater', 'static'),    'updater/static'),
    ],
    hiddenimports=[
        'flask',
        'webview',
        'pystray',
        # Sub-modules the Flask + pywebview combo lazy-imports
        'webview._common',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Strip absolutely every heavyweight we don't need
    excludes=[
        'tkinter', 'matplotlib', 'pandas', 'numpy', 'mss',
        'vgamepad', 'cryptography', 'sqlite3', 'PIL.ImageDraw',
        'PIL.ImageFont',
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
    name='GhostShellUpdater',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # we have our own window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # uac_admin so file copies into Program Files work without re-prompt
    uac_admin=True,
    icon=os.path.join(base_path, 'assets', 'ghostshell.ico'),
)
