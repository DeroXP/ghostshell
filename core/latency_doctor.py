"""Latency Doctor (beta.5) — honest, read-first input-latency diagnostic.

Scans the user's ACTUAL system state across the latency surface, scores it,
and tags each item with the honest research verdict (REAL / MARGINAL / INFO).
It deliberately does NOT invent placebo "wins" — items the deeper research
debunked (MenuShowDelay, ThreadDpcEnable, forcing HPET, global timer hacks)
are excluded.  Fixes route to already-tested code (competitive_latency +
network_tweaks), so the Doctor adds diagnosis, not new mutation logic.

scan()  -> {score, grade, checks:[...]}  (read-only)
fix()   -> applies the safe latency set, then returns a fresh scan
"""
from core.utils import run_cmd, run_ps, get_logger
from core import competitive_latency, timer_manager, network_tweaks

log = get_logger("latency_doctor")


def _reg_dword(key, val):
    r = run_cmd(["reg", "query", key, "/v", val])
    if not r.get("ok"):
        return None
    for line in (r.get("out") or "").splitlines():
        if val.lower() in line.lower() and "0x" in line:
            try:
                return int(line.strip().split("0x")[-1], 16)
            except Exception:
                return None
    return None


def _reg_sz(key, val):
    r = run_cmd(["reg", "query", key, "/v", val])
    if not r.get("ok"):
        return None
    for line in (r.get("out") or "").splitlines():
        if val.lower() in line.lower() and "REG_SZ" in line:
            return line.split("REG_SZ", 1)[1].strip()
    return None


def _chk(cid, label, status, current, ideal, verdict, detail, weight=0, fixable=False):
    return {"id": cid, "label": label, "status": status, "current": current,
            "ideal": ideal, "verdict": verdict, "detail": detail,
            "weight": weight, "fixable": fixable}


def scan() -> dict:
    checks = []

    # 1. Mouse acceleration — REAL (aim consistency).
    try:
        spd = _reg_sz(r"HKCU\Control Panel\Mouse", "MouseSpeed")
        off = (spd == "0")
        checks.append(_chk("mouse_accel", "Mouse acceleration (Enhance Pointer Precision)",
                           "good" if off else "bad",
                           "off (1:1)" if off else "ON",
                           "off",
                           "REAL (consistency)",
                           "1:1 raw movement = deterministic aim. The single most felt competitive change.",
                           weight=3, fixable=True))
    except Exception:
        pass

    # 2. Win11 windowed-game flip model — REAL.
    try:
        fm = competitive_latency._flip_model_enabled()
        checks.append(_chk("flip_model", "Win11 windowed-game optimizations (flip model)",
                           "good" if fm else "bad",
                           "on" if fm else "off", "on",
                           "REAL",
                           "Lets borderless reach exclusive-fullscreen latency on DX10/11.",
                           weight=2, fixable=True))
    except Exception:
        pass

    # 3. Game DVR / Game Bar — REAL (small).
    try:
        dvr = _reg_dword(r"HKCU\System\GameConfigStore", "GameDVR_Enabled")
        good = (dvr == 0)
        checks.append(_chk("gamedvr", "Game DVR / Game Bar capture",
                           "good" if good else "warn",
                           "off" if good else "on", "off",
                           "REAL (small)",
                           "Removes background capture overhead + overlay input hooks.",
                           weight=1, fixable=True))
    except Exception:
        pass

    # 4. Min CPU processor state — REAL (no clock-ramp on first input).
    try:
        r = run_ps("(powercfg /q SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN "
                   "| Select-String 'Current AC')[0].ToString()")
        line = (r.get("out") or "")
        pct = int(line.split("0x")[-1], 16) if "0x" in line else None
        if pct is not None:
            good = pct >= 100
            checks.append(_chk("cpu_min_state", "CPU minimum processor state",
                               "good" if good else "warn",
                               f"{pct}%", "100%",
                               "REAL",
                               "100% min keeps cores at clock so the first input after idle isn't delayed by ramp.",
                               weight=2, fixable=True))
    except Exception:
        pass

    # 5. NIC interrupt moderation — REAL (network/DPC).
    try:
        r = run_ps("$a=Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | "
                   "Select-Object -First 1 -ExpandProperty Name; "
                   "Get-NetAdapterAdvancedProperty -Name $a -ErrorAction SilentlyContinue | "
                   "Where-Object DisplayName -match 'Interrupt Moderation' | "
                   "Select-Object -First 1 -ExpandProperty DisplayValue")
        val = (r.get("out") or "").strip().splitlines()[-1].strip() if r.get("out") else ""
        if val:
            good = val.lower().startswith("disab")
            checks.append(_chk("nic_moderation", "NIC interrupt moderation",
                               "good" if good else "warn",
                               val, "Disabled",
                               "REAL (network)",
                               "Removes receive-coalescing delay (~hundreds of µs).",
                               weight=2, fixable=True))
    except Exception:
        pass

    # 6. Nagle's algorithm — REAL for TCP titles.
    try:
        r = run_ps("$b='HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces';"
                   "$on=0;$tot=0;Get-ChildItem $b|ForEach-Object{$p=Get-ItemProperty $_.PSPath;"
                   "if($p.IPAddress -or $p.DhcpIPAddress){$tot++;if($p.TcpAckFrequency -eq 1){$on++}}};\"$on/$tot\"")
        s = (r.get("out") or "").strip().splitlines()[-1].strip()
        on, tot = (int(x) for x in s.split("/")) if "/" in s else (0, 0)
        if tot:
            good = (on == tot)
            checks.append(_chk("nagle", "Nagle's algorithm (delayed-ACK)",
                               "good" if good else "warn",
                               f"off on {on}/{tot} adapters", "off (all)",
                               "REAL (TCP games)",
                               "Nagle adds up to ~40-200 ms on small TCP packets; off helps TCP-based titles.",
                               weight=1, fixable=True))
    except Exception:
        pass

    # 7. HAGS — enabler, not a direct win (INFO).
    try:
        hags = _reg_dword(r"HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode")
        checks.append(_chk("hags", "Hardware-accelerated GPU scheduling (HAGS)",
                           "good" if hags == 2 else "info",
                           "on" if hags == 2 else ("off" if hags == 1 else "unknown"),
                           "on (enabler)",
                           "INFO (enabler)",
                           "Microsoft: transparent plumbing — not a direct latency win, but enables some Reflex/FG paths. Reboot to change.",
                           weight=0, fixable=False))
    except Exception:
        pass

    # 8. GPU MSI mode (NVIDIA) — marginal/real for DPC (INFO; reboot to change).
    try:
        r = run_ps(
            "$g=Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\PCI' -EA SilentlyContinue|"
            "ForEach-Object{Get-ChildItem $_.PSPath -EA SilentlyContinue}|"
            "Where-Object{(Get-ItemProperty $_.PSPath -EA SilentlyContinue).DeviceDesc -match 'NVIDIA|GeForce'}|Select-Object -First 1;"
            "if($g){(Get-ItemProperty (Join-Path $g.PSPath 'Device Parameters\\Interrupt Management\\MessageSignaledInterruptProperties') -Name MSISupported -EA SilentlyContinue).MSISupported}")
        v = (r.get("out") or "").strip()
        if v in ("0", "1"):
            checks.append(_chk("gpu_msi", "GPU MSI mode (message-signaled interrupts)",
                               "good" if v == "1" else "info",
                               "on" if v == "1" else "off", "on",
                               "MARGINAL (DPC)",
                               "Lower interrupt/DPC overhead vs line-based IRQs. Reboot to change.",
                               weight=0, fixable=False))
    except Exception:
        pass

    # 9. Timer resolution — INFO (honest: best-effort, per-process since Win10 2004).
    try:
        res = timer_manager.query_timer_resolution_ms()
        checks.append(_chk("timer_res", "Timer resolution",
                           "info",
                           f"{res} ms" if res else "unknown", "0.5-1 ms",
                           "INFO (best-effort)",
                           "Largely per-process since Win10 2004 — Vispora requests 0.5 ms but it's not a guaranteed click-to-photon win.",
                           weight=0, fixable=False))
    except Exception:
        pass

    # 10. Display refresh + FPS-cap advice — REAL display-side lever (advice only).
    try:
        hz = competitive_latency._primary_refresh_hz()
        cap = competitive_latency.recommended_fps_cap(hz)
        if hz:
            checks.append(_chk("fps_cap", "In-game FPS cap (vs refresh)",
                               "info",
                               f"display {hz} Hz", f"cap {cap} fps in-game",
                               "REAL (if sustained)",
                               f"Cap in-game to {cap} (V-Sync/G-Sync). Only helps if you actually reach it; if GPU-bound (e.g. 4K), enable in-game NVIDIA Reflex instead. An in-game cap beats a driver cap.",
                               weight=0, fixable=False))
    except Exception:
        pass

    # ── Score (REAL items only; weighted) ──────────────────────────────
    scored = [c for c in checks if c["weight"] > 0]
    earned = sum((1.0 if c["status"] == "good" else 0.5 if c["status"] == "warn" else 0.0) * c["weight"]
                 for c in scored)
    total = sum(c["weight"] for c in scored) or 1
    pct = round(earned / total * 100)
    grade = ("A" if pct >= 90 else "B" if pct >= 80 else "C" if pct >= 70
             else "D" if pct >= 60 else "F")
    n_fix = sum(1 for c in checks if c["fixable"] and c["status"] in ("bad", "warn"))
    return {"score": pct, "grade": grade, "fixable_issues": n_fix, "checks": checks}


def fix() -> dict:
    """Apply the safe latency set, then re-scan.  Delegates to already-tested
    code paths — the Doctor doesn't reimplement mutations."""
    log.info("Latency Doctor: applying safe fixes...")
    applied = []
    try:
        r = competitive_latency.apply()
        applied.append({"name": "Competitive latency set", "ok": bool(r.get("ok"))})
    except Exception as e:
        applied.append({"name": "Competitive latency set", "ok": False, "err": str(e)})
    try:
        network_tweaks.apply_nagle_tweaks()
        network_tweaks.optimize_adapter_power()
        applied.append({"name": "Network (Nagle off, NIC moderation off)", "ok": True})
    except Exception as e:
        applied.append({"name": "Network tweaks", "ok": False, "err": str(e)})
    return {"ok": True, "applied": applied, "rescan": scan()}
