"""core/reg_originals.py - the record of what a setting was before a tweak
changed it, and putting exactly that back."""
import sandbox as m

import threading

from core import reg_originals as RO

KEY = r"HKLM\SOFTWARE\Vendor\Thing"
MOUSE = r"HKCU\Control Panel\Mouse"


def _write(key, name, data, vtype="REG_DWORD", scope="optimizer"):
    """What every tweak does: note the prior value, then write."""
    RO.remember(key, name, data, vtype, scope)
    return m.run_cmd(["reg", "add", key, "/v", name, "/t", vtype, "/d", data, "/f"])["ok"]


def test_sandbox_is_in_place():
    import config
    assert config.APPDATA_DIR.startswith(m.ROOT)
    assert RO.run_cmd is m.run_cmd
    assert RO._PATH.startswith(m.ROOT)


def test_first_change_wins():
    m.fresh()
    m.rset(KEY, "Speed", 7)
    _write(KEY, "Speed", "1")
    _write(KEY, "Speed", "2")                       # a second tweak of the same value
    assert m.rdata(KEY, "Speed") == 2
    assert RO.restore_all() == {"restored": 1, "failed": 0}
    assert m.rget(KEY, "Speed") == ("REG_DWORD", 7), "back to the value before the FIRST change"
    assert RO.summary()["registry"] == 0


def test_value_that_did_not_exist_is_removed_again():
    m.fresh()
    _write(KEY, "New", "1")
    assert m.rget(KEY, "New") == ("REG_DWORD", 1)
    RO.restore_all()
    assert m.rget(KEY, "New") is None


def test_value_already_at_the_tweak_value_is_not_recorded():
    m.fresh()
    m.rset(KEY, "Dec", 38)
    m.rset(KEY, "Hex", 0x3322)
    m.rset(KEY, "Text", "High")
    _write(KEY, "Dec", "38")
    _write(KEY, "Hex", "0x3322")                    # the same number, written as hex
    _write(KEY, "Text", "High", "REG_SZ")
    assert RO.summary()["registry"] == 0, "its true original is unknown - recording now would restore the tweak"


def test_same_data_but_a_different_type_is_a_change():
    m.fresh()
    m.rset(MOUSE, "MouseSpeed", "0")                # REG_SZ, as Windows stores it
    _write(MOUSE, "MouseSpeed", "0", "REG_DWORD")
    RO.restore_all()
    assert m.rget(MOUSE, "MouseSpeed") == ("REG_SZ", "0")


def test_every_value_type_round_trips():
    m.fresh()
    m.rset(KEY, "Bin", b"\x01\x02\xff")
    m.rset(KEY, "Multi", ["a", "b"])
    m.rset(KEY, "Expand", "%SystemRoot%\\x", "REG_EXPAND_SZ")
    m.rset(KEY, "Big", 2 ** 40, "REG_QWORD")
    before = m.flat()
    _write(KEY, "Bin", "00", "REG_BINARY")
    _write(KEY, "Multi", "x", "REG_MULTI_SZ")
    _write(KEY, "Expand", "y", "REG_EXPAND_SZ")
    _write(KEY, "Big", "5", "REG_QWORD")
    assert m.flat() != before
    assert RO.restore_all() == {"restored": 4, "failed": 0}
    assert m.flat() == before


def test_unknown_hive_is_ignored():
    m.fresh()
    RO.remember(r"HKXX\Nope", "v", "1")
    assert RO.summary()["registry"] == 0
    assert RO.read_value(r"HKXX\Nope", "v") is None


def test_scopes_restore_separately():
    m.fresh()
    m.rset(KEY, "A", 10)
    m.rset(KEY, "B", 20)
    _write(KEY, "A", "1", scope="optimizer")
    _write(KEY, "B", "1", scope="gpu")
    RO.restore_all("gpu")
    assert (m.rdata(KEY, "A"), m.rdata(KEY, "B")) == (1, 20)
    assert RO.has_original(KEY, "A") and not RO.has_original(KEY, "B", scope="gpu")
    RO.restore_all("optimizer")
    assert m.rdata(KEY, "A") == 10


def test_two_scopes_on_one_value_unwind_in_reverse_order():
    m.fresh()
    m.rset(KEY, "V", 5)
    _write(KEY, "V", "1", scope="optimizer")
    _write(KEY, "V", "2", scope="network")          # finds 1, records 1
    RO.restore_all("network")
    assert m.rdata(KEY, "V") == 1
    RO.restore_all("optimizer")
    assert m.rdata(KEY, "V") == 5


def test_a_value_that_cannot_be_restored_stays_on_record():
    m.fresh()
    m.rset(KEY, "Locked", 3)
    m.rset(KEY, "Fine", 3)
    _write(KEY, "Locked", "1")
    _write(KEY, "Fine", "1")
    m.REG_FAIL.add((KEY.lower(), "locked"))
    assert RO.restore_all() == {"restored": 1, "failed": 1}
    assert m.rdata(KEY, "Fine") == 3 and m.rdata(KEY, "Locked") == 1
    assert RO.has_original(KEY, "Locked"), "kept, so the next reset tries again"
    m.REG_FAIL.clear()
    assert RO.restore_all() == {"restored": 1, "failed": 0}
    assert m.rdata(KEY, "Locked") == 3


def test_records_survive_a_restart():
    m.fresh()
    m.rset(KEY, "V", 9)
    _write(KEY, "V", "1")
    RO._cache = None                                # the app is started again
    assert RO.has_original(KEY, "V")
    RO.restore_all()
    assert m.rdata(KEY, "V") == 9


def test_a_damaged_records_file_is_treated_as_empty():
    m.fresh()
    with open(RO._PATH, "w", encoding="utf-8") as f:
        f.write("{ not json")
    RO._cache = None
    assert RO.summary() == {"registry": 0, "power": 0, "state": 0, "notes": {}}
    with open(RO._PATH, "w", encoding="utf-8") as f:
        f.write("[1, 2]")
    RO._cache = None
    assert RO.restore_all() == {"restored": 0, "failed": 0}


def test_suspended_writes_are_not_recorded():
    m.fresh()
    m.rset(KEY, "V", 9)
    with RO.suspended():
        _write(KEY, "V", "1")
        assert RO.remember_power(m.PROC_SUB, m.IDLE) is False
        assert RO.remember_state("service", "X", "auto") is False
        RO.note("flag")
    assert RO.summary() == {"registry": 0, "power": 0, "state": 0, "notes": {}}
    _write(KEY, "V", "2")                           # outside the block it records again
    assert RO.summary()["registry"] == 1


def test_suspension_is_per_thread():
    """A game that starts while Reset All runs on another thread must still
    have its own changes recorded, or they could never be undone."""
    m.fresh()
    m.rset(KEY, "Game", 9)
    done = threading.Event()

    def game_thread():
        _write(KEY, "Game", "1", scope="gamemode")
        done.set()

    with RO.suspended():
        t = threading.Thread(target=game_thread)
        t.start()
        t.join(5)
    assert done.is_set() and RO.has_original(KEY, "Game", scope="gamemode")


def test_nested_suspension():
    m.fresh()
    with RO.suspended():
        with RO.suspended():
            pass
        assert RO._is_suspended(), "leaving the inner block must not end the outer one"
    assert not RO._is_suspended()


# ── a lasting tweak written while a per-game mode holds the value ─────
def test_optimizer_run_during_a_game_inherits_the_real_original():
    m.fresh()
    m.rset(KEY, "V", 2)                             # the user's value
    _write(KEY, "V", "38", scope="competitive")     # a game starts
    _write(KEY, "V", "40", scope="optimizer")       # the user runs the optimizer mid-game
    assert not RO.has_original(KEY, "V", scope="competitive"), "the per-game mode gives the value up"
    RO.restore_all("competitive")                   # the game closes
    assert m.rdata(KEY, "V") == 40, "the optimizer's setting is left alone"
    RO.restore_all("optimizer")                     # Reset All
    assert m.rdata(KEY, "V") == 2, "...and Reset All returns the user's value, not the gaming one"


def test_takeover_happens_even_when_the_values_match():
    m.fresh()
    m.rset(KEY, "V", 2)
    _write(KEY, "V", "38", scope="gamemode")
    _write(KEY, "V", "38", scope="optimizer")       # same value the game mode wrote
    RO.restore_all("gamemode")
    assert m.rdata(KEY, "V") == 38
    RO.restore_all("optimizer")
    assert m.rdata(KEY, "V") == 2


def test_game_started_after_the_optimizer_unwinds_normally():
    m.fresh()
    m.rset(KEY, "V", 2)
    _write(KEY, "V", "40", scope="optimizer")
    _write(KEY, "V", "38", scope="competitive")
    RO.restore_all("competitive")
    assert m.rdata(KEY, "V") == 40
    RO.restore_all("optimizer")
    assert m.rdata(KEY, "V") == 2


def test_one_per_game_mode_does_not_take_over_from_another():
    m.fresh()
    m.rset(KEY, "V", 2)
    _write(KEY, "V", "38", scope="gamemode")
    _write(KEY, "V", "40", scope="competitive")
    assert RO.has_original(KEY, "V", scope="gamemode") and RO.has_original(KEY, "V", scope="competitive")


# ── power-plan settings ───────────────────────────────────────────────
def test_hidden_power_settings_are_read_with_qh():
    m.fresh()
    m.cell(m.USERPLAN, m.IDLE)[:] = [1, 0]
    assert RO.query_power(m.USERPLAN, m.PROC_SUB, m.IDLE) == (1, 0)
    assert m.ran("powercfg", "/qh") and not m.ran("powercfg", "/query")
    # ...because /query exits 0 and simply leaves the setting out:
    hidden = m.run_cmd(["powercfg", "/query", m.USERPLAN, m.PROC_SUB, m.IDLE])
    assert hidden["ok"] and "Current AC" not in hidden["out"]


def test_power_reading_takes_the_last_two_numbers():
    m.fresh()
    m.cell(m.USERPLAN, m.PROC_MIN)[:] = [12, 34]    # the output also lists min / max / increment in hex
    assert RO.query_power(m.USERPLAN, m.PROC_SUB, m.PROC_MIN) == (12, 34)
    assert RO.query_power("ffffffff-0000-0000-0000-000000000000", m.PROC_SUB, m.PROC_MIN) is None


def test_power_setting_is_restored_on_the_plan_it_was_changed_on():
    m.fresh(plan=m.USERPLAN)
    m.cell(m.USERPLAN, m.PROC_MIN)[:] = [5, 6]
    assert RO.remember_power(m.PROC_SUB, m.PROC_MIN) is True
    m.run_cmd(["powercfg", "/setacvalueindex", "SCHEME_CURRENT", m.PROC_SUB, m.PROC_MIN, "100"])
    m.run_cmd(["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", m.PROC_SUB, m.PROC_MIN, "100"])
    m.run_cmd(["powercfg", "/setactive", m.BALANCED])       # the user switches plans afterwards
    m.cell(m.BALANCED, m.PROC_MIN)[:] = [50, 50]
    assert RO.restore_all() == {"restored": 1, "failed": 0}
    assert m.cell(m.USERPLAN, m.PROC_MIN) == [5, 6], "AC and DC, on the plan that was changed"
    assert m.cell(m.BALANCED, m.PROC_MIN) == [50, 50], "the plan that is active now is not touched"
    assert m.PLAN["active"] == m.BALANCED


def test_power_alias_and_guid_are_one_setting():
    m.fresh()
    assert RO.remember_power("SUB_PROCESSOR", "IdleDisable") is True
    m.cell(m.USERPLAN, m.IDLE)[0] = 1
    assert RO.remember_power(m.PROC_SUB, m.IDLE) is True    # already on record: still undoable
    assert RO.summary()["power"] == 1
    assert RO.has_power(m.PROC_SUB, m.IDLE) and RO.has_power("sub_processor", "idledisable")
    assert not RO.has_power(m.PROC_SUB, m.IDLE, scope="gamemode")
    RO.restore_all()
    assert m.cell(m.USERPLAN, m.IDLE) == [0, 0]


def test_remember_power_says_when_a_change_cannot_be_undone():
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.F("no") if a[:2] == ["powercfg", "/qh"] else None)
    assert RO.remember_power(m.PROC_SUB, m.IDLE) is False
    assert RO.summary()["power"] == 0
    m.fresh()
    m.CMD_HOOKS.append(lambda a: m.R("garbage") if a[:2] == ["powercfg", "/getactivescheme"] else None)
    assert RO.remember_power(m.PROC_SUB, m.IDLE) is False


def test_power_takeover_from_a_per_game_mode():
    m.fresh()
    assert RO.remember_power(m.PROC_SUB, m.IDLE, scope="gamemode")      # was [0, 0]
    m.cell(m.USERPLAN, m.IDLE)[0] = 1
    assert RO.remember_power(m.PROC_SUB, m.IDLE)                        # optimizer, mid-game
    assert not RO.has_power(m.PROC_SUB, m.IDLE, scope="gamemode")
    RO.restore_all("gamemode")
    assert m.cell(m.USERPLAN, m.IDLE) == [1, 0]
    RO.restore_all()
    assert m.cell(m.USERPLAN, m.IDLE) == [0, 0]


def test_restore_with_no_power_records_does_not_ask_powercfg():
    m.fresh()
    m.rset(KEY, "V", 2)
    _write(KEY, "V", "1")
    m.clear_logs()
    RO.restore_all()
    assert not m.ran("powercfg")


def test_restoring_the_active_plan_makes_it_re_read_its_settings():
    m.fresh()
    RO.remember_power(m.PROC_SUB, m.PROC_MAX)
    m.cell(m.USERPLAN, m.PROC_MAX)[0] = 100
    m.clear_logs()
    RO.restore_all()
    assert m.ran("powercfg", "/setactive", m.USERPLAN)


def test_a_power_setting_already_back_at_its_value_counts_as_restored():
    """A record whose setting Windows will no longer accept (an old tweak had
    narrowed its range on one PC) used to fail at every restore, for good."""
    m.fresh()
    RO.remember_power(m.PROC_SUB, m.PARK_E)                 # [100, 10]
    m.POWER_LIMITS[m.PARK_E] = 0                            # writing 100 is refused now
    m.clear_logs()
    assert RO.restore_all() == {"restored": 1, "failed": 0}
    assert not m.ran("powercfg", "setacvalueindex") and RO.summary()["power"] == 0


# ── notes and other state ─────────────────────────────────────────────
def test_notes():
    m.fresh()
    RO.note("hibernation_disabled")
    RO.note("plan", "abc")
    RO.note("plan", "second")                       # first one wins
    assert RO.noted("hibernation_disabled") is True and RO.noted("plan") == "abc" and RO.noted("x") is None
    RO.forget_notes("plan", "never-there")
    assert RO.summary()["notes"] == {"hibernation_disabled": True}


def test_state_records():
    m.fresh()
    assert RO.remember_state("service", "Spooler", "auto") is True
    assert RO.remember_state("service", "Spooler", "disabled") is False, "first change wins"
    assert RO.remember_state("bcd", "quietboot", None) is True          # "was not set" is a state too
    assert RO.remember_state("service", "Spooler", "demand", scope="gpu") is True
    assert RO.state_records("service") == {"Spooler": "auto"}
    assert RO.state_records("bcd") == {"quietboot": None}
    assert RO.has_state("service") and RO.has_state("bcd", "quietboot") and not RO.has_state("task")
    assert not RO.has_state("service", "Fax") and RO.has_state("service", "Spooler", scope="gpu")
    RO.forget_state("service", "Spooler")
    assert not RO.has_state("service") and RO.has_state("service", scope="gpu")
    assert RO.summary()["state"] == 2


def test_subkeys():
    m.fresh()
    assert sorted(RO.subkeys(m.IFACES_KEY)) == sorted(m.IFACES)
    assert RO.subkeys(r"HKLM\No\Such\Key") == [] and RO.subkeys(r"HKXX\Nope") == []
