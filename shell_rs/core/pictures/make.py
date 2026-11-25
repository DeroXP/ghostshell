"""Write the tray icon's three pictures for the shell, from the app's own drawing.

    python shell_rs/core/pictures/make.py

The app draws its tray icon when it starts (app.py, _render_tray_image):
the mark in three looks, for "Game Mode is off", "on" and "a game is
running".  The shell shows the same three pictures, dot for dot, so it does
not draw them a second time in a second way: this script has the app draw
them once and writes them down, and the shell carries the files
(shell_rs/core/src/tray.rs).

Each file is 64 by 64 dots, four bytes a dot (red, green, blue, and how
solid it is), row after row from the top, with nothing before or after.

tests/test_tray_calls.py draws them again at every run and compares: if the
app's drawing ever changes, that test says so, and this script is run again.

The app is loaded on the fake machine of the tests (tests/sandbox.py), as
everything is that loads it outside a real start.  Nothing on the PC is read
or changed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
LOOKS = ("idle", "armed", "playing")
SIZE = 64


def drawn(look):
    """The picture for `look`, as the app draws it today."""
    import app
    picture = app._render_tray_image(look)
    assert picture.mode == "RGBA" and picture.size == (SIZE, SIZE), (picture.mode, picture.size)
    return picture.tobytes()


def path(look):
    return os.path.join(HERE, f"tray-{look}.rgba")


if __name__ == "__main__":
    sys.path.insert(0, os.path.join(REPO, "tests"))
    import sandbox                                  # noqa: F401  (first: the fake machine)
    for look in LOOKS:
        with open(path(look), "wb") as f:
            f.write(drawn(look))
        print(f"{os.path.relpath(path(look), REPO)}: {os.path.getsize(path(look))} bytes")
    os._exit(0)
