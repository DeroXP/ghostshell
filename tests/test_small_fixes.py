"""Smaller safety rules: what the temp / junk cleaner may delete
(core/cleaner.py), which windows the Game Bar pop-up watchdog may close
(core/popup_killer.py), and the helpers that keep text out of PowerShell
command strings (core/utils.py)."""
import sandbox as m

import os
import unittest

import core.utils as U

CL = m.app("core.cleaner")
PK = m.app("core.popup_killer")


def _tree(name, files):
    root = os.path.join(m.ROOT, name)
    for rel, text in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return root


# ── cleaner ───────────────────────────────────────────────────────────
def test_junk_rule_honours_the_protected_extensions():
    assert CL._is_junk("setup.tmp", ".tmp") and CL._is_junk("crash.dmp", ".dmp")
    assert CL._is_junk("Thumbs.db", ".db"), "named outright, although .db is otherwise protected"
    assert not CL._is_junk("save.db", ".db") and not CL._is_junk("notes.txt", ".txt")
    assert not CL._is_junk("desktop.ini", ".ini"), "it holds a folder's icon and localised name"
    assert not (CL._PROTECTED_EXT & CL._JUNK_EXT), "an extension cannot be both"


def test_shallow_junk_sweep_only_takes_junk_files():
    root = _tree("shallow", {"a.tmp": "x", "b.log": "xx", "thumbs.db": "x", "keep.docx": "x", "desktop.ini": "x",
                             "game.sav": "x", "sub/deep.tmp": "x"})
    files, size = CL._clean_shallow_junk(root)
    assert (files, size) == (3, 4)
    assert sorted(os.listdir(root)) == ["desktop.ini", "game.sav", "keep.docx", "sub"]
    assert os.path.exists(os.path.join(root, "sub", "deep.tmp")), "no recursion into the user's folders"


def test_rmtree_does_not_follow_a_junction_out_of_the_folder():
    try:
        import _winapi
        make_junction = _winapi.CreateJunction
    except (ImportError, AttributeError):
        raise unittest.SkipTest("this Python cannot create a junction")
    precious = _tree("documents", {"thesis.docx": "years of work"})
    temp = _tree("temp_a", {"old.tmp": "x", "nested/older.tmp": "x"})
    make_junction(precious, os.path.join(temp, "link"))     # any unprivileged process can plant one of these
    assert U.is_reparse_point(os.path.join(temp, "link")) and not U.is_reparse_point(temp)
    files, _ = CL._safe_rmtree(temp)
    assert files == 2
    assert os.path.exists(os.path.join(precious, "thesis.docx")), "the cleaner emptied what the junction points at"
    assert CL._safe_rmtree(os.path.join(temp, "link")) == (0, 0), "nor when asked to clean the junction itself"
    assert os.path.exists(os.path.join(precious, "thesis.docx"))
    os.rmdir(os.path.join(temp, "link"))                    # removes the junction, not its target


def test_rmtree_leaves_a_running_apps_extracted_runtime_alone():
    temp = _tree("temp_b", {"junk.tmp": "x", "_MEI12345/base_library.zip": "python", "_MEI12345/lib/x.pyd": "x"})
    files, _ = CL._safe_rmtree(temp)
    assert files == 1
    assert os.path.exists(os.path.join(temp, "_MEI12345", "base_library.zip")), \
        "deleting it crashed the app (and any other PyInstaller app) at its next import"
    assert CL._is_in_pyinstaller_extract(r"C:\Temp\_MEI99\x.dll") and not CL._is_in_pyinstaller_extract(r"C:\Temp\x")


def test_rmtree_on_something_that_is_not_a_folder():
    assert CL._safe_rmtree(os.path.join(m.ROOT, "does-not-exist")) == (0, 0)


def test_disk_clean_ignores_anything_but_a_bare_drive_letter():
    out = CL.clean_disk_junk([r"C:\Users", "..", "C:/", "CC:", 5, None, ""])
    assert out["ok"] and out["total_files"] == 0 and out["drives"] == []


def test_unreadable_path_counts_as_a_reparse_point():
    assert U.is_reparse_point(os.path.join(m.ROOT, "nope", "nothing")) is True, "when in doubt, stay out"


# ── Game Bar pop-up watchdog ──────────────────────────────────────────
def test_title_match():
    assert PK._matches_target("Get an app to open this 'ms-gamebar' link", "x")
    assert PK._matches_target("You'll need a new app to open this ms-gamingoverlay link", "x")
    assert PK._matches_target("Get an app to open this link", "x")
    assert not PK._matches_target("", "ApplicationFrameWindow") and not PK._matches_target("Calculator", "x")


def test_only_the_picker_itself_is_closed():
    """A browser tab about this very error, a text file named after it and an
    Explorer folder all carry the same words in their title - and used to be
    sent WM_CLOSE once a second."""
    owners = {5: "openwith.exe", 2: "chrome.exe", 3: "notepad.exe", 4: "explorer.exe"}
    windows = [
        (1, "ApplicationFrameWindow", "Get an app to open this 'ms-gamebar' link"),
        (2, "Chrome_WidgetWin_1", "how to fix ms-gamebar link popup - Google Search"),
        (3, "Notepad", "ms-gamebar notes.txt - Notepad"),
        (4, "CabinetWClass", "ms-gamingoverlay fixes"),
        (5, "SomeNewHostClass", "Get an app to open this 'ms-gamingoverlay' link"),
        (6, "#32770", "Unrelated dialog"),
    ]
    closed = []

    class OneTick:
        def is_set(self):
            return False

        def wait(self, seconds):
            return True                             # stop after the first pass

    saved = (PK._enumerate_windows, PK._close_window, PK._owner_exe, PK._stop)
    PK._enumerate_windows = lambda: windows
    PK._close_window = lambda hwnd: closed.append(hwnd) or True
    PK._owner_exe = lambda hwnd: owners.get(hwnd, "")
    PK._stop = OneTick()
    try:
        PK._watchdog_loop()
    finally:
        PK._enumerate_windows, PK._close_window, PK._owner_exe, PK._stop = saved
    assert closed == [1, 5], "the picker by its window class, and by its owner where the class is unknown"


# ── text that goes into PowerShell commands ───────────────────────────
def test_safe_exe_name():
    for good in ("game.exe", "My Game-Win64-Shipping.exe", "r5apex_dx12.exe", "C++ Runner.EXE"):
        assert U.is_safe_ps_exe_name(good), good
    for bad in ('a";calc;".exe', "a`b.exe", "a$b.exe", "a|b.exe", "a\nb.exe", "noext", "", None, "x" * 130 + ".exe",
                "..\\evil.exe", "a(b).exe", "a&b.exe"):
        assert not U.is_safe_ps_exe_name(bad), bad


def test_safe_literal():
    assert U.is_safe_ps_literal(r"C:\Program Files (x86)\Steam") and U.is_safe_ps_literal("it's fine")
    for bad in ('say "hi"', "a`b", "$env:TEMP", "two\nlines", "curly \u201cquote\u201d", "low \u201e", "", None, 5):
        assert not U.is_safe_ps_literal(bad), repr(bad)


def test_atomic_writes_leave_no_temp_file_behind():
    path = os.path.join(m.ROOT, "atomic", "state.json")
    assert U.atomic_write_json(path, {"a": 1}) and U.atomic_write_text(path + ".txt", "hello")
    assert sorted(os.listdir(os.path.dirname(path))) == ["state.json", "state.json.txt"]
    assert U.atomic_write_json(os.path.join(path, "under-a-file.json"), {}) is False, "fails cleanly, no exception"


def test_log_entries_are_numbered_so_the_ui_can_ask_for_what_is_new():
    U.get_logger("t").warning("one")
    U.get_logger("t").warning("two")
    last_two = U.get_log_buffer()[-2:]
    assert [e["msg"] for e in last_two] == ["one", "two"] and last_two[1]["seq"] == last_two[0]["seq"] + 1
    assert U.set_log_level("nonsense")["ok"] is False and U.set_log_level("info")["ok"] is True
