"""app.py - the local API only answers the app's own window: right Host,
no foreign Origin, and this run's session token on every call."""
import sandbox as m

import unittest

import config

try:
    import flask                                    # noqa: F401
    import app as A                                 # importing the module starts nothing: main() is not called
except ImportError as e:                            # Flask (or another dependency) is not installed here
    A, _why = None, str(e)

HOST = {"Host": f"127.0.0.1:{config.APP_PORT}"}


def client():
    if A is None:
        raise unittest.SkipTest(f"the app could not be imported with this Python: {_why}")
    return A.app.test_client()


def auth(**extra):
    return {**HOST, A._API_TOKEN_HEADER: A._API_TOKEN, **extra}


def test_the_app_module_is_sandboxed():
    client()
    assert A.optimizer.run_cmd is m.run_cmd and A.config.APPDATA_DIR.startswith(m.ROOT)


def test_requests_under_another_hostname_are_refused():
    c = client()
    for host in ("evil.example", "127.0.0.1", f"127.0.0.1:{config.APP_PORT + 1}", f"evil.example:{config.APP_PORT}"):
        r = c.get("/", headers={"Host": host})
        assert r.status_code == 403 and r.get_json()["err"] == "bad host", host    # (DNS rebinding)
        assert c.get("/api/profiles/settings", headers={"Host": host, A._API_TOKEN_HEADER: A._API_TOKEN}).status_code == 403


def test_the_page_loads_without_a_token_but_does_not_contain_it():
    c = client()
    for host in (f"127.0.0.1:{config.APP_PORT}", f"localhost:{config.APP_PORT}"):
        r = c.get("/", headers={"Host": host})
        assert r.status_code == 200
    page = r.data.decode("utf-8")
    assert A._API_TOKEN not in page, "the token is handed over in the URL fragment, never served"
    assert A._ui_url().endswith("#t=" + A._API_TOKEN)
    assert config.APP_NAME in page and config.APP_VERSION in page


def test_api_calls_need_the_session_token():
    c = client()
    assert c.get("/api/profiles/settings", headers=HOST).status_code == 403
    wrong = {**HOST, A._API_TOKEN_HEADER: "x" * len(A._API_TOKEN)}
    r = c.get("/api/profiles/settings", headers=wrong)
    assert r.status_code == 403 and r.get_json()["err"] == "missing or wrong session token"
    assert c.post("/api/network/undo", headers=HOST).status_code == 403
    assert c.get("/api/profiles/settings", headers=auth()).status_code == 200


def test_cross_origin_calls_are_refused_even_with_the_token():
    c = client()
    assert c.get("/api/profiles/settings", headers=auth(Origin="https://evil.example")).status_code == 403
    assert c.get("/api/profiles/settings", headers=auth(Origin="null")).status_code == 403
    assert c.get("/api/profiles/settings", headers=auth(**{"Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert c.get("/api/profiles/settings", headers=auth(**{"Sec-Fetch-Site": "same-site"})).status_code == 403
    ours = f"http://127.0.0.1:{config.APP_PORT}"
    assert c.get("/api/profiles/settings", headers=auth(Origin=ours)).status_code == 200
    assert c.get("/api/profiles/settings", headers=auth(**{"Sec-Fetch-Site": "none"})).status_code == 200


def test_only_the_second_instance_ping_is_exempt_from_the_token():
    c = client()
    assert A._TOKEN_EXEMPT_PATHS == frozenset({"/api/window/show"})
    assert c.post("/api/window/show", headers=HOST).status_code == 200
    assert c.post("/api/window/show", headers={**HOST, "Origin": "https://evil.example"}).status_code == 403, \
        "exempt from the token, not from the other checks"
    assert c.post("/api/window/close", headers=HOST).status_code == 403


def test_errors_come_back_as_json():
    c = client()
    r = c.get("/api/no-such-route", headers=auth())
    assert r.status_code == 404 and r.get_json()["ok"] is False
    r = c.get("/api/network/undo", headers=auth())
    assert r.status_code == 405 and "not allowed" in r.get_json()["err"]


def test_game_mode_settings_round_trip():
    c = client()
    m.fresh()
    base = {"auto_enable_on_boot": True, "all_cores": True, "background_on_ecores": True, "priority_at_launch": True}
    assert c.get("/api/profiles/settings", headers=auth()).get_json()["settings"] == base
    after = c.post("/api/profiles/settings", headers=auth(), json={"all_cores": False}).get_json()["settings"]
    assert after == {**base, "all_cores": False}
    after = c.post("/api/profiles/settings", headers=auth(), json={"auto_enable_on_boot": False}).get_json()["settings"]
    assert after == {**base, "all_cores": False, "auto_enable_on_boot": False}, "the other switches are left alone"
    after = c.post("/api/profiles/settings", headers=auth(),
                   json={"background_on_ecores": False, "priority_at_launch": False}).get_json()["settings"]
    assert after == {"auto_enable_on_boot": False, "all_cores": False, "background_on_ecores": False,
                     "priority_at_launch": False}


def test_undo_routes_answer_on_a_machine_with_nothing_to_undo():
    c = client()
    m.fresh()
    for route in ("/api/network/undo", "/api/gpu/undo"):
        r = c.post(route, headers=auth())
        assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["nothing"] is True, route


def test_rerun_prep_route_forces_the_prep():
    c = client()
    m.fresh()
    A.boot_prep._state.update(running=False, done=True, steps=[])      # it already ran at start-up
    A.boot_prep.set_persistent_allowed(False)
    assert c.post("/api/boot-prep/run", headers=auth()).get_json()["ok"] is True
    assert m.wait(lambda: not A.boot_prep._state["running"] and len(A.boot_prep._state["steps"]) >= 7)
    assert c.get("/api/boot-prep/status", headers=auth()).get_json()["persistent"] is True


def test_page_and_script_describe_game_mode_as_it_is():
    c = client()
    page = c.get("/", headers=HOST).data.decode("utf-8")
    script = c.get("/static/js/app.js", headers=HOST).data.decode("utf-8")
    assert "Every CPU core made available to the game" in page
    assert "locked to P-Cores" not in page and "P-core affinity" not in page
    assert "toggleGameModeAllCores(this)" in script and "async function toggleGameModeAllCores" in script
    assert "undoNetworkTweaks" in script and "undoGpuTweaks" in script
