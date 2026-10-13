"""The GhostShell -> Vispora rename (config.py, updater/paths.py): which data
folder is used, and when the old one is moved across.

The folder is chosen when config is imported, so every case runs in its own
child interpreter with APPDATA pointed at a scratch folder."""
import sandbox as m

import os

import config

_n = [0]


def _appdata(*existing):
    """A scratch APPDATA holding the named data folders, each with a file."""
    _n[0] += 1
    root = os.path.join(m.ROOT, f"appdata_{_n[0]}")
    os.makedirs(root)
    for name in existing:
        os.makedirs(os.path.join(root, name))
        with open(os.path.join(root, name, "vault.db"), "w") as f:
            f.write(f"data of {name}")
    return root


def _run(appdata, as_app=False, module="config"):
    """Import `module` in a child interpreter and return its data folder.
    as_app: the child's main script is called app.py, as when the app runs."""
    env = dict(os.environ, APPDATA=appdata)
    code = (f"import sys; sys.path.insert(0, {m.REPO!r}); sys.dont_write_bytecode = True\n"
            f"import {module} as c\nprint(c.APPDATA_DIR)\n")
    if as_app:
        folder = os.path.join(appdata, "_run")
        os.makedirs(folder, exist_ok=True)
        script = os.path.join(folder, "app.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        rc, out, err = m.spawn([script], env=env)
    else:
        rc, out, err = m.spawn(["-c", code], env=env)
    assert rc == 0, err
    return out.strip().splitlines()[-1]


def test_fresh_install_uses_the_new_folder():
    root = _appdata()
    assert _run(root) == os.path.join(root, config.APP_NAME)
    assert os.path.isdir(os.path.join(root, config.APP_NAME, "backups"))
    assert not os.path.exists(os.path.join(root, config.LEGACY_APP_NAME))


def test_the_app_moves_an_old_data_folder_across_whole():
    root = _appdata(config.LEGACY_APP_NAME)
    old = os.path.join(root, config.LEGACY_APP_NAME)
    for suffix in ("", ".1"):
        with open(os.path.join(old, "ghostshell.log" + suffix), "w") as f:
            f.write("old log" + suffix)
    new = os.path.join(root, config.APP_NAME)
    assert _run(root, as_app=True) == new
    assert not os.path.exists(old), "one atomic rename: it lands whole or not at all"
    assert open(os.path.join(new, "vault.db")).read() == f"data of {config.LEGACY_APP_NAME}"
    assert open(os.path.join(new, "vispora.log")).read() == "old log", "the log history comes along"
    assert open(os.path.join(new, "vispora.log.1")).read() == "old log.1"
    assert not os.path.exists(os.path.join(new, "ghostshell.log"))


def test_anything_but_the_app_leaves_the_old_folder_where_it_is():
    root = _appdata(config.LEGACY_APP_NAME)
    old = os.path.join(root, config.LEGACY_APP_NAME)
    assert _run(root) == old, "a tool or test that merely imports config uses it as it is"
    assert os.path.isdir(old) and not os.path.exists(os.path.join(root, config.APP_NAME))


def test_a_locked_old_folder_is_used_in_place_until_the_next_launch():
    root = _appdata(config.LEGACY_APP_NAME)
    old = os.path.join(root, config.LEGACY_APP_NAME)
    with open(os.path.join(old, "vault.db")) as held:           # e.g. the updater that just launched us
        assert _run(root, as_app=True) == old
        assert not os.path.exists(os.path.join(root, config.APP_NAME))
        held.read()
    assert _run(root, as_app=True) == os.path.join(root, config.APP_NAME), "the next launch tries again"


def test_with_both_folders_the_new_one_wins_and_the_old_is_untouched():
    root = _appdata(config.LEGACY_APP_NAME, config.APP_NAME)
    assert _run(root, as_app=True) == os.path.join(root, config.APP_NAME)
    old = os.path.join(root, config.LEGACY_APP_NAME, "vault.db")
    assert open(old).read() == f"data of {config.LEGACY_APP_NAME}"


def test_the_updater_follows_the_same_rule_but_never_renames():
    root = _appdata(config.LEGACY_APP_NAME)
    old = os.path.join(root, config.LEGACY_APP_NAME)
    assert _run(root, as_app=True, module="updater.paths") == old
    assert os.path.isdir(old) and not os.path.exists(os.path.join(root, config.APP_NAME))
    root = _appdata(config.LEGACY_APP_NAME, config.APP_NAME)
    assert _run(root, module="updater.paths") == os.path.join(root, config.APP_NAME)
    root = _appdata()
    assert _run(root, module="updater.paths") == os.path.join(root, config.APP_NAME)


def test_media_folder_keeps_an_existing_pre_rename_collection():
    profile = os.environ["USERPROFILE"]
    assert config.user_media_dir("Videos", "Clips") == os.path.join(profile, "Videos", config.APP_NAME, "Clips")
    os.makedirs(os.path.join(profile, "Pictures", config.LEGACY_APP_NAME))
    assert config.user_media_dir("Pictures", "Shots") == \
        os.path.join(profile, "Pictures", config.LEGACY_APP_NAME, "Shots"), "their files stay in one place"
    os.makedirs(os.path.join(profile, "Pictures", config.APP_NAME))
    assert config.user_media_dir("Pictures", "Shots") == os.path.join(profile, "Pictures", config.APP_NAME, "Shots")


def test_names_that_must_survive_the_rename():
    assert config.OWN_PROCESS_NAMES == {"vispora.exe", "visporaupdater.exe", "ghostshell.exe", "ghostshellupdater.exe"}
    assert config.is_dead_update_server("https://ghostshell-site.up.railway.app/api")
    assert not config.is_dead_update_server(config.UPDATE_SERVER_URL_DEFAULT)
    assert not config.is_dead_update_server("") and not config.is_dead_update_server(None)
    assert not config._is_app_process(), "this test run is not the app"
