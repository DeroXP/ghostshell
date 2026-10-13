"""Checks that need nothing to run: every Python file compiles, no function
reads a name its module never defines, every button in the page calls a
function that exists, and the UI script parses."""
import sandbox as m

import builtins
import glob
import io
import os
import pathlib
import re
import subprocess
import symtable
import unittest

import config


def _python_files(with_tests=False):
    files = [os.path.join(m.REPO, "app.py"), os.path.join(m.REPO, "config.py")]
    for folder in ("core", "updater") + (("tests",) if with_tests else ()):
        files += sorted(glob.glob(os.path.join(m.REPO, folder, "*.py")))
    return files


def _read(path):
    return io.open(path, encoding="utf-8-sig").read()


def _rel(path):
    return os.path.relpath(path, m.REPO)


def test_every_python_file_compiles():
    files = _python_files(with_tests=True) + [os.path.join(m.REPO, "build.spec")]
    assert len(files) > 70
    bad = []
    for path in files:
        try:
            compile(_read(path), path, "exec")
        except SyntaxError as e:
            bad.append(f"{_rel(path)}:{e.lineno}: {e.msg}")
    assert bad == [], "\n".join(bad)


def _lint(path):
    """Two mistakes that have bitten this codebase and that nothing reports
    until the line runs - usually inside an `except Exception` that hides it:
      1. a function reads a global that nothing in the module binds (a module
         imported inside one function and used in another, a typo);
      2. a function assigns to a private module-level name without `global`,
         creating a local and leaving the module's state untouched."""
    src = _read(path)
    top = symtable.symtable(src, path, "exec")
    bound = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()}
    bound |= set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__", "__path__",
                                   "__package__"}
    undefined, shadowed = [], []

    def walk(table):
        for child in table.get_children():
            if child.get_type() == "function":
                for s in child.get_symbols():
                    n = s.get_name()
                    if s.is_global() and s.is_referenced() and n not in bound:
                        undefined.append(f"{_rel(path)}:{child.get_lineno()}: {child.get_name()}() reads '{n}', "
                                         f"which the module never defines")
                    if (s.is_local() and s.is_assigned() and not s.is_parameter() and n.startswith("_")
                            and not n.startswith("__") and n != "_" and n in bound
                            and child.get_name() != "<lambda>"):
                        shadowed.append(f"{_rel(path)}:{child.get_lineno()}: {child.get_name()}() assigns '{n}', "
                                        f"also a module-level name (missing 'global'?)")
            walk(child)

    walk(top)
    return undefined, shadowed


def test_no_function_reads_a_name_its_module_never_defines():
    found = [line for path in _python_files() for line in _lint(path)[0]]
    assert found == [], "\n".join(found)


def test_no_function_shadows_private_module_state():
    found = [line for path in _python_files() for line in _lint(path)[1]]
    assert found == [], "\n".join(found)


def test_the_lint_catches_what_it_is_for():
    path = os.path.join(m.ROOT, "lint_sample.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("_state = None\n"
                "def a():\n    import json\n    return json.dumps({})\n"
                "def b():\n    return json.loads('{}')\n"              # json is only imported inside a()
                "def c():\n    _state = 1\n"                           # meant the module's _state
                "def d():\n    global _state\n    _state = 2\n")
    undefined, shadowed = _lint(path)
    assert len(undefined) == 1 and "b() reads 'json'" in undefined[0]
    assert len(shadowed) == 1 and "c() assigns '_state'" in shadowed[0]


# ── the page and its script ───────────────────────────────────────────
_JS_WORDS = {"if", "return", "event", "this", "document", "window", "void", "alert", "confirm", "setTimeout",
             "function", "new", "typeof", "navigator", "console", "location", "JSON", "Math", "Number", "String"}


def _defined_in_script(js):
    names = set(re.findall(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", js))
    names |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|"
                            r"[A-Za-z_$][\w$]*\s*=>)", js))
    names |= set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=", js))
    return names


def test_every_inline_handler_calls_a_function_that_exists():
    page = _read(os.path.join(m.REPO, "templates", "index.html"))
    js = _read(os.path.join(m.REPO, "static", "js", "app.js"))
    defined = _defined_in_script(js) | _defined_in_script(page)
    assert len(defined) > 300
    handler = re.compile(r"""\bon[a-z]+\s*=\s*\\?["']\s*(?:return\s+)?([A-Za-z_$][\w$]*)\s*\(""")
    js = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))     # (comment examples)
    missing = sorted({name for text in (page, js) for name in handler.findall(text)
                      if name not in defined and name not in _JS_WORDS})
    assert missing == [], f"buttons that call nothing: {missing}"


def test_the_page_has_no_duplicate_element_ids():
    page = _read(os.path.join(m.REPO, "templates", "index.html"))
    ids = re.findall(r"""\bid\s*=\s*["']([^"'{}]+)["']""", page)
    twice = sorted({i for i in ids if ids.count(i) > 1})
    assert twice == [], f"getElementById finds only the first of each: {twice}"


def _edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        exe = os.path.join(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(exe):
            return exe
    return None


def test_the_ui_script_parses():
    """No Node on the build machine, so the script is loaded in headless Edge
    (the one real process this suite starts: it only reads a local page)."""
    edge = _edge()
    if not edge:
        raise unittest.SkipTest("Microsoft Edge not found")
    script = pathlib.Path(m.REPO, "static", "js", "app.js").as_uri()
    page = os.path.join(m.ROOT, "jscheck.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write('<!doctype html><html><head><meta charset="utf-8"></head><body><pre id="out">PENDING</pre>'
                "<script>var errs=[];window.onerror=function(m,s,l,c){errs.push(String(m)+' @'+l+':'+c);return true;};"
                "window.addEventListener('unhandledrejection',function(e){e.preventDefault();});</script>"
                f'<script src="{script}" charset="utf-8"></script>'
                "<script>document.getElementById('out').textContent='LOADED='+(typeof undoGpuTweaks==='function')"
                "+' SYNTAX='+JSON.stringify(errs.filter(function(e){return /SyntaxError/.test(e);}));</script>"
                "</body></html>")
    try:
        p = m.REAL_POPEN([edge, "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
                          f"--user-data-dir={os.path.join(m.ROOT, 'edge-profile')}", "--dump-dom",
                          pathlib.Path(page).as_uri()], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        out = p.communicate(timeout=90)[0].decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise unittest.SkipTest(f"headless Edge did not run: {e}")
    found = re.search(r"LOADED=(\w+) SYNTAX=(\[.*?\])", out)
    if not found:
        raise unittest.SkipTest("headless Edge printed no page")
    assert found.group(2) == "[]", f"static/js/app.js has a syntax error: {found.group(2)}"
    assert found.group(1) == "true", "the script loaded but its functions are not defined"


# ── things that must stay in step ─────────────────────────────────────
def test_updater_and_app_agree_on_names():
    import updater.paths as P
    assert (P.APP_NAME, P.LEGACY_APP_NAME) == (config.APP_NAME, config.LEGACY_APP_NAME)
    assert set(n.lower() for n in P.APP_EXE_NAMES) <= config.OWN_PROCESS_NAMES


def test_version_looks_like_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[a-z]+\.\d+)?", config.APP_VERSION), config.APP_VERSION
