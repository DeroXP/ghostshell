"""One file: the shell (shell_rs/) carries the app as its backend.

The exe that is to become Vispora.exe is the shell, and carries the app in
itself: built as a folder (backend.spec), made into one piece of bytes (a
pack), unpacked by the shell before it starts it.  No release is built this
way yet; build.spec and what a release carries are as they were.

What is checked here is the app's and the build's side, and what the two
sides have to agree on:

  * backend.spec builds what build.spec builds, as a folder, and build.spec
    is as it was;
  * the pack tool makes the same pack of the same folder, and says what is
    in it (for real, where the tool has been built);
  * an update is for the shell's exe when the app runs behind the shell,
    and for its own exe when it is started as it always was;
  * where the backend is unpacked, and which folder is used: read off the
    shell's source, for what the Rust tests and the trial then try for real
    (shell_rs/core: cargo test; shell_rs/app/try_it.py).

Nothing is unpacked into a real place here, no task is made, and the app's
real exe is never built into the repository or started.
"""
import sandbox as m

import inspect
import json
import os
import re
import subprocess
import sys
import unittest

import test_shell_rs as S                           # (its helpers: where the trial tool is, the files of the two crates)

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
    from core import updater as U
    import test_robustness_updates as R             # (its stand-in for the app's side of an install)
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)


def app():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A


# ═════════════════════════════════════════════════════════════════════
# What is built
# ═════════════════════════════════════════════════════════════════════
def test_the_backend_is_built_from_what_the_app_is_built_from_as_a_folder():
    ours, theirs = S.repo("backend.spec"), S.repo("build.spec")
    # What goes into it is decided in build.spec and nowhere else: all of
    # build.spec up to its Analysis is run as it stands.
    assert "_until = _the_apps_own.index('\\npyz = PYZ(')" in ours
    assert "exec(compile(_the_apps_own[:_until], os.path.join(_here, 'build.spec'), 'exec'))" in ours
    assert theirs.count("\npyz = PYZ(") == 1 and theirs.index("a = Analysis(") < theirs.index("\npyz = PYZ(")
    assert "Analysis(" not in ours.replace("the Analysis", ""), "a list of its own of what goes in"
    # A folder, not one exe: the program without its files, and the files beside it.
    exe = ours[ours.index("exe = EXE("):ours.index("coll = COLLECT(")]
    assert "exclude_binaries=True," in exe and "a.binaries" not in exe and "a.datas" not in exe
    coll = ours[ours.index("coll = COLLECT("):]
    for part in ("exe,", "a.binaries,", "a.zipfiles,", "a.datas,", "name='VisporaBackend',"):
        assert part in coll, part
    # Not named after the app, asks for no rights of its own, not squeezed twice.
    assert "name='VisporaBackend'," in exe and "uac_admin=False," in exe and "upx=False," in exe and "console=False," in exe
    # Never into the repository's dist/ or build/.
    assert "--distpath <out>\\dist --workpath <out>\\work" in ours
    # build.spec is as it was: one exe, named after the app, that asks for rights.
    released = theirs[theirs.index("exe = EXE("):]
    for part in ("a.binaries,", "a.datas,", "name='Vispora',", "uac_admin=True,", "runtime_tmpdir=None,"):
        assert part in released, part
    assert "COLLECT(" not in theirs and "VisporaBackend" not in theirs and "backend.spec" not in theirs


def a_made_up_backend(name, more=b""):
    folder = os.path.join(m.ROOT, "one-file", name, "VisporaBackend")
    os.makedirs(os.path.join(folder, "_internal"), exist_ok=True)
    for path, holds in (("VisporaBackend.exe", b"MZ not really a program" + more),
                        (os.path.join("_internal", "python312.dll"), b"a library. " * 9000),
                        (os.path.join("_internal", "empty.txt"), b"")):
        with open(os.path.join(folder, path), "wb") as f:
            f.write(holds)
    return folder


def pack_tool(*arguments):
    out = m.REAL_POPEN([S.TRY, *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    said, complaint = out.communicate(timeout=60)
    return out.returncode, said, complaint


def test_the_pack_tool_makes_the_same_pack_of_the_same_folder_and_says_what_is_in_it():
    app()
    if not S.TRY:
        return
    import config
    folder = a_made_up_backend("tool")
    first, second = os.path.join(m.ROOT, "one-file", "first.pack"), os.path.join(m.ROOT, "one-file", "second.pack")
    code, said, complaint = pack_tool("--pack", folder, first, "lzms")
    assert code == 0, complaint
    says = json.loads(said)
    assert (says["version"], says["start"], says["files"], says["bytes"]) == (config.APP_VERSION, "VisporaBackend.exe", 3, 23 + 99000)
    assert says["packed"] == os.path.getsize(first) < says["bytes"] // 5
    assert says["kept_as"] == f"Vispora-backend-kept-{config.APP_VERSION}-{says['id'][:16]}" and len(says["id"]) == 64
    with open(first, "rb") as f:
        assert f.read(8) == b"VSPPACK1"
    # The same folder, the same pack: what a folder that stays is named after.
    assert pack_tool("--pack", folder, second, "lzms")[0] == 0
    with open(first, "rb") as a, open(second, "rb") as b:
        assert a.read() == b.read()
    assert json.loads(pack_tool("--pack-says", first)[1]) == says
    # Another program in it: another pack, and another name for its folder.
    other = os.path.join(m.ROOT, "one-file", "other.pack")
    assert pack_tool("--pack", a_made_up_backend("tool-other", b"!"), other, "lzms")[0] == 0
    assert json.loads(pack_tool("--pack-says", other)[1])["kept_as"] != says["kept_as"]
    # What is not there to be packed, and what is not a pack, is said and not guessed at.
    code, said, complaint = pack_tool("--pack", os.path.join(m.ROOT, "one-file", "tool", "VisporaBackend", "_internal"), second, "lzms")
    assert code == 1 and "has no VisporaBackend.exe to start" in complaint, complaint
    assert pack_tool("--pack", folder, second, "zip")[0] == 2
    with open(second, "wb") as f:
        f.write(b"VSPPACK1 and then something else")
    code, said, complaint = pack_tool("--pack-says", second)
    assert code == 1 and "cannot be read" in complaint, complaint


# ═════════════════════════════════════════════════════════════════════
# Where it is unpacked, and which folder is used
# ═════════════════════════════════════════════════════════════════════
def test_the_exe_carries_its_backend_and_one_switch_says_how_it_is_kept():
    main, build, manifest = S.app_crate("src", "main.rs"), S.app_crate("build.rs"), S.app_crate("Cargo.toml")
    # Carried in the exe itself: read from the program as it was started,
    # not from a file beside it or from the exe's file on disk.
    assert 'static CARRIED: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/backend.pack"));' in main
    assert 'std::env::var("VISPORA_BACKEND_PACK")' in build
    assert "current_exe().ok().and_then(|exe| exe.parent()" not in main, "the backend is not looked for beside the exe any more"
    # A build without one has nothing to start, and says so.
    assert "if trial.is_none() && carried.is_empty() {" in main and "Vispora's files are incomplete" in main
    # One switch, off until the owner has chosen.
    assert 'const KEEPS_ITS_BACKEND: bool = cfg!(feature = "keep-backend");' in main
    assert re.search(r'^keep-backend = \[\]$', manifest, re.M) and re.search(r'^default = \["elevated"\]$', manifest, re.M)
    assert "let way = if keeps { backend::Way::OncePerVersion } else { backend::Way::EveryStart };" in main
    # Unpacked on the thread that starts the backend, after the window is
    # made: the window does not wait for it.
    run = main[main.index("fn run("):]
    made = run.index("the_backend_made_ready(&shell, carried, places, keeps,")
    assert run.index("let own_folder = win::private_folder(") < made < run.index("WebviewWindowBuilder::new(") < run.index("shell.start();")
    hook = main[main.index("fn the_backend_made_ready("):main.index("/// Open the window, start the backend")]
    assert "*shell.prepare_hook.lock().unwrap_or_else(|p| p.into_inner()) = Some(Arc::new(move || {" in hook
    shell = S.rust("src", "shell.rs")
    keep = shell[shell.index("    fn keep(self: &Arc<Self>) {"):shell.index("    /// Find out how the backend is started")]
    assert keep.index("if !self.prepared() {") < keep.index("self.start_backend()")
    prepared = shell[shell.index("    fn prepared(&self) -> bool {"):shell.index("    /// The backend will not be started again.")]
    assert prepared.index("st.launching = true;") < prepared.index("let made = prepare();") < prepared.index("st.launching = false;"), \
        "a quit waits for what is being unpacked"
    # In a trial the stand-in is started whatever was unpacked: nothing real runs.
    assert "Ok(if stand_in { start_as.clone() } else { Command { program: ready.program, ..start_as.clone() } })" in hook
    # What cannot be unpacked is said in words of its own, and the app ends as one that did not start.
    assert "Trouble::Backend(why.clone()).words()" in hook and 'shell.quit("the background program could not be made ready");' in hook
    assert "Ok(if never_started { 1 } else if code != 0 { code } else { quit })" in run


def test_the_backend_is_unpacked_only_where_a_program_without_rights_cannot_write():
    main, backend, win, pack = (S.app_crate("src", "main.rs"), S.rust("src", "backend.rs"), S.rust("src", "win.rs"), S.rust("src", "pack.rs"))
    run = main[main.index("fn run("):]
    # The places: for one run where the window's folder is made (Windows'
    # own Temp folder in the app as it ships); to stay, the folder for all
    # users' data.  Both found without the environment.  A trial: its own.
    assert "for_one_run: folder_in.clone()," in run
    assert "kept: if ASKS_FOR_RIGHTS { win::all_users_folder() } else { Some(folder_in.clone()) }," in run
    assert "open_to," in run and "(place, win::OpenTo::Administrators)" in run
    where = win[win.index("pub fn all_users_folder("):win.index("// The window's own folder, one per run")]
    assert "let windows = windows_folder().ok()?;" in where and 'format!("{drive}ProgramData")' in where
    assert "found.is_dir() && found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT == 0" in where
    for word in ("std::env::var", "temp_dir()", "SHGetFolderPath", "SHGetKnownFolderPath", "GetTempPath", "APPDATA", "TEMP"):
        for name, source in (("backend.rs", backend[:backend.index("#[cfg(test)]")]), ("pack.rs", pack[:pack.index("#[cfg(test)]")]), ("where", where)):
            assert word not in source, f"{name}: {word}"
    # For one run: a folder of this run's own making, marked as in use, like the window's.
    ways = backend[backend.index("pub fn make_ready("):backend.index("/// The folder that stays, checked or made, and held under its name.")]
    assert "win::private_folder_named(&places.for_one_run, win::BACKEND_FOLDER_PREFIX, places.open_to)?;" in ways
    assert 'pub const BACKEND_FOLDER_PREFIX: &str = "Vispora-backend-run-";' in win
    assert "const ONE_RUNS_FOLDERS: [&str; 2] = [WINDOW_FOLDER_PREFIX, BACKEND_FOLDER_PREFIX];" in win, \
        "what a run that was ended left behind is taken away at the next start, the backend's folder with the window's"
    # To stay: used only if it is this program's own making (owner and
    # access list, exactly) and holds exactly what the exe carries; asked
    # in that order, and of a folder that is held from then on, so that
    # what is under the name cannot be exchanged between the check and the
    # start.
    kept = backend[backend.index("fn kept("):backend.index("/// Take away the folders that stay")]
    check = kept[kept.index("let check = |folder: &str| -> Result<std::fs::File, String> {"):kept.index("if std::fs::symlink_metadata(&folder).is_ok() {")]
    assert check.index("let held = win::hold_own_folder(folder, open_to)?;") < check.index("pack.same_as(folder, &[])?;") < check.index("Ok(held)")
    # Every way a folder that stays comes to be used: one that was there
    # (held, then held against the exe), and by the check above one that
    # another start made meanwhile and one that was just made here.
    found = kept[kept.index("if std::fs::symlink_metadata(&folder).is_ok() {"):kept.index("// Made under another name")]
    assert found.index("let held = win::hold_own_folder(&folder, open_to)?;") < found.index("match pack.same_as(&folder, &[]) {") \
        < found.index("return Ok((folder, held));")
    assert kept.count("let held = check(&folder)?;") == 2 and kept.count("Ok((folder, held))") == 3
    assert kept.count("hold_own_folder(") == 2, "a folder is held somewhere without being held against the exe"
    assert kept.index("win::move_without_replacing(&making, &folder)") < kept.rindex("let held = check(&folder)?;")
    held = win[win.index("pub fn hold_own_folder("):win.index("/// What became of asking for a folder in order to take it away.")]
    # Held: opened as what is under the name itself (a link as the link it
    # is), so that nobody can remove or rename it, and then asked whose it
    # is by what was opened, not by its name again.
    assert "let read_and_write_but_not_remove = 1 | 2;" in held and ".share_mode(read_and_write_but_not_remove).custom_flags(AS_IT_IS)" in held
    assert "const AS_IT_IS: u32 = FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT;" in win
    assert held.index(".open(path)") < held.index("own_folder_behind(&held, path, open_to)?;")
    behind = win[win.index("fn own_folder_behind("):win.index("/// Hold the folder `path` under its name")]
    assert "let found = held.metadata()" in behind and "is_of_this_programs_making(&described_held(held, path)?, open_to, path)" in behind
    assert "GetSecurityInfo(held.as_raw_handle() as *mut c_void, SE_FILE_OBJECT, OWNER_AND_ACCESS_LIST," in win
    # ... and kept until the app ends: every start of the backend, also one
    # after a backend that died, is from the folder that was checked.
    assert "*slot.kept_held.lock().unwrap_or_else(|p| p.into_inner()) = ready.held;" in main
    assert "kept_held.lock().unwrap_or_else(|p| p.into_inner()).take()" not in main, "the folder is let go of before the app ends"
    # One that does not pass is never used and never put right in place.
    # Somebody else's: the error goes up before anything in it is looked
    # at, and the backend is unpacked for this run instead.  The shell's
    # own that is not what the exe carries: let go of, taken away whole if
    # no copy is using it (asked of the folder itself), and made again.
    assert 'said.push(format!("the folder that stays is not used: {why}. Unpacked for this run instead."))' in ways
    again = found[found.index("Err(why) => {"):]
    assert again.index("drop(held);") < again.index("take_away(&folder, open_to)") and "taken away to be made again" in again
    for word in ("remove_dir_all(&folder)", "remove_file", "set_permissions", "icacls", "unpack_into(&folder)"):
        assert word not in kept, f"a folder under the name is touched in place: {word}"
    taken = backend[backend.index("fn take_away("):backend.index("/// Was this made so long ago")]
    asked = taken.index("match win::open_to_remove(path, open_to) {")
    assert asked < taken.index("win::programs_let_go(std::path::Path::new(path))") < taken.index("std::fs::remove_dir_all(path)") < taken.index("drop(going);")
    assert "win::ToRemove::NotOurs => Err(NotTaken::NotOurs)," in taken and "win::ToRemove::InUse => Err(NotTaken::InUse)," in taken
    assert backend[:backend.index("#[cfg(test)]")].count("std::fs::remove_dir_all(") == 2, "a folder is taken away in a place of its own"
    # Made whole under another name, and never given a name that is taken.
    assert 'let making = format!("{folder}.{}{BEING_MADE}"' in kept and "win::move_without_replacing(&making, &folder)" in kept
    assert 'const BEING_MADE: &str = ".making";' in backend
    assert "let replacing_nothing = 0;" in win
    # This pack's own folder is never among those taken away, used or not.
    assert "keeps.then(|| backend::kept_name(&pack))" in main
    # Every file comes out with its length and its mark, or not at all.
    out = pack[pack.index("    pub fn bytes_of("):pack.index("    /// Unpack everything into `folder`")]
    assert "if data.len() != file.size || win::sha256_hex(&data) != file.mark {" in out
    # Squeezing is Windows' own, taken from Windows' own folder and from nowhere else.
    assert "LoadLibraryExW(name.as_ptr(), std::ptr::null_mut(), LOAD_LIBRARY_SEARCH_SYSTEM32)" in win
    assert '#[link(name = "cabinet")]' not in win and "const LOAD_LIBRARY_SEARCH_SYSTEM32: u32 = 0x800;" in win
    # The trial tries all of it for real.
    trial = S.app_crate("try_it.py")
    for said in ("(unpacked at every start)", "(unpacked once per version)", "is taken away whole and made again",
                 "that somebody else made is not used, though the right files are in it", "takes the other one's away",
                 "A run that Windows ended leaves its folder", "its folder cannot be given another name",
                 "meanwhile makes its own folder and leaves that one as it is", "that somebody else made is not taken away either",
                 "a second copy unpacks for its run instead", "while a program still runs in that folder leaves the folder whole",
                 "that somebody else made was not touched, and not used"):
        assert said in trial, said


# ═════════════════════════════════════════════════════════════════════
# An update is for the exe a person has
# ═════════════════════════════════════════════════════════════════════
def test_started_as_it_always_was_an_update_is_for_the_apps_own_exe():
    app()
    assert U._app_exe is None
    assert U._the_apps_exe() == sys.executable
    sys.frozen = True
    try:
        info = U.write_install_info()
        assert (info["install_path"], info["install_dir"]) == (sys.executable, os.path.dirname(sys.executable))
    finally:
        del sys.frozen
    info = U.write_install_info()
    assert info["install_path"] == os.path.abspath(sys.argv[0]), "from source, as before"
    # Nothing but the shell's hand-over names another exe.
    assert S.repo("app.py").count("updater.use_app_exe(") == 1
    assert 'updater.use_app_exe(handoff.get("shell_exe"))' in inspect.getsource(A._serve_for_the_shell)
    assert "use_app_exe" not in inspect.getsource(A.main)


def test_behind_the_shell_an_update_is_for_the_shell():
    """Behind the shell this process is the app's backend, unpacked somewhere
    by the shell.  Swapping its own exe would change a file the shell
    unpacks again anyway, and leave the app as it was.  The exe a person has
    is the shell: that is what the updater is told about, what is swapped,
    and what is started again."""
    app()
    shell = os.path.join(m.ROOT, "Downloads", "Vispora.exe")
    backend = sys.executable                        # (where this process's own exe is: nothing of it may show up)
    os.makedirs(os.path.dirname(shell), exist_ok=True)
    try:
        U.use_app_exe(shell)
        assert U._the_apps_exe() == shell
        # What the updater that is started by hand reads.
        for frozen in (False, True):
            if frozen:
                sys.frozen = True
            try:
                info = U.write_install_info()
            finally:
                if frozen:
                    del sys.frozen
            assert (info["install_path"], info["install_dir"]) == (shell, os.path.dirname(shell)), frozen
        with open(U.INSTALL_INFO_PATH, encoding="utf-8") as f:
            assert json.load(f)["install_path"] == shell
        # What the updater the app starts is told.
        with R._app_side({"body": R.UPDATER_BUILD}) as s:
            with open(U.UPDATER_EXE, "wb") as f:
                f.write(R.UPDATER_BUILD)
            del s["spawned"][:]
            assert U.install_update_now(force=True)["ok"]
            told = s["spawned"][-1][0]
            assert told[told.index("--target-exe") + 1] == shell and told[told.index("--target-dir") + 1] == os.path.dirname(shell)
            assert backend not in told
        # The swap without the updater (an update from a folder on this PC).
        os.makedirs(U.UPDATE_DIR, exist_ok=True)
        new_exe = os.path.join(U.UPDATE_DIR, "Vispora_9.9.9.exe")
        U._apply_exe_update_via_bat(new_exe, U._the_apps_exe())
        with open(os.path.join(U.UPDATE_DIR, "update.bat"), "rb") as f:
            script = f.read().decode("oem")
        assert f'copy /y "{new_exe}" "{shell}"' in script and f'start "" "{shell}"' in script
        # It waits for this process, and then for every process of the
        # shell's name: the shell goes when its backend has gone.
        assert f'Wait-Process -Id {os.getpid()}' in script and 'tasklist /FI "IMAGENAME eq Vispora.exe"' in script
        assert backend not in script
        swap = inspect.getsource(U.apply_update) if hasattr(U, "apply_update") else S.repo("core", "updater.py")
        assert "_apply_exe_update_via_bat(path, _the_apps_exe())" in swap and "current_exe = _the_apps_exe()" in S.repo("core", "updater.py")
        assert S.repo("core", "updater.py").count("sys.executable") == 2, "the app's exe is asked for in one place"
    finally:
        U.use_app_exe(None)
    assert U._the_apps_exe() == sys.executable


def test_an_update_leaves_no_unpacked_backend_of_the_version_before():
    """The updater swaps one exe.  What the version before unpacked is the
    next start's to take away, whichever way it was kept: a folder for one
    run that a run left behind (ended for the update, its mark held by
    nobody), and a folder that stayed (another version's, or any at all for
    a build that keeps none).  Both are tried for real in the Rust tests and
    in the trial; here, that the shell does it at every start."""
    main, backend, win = S.app_crate("src", "main.rs"), S.rust("src", "backend.rs"), S.rust("src", "win.rs")
    run = main[main.index("fn run("):]
    assert "let tidied = win::remove_private_folders(&tidy_in, open_to);" in run
    hook = main[main.index("fn the_backend_made_ready("):main.index("/// Open the window, start the backend")]
    assert "backend::remove_other_kept(&kept_in, but.as_deref(), open_to);" in hook
    assert "if let Some(kept_in) = places.kept.clone() {" in hook, "also by a build that keeps none"
    others = backend[backend.index("pub fn remove_other_kept("):backend.index("/// Why a folder was not taken away.")]
    # Only a folder of this program's own making, and only one that no copy
    # of the app (of the version before, in another session) is using.  A
    # copy holds its folder, and a folder that is held cannot be asked for;
    # a folder that somebody else made under the name is never touched.
    # (take_away, read above, is the one place a folder that stays goes.)
    asked = others.index("match take_away(&entry.path().to_string_lossy(), open_to) {")
    assert "Err(NotTaken::NotOurs) => others.not_ours += 1," in others and "Err(NotTaken::InUse) => others.in_use += 1," in others
    assert "std::fs::remove_" not in others, "something is taken away that was not asked for first"
    # One that a copy in another session may be unpacking into right now is
    # not asked for at all, until it is too old to be that.
    being_made = others.index("if name.ends_with(BEING_MADE) && !made_long_ago(&entry) {")
    assert being_made < asked and "is_some_and(|age| age >= win::MARK_EXPECTED_WITHIN)" in backend
    # One run's folders: the same rule.  Whose a folder is comes first,
    # asked of the folder once it is open; in somebody else's nothing is
    # opened, the mark included; in one of this program's own the mark,
    # then its programs (a backend that outlived its shell cannot be
    # deleted, and its folder is then left whole), then the rest.
    tidy = win[win.index("pub fn remove_private_folders("):win.index("/// Open what is under the mark's name in `folder`")]
    opened = tidy.index("match open_to_remove(&path.to_string_lossy(), open_to) {")
    assert opened < tidy.index("if left_behind(&path, &found) && programs_let_go(&path) && std::fs::remove_dir_all(&path).is_ok() {") < tidy.index("drop(going);")
    assert "ToRemove::NotOurs => tidied.not_ours += 1," in tidy and "ToRemove::InUse => tidied.left += 1," in tidy
    assert tidy.count("left_behind(") == 1 and win[:win.index("#[cfg(test)]")].count("left_behind(") == 2, "the mark is asked for somewhere else as well"
    programs = win[win.index("pub fn programs_let_go("):win.index("/// Remove what earlier runs left behind")]
    assert '.ends_with(".exe")' in programs and "Err(_) => return false," in programs
    assert "found.is_file() || found.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0" in programs
    going = win[win.index("pub fn open_to_remove("):win.index("// Squeezing, and the mark of what a file holds")]
    assert "Ok(held) if own_folder_behind(&held, path, open_to).is_ok() => ToRemove::Ours(held)," in going
    assert "Err(e) if e.raw_os_error() == Some(ERROR_SHARING_VIOLATION) => ToRemove::InUse," in going
    assert ".access_mode(DELETE | READ_CONTROL | FILE_READ_ATTRIBUTES).share_mode(anything)" in going and ".custom_flags(AS_IT_IS).open(path);" in going
    assert "ONE_RUNS_FOLDERS.iter().any(|prefix| name.starts_with(prefix))" in win
    # The app's side has nothing to take away: it does not know where it was unpacked.
    assert "Vispora-backend" not in S.repo("app.py") and "Vispora-backend" not in S.repo("core", "updater.py")


# ═════════════════════════════════════════════════════════════════════
# The app's own cleaning leaves the shell's folders alone
# ═════════════════════════════════════════════════════════════════════
def test_cleaning_windows_temp_leaves_the_folders_the_app_is_running_from():
    """The app empties Windows' own Temp folder (Clean now, Refresh PC, the
    junk clean).  The shell makes its window's folder there, and, unpacked
    at every start, the folder the app itself is running from.  The cleaner
    once took the app's own unpacked files from under it (_MEI...), and the
    next thing the app read failed; the same must not happen again under
    other names."""
    app()
    import core.utils as utils
    CL = m.app("core.cleaner")
    # The names are the shell's: every kind of folder it makes begins with one the cleaner knows.
    win, backend = S.rust("src", "win.rs"), S.rust("src", "backend.rs")
    made = [re.search(r'pub const %s: &str = "([^"]+)";' % name, source).group(1)
            for name, source in (("WINDOW_FOLDER_PREFIX", win), ("BACKEND_FOLDER_PREFIX", win), ("KEPT_FOLDER_PREFIX", backend))]
    assert made == ["Vispora-window-", "Vispora-backend-run-", "Vispora-backend-kept-"]
    for prefix in made:
        assert utils.is_running_programs_folder(prefix + "0123456789abcdef"), prefix
    assert utils.is_running_programs_folder("_MEI12345"), "another program's, or today's app's"
    for other in ("Vispora", "vispora-window-1", "MEI1", "Temp", "", None, 5):
        assert not utils.is_running_programs_folder(other), other

    run, window, other = "Vispora-backend-run-" + "ab" * 16, "Vispora-window-" + "cd" * 16, "_MEI424242"
    holds = {"junk.tmp": "x" * 10, os.path.join("somebody", "old.log"): "y" * 5,
             os.path.join(run, "in-use"): "", os.path.join(run, "VisporaBackend.exe"): "MZ" * 50,
             os.path.join(run, "_internal", "base_library.zip"): "python" * 20,
             os.path.join(run, "_internal", "a-folder", "keep.txt"): "k",
             os.path.join(window, "in-use"): "", os.path.join(window, "EBWebView", "Default", "Cookies"): "c" * 30,
             os.path.join(other, "base_library.zip"): "somebody else's program" * 3}
    # Folders with nothing in them, inside a running program's folder: a
    # program makes those too (a cache it has not written to yet), and one
    # that is taken away is missed when the program comes to use it.  The
    # cleaner takes empty folders away everywhere else.
    empty = [os.path.join(run, "_internal", "empty"), os.path.join(run, "empty-at-the-top"),
             os.path.join(window, "EBWebView", "Default", "Cache", "Cache_Data"), os.path.join(other, "empty"),
             os.path.join("somebody", "empty")]
    temp = os.path.join(m.ROOT, "one-file", "windows-temp")
    for rel, text in holds.items():
        os.makedirs(os.path.dirname(os.path.join(temp, rel)), exist_ok=True)
        with open(os.path.join(temp, rel), "w") as f:
            f.write(text)
    for rel in empty:
        os.makedirs(os.path.join(temp, rel))
    the_apps = sorted(rel for rel in holds if rel.startswith(("Vispora-", "_MEI")))

    def there():
        return sorted(os.path.relpath(os.path.join(at, name), temp) for at, _, names in os.walk(temp) for name in names)

    # What the clean says it can free is what it then frees: the two files that are nobody's.
    saved = CL._get_clean_locations
    CL._get_clean_locations = lambda: [{"name": "Windows Temp", "path": temp, "cat": "System"}]
    try:
        rows = CL.scan_temp_sizes()
    finally:
        CL._get_clean_locations = saved
    assert (rows[0]["files"], rows[0]["size"]) == (2, 15)
    assert CL._measure_dir(temp) == 15 and CL._count_files(temp) == 2
    assert CL.clean_location(temp)["files"] == 2
    assert there() == the_apps, "the cleaner took files of the running app"
    assert not os.path.exists(os.path.join(temp, "somebody")), "an ordinary folder is still cleaned, and taken away, with the empty one in it"
    for folder in (run, os.path.join(run, "_internal", "a-folder"), window, other) + tuple(empty[:-1]):
        assert os.path.isdir(os.path.join(temp, folder)), f"the cleaner took a folder of a running program away: {folder}"
    # Asked to clean one of those folders itself: nothing.
    for folder in (run, window, other):
        assert CL._safe_rmtree(os.path.join(temp, folder)) == (0, 0) and there() == the_apps
    for folder in empty[:-1]:
        assert os.path.isdir(os.path.join(temp, folder)), folder


def test_the_disk_analyzer_protects_the_folder_of_the_exe_a_person_starts():
    """The Disk Analyzer deletes what a person picks, and refuses the folder
    the app's exe is in.  Behind the shell that exe is the shell's; this
    process's own is in the folder it was unpacked into, which is refused
    as it always was."""
    app()
    DA = m.app("core.disk_analyzer")
    assert DA._the_shells_folder() is None, "started as it always was, there is no other exe"
    before = DA._build_deny_prefixes()
    downloads = os.path.join(m.ROOT, "Downloads-of-the-shell")
    os.makedirs(downloads, exist_ok=True)
    saved = DA._DENY_PREFIXES
    try:
        U.use_app_exe(os.path.join(downloads, "Vispora.exe"))
        assert DA._the_shells_folder() == downloads
        DA._DENY_PREFIXES = DA._build_deny_prefixes()
        assert set(DA._DENY_PREFIXES) - set(before) == {os.path.realpath(downloads).lower()}
        for refused in (downloads, os.path.join(downloads, "Vispora.exe"), os.path.join(downloads, "something", "else"), m.ROOT):
            assert DA._is_deny_listed(refused)[0], refused
        assert not DA._is_deny_listed(os.path.join(m.ROOT, "Downloads-of-somebody-else", "old.iso"))[0]
    finally:
        U.use_app_exe(None)
        DA._DENY_PREFIXES = saved
    # Loaded only when a page asks for it: after the shell has said where it is.
    source = S.repo("app.py")
    assert "\nfrom core import disk_analyzer" not in source and "\nimport core.disk_analyzer" not in source
    assert source.count("        from core import disk_analyzer\n") >= 4


# ═════════════════════════════════════════════════════════════════════
# Building it, and trying it
# ═════════════════════════════════════════════════════════════════════
def test_the_pack_is_read_whole_when_the_exe_is_built():
    """A damaged pack, or the pack of another version, is to stop the build,
    not to be found on a person's PC.  The build script asks the code that
    reads the pack there (shell_rs/core: fit_to_carry, which has its own
    test with cargo), with the app's version from config.py.  For that the
    core is a build dependency of the window's crate as well: a crate of
    this repository, in the lock file already."""
    build, manifest, lock = S.app_crate("build.rs"), S.app_crate("Cargo.toml"), S.app_crate("Cargo.lock")
    read = build[build.index("Some(named) => {"):build.index("None => std::fs::write(&carried, b\"\")")]
    asked = read.index("if let Err(why) = vispora_shell_core::pack::fit_to_carry(&bytes, version) {")
    assert read.index("std::fs::read(&named)") < asked < read.index("and this exe cannot carry it: {why}") < read.index("std::fs::write(&carried, bytes)")
    assert "starts_with(b\"VSPPACK1\")" not in build, "the pack's first bytes are all that is looked at"
    # (the version is the one the exe is given: config.py's)
    assert build.index('strip_prefix("APP_VERSION")') < build.index("fit_to_carry(&bytes, version)")
    fits = S.rust("src", "pack.rs")
    fits = fits[fits.index("pub fn fit_to_carry("):fits.index("impl<'a> Pack<'a> {")]
    assert fits.index("let pack = Pack::read(bytes)?;") < fits.index("if pack.version != version {") \
        < fits.index("for_each(&pack.files, |file| pack.bytes_of(file).map(drop))?;")
    # No new crate: the core by its path, twice, and the lock file's entry as it was.
    assert manifest.count('vispora-shell-core = { path = "../core" }') == 2
    built_with = manifest[manifest.index("[build-dependencies]"):manifest.index("[profile.release]")]
    assert re.findall(r"^([\w-]+) = ", built_with, re.M) == ["tauri-build", "vispora-shell-core"]
    entry = lock[lock.index('name = "vispora-shell"\n'):]
    assert entry[:entry.index("]")].split("dependencies = [")[1].split() == ['"tauri",', '"tauri-build",', '"vispora-shell-core",']


def the_trial():
    """shell_rs/app/try_it.py, as a module.  Loading it starts nothing."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shell_rs", "app", "try_it.py")
    spec = importlib.util.spec_from_file_location("the_window_trial", path)
    trial = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trial)
    return trial


def test_the_trial_finds_the_pack_tool_where_the_core_is_built_and_says_when_it_is_missing():
    """The trial of the window needs two things cargo built: the window's exe
    (its own crate's) and the trial tool, which makes packs (the core's).
    With each crate building into its own target folder the tool is in the
    core's, where the tests look for it too.  And a tool that is missing is
    said in words before anything is started, as a missing exe is."""
    import contextlib
    import io
    T = the_trial()
    here, core = os.path.join(m.ROOT, "one-file", "crates", "app"), os.path.join(m.ROOT, "one-file", "crates", "core")
    elsewhere = os.path.join(m.ROOT, "one-file", "crates", "kept-out-of-the-repository")
    for folder in (here, core, elsewhere):
        os.makedirs(os.path.join(folder, "target", "release"), exist_ok=True)
    os.makedirs(os.path.join(elsewhere, "release"), exist_ok=True)

    def put(*where):
        with open(os.path.join(*where), "wb") as f:
            f.write(b"MZ made up, with VISPORA_SHELL_TRY in it: never started")
        return os.path.join(*where)

    saved = (T.HERE, T.CORE, T.EXE, T.PACK_TOOL, os.environ.pop("CARGO_TARGET_DIR", None))
    try:
        T.HERE, T.CORE = here, core
        # Each crate builds into its own folder: the exe in the window's, the tool in the core's.
        exe, tool = put(here, "target", "release", "VisporaShell.exe"), put(core, "target", "release", "vispora-shell-try.exe")
        assert T.built("VisporaShell.exe") == exe and T.built("vispora-shell-try.exe", T.CORE) == tool
        assert not os.path.isfile(T.built("vispora-shell-try.exe")), "the tool is not the window's crate's"
        # One folder for everything that is built: both are there, and that comes first.
        os.environ["CARGO_TARGET_DIR"] = elsewhere
        assert T.built("VisporaShell.exe") == exe and T.built("vispora-shell-try.exe", T.CORE) == tool, "not there yet: the crates' own"
        both = put(elsewhere, "release", "VisporaShell.exe"), put(elsewhere, "release", "vispora-shell-try.exe")
        assert (T.built("VisporaShell.exe"), T.built("vispora-shell-try.exe", T.CORE)) == both
        source = S.app_crate("try_it.py")
        assert 'CORE = os.path.normpath(os.path.join(HERE, "..", "core"))' in source
        assert 'PACK_TOOL = built("vispora-shell-try.exe", CORE)' in source

        # The tool is missing: every way of running the trial that makes or
        # reads a pack says so and starts nothing; the others do not ask for it.
        os.remove(both[1])
        os.remove(tool)
        T.EXE, T.PACK_TOOL = T.built("VisporaShell.exe"), T.built("vispora-shell-try.exe", T.CORE)
        started = []
        T.checks = T.times = T.unpacking_times = T.picture = lambda *a, **k: started.append(a) or True
        for argv in ([], ["--one-file"], ["--time", "--pack", "some.pack"], ["--time", "--pack", "some.pack", "--carried-by", "some.exe"]):
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                try:
                    T.main(list(argv))
                    code = "came back"
                except SystemExit as stopped:
                    code = stopped.code
            assert code == 2 and not started, (argv, code, started)
            assert said.getvalue().startswith("Not started: " + tool + " is missing: build it first (cargo build --release --manifest-path "
                                              "shell_rs/core/Cargo.toml)."), said.getvalue()
        with contextlib.redirect_stdout(io.StringIO()):
            assert T.main(["--time"]) == 0 and T.main(["--picture", "x.png"]) == 0 and len(started) == 2, "the times of the window need no pack"
    finally:
        T.HERE, T.CORE, T.EXE, T.PACK_TOOL = saved[:4]
        os.environ.pop("CARGO_TARGET_DIR", None)
        if saved[4] is not None:
            os.environ["CARGO_TARGET_DIR"] = saved[4]
