import sandbox as m
# The cloud client side: license checks that must not drop a paying user's
# key over an answer the API never gave (core/license.py), VM detection that
# must not flag a real PC (core/anti_vm.py), the disk serial in the hardware
# fingerprint (core/hwfp.py), the CPU bucket (core/device_id.py), the device
# sync client (core/cloud_client.py) and the community games cache
# (core/community.py).  No test reaches a network: every URL here ends in
# .invalid and urlopen is replaced.

import io
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from core import anti_vm, cloud_client, community, device_id, hwfp
from core import license as lic

CLOUD = "https://cloud.invalid"


def _use_cloud():
    cloud_client._save_settings(dict(cloud_client.DEFAULT_SETTINGS, cloud_url=CLOUD))


class _FakeUrlopen:
    """urllib.request.urlopen, answering from a list of exceptions."""

    def __init__(self, *answers):
        self.answers, self.urls = list(answers), []

    def __call__(self, req, timeout=None):
        self.urls.append(getattr(req, "full_url", req))
        assert timeout, "every request needs a timeout"
        raise self.answers.pop(0)

    def __enter__(self):
        self.real, urllib.request.urlopen = urllib.request.urlopen, self
        return self

    def __exit__(self, *exc):
        urllib.request.urlopen = self.real
        return False


def _http_error(code, body):
    return urllib.error.HTTPError(CLOUD + "/v1/licenses/validate", code, "x", {}, io.BytesIO(body.encode()))


# ── license: what counts as the cloud rejecting the key ──────────────
def test_a_403_or_404_the_api_never_sent_keeps_the_license_key():
    m.fresh()
    _use_cloud()
    mgr = lic.LicenseManager()
    key = "ABCDE-FGHJK-MNPQR-STVWX"
    mgr._cache["license_key"] = key
    mgr.last_validated = datetime.now(timezone.utc) - timedelta(days=1)
    not_the_api = [
        (404, "<html><body>Not Found - the train has not arrived at the station</body></html>"),
        (404, json.dumps({"status": "error", "code": 404, "message": "Application not found"})),
        (404, json.dumps({"detail": "Not Found"})),            # a server rolled back to before /v1/licenses
        (403, "<html><body>Access denied by your network administrator</body></html>"),
    ]
    for code, body in not_the_api:
        with _FakeUrlopen(_http_error(code, body)):
            state = mgr._validate_paid(key, {})
        assert state == lic.LicenseState.OFFLINE_GRACE, (code, body, state)
        assert mgr._cache["license_key"] == key, f"the paying user's key was dropped over: {body}"


def test_the_apis_own_rejection_still_drops_the_key():
    m.fresh()
    _use_cloud()
    mgr = lic.LicenseManager()
    for code, detail in ((404, "license key not found"),
                         (403, "this hardware is not activated against this license")):
        mgr._cache["license_key"] = "ABCDE-FGHJK-MNPQR-STVWX"
        with _FakeUrlopen(_http_error(code, json.dumps({"detail": detail}))):
            assert mgr._validate_paid("ABCDE-FGHJK-MNPQR-STVWX", {}) == lic.LicenseState.ERROR
        assert mgr._cache["license_key"] is None


# ── anti_vm ───────────────────────────────────────────────────────────
def _machine(cs, bios, disks):
    answers = {"Win32_ComputerSystem |": cs, "Win32_BIOS |": bios, "Win32_DiskDrive |": disks}
    m.PS_HOOKS.append(lambda c: next((m.R(json.dumps(v)) for k, v in answers.items() if k in c), None))


def test_a_surface_pc_is_not_a_virtual_machine():
    m.fresh()
    _machine({"Manufacturer": "Microsoft Corporation", "Model": "Surface Laptop Studio 2"},
             {"Manufacturer": "Microsoft Corporation", "Version": "Microsoft Corporation - 1",
              "SerialNumber": "0F2A34B5C6D7E8"},
             [{"Model": "SK hynix PC801 HFS001TEJ9X101N", "Manufacturer": "(Standard disk drives)"}])
    r = anti_vm.detect()
    assert not r["is_vm"], r["signals"]


def test_a_hyper_v_guest_is_still_a_virtual_machine():
    m.fresh()
    _machine({"Manufacturer": "Microsoft Corporation", "Model": "Virtual Machine"},
             {"Manufacturer": "Microsoft Corporation", "Version": "VRTUAL - 1",
              "SerialNumber": "0000-0002-1234-5678-9012-3456-78"},
             [{"Model": "Msft Virtual Disk", "Manufacturer": "(Standard disk drives)"}])
    r = anti_vm.detect()
    assert r["is_vm"] and {s["vendor"] for s in r["signals"]} == {"Hyper-V (guest)"}, r


def test_a_service_tag_that_happens_to_spell_xen_or_kvm_is_not_a_vm():
    for tag in ("7XEN2K3", "H4KVM12", "N6NRKD0VBOX4145"):
        m.fresh()
        _machine({"Manufacturer": "Dell Inc.", "Model": "XPS 15 9530"},
                 {"Manufacturer": "Dell Inc.", "Version": "1.12.0", "SerialNumber": tag},
                 [{"Model": "PC SN810 NVMe WDC 1024GB", "Manufacturer": "(Standard disk drives)"}])
        r = anti_vm.detect()
        assert not r["is_vm"], (tag, r["signals"])


def test_a_vmware_serial_still_counts():
    m.fresh()
    _machine({"Manufacturer": "VMware, Inc.", "Model": "VMware7,1"},
             {"Manufacturer": "VMware, Inc.", "Version": "VMW71.00V.21100432.B64.2301110304",
              "SerialNumber": "VMware-56 4d 9a 3e 7b 21 0c 55-1f 2e 3d 4c 5b 6a 79 88"},
             [{"Model": "VMware Virtual NVMe Disk", "Manufacturer": "(Standard disk drives)"}])
    r = anti_vm.detect()
    assert r["is_vm"] and any(s["axis"] == "bios.serial" for s in r["signals"]), r


# ── hwfp: the disk serial must not depend on which query answered ───
def test_both_disk_serial_queries_give_the_same_serial():
    m.fresh()
    serial = "E823_8FA6_BF53_0001_001B_448B_4A7F_4C5D."      # how Windows shows many NVMe serials
    m.PS_HOOKS.append(lambda c: m.R(serial) if "Get-Partition" in c or "Win32_DiskDrive" in c else None)
    usual = hwfp._get_primary_disk_serial()
    m.PS_HOOKS.insert(0, lambda c: m.F("Get-Partition : Invalid property") if "Get-Partition" in c else None)
    fallback = hwfp._get_primary_disk_serial()
    assert usual == fallback == serial.rstrip("."), (usual, fallback)


# ── device_id: the CPU bucket ─────────────────────────────────────────
def test_cpu_family_keeps_the_intel_tier():
    cases = {
        "13th Gen Intel(R) Core(TM) i7-13700K": "intel core i7",
        "Intel(R) Core(TM) i5-10400F CPU @ 2.90GHz": "intel core i5",
        "Intel(R) Core(TM) Ultra 9 285K": "intel ultra",
        "Intel(R) Xeon(R) W-2245 CPU @ 3.90GHz": "intel xeon",
        "Intel(R) Core(TM)2 Quad CPU    Q6600  @ 2.40GHz": "intel core",
        "AMD Ryzen 9 7950X3D 16-Core Processor": "amd ryzen 9",
    }
    got = {name: device_id._cpu_family_from_name(name) for name in cases}
    assert got == cases, got


# ── cloud_client ──────────────────────────────────────────────────────
def test_the_legacy_insiders_channel_is_saved_as_beta():
    m.fresh()
    assert not cloud_client.is_enabled()                    # nothing is pushed anywhere
    cloud_client.update_local_settings(update_channel="insiders")
    assert cloud_client.get_settings()["update_channel"] == "beta"
    cloud_client.update_local_settings(update_channel="nightly")
    assert cloud_client.get_settings()["update_channel"] == "beta"
    cloud_client.update_local_settings(update_channel="stable")
    assert cloud_client.get_settings()["update_channel"] == "stable"


def test_a_failing_heartbeat_does_not_probe_the_hardware_every_minute():
    m.fresh()
    _use_cloud()
    probes, sent = [], []
    real = cloud_client.collect_hardware_fingerprint, cloud_client._request
    cloud_client.collect_hardware_fingerprint = lambda: probes.append(1) or {}
    cloud_client._request = lambda *a, **k: sent.append(a) or {"ok": False, "status": 0, "data": None,
                                                              "err": "network: offline"}
    cloud_client._last_heartbeat_try = None
    try:
        for _ in range(6):                                  # the loop ticks once a minute
            cloud_client.heartbeat()
        assert (len(probes), len(sent)) == (1, 1), (probes, sent)
        cloud_client._last_heartbeat_try -= cloud_client.RETRY_INTERVAL_SEC + 1
        cloud_client.heartbeat()
        assert len(sent) == 2, "it must try again once the retry interval has passed"
        cloud_client.heartbeat(force=True)
        assert len(sent) == 3
    finally:
        cloud_client.collect_hardware_fingerprint, cloud_client._request = real
        cloud_client._last_heartbeat_try = None


# ── community: the 6-hour games cache ────────────────────────────────
def test_a_community_cache_dated_in_the_future_is_refetched():
    m.fresh()
    community.set_settings(server_url="https://community.invalid")
    games = [{"exe": "oldgame.exe", "name": "Old Game"}]
    community._save_cache({"games": games, "fetched_at": time.time() + 30 * 86400})
    with _FakeUrlopen(urllib.error.URLError("offline")) as net:
        r = community.fetch_games()
    assert net.urls, "a cache stamped by a clock that ran ahead was taken as fresh"
    assert r["games"] == games, "and the cached list still serves while the fetch fails"

    community._save_cache({"games": games, "fetched_at": time.time() - 60})
    with _FakeUrlopen() as net:
        r = community.fetch_games()
    assert not net.urls and r.get("from_cache"), "a fresh cache needs no fetch"
