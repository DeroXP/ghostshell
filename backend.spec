# -*- mode: python ; coding: utf-8 -*-
# Vispora as the shell's backend: a folder of files, not one exe.
#
# The shell (shell_rs/, the Rust program that is to become Vispora.exe)
# carries the app and starts it as its backend.  What it carries is made
# here: the same app as build.spec makes, with the same files in it, as a
# folder with VisporaBackend.exe in it.  No release uses this yet; build.spec
# and what a release carries are as they were.
#
# Usage.  Never into the repository's dist/ or build/: the installed app runs
# from dist/.  Both folders are named, outside the repository:
#
#     pyinstaller backend.spec --noconfirm --distpath <out>\dist --workpath <out>\work
#
# and the folder <out>\dist\VisporaBackend is then packed into the shell
# (shell_rs/README.md, "One file").
#
# Why a folder and not one exe: an exe that carries its own files unpacks
# them into the person's temporary folder at every start, which a program
# without administrator rights can write to, and it runs as two processes.
# The shell unpacks the folder itself, into a folder of its own making that
# only administrators can write to, and starts one process.
#
# What goes into it is decided in build.spec and nowhere else: everything
# there up to and including the Analysis is run here as it stands, so the
# two builds cannot drift apart.  tests/test_one_file.py holds the rest of
# this file against build.spec.

import os

_here = os.path.dirname(os.path.abspath(SPEC))
with open(os.path.join(_here, 'build.spec'), encoding='utf-8') as _f:
    _the_apps_own = _f.read()
_until = _the_apps_own.index('\npyz = PYZ(')
exec(compile(_the_apps_own[:_until], os.path.join(_here, 'build.spec'), 'exec'))    # leaves the Analysis in `a`

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VisporaBackend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Not squeezed here: the shell squeezes the whole folder when it packs
    # it, and a squeezed DLL takes longer to load at every start.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # It asks for nothing: the shell that starts it has administrator
    # rights, and what it starts has them too.
    uac_admin=False,
    icon=os.path.join(base_path, 'assets', 'vispora.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VisporaBackend',
)
