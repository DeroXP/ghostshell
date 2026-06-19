"""Vispora — Main Flask application with pywebview launcher."""
import sys
import os
import json
import threading
import time

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, render_template, jsonify, request
import config
from core.utils import get_logger, get_log_buffer, clear_log_buffer, is_admin, create_restore_point
from core import (system_info, debloater, optimizer, dns_manager, network_tweaks,
                  nvidia_clean, privacy, vault, cleaner,
                  vbs_toggle, timer_manager, interrupt_affinity, scheduler_tweaks,
                  game_profiles, hw_monitor, updater, notifier, power, boot_prep,
                  gpu_overclock, autostart, driver_updater, temp_ceiling,
                  crash_recovery, perf_monitor, adaptive_tuning, health_audit,
                  snapshots, library_scanner, background_pauser, tournament_mode,
                  net_monitor, gamepad_mapper, competitive_latency, latency_doctor)

log = get_logger("app")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["SECRET_KEY"] = os.urandom(24).hex()


# ═══════════════════════════════════════════════════════════════════════════
# Global error handlers — every route returns JSON, never HTML.
# Without these, an uncaught exception sends Flask's default HTML 500
# page back to fetch() callers, which then trips JSON.parse with the
# infamous "Unexpected token '<'" error.
# ═══════════════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def _err_404(e):
    return jsonify({"ok": False, "err": "Endpoint not found", "path": request.path}), 404


@app.errorhandler(405)
def _err_405(e):
    return jsonify({"ok": False, "err": f"Method {request.method} not allowed for {request.path}"}), 405


@app.errorhandler(Exception)
def _err_unhandled(e):
    # Log full traceback to file/log buffer; return clean JSON to caller.
    import traceback
    tb = traceback.format_exc()
    log.error(f"Unhandled exception in {request.method} {request.path}: {e}\n{tb}")
    return jsonify({
        "ok": False,
        "err": f"{type(e).__name__}: {e}",
        "path": request.path,
    }), 500


# Track which modules have been run
_module_status = {
    "debloat": False, "optimize": False, "network": False,
    "gpu": False, "privacy": False, "cleaner": False,
}
_restore_point_created = False
# v2.9.4 — post-install detection.  Set on startup by main().  The frontend
# polls /api/updater/post-install-state once on load and renders a green
# "Updated to vX.Y.Z" toast (or red "install failed") accordingly.
_post_install_state = {"state": "idle"}


# ═══════════════════════════════════════════════════════════════════════════
# Pages
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html", version=config.APP_VERSION)


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/dashboard")
def api_dashboard():
    info = system_info.get_system_info()
    info["admin"] = is_admin()
    info["module_status"] = _module_status
    return jsonify(info)


@app.route("/api/system/capabilities")
def api_system_capabilities():
    """3.4.0 — hardware capability dict.  The UI reads this to gate
    NVIDIA-only pages (GPU OC, Adaptive Tuning) on AMD / Intel /
    CPU-only systems, and to label what telemetry / encode paths are
    available.  Cached after first call (hardware doesn't change
    mid-session); ?refresh=1 forces a recompute for eGPU hot-plug."""
    try:
        from core import capabilities
        force = request.args.get("refresh") in ("1", "true", "yes")
        return jsonify({"ok": True, "caps": capabilities.detect(force=force)})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/restore-point", methods=["POST"])
def api_restore_point():
    global _restore_point_created
    if _restore_point_created:
        return jsonify({"ok": True, "msg": "Restore point already created this session"})
    ok = create_restore_point()
    _restore_point_created = ok
    return jsonify({"ok": ok})


# ═══════════════════════════════════════════════════════════════════════════
# Debloat API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/debloat/scan")
def api_debloat_scan():
    apps = debloater.scan_installed_apps()
    services = debloater.scan_services()
    return jsonify({"apps": apps, "services": services})


@app.route("/api/debloat/run", methods=["POST"])
def api_debloat_run():
    data = request.get_json(force=True) or {}
    app_ids = data.get("apps")
    svc_ids = data.get("services")
    do_tasks = data.get("tasks", True)
    do_features = data.get("features", True)
    do_onedrive = data.get("onedrive", True)
    result = debloater.run_full_debloat(app_ids, svc_ids, do_tasks, do_features, do_onedrive)
    _module_status["debloat"] = True
    return jsonify(result)


@app.route("/api/debloat/remove-app", methods=["POST"])
def api_debloat_remove_app():
    data = request.get_json(force=True) or {}
    pkg = data.get("id", "")
    if not pkg:
        return jsonify({"ok": False, "err": "No package ID"})
    return jsonify(debloater.remove_app(pkg))


@app.route("/api/debloat/disable-service", methods=["POST"])
def api_debloat_disable_service():
    data = request.get_json(force=True) or {}
    svc = data.get("id", "")
    if not svc:
        return jsonify({"ok": False, "err": "No service ID"})
    return jsonify(debloater.disable_service(svc))


@app.route("/api/debloat/features", methods=["POST"])
def api_debloat_features():
    return jsonify({"results": debloater.disable_features()})


@app.route("/api/debloat/restore-services", methods=["POST"])
def api_debloat_restore_services():
    """3.4.2 — re-enable every service the debloater disabled, each
    restored to its recorded original Start type.  Previously there was
    no way to undo a debloat service-disable pass."""
    return jsonify(debloater.restore_services())


# ═══════════════════════════════════════════════════════════════════════════
# Optimizer API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/optimize/run", methods=["POST"])
def api_optimize_run():
    data = request.get_json(force=True) or {}
    categories = data.get("categories")
    result = optimizer.run_full_optimize(categories)
    _module_status["optimize"] = True
    return jsonify(result)


@app.route("/api/optimize/reset", methods=["POST"])
def api_optimize_reset():
    result = optimizer.reset_all_optimizations()
    _module_status["optimize"] = False
    return jsonify(result)


@app.route("/api/optimize/startup")
def api_optimize_startup():
    items = optimizer.scan_startup_items()
    return jsonify({"items": items})


@app.route("/api/optimize/disable-startup", methods=["POST"])
def api_optimize_disable_startup():
    data = request.get_json(force=True) or {}
    return jsonify(optimizer.disable_startup_item(data.get("name", ""), data.get("location", "")))


@app.route("/api/optimize/hard", methods=["POST"])
def api_optimize_hard():
    """Apply aggressive Windows 11 hard tweaks."""
    result = optimizer.apply_hard_tweaks()
    _module_status["optimize"] = True
    return jsonify({"results": result})


@app.route("/api/optimize/latency", methods=["POST"])
def api_optimize_latency():
    """Apply DPC/ISR latency-targeting tweaks."""
    return jsonify({"results": optimizer.apply_latency_tweaks()})


@app.route("/api/optimize/win11", methods=["POST"])
def api_optimize_win11():
    """Apply Windows 11 UX/bloat tweaks."""
    return jsonify({"results": optimizer.apply_windows11_tweaks()})


# ═══════════════════════════════════════════════════════════════════════════
# v3 — new optimizer categories (audio, input, search, defender, boot, legacy)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/optimize/audio", methods=["POST"])
def api_optimize_audio():
    """MMCSS Pro Audio task tuning + Bluetooth absolute-volume kill."""
    return jsonify({"results": optimizer.apply_audio_tweaks()})


@app.route("/api/optimize/input", methods=["POST"])
def api_optimize_input():
    """Mouse hover delay → 0, keyboard repeat → max, touch keyboard off."""
    return jsonify({"results": optimizer.apply_input_tweaks()})


@app.route("/api/optimize/search", methods=["POST"])
def api_optimize_search():
    """Cortana, Bing, search history & suggestions all off."""
    return jsonify({"results": optimizer.apply_search_tweaks()})


@app.route("/api/optimize/defender", methods=["POST"])
def api_optimize_defender():
    """Defender exclusions for Steam / Epic / Battle.net / Riot / Ubisoft / GOG /
    Xbox Game Pass — without disabling real-time protection."""
    return jsonify({"results": optimizer.apply_defender_gaming_tweaks()})


@app.route("/api/optimize/boot", methods=["POST"])
def api_optimize_boot():
    """Fast Startup off, hibernation off, legacy boot menu, faster boot."""
    return jsonify({"results": optimizer.apply_boot_tweaks()})


@app.route("/api/optimize/legacy", methods=["POST"])
def api_optimize_legacy():
    """Disable Print Spooler, Fax, Touch, IE, WSL, Hyper-V, Sandbox, etc."""
    return jsonify({"results": optimizer.apply_legacy_tweaks()})


# ═══════════════════════════════════════════════════════════════════════════
# v3 second wave — 10 more categories (~100 tweaks).  These all reach the
# same /api/optimize/run endpoint via the toggle-grid + Apply Selected
# flow, but standalone routes are exposed so external callers (or a
# future tray-icon "quick apply") can hit them directly.
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/optimize/network_stack", methods=["POST"])
def api_optimize_network_stack():
    return jsonify({"results": optimizer.apply_network_stack_tweaks()})


@app.route("/api/optimize/telemetry_deep", methods=["POST"])
def api_optimize_telemetry_deep():
    return jsonify({"results": optimizer.apply_telemetry_deep_tweaks()})


@app.route("/api/optimize/edge", methods=["POST"])
def api_optimize_edge():
    return jsonify({"results": optimizer.apply_edge_tweaks()})


@app.route("/api/optimize/update_control", methods=["POST"])
def api_optimize_update_control():
    return jsonify({"results": optimizer.apply_update_control_tweaks()})


@app.route("/api/optimize/notifications", methods=["POST"])
def api_optimize_notifications():
    return jsonify({"results": optimizer.apply_notification_tweaks()})


@app.route("/api/optimize/power_advanced", methods=["POST"])
def api_optimize_power_advanced():
    return jsonify({"results": optimizer.apply_power_advanced_tweaks()})


@app.route("/api/optimize/app_privacy", methods=["POST"])
def api_optimize_app_privacy():
    return jsonify({"results": optimizer.apply_app_privacy_tweaks()})


@app.route("/api/optimize/explorer", methods=["POST"])
def api_optimize_explorer():
    return jsonify({"results": optimizer.apply_explorer_tweaks()})


@app.route("/api/optimize/smartscreen", methods=["POST"])
def api_optimize_smartscreen():
    return jsonify({"results": optimizer.apply_smartscreen_tweaks()})


@app.route("/api/optimize/lock_screen", methods=["POST"])
def api_optimize_lock_screen():
    return jsonify({"results": optimizer.apply_lock_screen_tweaks()})


# ═══════════════════════════════════════════════════════════════════════════
# Network API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/network/info")
def api_network_info():
    dns = dns_manager.get_current_dns()
    active = dns_manager.get_active_adapters()
    all_adapters = dns_manager.get_all_adapters()
    return jsonify({
        "dns":           dns,
        "adapters":      active,
        "all_adapters":  all_adapters,    # NEW — includes disconnected WiFi etc.
        "presets":       config.DNS_PRESETS,
    })


@app.route("/api/network/dns", methods=["POST"])
def api_network_dns():
    data = request.get_json(force=True) or {}
    return jsonify(dns_manager.apply_dns(
        preset=data.get("preset"),
        primary=data.get("primary"),
        secondary=data.get("secondary"),
    ))


@app.route("/api/network/dns/reset", methods=["POST"])
def api_network_dns_reset():
    return jsonify(dns_manager.reset_dns_to_dhcp())


@app.route("/api/network/dns/test")
def api_network_dns_test():
    return jsonify({"results": dns_manager.test_dns_latency()})


@app.route("/api/network/restore", methods=["POST"])
def api_network_restore():
    """One-click recovery — restores WiFi/WLAN service, re-enables IPv6,
    resets adapter bindings.  For users whose networking got broken by
    a previous network-optimize pass."""
    return jsonify(network_tweaks.restore_networking())


@app.route("/api/network/optimize", methods=["POST"])
def api_network_optimize():
    result = network_tweaks.run_full_network_optimize()
    _module_status["network"] = True
    return jsonify(result)


@app.route("/api/network/ping")
def api_network_ping():
    return jsonify({"results": network_tweaks.ping_test()})


@app.route("/api/network/ipv6", methods=["POST"])
def api_network_ipv6():
    data = request.get_json(force=True) or {}
    return jsonify(network_tweaks.disable_ipv6(enable=data.get("enable", False)))


@app.route("/api/network/doh", methods=["POST"])
def api_network_doh():
    """Enable DNS-over-HTTPS (Win 11 native)."""
    return jsonify({"results": network_tweaks.apply_dns_over_https()})


@app.route("/api/network/netsh", methods=["POST"])
def api_network_netsh():
    """Apply global netsh TCP stack tweaks."""
    return jsonify({"results": network_tweaks.apply_netsh_tweaks()})


@app.route("/api/network/netbios", methods=["POST"])
def api_network_netbios():
    """Disable NetBIOS, Teredo, LLMNR."""
    return jsonify({"results": network_tweaks.disable_netbios_teredo()})


@app.route("/api/network/game-qos", methods=["POST"])
def api_network_game_qos():
    """Apply DSCP 46 for common game port ranges."""
    return jsonify({"results": network_tweaks.apply_game_port_qos()})


@app.route("/api/network/app-priority", methods=["POST"])
def api_network_app_priority():
    """Set DSCP priority for a specific exe."""
    data = request.get_json(force=True) or {}
    exe = data.get("exe", "").strip()
    if not exe:
        return jsonify({"ok": False, "err": "exe required"}), 400
    dscp = int(data.get("dscp", 46))
    return jsonify(network_tweaks.set_per_app_net_priority(exe, dscp))


@app.route("/api/network/app-priority/clear", methods=["POST"])
def api_network_app_priority_clear():
    return jsonify(network_tweaks.clear_per_app_net_priority())


# ═══════════════════════════════════════════════════════════════════════════
# GPU API
# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# Optimizer Pro Mode (v3.3.1-beta.8+)
# ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════
# Auto-recovery from legacy bad tweaks (v3.3.1-beta.9+)
# ═══════════════════════════════════════════════════════════════════
@app.route("/api/recover/last-report")
def api_recover_last_report():
    """UI polls this on first paint after boot.  Returns the most
    recent legacy-bad-tweaks recovery report if the user hasn't
    acknowledged it yet."""
    try:
        from core import auto_recover
        return jsonify(auto_recover.get_pending_recovery_report())
    except Exception as e:
        return jsonify({"pending": False, "err": str(e)})


@app.route("/api/recover/acknowledge", methods=["POST"])
def api_recover_acknowledge():
    """User has seen the apology toast/modal — rename the report so
    it doesn't fire again."""
    try:
        from core import auto_recover
        return jsonify(auto_recover.mark_recovery_seen())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/optimize/pro-mode", methods=["GET", "POST"])
def api_optimize_pro_mode():
    """Read or update Pro Mode toggle.  GET returns {enabled: bool};
    POST body {enabled: bool} sets it.  Gates the risky-but-real
    tweaks (HVCI, HPET, dynamictick, MPO, USB MSI, TDR delays,
    Windows Search disable, Memory Compression disable, hibernation
    off) from running as part of Apply All."""
    from core import optimizer
    try:
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            return jsonify(optimizer.set_pro_mode(bool(data.get("enabled"))))
        return jsonify(optimizer.get_pro_mode())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/gpu/info")
def api_gpu_info():
    return jsonify(nvidia_clean.get_nvidia_driver_info())


@app.route("/api/gpu/clean-prepare", methods=["POST"])
def api_gpu_clean_prepare():
    try:
        return jsonify(nvidia_clean.prepare_clean_install())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/gpu/tweaks", methods=["POST"])
def api_gpu_tweaks():
    result = nvidia_clean.apply_all_gpu_tweaks()
    _module_status["gpu"] = True
    return jsonify(result)


@app.route("/api/gpu/nvidia-tweaks", methods=["POST"])
def api_gpu_nvidia_tweaks():
    """Force NVIDIA-only tweaks (regardless of detected vendor)."""
    return jsonify({"results": nvidia_clean.apply_nvidia_tweaks()})


@app.route("/api/gpu/amd-tweaks", methods=["POST"])
def api_gpu_amd_tweaks():
    """Force AMD-only tweaks (regardless of detected vendor)."""
    return jsonify({"results": nvidia_clean.apply_amd_tweaks()})


@app.route("/api/gpu/detect")
def api_gpu_detect():
    """Just the detection — useful for UI to show vendor-specific buttons."""
    return jsonify(nvidia_clean.detect_gpu())


# ═══════════════════════════════════════════════════════════════════════════
# GPU Overclocking API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/gpu/oc/capability")
def api_gpu_oc_capability():
    """Detect OC features supported by the current GPU + reported limits."""
    return jsonify(gpu_overclock.detect_gpu_oc_capability())


@app.route("/api/gpu/oc/live")
def api_gpu_oc_live():
    """Live GPU stats: clocks, temp, power, utilization. Poll this during stress tests."""
    return jsonify(gpu_overclock.read_live_state())


@app.route("/api/gpu/oc/watchdog")
def api_gpu_oc_watchdog():
    """OC watchdog status — what targets are being enforced, how many
    drift events the watchdog has corrected, last drift snapshot."""
    return jsonify(gpu_overclock.get_oc_watchdog_status())


@app.route("/api/gpu/oc/apply", methods=["POST"])
def api_gpu_oc_apply():
    """Apply core/mem offsets + power limit. Body: {core_offset_mhz, mem_offset_mhz, power_pct}."""
    data = request.get_json(force=True) or {}
    return jsonify(gpu_overclock.apply_oc(
        core_offset_mhz=int(data.get("core_offset_mhz", 0)),
        mem_offset_mhz=int(data.get("mem_offset_mhz", 0)),
        power_pct=int(data.get("power_pct", 100)),
    ))


@app.route("/api/gpu/oc/reset", methods=["POST"])
def api_gpu_oc_reset():
    """Fully revert to stock clocks and power."""
    return jsonify(gpu_overclock.reset_oc())


@app.route("/api/gpu/oc/probe/start", methods=["POST"])
def api_gpu_oc_probe_start():
    """Begin a stability probe session. Body: {expected_core_max: int (optional)}."""
    data = request.get_json(silent=True) or {}
    return jsonify(gpu_overclock.start_stability_probe(
        expected_core_max=int(data.get("expected_core_max", 0))
    ))


@app.route("/api/gpu/oc/probe/tick", methods=["POST"])
def api_gpu_oc_probe_tick():
    """Poll during WebGL stress. Frontend calls ~once/sec.

    Body (all optional): {frame_time_ms, context_lost, pixel_check_failed}
    """
    data = request.get_json(silent=True) or {}
    return jsonify(gpu_overclock.run_probe_tick(
        frame_time_ms=data.get("frame_time_ms"),
        context_lost=bool(data.get("context_lost", False)),
        pixel_check_failed=bool(data.get("pixel_check_failed", False)),
    ))


@app.route("/api/gpu/oc/probe/end", methods=["POST"])
def api_gpu_oc_probe_end():
    """Finalize probe and get verdict (includes FPS, frame variance, TDR count)."""
    return jsonify(gpu_overclock.end_stability_probe())


@app.route("/api/gpu/oc/verify")
def api_gpu_oc_verify():
    """Read back actual GPU clocks and confirm the requested OC was applied.

    Query params: core_offset (int), mem_offset (int), power_pct (int)
    """
    core = int(request.args.get("core_offset", 0))
    mem = int(request.args.get("mem_offset", 0))
    power = int(request.args.get("power_pct", 100))
    return jsonify(gpu_overclock.verify_oc_applied(core, mem, power))


@app.route("/api/gpu/oc/driver-events")
def api_gpu_oc_driver_events():
    """Recent NVIDIA/AMD driver TDR/crash events from Windows Event Log."""
    seconds = int(request.args.get("seconds", 300))
    return jsonify(gpu_overclock.check_driver_events(seconds_back=seconds))


# v2.9.3 — diagnostic + conflict-detection endpoints.
# When NVAPI returns OK but the boost ceiling doesn't move, the user needs
# a way to see exactly what's happening.  These two endpoints expose:
#   - the raw nvidia-smi clock values
#   - the raw NVAPI offset readback (VF curve + P-state freqDelta)
#   - any other OC tools running that may be overriding our writes
@app.route("/api/gpu/oc/diagnostic")
def api_gpu_oc_diagnostic():
    return jsonify(gpu_overclock.get_oc_diagnostic())


@app.route("/api/gpu/oc/conflicts")
def api_gpu_oc_conflicts():
    return jsonify(gpu_overclock.detect_oc_tools_running())


# v2.9.4 — true stock baseline endpoints.
# GET  → returns the cached baseline (or captures one if missing).
# POST → re-captures fresh, overwriting the cache.  Use after closing
#        Afterburner / driver update / when "stock" looks wrong.
@app.route("/api/gpu/oc/baseline", methods=["GET", "POST"])
def api_gpu_oc_baseline():
    if request.method == "POST":
        return jsonify(gpu_overclock.recalibrate_stock_baseline())
    return jsonify(gpu_overclock.get_stock_baseline())


@app.route("/api/gpu/oc/benchmark/start", methods=["POST"])
def api_gpu_oc_benchmark_start():
    """Start before/after benchmark. Body: {oc: {core, mem, power_pct}} or null to use saved profile."""
    data = request.get_json(silent=True) or {}
    oc = data.get("oc")
    if oc:
        oc = {
            "core_offset_mhz": int(oc.get("core_offset_mhz", 0)),
            "mem_offset_mhz": int(oc.get("mem_offset_mhz", 0)),
            "power_pct": int(oc.get("power_pct", 100)),
        }
    return jsonify(gpu_overclock.benchmark_start(saved_oc=oc))


@app.route("/api/gpu/oc/benchmark/record", methods=["POST"])
def api_gpu_oc_benchmark_record():
    """Submit a probe verdict and advance to next benchmark phase. Body = end_stability_probe verdict."""
    data = request.get_json(force=True) or {}
    return jsonify(gpu_overclock.benchmark_record_result(data))


@app.route("/api/gpu/oc/benchmark/cancel", methods=["POST"])
def api_gpu_oc_benchmark_cancel():
    return jsonify(gpu_overclock.benchmark_cancel())


@app.route("/api/gpu/oc/benchmark/status")
def api_gpu_oc_benchmark_status():
    return jsonify(gpu_overclock.benchmark_status())


@app.route("/api/gpu/oc/auto/start", methods=["POST"])
def api_gpu_oc_auto_start():
    """Begin auto-OC. Body: {core_step_mhz, mem_step_mhz, max_steps}."""
    data = request.get_json(force=True) or {}
    return jsonify(gpu_overclock.auto_oc_start(
        core_step_mhz=int(data.get("core_step_mhz", 15)),
        mem_step_mhz=int(data.get("mem_step_mhz", 50)),
        max_steps=int(data.get("max_steps", 10)),
    ))


@app.route("/api/gpu/oc/auto/next", methods=["POST"])
def api_gpu_oc_auto_next():
    """Advance auto-OC. Body: {stable: bool, reason: str, max_temp_c: int} or null for first step."""
    data = request.get_json(force=True) or {}
    prev = data if data.get("reported", False) else None
    return jsonify(gpu_overclock.auto_oc_next(prev_result=prev))


@app.route("/api/gpu/oc/auto/cancel", methods=["POST"])
def api_gpu_oc_auto_cancel():
    """Abort auto-OC and reset to stock."""
    return jsonify(gpu_overclock.auto_oc_cancel())


# ═══════════════════════════════════════════════════════════════════════════
# Benchmark-Tune (v2.9.9.0) — slower auto-OC mode that picks the offset
# with the BEST measured performance, not just the highest stable one.
# Walks an offset ladder, runs a 23 s benchmark per step, scores via the
# stress-test reference formula (avg_fps + 1%-low + frametime variance +
# thermal penalty), then validates the winner for 30 s.
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/gpu/oc/benchmark-tune/start", methods=["POST"])
def api_gpu_oc_benchmark_tune_start():
    data = request.get_json(force=True) or {}
    return jsonify(gpu_overclock.benchmark_oc_start(
        core_max_offset=int(data.get("core_max_offset", 300)),
        mem_max_offset=int(data.get("mem_max_offset", 1500)),
        max_power=bool(data.get("max_power", True)),
    ))


@app.route("/api/gpu/oc/benchmark-tune/next", methods=["POST"])
def api_gpu_oc_benchmark_tune_next():
    data = request.get_json(force=True) or {}
    prev = data if data.get("reported", False) else None
    return jsonify(gpu_overclock.benchmark_oc_next(prev_result=prev))


@app.route("/api/gpu/oc/benchmark-tune/cancel", methods=["POST"])
def api_gpu_oc_benchmark_tune_cancel():
    return jsonify(gpu_overclock.benchmark_oc_cancel())


@app.route("/api/gpu/oc/benchmark-tune/status")
def api_gpu_oc_benchmark_tune_status():
    return jsonify(gpu_overclock.benchmark_oc_status())


# ═══════════════════════════════════════════════════════════════════════════
# v2.9.9.2 — GPU CRASH RECOVERY ENDPOINTS
# Frontend hits emergency-reset the moment WebGL loses its context.  The
# crash-state endpoint lets the UI poll for recent crashes (so a banner
# can be shown after the user comes back to the page from a black screen).
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/gpu/oc/emergency-reset", methods=["POST"])
def api_gpu_oc_emergency_reset():
    """Best-effort, never-raises reset for crash recovery.  Body optional."""
    data = request.get_json(silent=True) or {}
    return jsonify(gpu_overclock.emergency_reset_oc(
        reason=str(data.get("reason", "user_triggered"))
    ))


@app.route("/api/gpu/oc/crash-state")
def api_gpu_oc_crash_state():
    """Get the most recent crash record (for the UI banner)."""
    return jsonify(gpu_overclock.get_crash_state())


@app.route("/api/gpu/oc/crash-state", methods=["POST"])
def api_gpu_oc_crash_state_clear():
    """Acknowledge / clear the recent crash."""
    gpu_overclock.clear_crash_state()
    return jsonify({"ok": True})


@app.route("/api/gpu/oc/auto/status")
def api_gpu_oc_auto_status():
    return jsonify(gpu_overclock.auto_oc_status())


@app.route("/api/gpu/oc/profile")
def api_gpu_oc_profile():
    """Get the saved profile."""
    return jsonify(gpu_overclock.load_profile())


@app.route("/api/gpu/oc/profile", methods=["POST"])
def api_gpu_oc_profile_save():
    """Save a profile. Body: {core_offset_mhz, mem_offset_mhz, power_pct, apply_on_startup}."""
    data = request.get_json(force=True) or {}
    return jsonify(gpu_overclock.save_profile(data))


@app.route("/api/gpu/oc/profile", methods=["DELETE"])
def api_gpu_oc_profile_delete():
    """Delete saved profile and reset GPU."""
    return jsonify(gpu_overclock.delete_profile())


@app.route("/api/gpu/oc/history")
def api_gpu_oc_history():
    """Return history of previously-saved profiles."""
    return jsonify({"history": gpu_overclock.get_history()})


# ─── Hard GPU temp ceiling (v3) ─────────────────────────────────────────
@app.route("/api/gpu/temp-ceiling/status")
def api_temp_ceiling_status():
    """Live status — current temp, sustained-above seconds, trip count."""
    return jsonify(temp_ceiling.get_status())


@app.route("/api/gpu/temp-ceiling/settings", methods=["POST"])
def api_temp_ceiling_set():
    """Update temp-ceiling settings.  Body accepts any subset of:
       enabled (bool), ceiling_c (int 60-95), sustained_seconds (int 1-60),
       action ('reset'|'notify'), cooldown_seconds (int 10-600)."""
    data = request.get_json(force=True) or {}
    return jsonify(temp_ceiling.set_settings(**data))


@app.route("/api/gpu/temp-ceiling/start", methods=["POST"])
def api_temp_ceiling_start():
    return jsonify(temp_ceiling.start_watchdog())


@app.route("/api/gpu/temp-ceiling/stop", methods=["POST"])
def api_temp_ceiling_stop():
    return jsonify(temp_ceiling.stop_watchdog())


@app.route("/api/gpu/temp-ceiling/test", methods=["POST"])
def api_temp_ceiling_test():
    """Fire a test trip (toast + log entry only — no actual reset).
    Useful for verifying notifications work before you actually need them."""
    return jsonify(temp_ceiling.test_trip())


@app.route("/api/gpu/temp-ceiling/trips")
def api_temp_ceiling_trips():
    return jsonify({"trips": temp_ceiling.get_trips(limit=50)})


@app.route("/api/gpu/temp-ceiling/trips", methods=["DELETE"])
def api_temp_ceiling_trips_clear():
    return jsonify(temp_ceiling.clear_trips())


# ─── Crash recovery (v3) ────────────────────────────────────────────────
@app.route("/api/gpu/crash-recovery/history")
def api_crash_recovery_history():
    return jsonify({"history": crash_recovery.get_history(limit=50)})


@app.route("/api/gpu/crash-recovery/blacklist")
def api_crash_recovery_blacklist():
    """Returns either {exe: [fails]} for all games, or [fails] for one
    if ?exe=name.exe is provided."""
    exe = request.args.get("exe")
    if exe:
        return jsonify({"exe": exe, "fails": crash_recovery.get_blacklist(exe)})
    return jsonify({"all": crash_recovery.get_blacklist()})


@app.route("/api/gpu/crash-recovery/blacklist", methods=["DELETE"])
def api_crash_recovery_blacklist_clear():
    """Clear blacklist for one game (?exe=name.exe) or all (no param)."""
    exe = request.args.get("exe")
    return jsonify(crash_recovery.clear_blacklist(exe))


@app.route("/api/gpu/crash-recovery/history", methods=["DELETE"])
def api_crash_recovery_history_clear():
    return jsonify(crash_recovery.clear_history())


@app.route("/api/gpu/crash-recovery/test", methods=["POST"])
def api_crash_recovery_test():
    """Manually trigger handle_game_exit() with a fake (offset, kind)
    pair.  Body: { exe, display_name, core, mem }.  Useful for verifying
    the toast + step-down + blacklist write all fire end-to-end."""
    data = request.get_json(force=True) or {}
    return jsonify(crash_recovery.handle_game_exit(
        exe=data.get("exe", "test.exe"),
        display_name=data.get("display_name", "Test Game"),
        active_core=int(data.get("core", 100)),
        active_mem=int(data.get("mem", 500)),
    ))


# ─── Live Performance Monitor (v3) ──────────────────────────────────────
@app.route("/api/perf/status")
def api_perf_status():
    """Sampler running? How many samples? Latest reading?"""
    return jsonify(perf_monitor.get_status())


@app.route("/api/perf/samples")
def api_perf_samples():
    """Chart-ready arrays for the last N seconds (default 60)."""
    seconds = int(request.args.get("seconds", 60))
    return jsonify(perf_monitor.get_chart_data(seconds=seconds))


@app.route("/api/perf/score")
def api_perf_score():
    """Stability score (0-100) over the last N seconds (default 30)."""
    seconds = int(request.args.get("seconds", 30))
    return jsonify(perf_monitor.compute_stability_score(seconds=seconds))


@app.route("/api/perf/start", methods=["POST"])
def api_perf_start():
    data = request.get_json(force=True) or {}
    return jsonify(perf_monitor.start(consumer=data.get("consumer", "manual")))


@app.route("/api/perf/stop", methods=["POST"])
def api_perf_stop():
    data = request.get_json(force=True) or {}
    return jsonify(perf_monitor.stop(
        consumer=data.get("consumer", "manual"),
        force=bool(data.get("force", False)),
    ))


# ─── Adaptive Tuning v1 (v3) ────────────────────────────────────────────
@app.route("/api/adaptive/status")
def api_adaptive_status():
    """Live snapshot — current game, current offsets, last score, etc."""
    return jsonify(adaptive_tuning.get_status())


@app.route("/api/adaptive/settings", methods=["GET", "POST"])
def api_adaptive_settings():
    """GET: current global settings.  POST: partial update + persist."""
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        return jsonify(adaptive_tuning.set_settings(**data))
    return jsonify(adaptive_tuning.get_settings())


@app.route("/api/adaptive/games")
def api_adaptive_games():
    """All per-game state rows.  Each row has enabled, current offsets,
    best stable, session count, etc."""
    states = adaptive_tuning.get_game_state()
    return jsonify({"games": states})


@app.route("/api/adaptive/game-toggle", methods=["POST"])
def api_adaptive_game_toggle():
    """Body: { exe, enabled }.  Toggle AT for one specific game."""
    data = request.get_json(force=True) or {}
    return jsonify(adaptive_tuning.set_game_enabled(
        exe=data.get("exe", ""),
        enabled=bool(data.get("enabled", False)),
    ))


@app.route("/api/adaptive/reset", methods=["POST"])
def api_adaptive_reset():
    """Body: { exe } to reset one game, no body for all."""
    data = request.get_json(force=True) or {}
    return jsonify(adaptive_tuning.reset_game_state(exe=data.get("exe")))


@app.route("/api/adaptive/reseed", methods=["POST"])
def api_adaptive_reseed():
    """Body: { exe } to reseed one game from the user's current OC
    baseline, no body to reseed all games.  Unlike /reset, this keeps
    session history + warned offsets, just bumps the starting point
    so AT explores from the user's current OC outwards instead of
    crawling up from 0 or from a stale low baseline."""
    data = request.get_json(silent=True) or {}
    return jsonify(adaptive_tuning.reseed_from_baseline(exe=data.get("exe")))


@app.route("/api/adaptive/history")
def api_adaptive_history():
    return jsonify({"history": adaptive_tuning.get_history(limit=50)})


# v3.1 — per-game mode (balanced / competitive / visual)
@app.route("/api/adaptive/game-mode", methods=["POST"])
def api_adaptive_game_mode():
    """Body: { exe, mode }.  Set the tuning mode for one game."""
    data = request.get_json(force=True) or {}
    return jsonify(adaptive_tuning.set_game_mode(
        exe=data.get("exe", ""),
        mode=data.get("mode", "balanced"),
    ))


@app.route("/api/adaptive/apply-best", methods=["POST"])
def api_adaptive_apply_best():
    """Body: { exe }.  Push this game's recorded best-stable offsets to
    the GPU + saved profile right now (without waiting for game launch)."""
    data = request.get_json(force=True) or {}
    return jsonify(adaptive_tuning.apply_best_stable(exe=data.get("exe", "")))


@app.route("/api/adaptive/clear-blacklist", methods=["POST"])
def api_adaptive_clear_blacklist():
    """Body: { exe } for one game, no body for all.  Clears the per-game
    crash blacklist + warned_offsets list, but keeps best_stable +
    session counts (useful after a driver update)."""
    data = request.get_json(force=True) or {}
    return jsonify(adaptive_tuning.clear_blacklist_only(exe=data.get("exe")))


# ─── AT v3.1 extensions ────────────────────────────────────────────────
@app.route("/api/adaptive/pause", methods=["POST"])
def api_adaptive_pause():
    """Body: { source? } — defaults to 'user'.  Manually pause AT.  The
    pause is stacked by source string, so the matching /resume call needs
    the SAME source to release it."""
    data = request.get_json(force=True) or {}
    src = (data.get("source") or "user").strip() or "user"
    adaptive_tuning.pause(src)
    return jsonify({"ok": True, "paused": True, "source": src,
                    "sources": adaptive_tuning.get_pause_sources()})


@app.route("/api/adaptive/resume", methods=["POST"])
def api_adaptive_resume():
    """Body: { source? } — defaults to 'user'.  Release a pause matching
    the source.  No-op if that source wasn't previously paused."""
    data = request.get_json(force=True) or {}
    src = (data.get("source") or "user").strip() or "user"
    adaptive_tuning.resume(src)
    return jsonify({"ok": True, "paused": adaptive_tuning.is_paused(),
                    "source": src,
                    "sources": adaptive_tuning.get_pause_sources()})


@app.route("/api/adaptive/cross-blacklist", methods=["GET", "POST"])
def api_adaptive_cross_blacklist():
    """GET returns the cross-game offset blacklist (driver-level crashes
    at offsets X mean *no game* gets to try X anymore).  POST adds an
    entry: body { core, mem, source_exe?, reason? }."""
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        try:
            core = int(data.get("core", 0))
            mem  = int(data.get("mem", 0))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "err": "core/mem must be integers"}), 400
        adaptive_tuning.add_cross_game_blacklist(
            core, mem,
            source_exe=data.get("source_exe", ""),
            reason=data.get("reason", "manual"),
        )
        return jsonify({"ok": True,
                        "entries": adaptive_tuning.get_cross_game_blacklist()})
    return jsonify({"entries": adaptive_tuning.get_cross_game_blacklist()})


@app.route("/api/adaptive/clear-cross-blacklist", methods=["POST"])
def api_adaptive_clear_cross_blacklist():
    """Wipe the cross-game blacklist.  Use after a driver/firmware update
    that may have fixed the underlying instability."""
    adaptive_tuning.clear_cross_game_blacklist()
    return jsonify({"ok": True,
                    "entries": adaptive_tuning.get_cross_game_blacklist()})


@app.route("/api/adaptive/diagnostics")
def api_adaptive_diagnostics():
    """Comprehensive AT diagnostic dump — surfaces every reason AT might
    be (in)active, plus live GPU/frame readings, plus the engagement
    state breakdown.  The AT diagnostic panel polls this on a slow
    interval (~5 s) so the user can see exactly what AT is doing."""
    return jsonify(adaptive_tuning.get_diagnostics())


@app.route("/api/frame-telemetry/status")
def api_frame_telemetry_status():
    """Status of the frame telemetry source (RTSS shared memory).  UI
    shows this in the AT live row + the diagnostics panel."""
    try:
        from core import frame_telemetry
        return jsonify(frame_telemetry.get_status())
    except Exception as e:
        return jsonify({"running": False, "available": {"available": False,
                        "reason": f"frame_telemetry import failed: {e}"}})


@app.route("/api/frame-telemetry/start", methods=["POST"])
def api_frame_telemetry_start():
    """Body: { pid?, exe_name? }.  Force-start the frame telemetry sampler
    targeted at this process.  Normally AT does this on engage — but the
    diagnostic panel exposes a manual trigger for cases where AT isn't
    engaging (unknown game) but the user still wants to see frame data."""
    try:
        from core import frame_telemetry
        data = request.get_json(force=True) or {}
        pid = data.get("pid")
        exe = data.get("exe_name") or data.get("exe")
        return jsonify(frame_telemetry.start_tracking(
            pid=int(pid) if pid else None,
            exe_name=exe,
        ))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/frame-telemetry/stop", methods=["POST"])
def api_frame_telemetry_stop():
    try:
        from core import frame_telemetry
        return jsonify(frame_telemetry.stop_tracking())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/frame-telemetry/install", methods=["POST"])
def api_frame_telemetry_install():
    """One-click install for PresentMon (Microsoft's frame timing tool,
    ~600 KB from their official GitHub release).  Runs only when AT
    engages on a game — no background process.  Non-blocking; poll
    /api/frame-telemetry/install/progress to see download status."""
    try:
        from core import frame_telemetry
        return jsonify(frame_telemetry.install_presentmon())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/frame-telemetry/install/progress")
def api_frame_telemetry_install_progress():
    try:
        from core import frame_telemetry
        return jsonify(frame_telemetry.install_progress())
    except Exception as e:
        return jsonify({"state": "error", "err": str(e)})


# ── v3.2-beta.3: gameplay-vs-menu classifier ────────────────────────
@app.route("/api/adaptive/gameplay-state")
def api_adaptive_gameplay_state():
    """Current gameplay/menu classification for a game.  Defaults to
    the currently-engaged exe when no `exe` query param is given."""
    try:
        from core import gameplay_state
        exe = request.args.get("exe") or ""
        if not exe:
            st = adaptive_tuning.get_status()
            exe = st.get("active_exe") or ""
        if not exe:
            return jsonify({"state": "unknown", "reason": "no active game"})
        return jsonify(gameplay_state.get_state(exe))
    except Exception as e:
        return jsonify({"state": "error", "err": str(e)})


@app.route("/api/adaptive/gameplay-baselines")
def api_adaptive_gameplay_baselines():
    try:
        from core import gameplay_state
        return jsonify({"baselines": gameplay_state.get_all_baselines()})
    except Exception as e:
        return jsonify({"baselines": {}, "err": str(e)})


@app.route("/api/adaptive/gameplay-recalibrate", methods=["POST"])
def api_adaptive_gameplay_recalibrate():
    """Body: { exe }.  Anchor the per-game baseline to the current
    rolling-window median (marked manual so auto-rebuild doesn't drift
    it)."""
    try:
        from core import gameplay_state
        data = request.get_json(force=True) or {}
        exe = data.get("exe") or ""
        if not exe:
            st = adaptive_tuning.get_status()
            exe = st.get("active_exe") or ""
        if not exe:
            return jsonify({"ok": False, "err": "no exe provided"})
        return jsonify(gameplay_state.recalibrate_baseline(exe))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/adaptive/gameplay-reset", methods=["POST"])
def api_adaptive_gameplay_reset():
    """Body: { exe }.  Wipe samples + baseline + cached state for one
    game.  AT will rebuild from scratch on the next engage."""
    try:
        from core import gameplay_state
        data = request.get_json(force=True) or {}
        exe = data.get("exe") or ""
        if not exe:
            return jsonify({"ok": False, "err": "no exe provided"})
        return jsonify(gameplay_state.reset(exe))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/adaptive/menu-signature/add", methods=["POST"])
def api_adaptive_menu_sig_add():
    """Body: { exe? }.  Record the current per-game sample (fps, util)
    as a known menu signature.  Used when the user knows they're in a
    menu but the auto-classifier hasn't picked it up yet."""
    try:
        from core import gameplay_state
        data = request.get_json(force=True) or {}
        exe = data.get("exe") or ""
        if not exe:
            st = adaptive_tuning.get_status()
            exe = st.get("active_exe") or ""
        if not exe:
            return jsonify({"ok": False, "err": "no exe provided"})
        return jsonify(gameplay_state.add_menu_signature(exe, source="manual"))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ── v3.2-beta.7: dependency manager ─────────────────────────────────
@app.route("/api/dependencies/status")
def api_dependencies_status():
    """List every tracked optional dependency + whether it's installed.
    UI polls this on a slow timer + after install actions."""
    try:
        from core import dependencies
        st  = dependencies.check_all()
        prog = dependencies.get_progress()
        settings = dependencies.get_settings()
        restart  = dependencies.has_pending_restart()
        return jsonify({
            "deps":            st["deps"],
            "checked_at":      st["checked_at"],
            "progress":        prog,
            "settings":        settings,
            "pending_restart": restart,
        })
    except Exception as e:
        return jsonify({"deps": [], "err": str(e)})


@app.route("/api/dependencies/install", methods=["POST"])
def api_dependencies_install():
    """Body: { keys: ["presentmon2", "vigembus"] } — or omit to install
    every missing auto-installable dep.  Runs in a background thread;
    poll /api/dependencies/status.progress for updates."""
    try:
        from core import dependencies
        data = request.get_json(force=True) or {}
        keys = data.get("keys")
        return jsonify(dependencies.install_missing_async(only=keys))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/dependencies/settings", methods=["POST"])
def api_dependencies_settings():
    """Body: { auto_install_enabled?: bool, opt_out?: {key, value} }"""
    try:
        from core import dependencies
        data = request.get_json(force=True) or {}
        if "auto_install_enabled" in data:
            dependencies.set_auto_install_enabled(bool(data["auto_install_enabled"]))
        if "opt_out" in data:
            o = data["opt_out"] or {}
            dependencies.set_opt_out(o.get("key", ""), bool(o.get("value", False)))
        return jsonify({"ok": True, "settings": dependencies.get_settings()})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/dependencies/restart-now", methods=["POST"])
def api_dependencies_restart_now():
    try:
        from core import dependencies
        return jsonify(dependencies.request_restart_now())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/dependencies/restart-later", methods=["POST"])
def api_dependencies_restart_later():
    try:
        from core import dependencies
        return jsonify(dependencies.cancel_pending_restart())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ─── v3.3 — Capture: clipper + screenshotter + hotkeys ─────────────────

# ─── v3.3 — Error reporter (replaces GitHub issue flow) ────────────

@app.route("/api/errors/preview", methods=["POST"])
def api_errors_preview():
    """Build the report payload WITHOUT sending it.  UI uses this to
    show the user exactly what will be transmitted before they click
    Submit — full transparency."""
    try:
        from core import error_reporter
        data = request.get_json(force=True) or {}
        return jsonify(error_reporter.build_report(
            kind=data.get("kind", ""),
            message=data.get("message", ""),
            detail=data.get("detail", ""),
            user_note=data.get("user_note", ""),
        ))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ─── beta.17: Disk space analyzer (WizTree-style) ─────────────────────
@app.route("/api/cleaner/disk/drives")
def api_cleaner_disk_drives():
    """List local fixed drives with total/free GB + drive label.  UI's
    drive picker reads this on Cleaner page open."""
    try:
        from core import disk_analyzer
        return jsonify({"ok": True, "drives": disk_analyzer.list_drives()})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/scan", methods=["POST"])
def api_cleaner_disk_scan():
    """Kick off a background drive scan.  Returns scan_id; poll
    /status for progress."""
    try:
        from core import disk_analyzer
        data = request.get_json(force=True) or {}
        drive = (data.get("drive") or "C:").strip()
        return jsonify(disk_analyzer.start_scan(drive))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/status")
def api_cleaner_disk_status():
    """Scan progress + final summary once done.  Always cheap to poll."""
    try:
        from core import disk_analyzer
        return jsonify(disk_analyzer.get_status())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/result")
def api_cleaner_disk_result():
    """Drill-down result fetch.  Pass ?path=... (default root) and
    ?depth=N to control how many sub-levels of children come back —
    keeps the JSON payload reasonable for big drives."""
    try:
        from core import disk_analyzer
        path  = request.args.get("path", "")
        try: depth = int(request.args.get("depth", 1))
        except ValueError: depth = 1
        return jsonify(disk_analyzer.get_result(path=path, depth=depth))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/cancel", methods=["POST"])
def api_cleaner_disk_cancel():
    """Request cancellation of the running scan."""
    try:
        from core import disk_analyzer
        return jsonify(disk_analyzer.cancel_scan())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/delete", methods=["POST"])
def api_cleaner_disk_delete():
    """Delete selected paths.  Hard-capped at 100 paths and 50 GB per
    call; refuses anything under Windows / Program Files / GhostShell.
    Pass `to_recycle_bin: true` to route through Recycle Bin (slower
    but reversible)."""
    try:
        from core import disk_analyzer
        data = request.get_json(force=True) or {}
        paths = data.get("paths") or []
        return jsonify(disk_analyzer.delete_paths(
            paths=paths,
            to_recycle_bin=bool(data.get("to_recycle_bin", False)),
        ))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/cleaner/disk/delete-log")
def api_cleaner_disk_delete_log():
    """Ring-buffer audit log of every analyzer-initiated delete."""
    try:
        from core import disk_analyzer
        try: limit = int(request.args.get("limit", 50))
        except ValueError: limit = 50
        return jsonify({"ok": True,
                        "rows": disk_analyzer.get_delete_log(limit)})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ─── beta.16: PC integrity scan ───────────────────────────────────────
@app.route("/api/integrity/status")
def api_integrity_status():
    """Current state + settings + last cycle summary."""
    try:
        from core import integrity_scan
        return jsonify(integrity_scan.get_status())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ─── Competitive Input Latency (beta.3) ──────────────────────────────
@app.route("/api/competitive/status")
def api_competitive_status():
    return jsonify(competitive_latency.status())


@app.route("/api/competitive/apply", methods=["POST"])
def api_competitive_apply():
    return jsonify(competitive_latency.apply())


@app.route("/api/competitive/reset", methods=["POST"])
def api_competitive_reset():
    return jsonify(competitive_latency.reset())


# ─── Latency Doctor (beta.5) ─────────────────────────────────────────
@app.route("/api/latency-doctor/scan")
def api_latency_doctor_scan():
    return jsonify(latency_doctor.scan())


@app.route("/api/latency-doctor/fix", methods=["POST"])
def api_latency_doctor_fix():
    return jsonify(latency_doctor.fix())


@app.route("/api/integrity/history")
def api_integrity_history():
    """Last N cycles from the audit log (default 20, max 100)."""
    try:
        from core import integrity_scan
        try: limit = int(request.args.get("limit", 20))
        except ValueError: limit = 20
        return jsonify({"ok": True, "rows": integrity_scan.get_history(limit)})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/integrity/run-now", methods=["POST"])
def api_integrity_run_now():
    """Manual trigger — run a scan cycle synchronously.  Useful for the
    UI's 'Run now' button.  Returns the cycle summary once finished
    (which can take up to 5 minutes in the worst case)."""
    try:
        from core import integrity_scan
        return jsonify(integrity_scan.run_once())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/integrity/settings", methods=["GET", "POST"])
def api_integrity_settings():
    try:
        from core import integrity_scan
        if request.method == "POST":
            data = request.get_json(force=True) or {}
            return jsonify({"ok": True,
                            "settings": integrity_scan.set_settings(**data)})
        return jsonify({"ok": True,
                        "settings": integrity_scan.get_settings()})
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/errors/submit", methods=["POST"])
def api_errors_submit():
    """POST one error report to Railway."""
    try:
        from core import error_reporter
        data = request.get_json(force=True) or {}
        return jsonify(error_reporter.submit_report(
            kind=data.get("kind", ""),
            message=data.get("message", ""),
            detail=data.get("detail", ""),
            user_note=data.get("user_note", ""),
        ))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/errors/status")
def api_errors_status():
    try:
        from core import error_reporter
        return jsonify({
            **error_reporter.get_status(),
            "local_reports": error_reporter.list_local_reports(50),
        })
    except Exception as e:
        return jsonify({"err": str(e)})


@app.route("/api/errors/settings", methods=["POST"])
def api_errors_settings():
    try:
        from core import error_reporter
        data = request.get_json(force=True) or {}
        return jsonify(error_reporter.set_settings(**data))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/util/open-path", methods=["POST"])
def api_util_open_path():
    """Open a path in Explorer.

    Body:
      { path, mode? }   mode = 'reveal' (default for files — opens
                        parent folder + highlights), 'launch' (opens
                        the file in its default app), 'open-folder'
                        (folder paths only — opens it in Explorer).

    v3.3.0-beta.12 fix: GhostShell runs with uac_admin=True (manifest
    requests elevation), so subprocess.Popen(['explorer', ...]) fails
    silently — Windows refuses to spawn user-level explorer.exe from
    an admin process and the call just drops.  Use ShellExecuteW
    instead; the shell explicitly handles cross-integrity-level
    launches, and that's the form Microsoft documents.
    """
    try:
        import ctypes
        data = request.get_json(force=True) or {}
        p = (data.get("path") or "").strip().strip('"')
        mode = (data.get("mode") or "").strip().lower()
        if not p:
            return jsonify({"ok": False, "err": "no path"})

        SW_SHOW = 5

        def _shell_execute(file_, params=None):
            """Returns HINSTANCE-cast int from ShellExecuteW.  >32 = ok."""
            shell32 = ctypes.windll.shell32
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            r = shell32.ShellExecuteW(
                None, "open", file_, params, None, SW_SHOW,
            )
            return int(r) if r else 0

        # URL handling — http:// or https:// goes straight to the
        # default browser via ShellExecuteW.  No path-existence check
        # needed; the shell resolves the URL itself.
        low = p.lower()
        if low.startswith(("http://", "https://", "mailto:")):
            r = _shell_execute(p, None)
            if r <= 32:
                return jsonify({"ok": False, "err": f"ShellExecute URL rc={r}"})
            return jsonify({"ok": True, "mode": "url", "path": p})

        if not os.path.exists(p):
            return jsonify({"ok": False, "err": f"path not found: {p}"})
        abs_p = os.path.abspath(p)

        if os.path.isdir(abs_p) or mode == "open-folder":
            r = _shell_execute(abs_p, None)
            if r <= 32:
                return jsonify({"ok": False, "err": f"ShellExecute rc={r}"})
        elif mode == "launch":
            # Open file in its default app — image viewer, video player, etc.
            r = _shell_execute(abs_p, None)
            if r <= 32:
                return jsonify({"ok": False, "err": f"ShellExecute rc={r}"})
        else:
            # mode == "reveal" (default): explorer.exe /select,"<path>"
            # via ShellExecuteW so the cross-integrity-level transition
            # is handled by the shell.  Quote the path because file
            # names with spaces would otherwise get split by explorer.
            r = _shell_execute("explorer.exe", f'/select,"{abs_p}"')
            if r <= 32:
                # Fallback: open just the parent folder (no selection
                # but at least the user sees their clip's directory).
                parent = os.path.dirname(abs_p)
                if os.path.isdir(parent):
                    r2 = _shell_execute(parent, None)
                    if r2 <= 32:
                        return jsonify({"ok": False,
                                         "err": f"ShellExecute reveal rc={r} parent rc={r2}"})
                else:
                    return jsonify({"ok": False, "err": f"ShellExecute reveal rc={r}"})
        return jsonify({"ok": True, "mode": mode or "reveal", "path": abs_p})
    except Exception as e:
        log.exception("open-path failed")
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/capture/status")
def api_capture_status():
    """Combined snapshot of clipper + hotkey state + recent files."""
    try:
        from core import clipper, screenshotter, capture_hotkeys
        return jsonify({
            "clipper":      clipper.get_status(),
            "hotkeys":      capture_hotkeys.get_status(),
            "recent_clips":       clipper.list_recent(20),
            "recent_screenshots": screenshotter.list_recent(20),
            "screenshots_dir":    screenshotter.get_screenshots_dir(),
        })
    except Exception as e:
        return jsonify({"err": str(e)})


@app.route("/api/clipper/start", methods=["POST"])
def api_clipper_start():
    try:
        from core import clipper
        return jsonify(clipper.start_buffer())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/stop", methods=["POST"])
def api_clipper_stop():
    try:
        from core import clipper
        return jsonify(clipper.stop_buffer())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/save", methods=["POST"])
def api_clipper_save():
    """Body: { seconds? }.  Save the last N seconds of buffer as a clip."""
    try:
        from core import clipper
        data = request.get_json(force=True) or {}
        secs = data.get("seconds")
        return jsonify(clipper.save_clip(seconds=int(secs) if secs else None))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/settings", methods=["POST"])
def api_clipper_set_settings():
    try:
        from core import clipper, capture_hotkeys
        data = request.get_json(force=True) or {}
        out = clipper.set_settings(**data)
        # If hotkeys changed, re-register them
        s = out["settings"]
        if "save_hotkey" in data or "screenshot_hotkey" in data:
            try:
                _wire_capture_hotkeys(s)
            except Exception as e:
                out["hotkey_warning"] = str(e)
        return jsonify(out)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/audio-devices")
def api_clipper_audio_devices():
    """Probe dshow for available audio inputs (mic + loopback)."""
    try:
        from core import clipper
        return jsonify(clipper.list_audio_devices())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/monitors")
def api_clipper_monitors():
    try:
        from core import clipper
        return jsonify({"monitors": clipper.list_monitors()})
    except Exception as e:
        return jsonify({"monitors": [], "err": str(e)})


# ═══════════════════════════════════════════════════════════════════
# OBS Studio backend (v3.3.0-beta.15+)
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/obs/status")
def api_obs_status():
    """Detection + settings + readiness, for the Capture page UI."""
    try:
        from core import obs_backend
        return jsonify(obs_backend.get_status())
    except Exception as e:
        log.exception("obs_status failed")
        return jsonify({"err": str(e),
                          "detected": {"ok": False},
                          "settings": {},
                          "ready":    False})


@app.route("/api/obs/detect", methods=["POST"])
def api_obs_detect():
    """Force a fresh OBS-installation probe (bypasses the 60-s cache)."""
    try:
        from core import obs_backend
        return jsonify(obs_backend.detect_obs(force_refresh=True))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/settings", methods=["POST"])
def api_obs_settings():
    """Update OBS backend settings (toggle, exe override, etc)."""
    try:
        from core import obs_backend
        data = request.get_json(force=True) or {}
        return jsonify(obs_backend.set_settings(**data))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/connect", methods=["POST"])
def api_obs_connect():
    """Attempt to connect to OBS' WebSocket server with the current
    persisted settings.  Returns the connection state dict."""
    try:
        from core import obs_backend
        data = request.get_json(force=True) or {}
        force = bool(data.get("force"))
        return jsonify(obs_backend.connect(force_reconnect=force))
    except Exception as e:
        log.exception("obs connect failed")
        return jsonify({"connected": False, "connect_err": str(e)})


@app.route("/api/obs/disconnect", methods=["POST"])
def api_obs_disconnect():
    try:
        from core import obs_backend
        return jsonify(obs_backend.disconnect())
    except Exception as e:
        return jsonify({"connected": False, "connect_err": str(e)})


@app.route("/api/obs/launch", methods=["POST"])
def api_obs_launch():
    """Spawn OBS Studio detached.  Returns once the process has been
    created; caller is expected to poll status until WebSocket comes
    online (usually 2-5 s)."""
    try:
        from core import obs_backend
        return jsonify(obs_backend.launch_obs())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/setup-scene", methods=["POST"])
def api_obs_setup_scene():
    """Create the GhostShell scene + sources in OBS based on the
    current clipper settings (selected monitor, mic on/off,
    system audio on/off).  Idempotent — running it multiple times
    only adds anything missing."""
    try:
        from core import obs_backend, clipper
        cs = clipper.get_settings()
        target = (cs.get("capture_target") or "monitor:0").strip()
        # Resolve monitor index from "monitor:N" form
        idx = 0
        if target.startswith("monitor:"):
            try: idx = int(target.split(":", 1)[1])
            except Exception: idx = 0
        return jsonify(obs_backend.setup_scene(
            monitor_idx  = idx,
            system_audio = bool(cs.get("system_audio_enabled")),
            mic_enabled  = bool(cs.get("mic_enabled")),
        ))
    except Exception as e:
        log.exception("obs setup-scene failed")
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/start-buffer", methods=["POST"])
def api_obs_start_buffer():
    """Ensure the OBS replay buffer is configured + running with the
    clipper's max_buffer_seconds."""
    try:
        from core import obs_backend, clipper
        cs = clipper.get_settings()
        return jsonify(obs_backend.ensure_replay_buffer(
            buffer_seconds = cs.get("max_buffer_seconds") or 60,
        ))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/stop-buffer", methods=["POST"])
def api_obs_stop_buffer():
    """Cleanly stop the OBS replay buffer if running."""
    try:
        from core import obs_backend
        return jsonify(obs_backend.stop_replay_buffer_safe())
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


# ═══════════════════════════════════════════════════════════════════
# Stream Helper (v3.3.1-beta.1+) — auto OBS configuration for
# streaming gameplay.  Hides the manual Capture controls when on.
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/stream-helper/status")
def api_stream_helper_status():
    try:
        from core import stream_helper
        return jsonify(stream_helper.get_status())
    except Exception as e:
        log.exception("stream_helper status failed")
        return jsonify({"err": str(e), "settings": {}, "obs_connected": False})


@app.route("/api/stream-helper/settings", methods=["POST"])
def api_stream_helper_settings():
    try:
        from core import stream_helper
        data = request.get_json(force=True) or {}
        r = stream_helper.set_settings(**data)
        # Start/stop the watcher when the master toggle flips
        s = r.get("settings") or {}
        if s.get("enabled"):
            stream_helper.start_watcher()
        else:
            stream_helper.stop_watcher()
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/stream-helper/setup", methods=["POST"])
def api_stream_helper_setup():
    """Force a one-shot scene + source setup in OBS.  Useful when the
    user has just enabled the toggle and wants the scene created
    immediately without waiting for the watcher loop to tick."""
    try:
        from core import stream_helper, game_profiles
        fg = game_profiles.get_foreground_game()
        exe = (fg or {}).get("exe") if fg else None
        return jsonify(stream_helper.setup_scene(active_game_exe=exe))
    except Exception as e:
        log.exception("stream_helper setup failed")
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/obs/save-replay", methods=["POST"])
def api_obs_save_replay():
    """Trigger SaveReplayBuffer + move the file into Videos/GhostShell/Clips.
    Returns the final clip path."""
    try:
        from core import obs_backend, clipper
        from datetime import datetime
        cs = clipper.get_settings()
        out_dir = cs.get("output_dir") or clipper.CLIPS_DIR
        # Build a sensible name from the foreground exe like FFmpeg path does
        try:
            fg = clipper._foreground_window_info()
            base = (fg.get("exe") or "clip").replace(".exe", "")
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
        except Exception:
            safe = "clip"
        stem = f"{safe}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        return jsonify(obs_backend.save_replay(target_dir=out_dir, prefix=stem))
    except Exception as e:
        log.exception("obs save-replay failed")
        return jsonify({"ok": False, "err": str(e), "path": ""})


@app.route("/api/community/status")
def api_community_status():
    try:
        from core import community
        return jsonify(community.get_status())
    except Exception as e:
        return jsonify({"err": str(e)})


@app.route("/api/community/refresh", methods=["POST"])
def api_community_refresh():
    """Force-refetch community games list from Railway + merge."""
    try:
        from core import community
        r = community.fetch_games(force=True)
        if r.get("ok") or r.get("games"):
            community.merge_into_known_games_async()
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/community/settings", methods=["POST"])
def api_community_settings():
    try:
        from core import community
        data = request.get_json(force=True) or {}
        return jsonify(community.set_settings(**data))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/clipper/delete", methods=["POST"])
def api_clipper_delete():
    try:
        from core import clipper
        data = request.get_json(force=True) or {}
        return jsonify(clipper.delete_clip(data.get("path", "")))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/screenshot/take", methods=["POST"])
def api_screenshot_take():
    """Body: { target? } — 'foreground' (default), 'game', or 'display'."""
    try:
        from core import screenshotter
        data = request.get_json(force=True) or {}
        target = (data.get("target") or "foreground").lower()
        return jsonify(screenshotter.take_screenshot(target=target))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/screenshot/delete", methods=["POST"])
def api_screenshot_delete():
    try:
        from core import screenshotter
        data = request.get_json(force=True) or {}
        return jsonify(screenshotter.delete_screenshot(data.get("path", "")))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


def _wire_capture_hotkeys(settings: dict) -> None:
    """Register / re-register the save-clip + screenshot hotkeys based
    on the clipper settings.  Called at boot and on every settings save."""
    from core import capture_hotkeys, clipper, screenshotter
    save_hk = settings.get("save_hotkey", "F10")
    ss_hk   = settings.get("screenshot_hotkey", "F9")

    def _save_clip_cb():
        clipper.save_clip()    # default duration from settings
    def _screenshot_cb():
        screenshotter.take_screenshot(target="foreground")

    capture_hotkeys.start()
    capture_hotkeys.register_binding("save_clip",  save_hk, _save_clip_cb)
    capture_hotkeys.register_binding("screenshot", ss_hk,   _screenshot_cb)


# beta.15 — /api/dependencies/start-rtss removed.  GhostShell no longer
# launches RTSS from any auto-path.  Old clients that still POST here
# get a clean 410 GONE so they know to drop the call rather than seeing
# a 404 + retry loop.  The fallback message tells the user to launch
# RTSS themselves if they actually want it running.
@app.route("/api/dependencies/start-rtss", methods=["POST"])
def api_dependencies_start_rtss_gone():
    return jsonify({
        "ok": False,
        "removed": True,
        "err": ("RTSS is no longer auto-launched or auto-installed by "
                "Vispora.  If you want RTSS running, start it from "
                "the Start menu — Vispora will detect it for the "
                "OC-tool conflict warning but won't touch it.")
    }), 410


@app.route("/api/adaptive/menu-signature/remove", methods=["POST"])
def api_adaptive_menu_sig_remove():
    """Body: { exe, index }.  Remove one learned menu signature by index."""
    try:
        from core import gameplay_state
        data = request.get_json(force=True) or {}
        exe = data.get("exe") or ""
        idx = int(data.get("index", -1))
        if not exe or idx < 0:
            return jsonify({"ok": False, "err": "exe + index required"})
        return jsonify(gameplay_state.remove_menu_signature(exe, idx))
    except Exception as e:
        return jsonify({"ok": False, "err": str(e)})


@app.route("/api/adaptive/game-knobs", methods=["POST"])
def api_adaptive_game_knobs():
    """Body: { exe, thermal_limit_c?, power_bound_pct?, step_multiplier?,
    dry_run? }.  Per-game tuning knobs — any omitted field is left
    untouched.  Returns the resulting state."""
    data = request.get_json(force=True) or {}
    exe = (data.get("exe") or "").strip()
    if not exe:
        return jsonify({"ok": False, "err": "exe required"}), 400
    knobs = {}
    for k in ("thermal_limit_c", "power_bound_pct", "step_multiplier"):
        if k in data and data[k] is not None:
            try:
                knobs[k] = float(data[k])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "err": f"{k} must be numeric"}), 400
    if "dry_run" in data:
        knobs["dry_run"] = bool(data["dry_run"])
    return jsonify(adaptive_tuning.set_game_knobs(exe=exe, **knobs))


# ─── System Health Audit (Dashboard card) ──────────────────────────────
@app.route("/api/health/audit")
def api_health_audit():
    """Full read-only system audit — display, RAM, GPU, power, storage,
    GhostShell state.  Runs in a couple of seconds; safe to poll."""
    return jsonify(health_audit.audit())


# ─── Snapshots & Restore (v3.1) ────────────────────────────────────────
@app.route("/api/snapshots")
def api_snapshots_list():
    """List all snapshots, newest first."""
    return jsonify({"snapshots": snapshots.list_snapshots()})


@app.route("/api/snapshots", methods=["POST"])
def api_snapshots_create():
    """Body: { name, description? }.  Take a 'full' snapshot of every
    GhostShell-touched registry path."""
    data = request.get_json(force=True) or {}
    return jsonify(snapshots.take_snapshot(
        name=data.get("name", "manual"),
        description=data.get("description", ""),
        scope="full",
    ))


@app.route("/api/snapshots/<snap_id>")
def api_snapshots_get(snap_id):
    s = snapshots.get_snapshot(snap_id)
    if s is None:
        return jsonify({"ok": False, "err": "not found"}), 404
    return jsonify(s)


@app.route("/api/snapshots/<snap_id>/restore", methods=["POST"])
def api_snapshots_restore(snap_id):
    """Re-import every .reg file in the snapshot bundle.  REBOOT
    RECOMMENDED after a full restore."""
    return jsonify(snapshots.restore_snapshot(snap_id))


@app.route("/api/snapshots/<snap_id>", methods=["DELETE"])
def api_snapshots_delete(snap_id):
    return jsonify(snapshots.delete_snapshot(snap_id))


@app.route("/api/snapshots/prune", methods=["POST"])
def api_snapshots_prune():
    data = request.get_json(force=True) or {}
    return jsonify(snapshots.prune_old(keep_last=int(data.get("keep_last", 10))))


# ─── Game Library Scanner (v3.1) ───────────────────────────────────────
@app.route("/api/library")
def api_library_get():
    """Return the most recent cached library scan.  Empty if never scanned."""
    return jsonify(library_scanner.get_cached())


@app.route("/api/library/scan", methods=["POST"])
def api_library_scan():
    """Force a fresh scan across every supported launcher.  Body may
    contain { force: true } — older cached results are bypassed."""
    data = request.get_json(silent=True) or {}
    return jsonify(library_scanner.scan_all(force=bool(data.get("force", True))))


@app.route("/api/library/enable_at", methods=["POST"])
def api_library_enable_at():
    """Body: { exe: "...", enabled: true, mode?: "competitive" }.
    Enables Adaptive Tuning for one detected game and optionally sets the
    mode at the same time.  No game-launch required — the AT state row is
    created on the spot."""
    data = request.get_json(force=True) or {}
    exe = (data.get("exe") or "").strip()
    if not exe:
        return jsonify({"ok": False, "err": "exe required"}), 400
    enabled = bool(data.get("enabled", True))
    out = adaptive_tuning.set_game_enabled(exe, enabled)
    mode = data.get("mode")
    if mode:
        adaptive_tuning.set_game_mode(exe, mode)
    return jsonify({"ok": True, "state": out})


# ─── Background Pauser (v3.1) ──────────────────────────────────────────
@app.route("/api/pauser/status")
def api_pauser_status():
    return jsonify(background_pauser.get_status())


@app.route("/api/pauser/settings", methods=["POST"])
def api_pauser_set_settings():
    data = request.get_json(force=True) or {}
    return jsonify(background_pauser.set_settings(**data))


@app.route("/api/pauser/category", methods=["POST"])
def api_pauser_category():
    data = request.get_json(force=True) or {}
    return jsonify(background_pauser.set_category_enabled(
        data.get("category", ""), bool(data.get("enabled", False))))


@app.route("/api/pauser/pause_now", methods=["POST"])
def api_pauser_pause_now():
    return jsonify(background_pauser.pause_now(reason="manual"))


@app.route("/api/pauser/resume_now", methods=["POST"])
def api_pauser_resume_now():
    return jsonify(background_pauser.resume_now(reason="manual"))


# ─── Tournament Mode (v3.1) ────────────────────────────────────────────
@app.route("/api/tournament/status")
def api_tournament_status():
    return jsonify(tournament_mode.get_status())


@app.route("/api/tournament/enable", methods=["POST"])
def api_tournament_enable():
    data = request.get_json(silent=True) or {}
    duration = int(data.get("duration_min", 60))
    return jsonify(tournament_mode.enable(duration_min=duration))


@app.route("/api/tournament/disable", methods=["POST"])
def api_tournament_disable():
    return jsonify(tournament_mode.disable(reason="manual"))


@app.route("/api/tournament/extend", methods=["POST"])
def api_tournament_extend():
    data = request.get_json(force=True) or {}
    extra = int(data.get("extra_min", 15))
    return jsonify(tournament_mode.extend_timer(extra))


# ─── Network burst monitor (v3.1) ──────────────────────────────────────
@app.route("/api/network/live")
def api_network_live():
    return jsonify(net_monitor.get_live())


@app.route("/api/network/history")
def api_network_history():
    secs = int(request.args.get("seconds", 60))
    return jsonify({"history": net_monitor.get_history(seconds=secs)})


@app.route("/api/network/top")
def api_network_top():
    n = int(request.args.get("limit", 15))
    return jsonify({"top": net_monitor.get_top_processes(limit=n)})


@app.route("/api/network/pause-process", methods=["POST"])
def api_network_pause_process():
    """Suspend a process by PID — same ctypes path as background_pauser."""
    data = request.get_json(force=True) or {}
    pid = int(data.get("pid", 0))
    if not pid:
        return jsonify({"ok": False, "err": "pid required"}), 400
    return jsonify(net_monitor.pause_process(pid))


@app.route("/api/network/resume-process", methods=["POST"])
def api_network_resume_process():
    data = request.get_json(force=True) or {}
    pid = int(data.get("pid", 0))
    if not pid:
        return jsonify({"ok": False, "err": "pid required"}), 400
    return jsonify(net_monitor.resume_process(pid))


# ─── Gamepad Mapper (v3.1) ─────────────────────────────────────────────
@app.route("/api/gamepad/status")
def api_gamepad_status():
    return jsonify(gamepad_mapper.get_status())


@app.route("/api/gamepad/profiles")
def api_gamepad_profiles():
    return jsonify(gamepad_mapper.list_profiles())


@app.route("/api/gamepad/profiles", methods=["POST"])
def api_gamepad_save_profile():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "err": "name required"}), 400
    return jsonify(gamepad_mapper.save_profile(name, data))


@app.route("/api/gamepad/profiles/<name>", methods=["DELETE"])
def api_gamepad_delete_profile(name):
    return jsonify(gamepad_mapper.delete_profile(name))


@app.route("/api/gamepad/active", methods=["POST"])
def api_gamepad_set_active():
    data = request.get_json(force=True) or {}
    return jsonify(gamepad_mapper.set_active_profile(data.get("name") or None))


@app.route("/api/gamepad/enabled", methods=["POST"])
def api_gamepad_set_enabled():
    data = request.get_json(force=True) or {}
    return jsonify(gamepad_mapper.set_enabled(bool(data.get("enabled", False))))


@app.route("/api/gamepad/capture-button", methods=["POST"])
def api_gamepad_capture_button():
    """Block briefly until the user presses any button on any connected
    XInput slot.  Used by the UI's 'press button to bind' flow."""
    data = request.get_json(silent=True) or {}
    timeout = float(data.get("timeout", 8.0))
    return jsonify(gamepad_mapper.capture_next_button(timeout_sec=timeout))


@app.route("/api/gamepad/install-status")
def api_gamepad_install_status():
    """Full ViGEmBus + vgamepad capability check.  Heavier than
    /api/gamepad/status — actually tries to create a virtual pad."""
    return jsonify(gamepad_mapper.get_install_status())


@app.route("/api/gamepad/hidhide-status")
def api_gamepad_hidhide_status():
    """HidHide kernel driver + CLI presence probe.  Cheap (no IOCTL)."""
    return jsonify(gamepad_mapper.get_hidhide_status())


@app.route("/api/gamepad/hidhide-engage", methods=["POST"])
def api_gamepad_hidhide_engage():
    """User-triggered HidHide engage retry.  Returns detailed error
    info if engagement fails so the UI can surface what went wrong."""
    return jsonify(gamepad_mapper.force_hidhide_engage())


@app.route("/api/gamepad/hidhide-diagnostic")
def api_gamepad_hidhide_diagnostic():
    """List every controller-class device on the system, tagged with
    whether it's a ViGEmBus virtual pad (ours, kept visible) or any
    OTHER controller / virtual-controller layer (will be hidden).
    Used by the UI's troubleshooting panel so users can spot Razer
    Synapse / Steam Input / DS4Windows virtual controllers competing
    with our virtual pad."""
    return jsonify(gamepad_mapper.get_hidhide_diagnostic())


# ─── v3.4 macro recorder ──────────────────────────────────────────────
@app.route("/api/gamepad/macro/record/start", methods=["POST"])
def api_gamepad_macro_record_start():
    """Body: { skip_move?, start_delay_sec?, filters?, sample_hz fields }.

    `filters` is a partial dict — any subset of:
        keyboard, mouse_buttons, mouse_move,
        controller_buttons, controller_sticks, controller_triggers
    Unspecified keys keep their defaults (digital on, analog off).

    `start_delay_sec` defaults to 2.0 — countdown before recording flips
    on, gives the user time to focus the target window."""
    from core import macro_recorder
    data = request.get_json(silent=True) or {}
    return jsonify(macro_recorder.start_recording(
        skip_move=bool(data.get("skip_move", False)),
        start_delay_sec=float(data.get("start_delay_sec", 2.0) or 2.0),
        filters=data.get("filters"),
        stick_sample_hz=int(data.get("stick_sample_hz", 30) or 30),
        trigger_sample_hz=int(data.get("trigger_sample_hz", 30) or 30),
        mouse_move_sample_hz=int(data.get("mouse_move_sample_hz", 30) or 30),
    ))


@app.route("/api/gamepad/macro/record/stop", methods=["POST"])
def api_gamepad_macro_record_stop():
    from core import macro_recorder
    return jsonify(macro_recorder.stop_recording())


@app.route("/api/gamepad/macro/record/cancel", methods=["POST"])
def api_gamepad_macro_record_cancel():
    from core import macro_recorder
    return jsonify(macro_recorder.cancel_recording())


@app.route("/api/gamepad/macro/record/status")
def api_gamepad_macro_record_status():
    from core import macro_recorder
    return jsonify(macro_recorder.get_status())


@app.route("/api/gamepad/test-virtual-pad", methods=["POST"])
def api_gamepad_test_virtual_pad():
    """Fire a verification sequence on the virtual pad — A, stick down,
    B — so the user can see if games are actually picking up the
    virtual controller's state."""
    return jsonify(gamepad_mapper.test_virtual_pad())


@app.route("/api/gamepad/recoil-presets")
def api_gamepad_recoil_presets():
    """Curated anti-recoil patterns for popular shooters + generic ones,
    MERGED with user-captured patterns saved via the capture workflow.
    User presets carry `is_user: true` so the UI can render Edit/Delete.

    Note: includes a disclaimer string the UI surfaces — these patterns
    can violate game ToS in ranked / competitive play."""
    return jsonify(gamepad_mapper.get_recoil_presets())


# ─── v3.6 — recoil pattern capture from gameplay ───────────────────
@app.route("/api/gamepad/capture/availability")
def api_gamepad_capture_availability():
    """Check whether the bundle includes the libs needed for capture
    (numpy + mss).  The UI hides the capture buttons if not."""
    from core import recoil_capture
    return jsonify(recoil_capture.is_available())


@app.route("/api/gamepad/capture/screen/start", methods=["POST"])
def api_gamepad_capture_screen_start():
    """Body: { rpm?, max_duration_sec?, capture_hz?, auto_fire? }.
    `auto_fire` (controller users): if true, GhostShell pushes RT through
    the virtual pad while capturing so the user doesn't have to physically
    hold their fire trigger.  Requires virtual_controller mode."""
    from core import recoil_capture
    data = request.get_json(silent=True) or {}
    return jsonify(recoil_capture.start_screen_capture(
        rpm=int(data.get("rpm", 600) or 600),
        max_duration_sec=float(data.get("max_duration_sec", 6.0) or 6.0),
        capture_hz=int(data.get("capture_hz", 120) or 120),
        auto_fire=bool(data.get("auto_fire", False)),
    ))


@app.route("/api/gamepad/capture/counter-pull/start", methods=["POST"])
def api_gamepad_capture_counter_pull_start():
    """Body: { rpm?, max_duration_sec?, source?: 'mouse' | 'controller_rstick',
              auto_fire?: bool }.

    Records the user's manual counter-pull during firing — what they're
    doing with their mouse / right-stick to compensate IS the
    compensation pattern."""
    from core import recoil_capture
    data = request.get_json(silent=True) or {}
    return jsonify(recoil_capture.start_counter_pull_capture(
        rpm=int(data.get("rpm", 600) or 600),
        max_duration_sec=float(data.get("max_duration_sec", 6.0) or 6.0),
        source=str(data.get("source", "mouse")),
        auto_fire=bool(data.get("auto_fire", False)),
    ))


@app.route("/api/gamepad/capture/counter-pull/stop", methods=["POST"])
def api_gamepad_capture_counter_pull_stop():
    from core import recoil_capture
    return jsonify(recoil_capture.stop_counter_pull_capture())


@app.route("/api/gamepad/capture/auto-fire-mag", methods=["POST"])
def api_gamepad_capture_auto_fire_mag():
    """Body: { duration_sec?: float }.  One-shot — fires RT through the
    virtual pad for N seconds so the user can press Capture-Before, click
    Fire, click Capture-After in sequence without alt-tabbing to manually
    hold RT.  Requires virtual_controller mode."""
    from core import recoil_capture
    data = request.get_json(silent=True) or {}
    return jsonify(recoil_capture.auto_fire_mag(
        duration_sec=float(data.get("duration_sec", 3.0) or 3.0)
    ))


@app.route("/api/gamepad/capture/screen/stop", methods=["POST"])
def api_gamepad_capture_screen_stop():
    from core import recoil_capture
    return jsonify(recoil_capture.stop_screen_capture())


@app.route("/api/gamepad/capture/status")
def api_gamepad_capture_status():
    from core import recoil_capture
    return jsonify(recoil_capture.get_capture_status())


@app.route("/api/gamepad/capture/bullet-holes/snapshot", methods=["POST"])
def api_gamepad_capture_bh_snapshot():
    """Body: { which: 'before' | 'after' }.  Captures the foreground
    window's current frame as either reference."""
    from core import recoil_capture
    data = request.get_json(silent=True) or {}
    return jsonify(recoil_capture.bullet_hole_snapshot(
        str(data.get("which", "")).lower()
    ))


@app.route("/api/gamepad/capture/bullet-holes/detect", methods=["POST"])
def api_gamepad_capture_bh_detect():
    """Body: { rpm?, threshold?, min_size_px?, max_size_px? }.  Diffs
    the before/after snapshots and returns the detected hole centroids
    in firing-order (top-down)."""
    from core import recoil_capture
    data = request.get_json(silent=True) or {}
    return jsonify(recoil_capture.detect_bullet_holes(
        rpm=int(data.get("rpm", 600) or 600),
        threshold=int(data.get("threshold", 60) or 60),
        min_size_px=int(data.get("min_size_px", 12) or 12),
        max_size_px=int(data.get("max_size_px", 800) or 800),
    ))


@app.route("/api/gamepad/capture/bullet-holes/state")
def api_gamepad_capture_bh_state():
    from core import recoil_capture
    return jsonify(recoil_capture.get_bullet_hole_state())


@app.route("/api/gamepad/capture/bullet-holes/reset", methods=["POST"])
def api_gamepad_capture_bh_reset():
    from core import recoil_capture
    return jsonify(recoil_capture.reset_bullet_hole_state())


@app.route("/api/gamepad/capture/save", methods=["POST"])
def api_gamepad_capture_save():
    """Body: { name, binned: [{dx, dy}], rpm, method, sensitivity_scale?, ... }.
    Persists a captured pattern as a user recoil preset."""
    from core import recoil_capture
    data = request.get_json(force=True) or {}
    return jsonify(recoil_capture.save_capture_as_preset(
        name=str(data.get("name", "")),
        binned=list(data.get("binned") or []),
        rpm=int(data.get("rpm", 600) or 600),
        method=str(data.get("method", "screen")),
        sensitivity_scale=float(data.get("sensitivity_scale", 0.005) or 0.005),
        weapon=str(data.get("weapon", "")),
        game=str(data.get("game", "")),
        description=str(data.get("description", "")),
        play_mode=str(data.get("play_mode", "once")),
        auto_hold_rt=bool(data.get("auto_hold_rt", True)),
        id_override=data.get("id_override"),
    ))


@app.route("/api/gamepad/capture/delete", methods=["POST"])
def api_gamepad_capture_delete():
    from core import recoil_capture
    data = request.get_json(force=True) or {}
    return jsonify(recoil_capture.delete_user_preset(str(data.get("id", ""))))


# ─── v3.5 — profile import / export ────────────────────────────────
@app.route("/api/gamepad/profile/export", methods=["POST"])
def api_gamepad_profile_export():
    """Body: { name }.  Returns a JSON blob the UI can copy to
    clipboard or save as a .json file for sharing / backup."""
    data = request.get_json(force=True) or {}
    return jsonify(gamepad_mapper.export_profile(data.get("name", "")))


@app.route("/api/gamepad/profile/import", methods=["POST"])
def api_gamepad_profile_import():
    """Body: { blob: <export-blob>, override_name?: str }.  Imports a
    profile.  If a profile with the same name exists and no
    override_name was provided, the new one gets a unique suffix."""
    data = request.get_json(force=True) or {}
    return jsonify(gamepad_mapper.import_profile(
        data.get("blob") or {},
        override_name=data.get("override_name"),
    ))


# ─── v3.5 — toggle hotkey ──────────────────────────────────────────
@app.route("/api/gamepad/toggle-hotkey", methods=["POST"])
def api_gamepad_set_toggle_hotkey():
    """Body: { vk: int, name?: str }.  Set or clear (vk=0) the global
    keyboard hotkey that toggles the mapper on/off."""
    data = request.get_json(force=True) or {}
    return jsonify(gamepad_mapper.set_toggle_hotkey(
        int(data.get("vk", 0) or 0),
        str(data.get("name", "") or ""),
    ))


# ─── v3.5 — latency probe ──────────────────────────────────────────
@app.route("/api/gamepad/calibrate-sticks", methods=["POST"])
def api_gamepad_calibrate_sticks():
    """Body: { duration_sec?, margin?, save_to_profile? }.  Samples the
    physical XInput sticks at rest for `duration_sec` and writes the
    measured wobble + margin into the active profile's per-stick
    `deadzone_in`.  Eliminates drift caused by tiny sub-noticeable
    stick motion bleeding through the snapshot pipeline."""
    data = request.get_json(silent=True) or {}
    return jsonify(gamepad_mapper.calibrate_sticks_at_rest(
        duration_sec=float(data.get("duration_sec", 2.0) or 2.0),
        margin=float(data.get("margin", 0.01) or 0.01),
        save_to_profile=bool(data.get("save_to_profile", True)),
    ))


@app.route("/api/gamepad/measure-latency", methods=["POST"])
def api_gamepad_measure_latency():
    """Body: { samples?: int }.  Run the latency probe — measures the
    round-trip from injecting a held vbutton into our internal state
    to seeing it reflected in the next snapshot pushed to ViGEmBus."""
    data = request.get_json(silent=True) or {}
    return jsonify(gamepad_mapper.measure_latency(int(data.get("samples", 50))))


@app.route("/api/gamepad/install-hidhide", methods=["POST"])
def api_gamepad_install_hidhide():
    """Auto-install HidHide.  Runs in a background thread so the HTTP
    response returns immediately; the UI polls /api/gamepad/install-progress
    for status."""
    import threading
    from core import hidhide
    threading.Thread(target=hidhide.download_and_install, daemon=True,
                     name="hidhide-install").start()
    return jsonify({"ok": True, "msg": "HidHide install started — poll /install-progress"})


@app.route("/api/gamepad/install-vigembus", methods=["POST"])
def api_gamepad_install_vigembus():
    import threading
    from core import virtual_gamepad
    threading.Thread(target=virtual_gamepad.download_and_install, daemon=True,
                     name="vigembus-install").start()
    return jsonify({"ok": True, "msg": "ViGEmBus install started — poll /install-progress"})


@app.route("/api/gamepad/install-progress")
def api_gamepad_install_progress():
    """Poll-able status of the most recent install — combined view of
    both HidHide and ViGEmBus.  Returns whichever one is in progress
    (only one runs at a time)."""
    from core import hidhide, virtual_gamepad
    h = hidhide.get_install_progress()
    v = virtual_gamepad.get_install_progress()
    if h.get("in_progress"):
        return jsonify({"target": "hidhide",  "status": h})
    if v.get("in_progress"):
        return jsonify({"target": "vigembus", "status": v})
    # Neither in progress — return the most recent (highest "phase"-rank)
    last = h if h.get("phase") not in ("idle", "") else v
    target = "hidhide" if h.get("phase") not in ("idle", "") else "vigembus"
    return jsonify({"target": target, "status": last})


@app.route("/api/gamepad/live-state")
def api_gamepad_live_state():
    """Backend-driven controller state — fallback for when the WebView's
    `navigator.getGamepads()` doesn't surface the controller (Edge
    WebView2 sometimes gates the API).  Reads XInput slots 0-3 directly
    via ctypes — same data the mapper polling loop uses, but exposed
    as JSON for the SVG visualizer to render against.

    Also returns the latest virtual-pad snapshot (v3.5) so the UI can
    render what the GAME sees alongside what the physical pad reports."""
    from core import xinput_ctypes as xi
    out = []
    for slot in range(4):
        s = xi.get_state(slot)
        if s is not None:
            out.append({"slot": slot, "state": s})
    return jsonify({
        "ok":          True,
        "available":   xi.is_available(),
        "controllers": out,
        "vpad":        gamepad_mapper.get_last_vpad_snapshot(),
    })


@app.route("/api/library/enable_at_bulk", methods=["POST"])
def api_library_enable_at_bulk():
    """Body: { exes: [...], enabled: true, only_known?: true }.  Bulk-flip
    AT for many games at once.  If only_known=True, the server filters the
    list down to titles that exist in KNOWN_GAME_EXES (so the user can
    just say 'enable AT for everything I have installed')."""
    data = request.get_json(force=True) or {}
    exes = data.get("exes") or []
    if not isinstance(exes, list) or not exes:
        return jsonify({"ok": False, "err": "exes list required"}), 400
    enabled = bool(data.get("enabled", True))

    if data.get("only_known"):
        try:
            from core.game_profiles import KNOWN_GAME_EXES
            exes = [e for e in exes if (e or "").lower() in KNOWN_GAME_EXES]
        except Exception:
            pass

    flipped = 0
    for exe in exes:
        try:
            adaptive_tuning.set_game_enabled(exe, enabled)
            flipped += 1
        except Exception as e:
            log.warning(f"bulk AT enable failed for {exe}: {e}")
    return jsonify({"ok": True, "flipped": flipped, "total": len(exes)})


# ═══════════════════════════════════════════════════════════════════════════
# Privacy API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/privacy/status")
def api_privacy_status():
    hosts = privacy.get_hosts_status()
    devices = privacy.get_device_access_status()
    return jsonify({"hosts": hosts, "devices": devices})


@app.route("/api/privacy/run", methods=["POST"])
def api_privacy_run():
    result = privacy.run_full_privacy()
    _module_status["privacy"] = True
    return jsonify(result)


@app.route("/api/privacy/telemetry", methods=["POST"])
def api_privacy_telemetry():
    return jsonify({"results": privacy.apply_telemetry_tweaks()})


@app.route("/api/privacy/hosts", methods=["POST"])
def api_privacy_hosts():
    return jsonify(privacy.apply_hosts_blocking())


@app.route("/api/privacy/hosts/remove", methods=["POST"])
def api_privacy_hosts_remove():
    return jsonify(privacy.remove_hosts_blocking())


@app.route("/api/privacy/firewall", methods=["POST"])
def api_privacy_firewall():
    return jsonify({"results": privacy.apply_firewall_rules()})


@app.route("/api/privacy/webcam", methods=["POST"])
def api_privacy_webcam():
    data = request.get_json(force=True) or {}
    return jsonify(privacy.toggle_webcam(data.get("enable", False)))


# NOTE: /api/privacy/microphone was REMOVED in v3.1 — flipping the mic
# capability gate broke voice chat for users. Mic privacy is now strictly
# managed by the user via Windows Settings.


@app.route("/api/privacy/updates", methods=["POST"])
def api_privacy_updates():
    return jsonify({"results": privacy.apply_update_tweaks()})


# ═══════════════════════════════════════════════════════════════════════════
# Vault API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/vault/status")
def api_vault_status():
    return jsonify({
        "pin_set": vault.is_pin_set(),
        "unlocked": vault.is_unlocked(),
    })


@app.route("/api/vault/setup", methods=["POST"])
def api_vault_setup():
    data = request.get_json(force=True) or {}
    return jsonify(vault.setup_pin(data.get("pin", "")))


@app.route("/api/vault/unlock", methods=["POST"])
def api_vault_unlock():
    data = request.get_json(force=True) or {}
    return jsonify(vault.unlock(data.get("pin", "")))


@app.route("/api/vault/lock", methods=["POST"])
def api_vault_lock():
    vault.lock()
    return jsonify({"ok": True})


@app.route("/api/vault/entries")
def api_vault_entries():
    search = request.args.get("search", "")
    category = request.args.get("category", "")
    return jsonify(vault.get_entries(search, category))


@app.route("/api/vault/entry", methods=["POST"])
def api_vault_add_entry():
    data = request.get_json(force=True) or {}
    return jsonify(vault.add_entry(
        service=data.get("service", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        url=data.get("url", ""),
        notes=data.get("notes", ""),
        category=data.get("category", "General"),
    ))


@app.route("/api/vault/entry/<entry_id>", methods=["PUT"])
def api_vault_update_entry(entry_id):
    data = request.get_json(force=True) or {}
    return jsonify(vault.update_entry(entry_id, **data))


@app.route("/api/vault/entry/<entry_id>", methods=["DELETE"])
def api_vault_delete_entry(entry_id):
    return jsonify(vault.delete_entry(entry_id))


@app.route("/api/vault/entry/<entry_id>/password")
def api_vault_get_password(entry_id):
    return jsonify(vault.get_entry_password(entry_id))


@app.route("/api/vault/generate")
def api_vault_generate():
    length = request.args.get("length", 20, type=int)
    upper = request.args.get("upper", "true") == "true"
    lower = request.args.get("lower", "true") == "true"
    digits = request.args.get("digits", "true") == "true"
    symbols = request.args.get("symbols", "true") == "true"
    exclude = request.args.get("exclude_ambiguous", "false") == "true"
    pw = vault.generate_password(length, upper, lower, digits, symbols, exclude)
    return jsonify({"password": pw})


@app.route("/api/vault/export", methods=["POST"])
def api_vault_export():
    data = request.get_json(force=True) or {}
    return jsonify(vault.export_vault(data.get("pin", "")))


@app.route("/api/vault/reset", methods=["POST"])
def api_vault_reset():
    return jsonify(vault.reset_vault())


# ═══════════════════════════════════════════════════════════════════════════
# Cleaner API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/cleaner/scan")
def api_cleaner_scan():
    return jsonify({"locations": cleaner.scan_temp_sizes()})


@app.route("/api/cleaner/run", methods=["POST"])
def api_cleaner_run():
    result = cleaner.clean_all()
    _module_status["cleaner"] = True
    return jsonify(result)


@app.route("/api/cleaner/ram", methods=["POST"])
def api_cleaner_ram():
    return jsonify(cleaner.clean_ram())


@app.route("/api/cleaner/disks")
def api_cleaner_disks():
    return jsonify({"drives": cleaner.get_disk_list()})


# beta.17 — renamed from api_cleaner_disk_scan to disambiguate from
# the new disk-analyzer endpoint of the same Python name.  Route URL
# unchanged (/api/cleaner/disk-scan with a hyphen) so the old "Scan
# disks for junk" button keeps working.
@app.route("/api/cleaner/disk-scan", methods=["POST"])
def api_cleaner_disk_junk_scan():
    data = request.get_json(force=True) or {}
    drive = data.get("drive", "C:")
    return jsonify({"junk": cleaner.scan_disk_junk(drive)})


@app.route("/api/cleaner/disk-clean", methods=["POST"])
def api_cleaner_disk_clean():
    data = request.get_json(force=True) or {}
    drives = data.get("drives", ["C:"])
    return jsonify(cleaner.clean_disk_junk(drives))


# ═══════════════════════════════════════════════════════════════════════════
# Logs API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/logs")
def api_logs():
    since = request.args.get("since", 0, type=int)
    buf = get_log_buffer()
    # Return entries after index `since`
    if since > 0 and since < len(buf):
        buf = buf[since:]
    return jsonify({"entries": buf, "total": len(get_log_buffer())})


@app.route("/api/logs/clear", methods=["POST"])
def api_logs_clear():
    clear_log_buffer()
    return jsonify({"ok": True})


# v2.9.9.8 — diagnostic snapshot for bug reports.
# Returns the full context the user (or the Report-Issue modal) needs to
# attach to a GitHub issue: app version, OS, GPU/CPU, current OC state,
# admin status, and the last ~80 log lines.  No PII beyond the username
# (which is part of the file paths the logger prints anyway).
@app.route("/api/diagnostics/snapshot")
def api_diagnostics_snapshot():
    import platform
    snap = {
        "app": {
            "version":    config.APP_VERSION,
            "name":       config.APP_NAME,
            "admin":      is_admin(),
            "frozen":     getattr(sys, "frozen", False),
        },
        "os": {
            "platform":   platform.platform(),
            "release":    platform.release(),
            "version":    platform.version(),
            "build":      platform.win32_ver()[1] if sys.platform == "win32" else "",
        },
        "hardware": {},
        "gpu_oc":   {},
        "logs":     [],
    }
    try:
        info = system_info.get_system_info()
        snap["hardware"] = {
            "cpu":      info.get("cpu", {}),
            "gpu":      info.get("gpu", {}),
            "ram_gb":   info.get("ram_gb"),
            "drives":   info.get("drives", [])[:3],   # cap to 3 entries
        }
    except Exception as e:
        snap["hardware"]["err"] = str(e)
    try:
        snap["gpu_oc"] = {
            "current_offsets": gpu_overclock.get_current_offsets(),
            "live":            gpu_overclock.read_live_state(),
            "stock_baseline":  gpu_overclock.get_stock_baseline(),
            "crash_state":     gpu_overclock.get_crash_state(),
        }
    except Exception as e:
        snap["gpu_oc"]["err"] = str(e)
    try:
        # Last 80 log entries — enough context, won't blow up the issue body
        snap["logs"] = get_log_buffer()[-80:]
    except Exception:
        pass
    return jsonify(snap)


# ═══════════════════════════════════════════════════════════════════════════
# VBS/HVCI API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/vbs/status")
def api_vbs_status():
    return jsonify(vbs_toggle.get_vbs_status())


@app.route("/api/vbs/disable-hvci", methods=["POST"])
def api_vbs_disable_hvci():
    return jsonify(vbs_toggle.disable_hvci())


@app.route("/api/vbs/enable-hvci", methods=["POST"])
def api_vbs_enable_hvci():
    return jsonify(vbs_toggle.enable_hvci())


@app.route("/api/vbs/disable-all", methods=["POST"])
def api_vbs_disable_all():
    return jsonify(vbs_toggle.disable_vbs())


@app.route("/api/vbs/enable-all", methods=["POST"])
def api_vbs_enable_all():
    return jsonify(vbs_toggle.enable_vbs())


# ═══════════════════════════════════════════════════════════════════════════
# Timer & ISLC API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/timer/status")
def api_timer_status():
    ts = timer_manager.get_timer_status()
    ts["memory"] = timer_manager.get_memory_status()
    ts["islc"] = timer_manager.get_islc_status()
    return jsonify(ts)


@app.route("/api/timer/apply", methods=["POST"])
def api_timer_apply():
    data = request.get_json(force=True) or {}
    return jsonify({"results": timer_manager.apply_timer_tweaks(
        disable_dynamic_tick=data.get("dynamic_tick", True),
        disable_platform_clock=data.get("platform_clock", True),
        disable_hpet=data.get("hpet", True),
    )})


@app.route("/api/timer/restore", methods=["POST"])
def api_timer_restore():
    return jsonify({"results": timer_manager.restore_timer_defaults()})


@app.route("/api/timer/memory")
def api_timer_memory():
    return jsonify(timer_manager.get_memory_status())


@app.route("/api/timer/clear-standby", methods=["POST"])
def api_timer_clear_standby():
    return jsonify(timer_manager.clear_standby_list())


@app.route("/api/timer/islc/start", methods=["POST"])
def api_islc_start():
    data = request.get_json(force=True) or {}
    return jsonify(timer_manager.start_islc(
        free_threshold_mb=data.get("free_threshold", 1024),
        standby_threshold_mb=data.get("standby_threshold", 1024),
        interval_sec=data.get("interval", 10),
    ))


@app.route("/api/timer/islc/stop", methods=["POST"])
def api_islc_stop():
    return jsonify(timer_manager.stop_islc())


# ═══════════════════════════════════════════════════════════════════════════
# Interrupt Affinity API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/irq/devices")
def api_irq_devices():
    devices = interrupt_affinity.get_pci_devices()
    cores = interrupt_affinity.get_cpu_core_count()
    return jsonify({"devices": devices, "core_count": cores})


@app.route("/api/irq/auto-optimize", methods=["POST"])
def api_irq_auto_optimize():
    return jsonify({"results": interrupt_affinity.auto_optimize_interrupts()})


@app.route("/api/irq/msi", methods=["POST"])
def api_irq_msi():
    data = request.get_json(force=True) or {}
    iid = data.get("instance_id", "")
    enable = data.get("enable", True)
    if enable:
        return jsonify(interrupt_affinity.enable_msi_mode(iid))
    return jsonify(interrupt_affinity.disable_msi_mode(iid))


@app.route("/api/irq/affinity", methods=["POST"])
def api_irq_affinity():
    data = request.get_json(force=True) or {}
    return jsonify(interrupt_affinity.set_device_affinity(
        data.get("instance_id", ""), data.get("core", 0)))


@app.route("/api/irq/priority", methods=["POST"])
def api_irq_priority():
    data = request.get_json(force=True) or {}
    return jsonify(interrupt_affinity.set_device_priority(
        data.get("instance_id", ""), data.get("priority", 3)))


@app.route("/api/irq/reset", methods=["POST"])
def api_irq_reset():
    data = request.get_json(force=True) or {}
    return jsonify(interrupt_affinity.reset_device_affinity(data.get("instance_id", "")))


# ─── Input device priority — v3.1 grouped flow ───────────────────────────
# Replaces the old "set priority on any device" UI with three intentional
# flows: bulk-boost keyboards, bulk-boost mice, identify-and-boost a single
# controller (via the HTML5 Gamepad API on the front end).

@app.route("/api/kernel/input-devices")
def api_kernel_input_devices():
    """Return inputs bucketed by kind: keyboards / mice / controllers.
    Generic HID and Bluetooth-radio entries are intentionally hidden —
    those were the foot-guns that let users boost the wrong device."""
    return jsonify(interrupt_affinity.get_input_devices_grouped())


@app.route("/api/kernel/boost-keyboards", methods=["POST"])
def api_kernel_boost_keyboards():
    return jsonify(interrupt_affinity.boost_all_keyboards())


@app.route("/api/kernel/boost-mice", methods=["POST"])
def api_kernel_boost_mice():
    return jsonify(interrupt_affinity.boost_all_mice())


@app.route("/api/kernel/boost-controller", methods=["POST"])
def api_kernel_boost_controller():
    """Body: { vid, pid } from the JS Gamepad API's id string, OR
    { instance_id } if the user picked from the dropdown manually.
    Boosts every interface of the matching physical controller."""
    data = request.get_json(force=True) or {}
    vid = (data.get("vid") or "").strip()
    pid = (data.get("pid") or "").strip()
    iid = (data.get("instance_id") or "").strip()
    if iid:
        return jsonify(interrupt_affinity.boost_controller_by_instance_id(iid))
    if vid and pid:
        return jsonify(interrupt_affinity.boost_controller_by_vid_pid(vid, pid))
    return jsonify({"ok": False, "err": "Provide either { vid, pid } or { instance_id }"}), 400


@app.route("/api/kernel/reset-input-priorities", methods=["POST"])
def api_kernel_reset_input_priorities():
    """Single-button revert: clears any priority override GhostShell set
    on every keyboard, mouse, and controller it can see."""
    kb = interrupt_affinity.reset_all_keyboards()
    mc = interrupt_affinity.reset_all_mice()
    ct = interrupt_affinity.reset_all_controllers()
    return jsonify({"ok": True,
                    "keyboards": kb,
                    "mice": mc,
                    "controllers": ct})


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/scheduler/status")
def api_scheduler_status():
    return jsonify(scheduler_tweaks.get_scheduler_status())


@app.route("/api/scheduler/run", methods=["POST"])
def api_scheduler_run():
    return jsonify(scheduler_tweaks.run_full_scheduler_optimize())


@app.route("/api/scheduler/mmcss", methods=["POST"])
def api_scheduler_mmcss():
    return jsonify({"results": scheduler_tweaks.apply_mmcss_gaming_profile()})


@app.route("/api/scheduler/priority-sep", methods=["POST"])
def api_scheduler_priority():
    data = request.get_json(force=True) or {}
    return jsonify(scheduler_tweaks.apply_priority_separation(data.get("mode", "competitive")))


@app.route("/api/scheduler/cpu-idle", methods=["POST"])
def api_scheduler_idle():
    data = request.get_json(force=True) or {}
    return jsonify(scheduler_tweaks.toggle_cpu_idle(data.get("disable", True)))


@app.route("/api/scheduler/spectre", methods=["POST"])
def api_scheduler_spectre():
    data = request.get_json(force=True) or {}
    if data.get("disable", True):
        return jsonify(scheduler_tweaks.disable_spectre_meltdown())
    return jsonify(scheduler_tweaks.enable_spectre_meltdown())


# ═══════════════════════════════════════════════════════════════════════════
# Game Profiles API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/profiles")
def api_profiles_list():
    profiles = game_profiles.get_all_profiles()
    return jsonify({"profiles": profiles})


@app.route("/api/profiles/status")
def api_profiles_status():
    return jsonify(game_profiles.get_engine_status())


@app.route("/api/profiles/start", methods=["POST"])
def api_profiles_start():
    return jsonify(game_profiles.start_monitoring())


# v2.9.9.6 — game-mode auto-enable settings
@app.route("/api/profiles/settings", methods=["GET", "POST"])
def api_profiles_settings():
    """GET → current game-mode auto-enable settings.
    POST → update.  Body: {auto_enable_on_boot: bool}."""
    if request.method == "GET":
        return jsonify({"ok": True, "settings": game_profiles.get_settings()})
    data = request.get_json(force=True) or {}
    return jsonify(game_profiles.set_settings(
        auto_enable_on_boot=data.get("auto_enable_on_boot"),
    ))


@app.route("/api/profiles/stop", methods=["POST"])
def api_profiles_stop():
    return jsonify(game_profiles.stop_monitoring())


@app.route("/api/profiles/restore-normal", methods=["POST"])
def api_profiles_restore_normal():
    game_profiles.restore_normal_mode("manual")
    return jsonify({"ok": True})


@app.route("/api/profiles/refresh", methods=["POST"])
def api_profiles_refresh():
    """Manually trigger the post-game PC refresh (RAM clean, DNS flush, kill orphans, MMCSS reset)."""
    threading.Thread(target=game_profiles._post_game_refresh, args=("manual refresh",), daemon=True).start()
    return jsonify({"ok": True, "msg": "Refresh started in background"})


@app.route("/api/profiles/recapture-baseline", methods=["POST"])
def api_profiles_recapture():
    return jsonify(game_profiles.recapture_baseline())


@app.route("/api/profiles/add-game", methods=["POST"])
def api_profiles_add_game():
    data = request.get_json(force=True) or {}
    return jsonify(game_profiles.add_custom_game(
        data.get("exe", ""), data.get("name", "")))


@app.route("/api/profiles/remove-game", methods=["POST"])
def api_profiles_remove_game():
    data = request.get_json(force=True) or {}
    return jsonify(game_profiles.remove_custom_game(data.get("exe", "")))


# ═══════════════════════════════════════════════════════════════════════════
# Hardware Monitor API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    data = request.get_json(force=True) or {}
    return jsonify(hw_monitor.start_monitor(data.get("ping_target", "1.1.1.1")))


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    return jsonify(hw_monitor.stop_monitor())


@app.route("/api/monitor/snapshot")
def api_monitor_snapshot():
    return jsonify(hw_monitor.get_monitor_snapshot())


@app.route("/api/monitor/processes")
def api_monitor_processes():
    return jsonify({"processes": hw_monitor.get_top_processes()})


@app.route("/api/monitor/benchmark", methods=["POST"])
def api_monitor_benchmark():
    return jsonify(hw_monitor.run_benchmark())


# ═══════════════════════════════════════════════════════════════════════════
# Full Spectrum (run everything in sequence) — internal route kept as /api/full-ghost
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/full-ghost", methods=["POST"])
def api_full_ghost():
    log.info("╔══════════════════════════════════════╗")
    log.info("║     FULL GHOST MODE ACTIVATED        ║")
    log.info("╚══════════════════════════════════════╝")

    global _restore_point_created
    if not _restore_point_created:
        create_restore_point()
        _restore_point_created = True

    results = {}
    results["debloat"] = debloater.run_full_debloat()
    _module_status["debloat"] = True

    results["optimize"] = optimizer.run_full_optimize()
    _module_status["optimize"] = True

    results["network"] = network_tweaks.run_full_network_optimize()
    _module_status["network"] = True

    results["gpu"] = nvidia_clean.apply_all_gpu_tweaks()
    _module_status["gpu"] = True

    results["privacy"] = privacy.run_full_privacy()
    _module_status["privacy"] = True

    results["cleaner"] = cleaner.clean_all()
    _module_status["cleaner"] = True

    # New v2 modules
    results["scheduler"] = scheduler_tweaks.run_full_scheduler_optimize()
    results["timer"] = timer_manager.apply_timer_tweaks()
    results["interrupts"] = interrupt_affinity.auto_optimize_interrupts()

    log.info("╔══════════════════════════════════════╗")
    log.info("║    FULL GHOST COMPLETE — REBOOT!     ║")
    log.info("╚══════════════════════════════════════╝")
    return jsonify(results)


# ═══════════════════════════════════════════════════════════════════════════
# Auto-Updater API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/updater/check", methods=["POST"])
def api_updater_check():
    """Force an immediate check against the update server (Railway-hosted /version/<channel>)."""
    return jsonify(updater.check_for_update())


@app.route("/api/updater/status")
def api_updater_status():
    """Current updater state — UI polls this for the badge / toast."""
    return jsonify(updater.get_update_status())


@app.route("/api/updater/download", methods=["POST"])
def api_updater_download():
    """Download the pending update without installing it."""
    return jsonify(updater.download_update())


@app.route("/api/updater/install", methods=["POST"])
def api_updater_install():
    """Apply the downloaded update and restart GhostShell.

    The Python process exits ~800ms after this returns.  The new exe is
    swapped in by a detached batch script which then relaunches.
    """
    return jsonify(updater.install_update_now())


# v2.9.4 — post-install state endpoint.
# The frontend polls this once on load so the user sees a clear toast
# confirming the update landed (or warning that it didn't).  Returns
# {state: 'idle'|'updated'|'install_failed', previous, current, expected}.
@app.route("/api/updater/post-install-state")
def api_updater_post_install_state():
    return jsonify(_post_install_state)


@app.route("/api/updater/settings", methods=["GET", "POST"])
def api_updater_settings():
    """GET → current settings.  POST → update settings (any subset of fields).

    Body fields:
        auto_check, auto_download, auto_install (bool)
        channel ('stable' | 'beta')         ← v3
        server_url (str, optional)          ← v3 — override Railway URL
    """
    if request.method == "GET":
        return jsonify({"ok": True, "settings": updater.get_settings()})
    data = request.get_json(force=True) or {}
    return jsonify(updater.set_settings(
        auto_check=data.get("auto_check"),
        auto_download=data.get("auto_download"),
        auto_install=data.get("auto_install"),
        channel=data.get("channel"),
        server_url=data.get("server_url"),
    ))


# Legacy alias — keeps old frontend callers working
@app.route("/api/updater/apply", methods=["POST"])
def api_updater_apply():
    """Legacy — combines download + install in one call."""
    dl = updater.download_update()
    if not dl.get("ok"):
        return jsonify(dl)
    return jsonify(updater.install_update_now())


# ═══════════════════════════════════════════════════════════════════════════
# Window Control API (called from JS)
# ═══════════════════════════════════════════════════════════════════════════
_webview_window = None
_window_hidden = False


def _win_minimize():
    if not _webview_window:
        return False
    try:
        _webview_window.minimize()
        return True
    except Exception as e:
        log.debug(f"minimize failed: {e}")
        return False


def _win_hide_to_tray():
    """Hide the window to the tray. Minimize first so WebView2's internal
    state is consistent — without this first-minimize step, subsequent
    hide() calls sometimes no-op after a show() cycle on Windows."""
    global _window_hidden
    if not _webview_window:
        return False
    try:
        try:
            _webview_window.minimize()
        except Exception:
            pass
        _webview_window.hide()
        _window_hidden = True
        return True
    except Exception as e:
        log.debug(f"hide failed: {e}")
        return False


def _win_show():
    global _window_hidden
    if not _webview_window:
        return False
    try:
        _webview_window.show()
        _webview_window.restore()
        _window_hidden = False
        # Nudge to foreground — webview.show() alone sometimes leaves it behind other apps.
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.FindWindowW(None, config.APP_NAME)
                if hwnd:
                    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    ctypes.windll.user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        return True
    except Exception as e:
        log.debug(f"show failed: {e}")
        return False


class WindowJSAPI:
    """Exposed to JS as `window.pywebview.api.*`. Called directly from the
    frontend — bypasses Flask so the calls still work after the window has
    been hidden and re-shown (WebView2 throttles fetch() in hidden tabs)."""

    def minimize(self):
        return _win_minimize()

    def close_to_tray(self):
        return _win_hide_to_tray()

    def show(self):
        return _win_show()

    def is_hidden(self):
        return _window_hidden


@app.route("/api/window/minimize", methods=["POST"])
def api_window_minimize():
    return jsonify({"ok": _win_minimize()})


@app.route("/api/window/close", methods=["POST"])
def api_window_close():
    """Hide to tray — app continues running."""
    return jsonify({"ok": _win_hide_to_tray()})


@app.route("/api/window/show", methods=["POST"])
def api_window_show():
    return jsonify({"ok": _win_show()})


# ─────────────────────────────────────────────────────────────────────────
# v2.9.0 — close-button rework.
# The titlebar X used to silently hide-to-tray.  Now the UI presents the
# user with a choice:
#     1. "Quit GhostShell"        →  /api/window/quit  (full process exit)
#     2. "Hide + Game Mode"       →  /api/window/close-and-game-mode
#                                    (hide to tray + arm the game-detection
#                                     monitor so any launched game gets the
#                                     gaming profile applied automatically)
#     3. "Hide to tray"           →  /api/window/close (legacy behaviour)
# ─────────────────────────────────────────────────────────────────────────
@app.route("/api/window/quit", methods=["POST"])
def api_window_quit():
    """Fully shut down GhostShell.

    Restores any active gaming-mode tweaks first so the system is not
    left in a half-modified state, then schedules os._exit() so the
    HTTP response can be flushed before the process dies.
    """
    log.info("Quit requested by user — shutting down")
    try:
        # Roll back any active gaming tweaks before the process disappears.
        status = game_profiles.get_engine_status()
        if status.get("gaming_mode"):
            game_profiles.restore_normal_mode("user quit")
        game_profiles.stop_monitoring()
    except Exception as e:
        log.warning(f"Error restoring state during quit: {e}")

    def _suicide():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_suicide, daemon=True).start()
    return jsonify({"ok": True, "msg": "Goodbye"})


@app.route("/api/window/close-and-game-mode", methods=["POST"])
def api_window_close_and_game_mode():
    """Hide UI to tray AND turn on the game-profile monitor.

    The monitor scans for known game executables; when one starts, the
    gaming profile (priority, affinity, OC, etc.) is applied automatically.
    When the game exits, the original system state is restored.
    """
    started = game_profiles.start_monitoring()
    hidden = _win_hide_to_tray()
    log.info(f"Close+Game Mode: hidden={hidden} monitor_started={started.get('ok')}")
    return jsonify({
        "ok": True,
        "hidden": hidden,
        "game_monitor": started,
    })


# ═══════════════════════════════════════════════════════════════════════════
# Power (shutdown/restart with cleanup) API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/power/shutdown", methods=["POST"])
def api_power_shutdown():
    data = request.get_json(force=True) or {}
    return jsonify(power.shutdown(
        delay_sec=int(data.get("delay", 10)),
        clean=bool(data.get("clean", True)),
    ))


@app.route("/api/power/restart", methods=["POST"])
def api_power_restart():
    data = request.get_json(force=True) or {}
    return jsonify(power.restart(
        delay_sec=int(data.get("delay", 10)),
        clean=bool(data.get("clean", True)),
    ))


@app.route("/api/power/cancel", methods=["POST"])
def api_power_cancel():
    return jsonify(power.cancel())


@app.route("/api/power/cleanup", methods=["POST"])
def api_power_cleanup():
    return jsonify(power.cleanup_only())


# ═══════════════════════════════════════════════════════════════════════════
# Notifier API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/notifier/status")
def api_notifier_status():
    return jsonify({"enabled": notifier.is_enabled()})


@app.route("/api/notifier/toggle", methods=["POST"])
def api_notifier_toggle():
    data = request.get_json(force=True) or {}
    return jsonify(notifier.set_enabled(bool(data.get("enabled", True))))


@app.route("/api/notifier/test", methods=["POST"])
def api_notifier_test():
    return jsonify(notifier.toast("Vispora", "Notifications are working.", force=True))


# ═══════════════════════════════════════════════════════════════════════════
# Autostart API (v2.9.5)
#   GET  /api/autostart            → enabled? exe? delay?
#   POST /api/autostart/enable     → register Run key with polite delay
#   POST /api/autostart/disable    → remove from Run key
#   POST /api/autostart/delay      → change delay (and re-register if enabled)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/autostart")
def api_autostart_status():
    return jsonify(autostart.get_status())


@app.route("/api/autostart/enable", methods=["POST"])
def api_autostart_enable():
    data = request.get_json(silent=True) or {}
    delay = data.get("delay_seconds")
    return jsonify(autostart.enable(
        delay_seconds=int(delay) if delay is not None else None
    ))


@app.route("/api/autostart/disable", methods=["POST"])
def api_autostart_disable():
    return jsonify(autostart.disable())


@app.route("/api/autostart/delay", methods=["POST"])
def api_autostart_delay():
    data = request.get_json(force=True) or {}
    return jsonify(autostart.set_delay(int(data.get("delay_seconds", 30))))


# ═══════════════════════════════════════════════════════════════════════════
# GPU Driver Updater API (v2.9.6)
#   GET  /api/driver/status          → current vs latest driver, settings, etc.
#   POST /api/driver/check           → force a fresh GitHub-style lookup
#   POST /api/driver/download        → fetch the .exe to APPDATA/drivers/
#   POST /api/driver/install         → launch the installer (interactive)
#   GET  /api/driver/settings        → auto_check + auto_download flags
#   POST /api/driver/settings        → update either flag
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/driver/status")
def api_driver_status():
    return jsonify(driver_updater.get_status())


@app.route("/api/driver/check", methods=["POST"])
def api_driver_check():
    return jsonify(driver_updater.check_for_update())


@app.route("/api/driver/download", methods=["POST"])
def api_driver_download():
    return jsonify(driver_updater.download_driver())


@app.route("/api/driver/install", methods=["POST"])
def api_driver_install():
    return jsonify(driver_updater.install_driver())


@app.route("/api/driver/settings", methods=["GET", "POST"])
def api_driver_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "settings": driver_updater.get_settings()})
    data = request.get_json(force=True) or {}
    return jsonify(driver_updater.set_settings(
        auto_check=data.get("auto_check"),
        auto_download=data.get("auto_download"),
    ))


# ═══════════════════════════════════════════════════════════════════════════
# Boot Prep API
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/boot-prep/status")
def api_boot_prep_status():
    return jsonify(boot_prep.get_status())


@app.route("/api/boot-prep/run", methods=["POST"])
def api_boot_prep_run():
    boot_prep.prep_for_gaming_async()
    return jsonify({"ok": True, "msg": "Boot prep started"})


# ═══════════════════════════════════════════════════════════════════════════
# Launcher
# ═══════════════════════════════════════════════════════════════════════════
def start_flask():
    app.run(host="127.0.0.1", port=config.APP_PORT, debug=False, threaded=True, use_reloader=False)


def _set_efficiency_mode():
    """Set this process to efficiency mode (low priority + EcoQoS).
    Used when GhostShell is idle (no game running, no mapper firing) so
    it stays out of the way of foreground work + saves laptop battery."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)

        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]
        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = 1
        state.ControlMask = 1  # EXECUTION_SPEED
        state.StateMask = 1    # Enable throttling
        kernel32.SetProcessInformation(
            handle, 4,
            ctypes.byref(state),
            ctypes.sizeof(state)
        )
        log.info("Efficiency mode enabled (Below Normal + EcoQoS)")
    except Exception as e:
        log.warning(f"Could not set efficiency mode: {e}")


def _set_responsive_mode():
    """Drop efficiency throttling — set Normal priority + clear EcoQoS.
    Called when GhostShell needs to keep up with real work: a game is
    running (mapper is forwarding state), tournament mode is on, the
    network monitor is alerting on a burst, etc.

    Without this, GhostShell stays in Below-Normal/EcoQoS while the
    game runs at HIGH priority on P-cores — UI lags, mapper polling
    drops from 4 kHz target to 200 Hz actual, HidHide retry timer
    stalls.  Calling this restores the process to Normal priority on
    P-cores."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        NORMAL_PRIORITY_CLASS = 0x00000020
        kernel32.SetPriorityClass(handle, NORMAL_PRIORITY_CLASS)

        # Clear EcoQoS — opt OUT of execution-speed throttling
        class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_ulong),
                ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong),
            ]
        state = PROCESS_POWER_THROTTLING_STATE()
        state.Version = 1
        state.ControlMask = 1  # EXECUTION_SPEED
        state.StateMask = 0    # 0 = OPT OUT of throttling (default scheduling)
        kernel32.SetProcessInformation(
            handle, 4,
            ctypes.byref(state),
            ctypes.sizeof(state)
        )
        log.info("Responsive mode enabled (Normal priority, EcoQoS cleared)")
    except Exception as e:
        log.warning(f"Could not set responsive mode: {e}")


def _render_tray_image(state: str = "idle"):
    """Render a 64x64 tray-icon variant for one of three states.

    state ∈ {idle, armed, playing}:
      • idle    — Game Mode is off.  Mark drawn in muted gray.
      • armed   — Game Mode is on, no game detected.  Mark in full green.
      • playing — A game is currently active.  Bright green + corner dot.

    Reuses the layered-mark geometry from make_icon.py so the tray icon
    matches the .exe icon exactly.
    """
    from PIL import Image, ImageDraw
    SS = 4                         # super-sample for clean edges
    size = 64
    canvas = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    # Mark colours per state — Nacre iridescent (mint / lilac / rose layers)
    if state == "playing":
        c_top, c_mid, c_bot = ((167, 240, 228, 255),
                                (217, 184, 255, 255),
                                (255, 196, 230, 150))
    elif state == "armed":
        c_top, c_mid, c_bot = ((184, 200, 255, 255),
                                (159, 147, 230, 255),
                                (196, 193, 255, 120))
    else:  # idle
        c_top, c_mid, c_bot = ((140, 145, 152, 255),
                                (95, 100, 108, 255),
                                (140, 145, 152, 110))

    # Velocity V geometry (96x96 design space scaled to inner area).  Two
    # tapered blades, 2-toned per state so idle/armed/playing stay legible at
    # tray size; matches the .exe icon mark (make_icon.py).
    inner = int(size * SS * 0.82)
    pad = (size * SS - inner) // 2

    def p(x, y):
        return (x / 96 * inner + pad, y / 96 * inner + pad)

    v_left  = [p(22, 28), p(38, 28), p(49, 57), p(43, 70)]
    v_right = [p(49, 57), p(58, 28), p(74, 28), p(55, 70)]

    d.polygon(v_left,  fill=c_top)
    d.polygon(v_right, fill=c_mid)

    # "Playing" state: small accent dot in the top-right (notification cue)
    if state == "playing":
        r = size * SS // 9
        cx, cy = size * SS - r - 4 * SS, r + 4 * SS
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 200, 80, 255))

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _create_tray_icon(show_window_func, quit_func):
    """Create the system tray icon with a state-aware dynamic menu.

    Spawns a watcher thread that polls the game-monitor every 3 s.
    When the game-mode state changes (off → armed, armed → playing,
    playing → armed) the tray icon swaps to the matching variant and
    the right-click menu regenerates with the current game name.
    """
    try:
        from pystray import Icon, Menu, MenuItem
        from core import game_profiles, cleaner, power, notifier
    except ImportError:
        log.warning("pystray or Pillow not installed — no system tray icon")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    # Pre-render the three state variants so swaps are instant.
    images = {
        "idle":    _render_tray_image("idle"),
        "armed":   _render_tray_image("armed"),
        "playing": _render_tray_image("playing"),
    }

    def _current_state():
        try:
            s = game_profiles.get_engine_status()
            if s.get("active_pid"):
                return "playing"
            if s.get("monitoring"):
                return "armed"
        except Exception:
            pass
        return "idle"

    # ── Menu actions ─────────────────────────────────────────────────
    def _action_show(icon, item):
        try: show_window_func()
        except Exception as e: log.warning(f"tray show failed: {e}")

    def _action_toggle_game_mode(icon, item):
        try:
            s = game_profiles.get_engine_status()
            if s.get("monitoring"):
                game_profiles.stop_monitoring()
                if hasattr(notifier, "toast"):
                    notifier.toast("Game Mode", "Monitoring stopped.")
            else:
                game_profiles.start_monitoring()
                if hasattr(notifier, "toast"):
                    notifier.toast("Game Mode", "Now watching for games.")
        except Exception as e:
            log.warning(f"tray toggle failed: {e}")
        try: icon.update_menu()
        except Exception: pass

    def _action_refresh_pc(icon, item):
        def _bg():
            try:
                power.cleanup_only()
                if hasattr(notifier, "toast"):
                    notifier.toast("PC Refreshed",
                                   "Standby cleared, DNS flushed, MMCSS reset.")
            except Exception as e:
                log.warning(f"tray refresh failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()

    def _action_clean_ram(icon, item):
        def _bg():
            try:
                r = cleaner.clean_ram()
                msg = f"Freed {r.get('freed_mb', 0)} MB" if r.get("ok") else "RAM clean failed"
                if hasattr(notifier, "toast"):
                    notifier.toast("RAM Cleaned", msg)
            except Exception as e:
                log.warning(f"tray clean ram failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()

    def _action_force_normal(icon, item):
        def _bg():
            try:
                game_profiles.restore_normal_mode("user-tray")
                if hasattr(notifier, "toast"):
                    notifier.toast("Normal Mode", "All gaming tweaks reverted.")
            except Exception as e:
                log.warning(f"tray force normal failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()
        try: icon.update_menu()
        except Exception: pass

    def _action_quit(icon, item):
        try: icon.stop()
        except Exception: pass
        try: quit_func()
        except Exception: pass

    # ── Menu factory — called every time the menu is opened so labels
    #    reflect live state (current game name, monitor on/off, etc.)
    def _menu_factory():
        s = {}
        try:
            s = game_profiles.get_engine_status()
        except Exception:
            pass
        monitoring = bool(s.get("monitoring"))
        active_game = s.get("active_game") or None

        # Status header line — non-clickable
        if active_game:
            status_line = f"▶  {active_game[:38]}"
        elif monitoring:
            status_line = "●  Game Mode: armed"
        else:
            status_line = "○  Game Mode: off"

        return Menu(
            MenuItem("Open Vispora", _action_show, default=True),
            Menu.SEPARATOR,
            MenuItem(status_line, None, enabled=False),
            MenuItem(
                "Game Mode",
                _action_toggle_game_mode,
                checked=lambda _: monitoring,
            ),
            MenuItem("Force Normal Mode", _action_force_normal,
                     visible=lambda _: bool(active_game)),
            Menu.SEPARATOR,
            MenuItem("Refresh PC now", _action_refresh_pc),
            MenuItem("Clean RAM now",  _action_clean_ram),
            Menu.SEPARATOR,
            MenuItem("Quit", _action_quit),
        )

    icon = Icon(
        "Vispora",
        images["idle"],
        "Vispora",
        menu=_menu_factory(),
    )

    # ── Watcher thread: swap icon + tooltip when state changes ───────
    def _watch():
        last_state = "idle"
        last_game  = None
        while True:
            try:
                state = _current_state()
                game = None
                try:
                    game = (game_profiles.get_engine_status() or {}).get("active_game")
                except Exception:
                    pass
                if state != last_state:
                    icon.icon = images.get(state, images["idle"])
                    last_state = state
                    log.info(f"Tray icon → {state}")
                if game != last_game:
                    icon.title = (
                        f"Vispora — playing {game[:48]}" if game else
                        "Vispora — game mode armed"   if state == "armed" else
                        "Vispora"
                    )
                    last_game = game
                # Refresh menu so the status line + checkbox reflect live state.
                icon.menu = _menu_factory()
                try: icon.update_menu()
                except Exception: pass
            except Exception as e:
                log.debug(f"tray watcher tick failed: {e}")
            time.sleep(3)

    threading.Thread(target=_watch, daemon=True, name="tray-watcher").start()

    icon.run()


def _focus_running_instance() -> bool:
    """If another GhostShell process owns the Flask port, ask it to show
    its window and bring it to the foreground.  Returns True if we
    successfully handed off to the existing instance.

    v2.9.6 — second-launch handling.  Without this, double-clicking the
    .exe while it's already running in the tray spawns a brand-new
    Python+webview process that fights the original for the Flask port
    (and the user gets two ghostshells).  Now the second launch just
    pings the original via /api/window/show and exits.
    """
    import urllib.request, json
    try:
        url = f"http://127.0.0.1:{config.APP_PORT}/api/window/show"
        req = urllib.request.Request(url, method="POST",
                                     headers={"Content-Type": "application/json"},
                                     data=b"{}")
        with urllib.request.urlopen(req, timeout=2) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            try:
                data = json.loads(body)
            except Exception:
                data = {}
            log.info(f"Existing GhostShell instance focused (HTTP {resp.status})")
            return bool(data.get("ok"))
    except Exception as e:
        log.debug(f"No reachable existing instance: {e}")
        return False


# Module-scoped mutex handle.  We have to keep a Python reference so the
# OS doesn't garbage-collect it (which would release the mutex and let a
# second instance through).
_single_instance_mutex_handle = None


def _acquire_single_instance_lock() -> bool:
    """Try to claim the global GhostShell mutex.

    Returns True on success (we are the canonical instance).
    Returns False if another GhostShell already owns it — caller should
    hand off to that instance via _focus_running_instance() and exit.
    """
    global _single_instance_mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # Global\ prefix → visible across user sessions.  Use Local\ to
        # scope to the current desktop session only (which is what we
        # want — autostart runs as the same user, so Local is enough
        # AND avoids needing SeCreateGlobalPrivilege).
        MUTEX_NAME      = r"Local\Vispora-SingleInstance-{8B7A1F23}"
        ERROR_ALREADY_EXISTS = 183

        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype  = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        last_err = ctypes.get_last_error()
        if not handle:
            log.warning(f"CreateMutexW returned NULL (err {last_err}) — assuming first instance")
            return True
        if last_err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False

        _single_instance_mutex_handle = handle  # keep alive for process lifetime
        return True
    except Exception as e:
        # If anything goes wrong with the mutex, fall back to "let it
        # through" — an extra instance is annoying but not catastrophic.
        log.warning(f"Single-instance lock failed ({e}); allowing launch")
        return True


def main():
    global _webview_window

    # v2.9.6 — single-instance check.
    # If GhostShell is already running, hand off to it and exit silently.
    # Done BEFORE the admin elevation prompt so the user doesn't get a
    # UAC popup just to be told "already running".
    if not _acquire_single_instance_lock():
        log.info("Another GhostShell instance is running — handing off")
        if _focus_running_instance():
            sys.exit(0)
        # If the existing instance didn't respond, it may be hung — the
        # mutex name will remain held by the dead-but-still-listed
        # process for a moment.  Exit anyway to avoid running two.
        log.warning("Existing instance held the mutex but didn't respond — exiting")
        sys.exit(0)

    # Check admin on Windows
    if sys.platform == "win32" and not is_admin():
        log.warning("Not running as administrator — requesting elevation")
        try:
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, " ".join(sys.argv), None, 1
            )
            sys.exit(0)
        except Exception:
            log.error("Failed to elevate. Some features will not work.")

    # v2.9.5 — autostart-mode detection.
    # Set by the registry Run-key launcher.  When true:
    #   * The window starts hidden (tray-only) — no focus stealing on boot.
    #   * Heavy startup work (boot prep, GitHub update check) defers further.
    from_autostart = "--from-autostart" in sys.argv

    log.info(f"Starting {config.APP_NAME} v{config.APP_VERSION}"
             + (" (autostart mode — gentle launch)" if from_autostart else ""))

    # v2.9.4 — post-install detection.
    # Right after an auto-update swap, this returns 'updated' (success) or
    # 'install_failed' (the batch couldn't replace the exe — usually due to
    # antivirus locking the file).  Either way, fire a Windows toast so the
    # user knows what happened, and stash the result for the UI to render.
    try:
        global _post_install_state
        _post_install_state = updater.check_post_install_state()
        if _post_install_state.get("state") == "updated":
            notifier.notify_update_installed(
                _post_install_state.get("current") or config.APP_VERSION,
                _post_install_state.get("previous") or "an earlier version",
            )
        elif _post_install_state.get("state") == "install_failed":
            reason = _post_install_state.get("bat") or "swap failed"
            notifier.notify_update_failed(reason)
    except Exception as e:
        log.warning(f"Post-install state check failed: {e}")

    # Set efficiency mode so GhostShell itself is lightweight
    _set_efficiency_mode()

    # v3.1 fix — restore microphone access if it was killed by an earlier
    # GhostShell version's bulk privacy tweak.  Idempotent + safe: only ever
    # PERMITS the mic, never blocks it.  Called on every launch so users
    # whose mic was broken simply have to open GhostShell once more.
    try:
        restore = privacy.restore_microphone_access()
        log.info(f"Mic-restore boot pass: {restore}")
    except Exception as e:
        log.debug(f"Mic-restore failed: {e}")

    # v3.1 fix — restore Microsoft Store / Xbox app update functionality.
    # Earlier GhostShell versions disabled DiagTrack and denied
    # LetAppsRunInBackground, which broke the Store update flow.  Safe +
    # idempotent: re-enables only what Store/Xbox need to fetch updates.
    try:
        restore_store = privacy.restore_store_updates()
        log.info(f"Store-update-restore boot pass: {restore_store}")
    except Exception as e:
        log.debug(f"Store-update-restore failed: {e}")

    # v3.3 fix — block ms-gamebar* URL protocols.  Anyone who already
    # debloated Game Bar pre-v3.3 will be hitting the "Get an app to
    # open this link" picker on system events that try ms-gamebar://.
    # Idempotent + per-user (HKCU only), so safe to fire every launch.
    try:
        from core import debloater
        gb_block = debloater.block_gamebar_protocols()
        log.debug(f"Gamebar protocol block boot pass: {gb_block}")
    except Exception as e:
        log.debug(f"Gamebar block failed: {e}")

    # v3.3 — popup watchdog.  Defensive layer that auto-closes the
    # 'Get an app to open this <ms-gamebar> link' dialog if it ever
    # slips through the URL-protocol no-op handler (rare race conditions
    # or third-party apps invoking via paths that bypass HKCU classes).
    try:
        from core import popup_killer
        popup_killer.start()
    except Exception as e:
        log.debug(f"popup_killer start failed: {e}")

    # Check if gaming tweaks are stuck from a previous session (crash/reboot mid-game)
    game_profiles.check_and_restore_on_boot()

    # v3.1 fix — clear AT runtime if a previous run left it tracking a
    # process that no longer exists (e.g. game closed without firing exit).
    try:
        orphan = adaptive_tuning.clear_runtime_if_orphaned()
        if orphan.get("orphan"):
            log.warning(f"AT orphan clear at boot: {orphan}")
    except Exception as e:
        log.debug(f"AT orphan check failed: {e}")

    # v3.1 — start the network-burst sampler so the Network tab has data
    # ready when the user opens it, and so the burst-alert system is
    # always armed during gameplay.
    try:
        net_monitor.start()
        log.info("net_monitor sampler started")
    except Exception as e:
        log.debug(f"net_monitor start failed: {e}")

    # v3.1 — start the gamepad mapper.  Keys are only emitted when the
    # master switch is on AND a profile is active AND a game is in the
    # foreground (depending on profile setting), so this is safe to run
    # all the time — it's also what powers the 'press a button to bind' UI.
    try:
        gamepad_mapper.start()
        log.info("gamepad_mapper loop started")
    except Exception as e:
        log.debug(f"gamepad_mapper start failed: {e}")

    # v3.1 — Background Pauser orphan recovery.  If a previous run crashed
    # mid-game, any browser / AI app we suspended is still frozen.  Resume
    # them immediately so the user isn't stuck.
    try:
        from core import background_pauser
        recover = background_pauser.recover_orphans()
        if recover.get("orphans") or recover.get("resumed"):
            log.warning(f"Pauser orphan recovery: {recover}")
    except Exception as e:
        log.debug(f"Pauser orphan recovery failed: {e}")

    # v3.1 — Tournament Mode stale-state recovery.  If a previous run died
    # with Tournament ON and the timer has since expired, revert all
    # tweaks now so the user isn't stuck with no notifications etc.
    try:
        from core import tournament_mode
        tr = tournament_mode.recover_if_stale()
        if tr.get("stale"):
            log.warning(f"Tournament stale-recovery: {tr}")
    except Exception as e:
        log.debug(f"Tournament stale-recovery failed: {e}")

    # v2.9.7 — migrate from the v2.9.5/2.9.6 Run-key autostart to the
    # new Scheduled-Task implementation.  No-op for users who never had
    # autostart enabled.  Runs in a thread so it can't block startup.
    threading.Thread(target=autostart.migrate_from_legacy_runkey,
                     daemon=True).start()

    # v2.9.5 — startup pacing.
    # Manual launch:    everything fires immediately — user wants the app NOW.
    # Autostart launch: defer the heavy stuff (GitHub check, boot prep,
    #                   saved-OC apply) so we don't fight Windows for CPU /
    #                   network during the user's login.  Keep the UI's
    #                   own thread responsive but don't ping the network.
    update_check_delay  = 90 if from_autostart else 0    # GitHub HTTPS round-trip
    boot_prep_delay     = 60 if from_autostart else 0    # WMI scans, ping
    saved_oc_delay      = 15 if from_autostart else 0    # NVAPI write
    # v2.9.6 — driver updater also checks via the network (NVIDIA's API
    # + a ~800 MB download if the user has auto_download on), so defer it
    # heavily on autostart so it doesn't fight Windows for the link during
    # boot.  120s gives login/startup apps + boot prep enough breathing room.
    driver_check_delay  = 120 if from_autostart else 5

    def _delayed(delay_s, fn):
        def _wrap():
            try:
                if delay_s > 0:
                    time.sleep(delay_s)
                fn()
            except Exception as e:
                log.warning(f"deferred startup task failed: {e}")
        threading.Thread(target=_wrap, daemon=True).start()

    _delayed(update_check_delay, updater.check_update_background)

    def _on_prep_done(duration):
        try: notifier.notify_boot_prep_done(duration)
        except Exception: pass

    _delayed(boot_prep_delay,
             lambda: boot_prep.prep_for_gaming_async(on_done=_on_prep_done))

    def _apply_saved_oc():
        try:
            r = gpu_overclock.apply_saved_profile_on_startup()
            if r.get("applied"):
                log.info("GPU OC profile applied on startup")
        except Exception as e:
            log.warning(f"Failed to apply saved OC profile: {e}")
    _delayed(saved_oc_delay, _apply_saved_oc)

    # v2.9.6 — start the GPU driver updater background loop.
    # Internally schedules: 1 immediate check + a 12h periodic loop
    # (both gated by the auto_check setting).  We wrap it in _delayed()
    # so the autostart-mode startup doesn't fire it during the login storm.
    _delayed(driver_check_delay, driver_updater.check_in_background)

    # v2.9.9.6 — auto-enable game-mode (game-detection monitor) on boot
    # if the user opted in (default ON).  Uses the same boot-prep delay
    # so we don't compete with Windows during login.  When enabled, any
    # game from the ~280-entry database that launches afterwards gets the
    # gaming profile (priority, affinity, OC, etc.) applied automatically
    # and reverted when the game closes.
    _delayed(boot_prep_delay,
             lambda: game_profiles.auto_enable_on_boot_if_set())

    # v3 — start the hard GPU temp-ceiling watchdog.  Polls GPU temp
    # every 1 s and emergency-resets the OC if it sustains above the
    # user-set ceiling (default 83°C / 5 s sustain).  Safety net under
    # every other GPU feature.  Settings live in
    # %APPDATA%/GhostShell/temp_ceiling_settings.json and are loaded
    # by the watchdog itself, so no parameters are needed here.
    def _start_temp_ceiling():
        try:
            s = temp_ceiling.get_settings()
            if s.get("enabled"):
                temp_ceiling.start_watchdog()
                log.info(f"Temp ceiling watchdog started (ceiling {s['ceiling_c']}°C, "
                         f"{s['sustained_seconds']}s sustain, action={s['action']})")
        except Exception as e:
            log.warning(f"Failed to start temp_ceiling watchdog: {e}")
    _delayed(saved_oc_delay, _start_temp_ceiling)

    # beta.3 — if Competitive Latency mode was left enabled, re-assert the
    # 0.5 ms timer-resolution hold (it only lasts for the process lifetime).
    def _reassert_competitive():
        try:
            competitive_latency.apply_on_boot()
        except Exception as e:
            log.warning(f"competitive latency boot re-assert failed: {e}")
    _delayed(2, _reassert_competitive)

    # Start Flask in background thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()

    # v2.9.9.0 — fast Flask-readiness wait.  Old code blocked 500 ms blindly.
    # Now we poll the port until it accepts a connection (typically <80 ms),
    # capped at 1 s so we never deadlock.  Cuts ~400 ms off cold-start.
    import socket
    flask_ready_deadline = time.time() + 1.0
    while time.time() < flask_ready_deadline:
        try:
            with socket.create_connection(("127.0.0.1", config.APP_PORT), timeout=0.05):
                break
        except OSError:
            time.sleep(0.02)

    # v3.2-beta.7 — boot-time dependency check + auto-install.  Runs in
    # a background thread so it doesn't delay the UI window.  Honors the
    # user's `auto_install_enabled` setting (default True).  Installs
    # ANY missing auto-installable optional deps — PresentMon 1.x,
    # PresentMon 2.x, ViGEmBus, HidHide.  RTSS is browser-only so it's
    # just surfaced in the UI for the user to install manually.
    def _boot_dep_check():
        try:
            time.sleep(2)   # let Flask + UI settle before we hammer disk
            from core import dependencies
            r = dependencies.check_on_boot()
            log.info(f"dependency boot check complete: {r}")
        except Exception as e:
            log.warning(f"dependency boot check failed: {e}")
    threading.Thread(target=_boot_dep_check, daemon=True,
                     name="dep-boot-check").start()

    # v3.3 — Community games merge.  Background fetch + add any
    # community-contributed game exes to our KNOWN_GAME_EXES so games
    # other users have already submitted get auto-recognized here too.
    try:
        from core import community
        community.merge_into_known_games_async()
    except Exception as e:
        log.warning(f"community games bootstrap failed: {e}")

    # v3.3 — Capture: register global save-clip + screenshot hotkeys,
    # and auto-start the rolling clipper buffer if the user has it
    # enabled in settings.  Done lazily on a worker thread because
    # parsing the FFmpeg encoder probe takes ~100 ms.
    def _boot_capture_init():
        try:
            time.sleep(1)
            from core import clipper
            s = clipper.get_settings()
            _wire_capture_hotkeys(s)
            log.info(f"Capture hotkeys wired: save={s.get('save_hotkey')} "
                     f"screenshot={s.get('screenshot_hotkey')}")
            if s.get("enabled") and clipper._find_ffmpeg():
                r = clipper.start_buffer()
                log.info(f"Clipper auto-start: {r}")
            # v3.3.0-beta.17 — also kick the OBS auto-reconnect if the
            # OBS backend toggle is on, so the Capture page lights up
            # green from the moment it loads.  Runs in a separate
            # daemon thread inside boot_autoconnect() — no blocking.
            try:
                from core import obs_backend
                obs_backend.boot_autoconnect()
            except Exception as e:
                log.debug(f"obs auto-reconnect on boot failed: {e}")
            # v3.3.1-beta.1 — if Stream Helper is on, start the
            # foreground-game watcher so OBS' Game Capture + per-app
            # audio sources track whichever game is currently active.
            try:
                from core import stream_helper
                if stream_helper.get_settings().get("enabled"):
                    stream_helper.start_watcher()
                    log.info("Stream Helper watcher started on boot")
            except Exception as e:
                log.debug(f"stream helper boot start failed: {e}")
            # v3.3.1-beta.9 — scan registry/BCD for tweaks older
            # GhostShell builds applied that we now know were bad
            # (DisableAbsoluteVolume, recoveryenabled No, ScheduledDefrag
            # disabled, FSO=2, etc.).  Anything found gets reverted +
            # written to a recovery report the UI shows as an
            # apology toast on next paint.
            try:
                from core import auto_recover
                auto_recover.recover_legacy_bad_tweaks()
            except Exception as e:
                log.debug(f"auto_recover on boot failed: {e}")
        except Exception as e:
            log.warning(f"capture boot init failed: {e}")
    threading.Thread(target=_boot_capture_init, daemon=True,
                     name="capture-boot").start()

    # v3.2.2 — write install_info.json (where we live + which channel +
    # which version) so the standalone updater can find us, then auto-
    # fetch the updater binary itself from Railway if missing.  Both run
    # in a background thread — the updater download is ~30 MB so we
    # don't want it competing with the UI for cold-start time.
    def _boot_updater_bootstrap():
        try:
            from core import updater as _upd
            info = _upd.write_install_info()
            log.info(f"install_info.json: {info}")
            # v1.2.0 — was ensure_updater_installed_async; upgraded to
            # also refresh the local updater binary when Railway is
            # hosting a newer one.  Same fast non-blocking semantics.
            r = _upd.ensure_updater_up_to_date_async()
            log.info(f"updater bootstrap: {r}")
        except Exception as e:
            log.warning(f"updater bootstrap failed: {e}")
    threading.Thread(target=_boot_updater_bootstrap, daemon=True,
                     name="updater-bootstrap").start()

    # beta.16 — twice-daily PC integrity scan.  Runs in its own
    # BELOW_NORMAL-priority thread; gates on user-idle + no game +
    # GhostShell not in foreground.  See core/integrity_scan.py for
    # the full safety model (allow-list patterns, path-traversal
    # protection, per-file try/except, 5-minute time budget).
    def _boot_integrity_scan():
        try:
            from core import integrity_scan
            r = integrity_scan.start()
            log.info(f"integrity-scan scheduler: {r}")
        except Exception as e:
            log.warning(f"integrity-scan scheduler boot failed: {e}")
    threading.Thread(target=_boot_integrity_scan, daemon=True,
                     name="integrity-scan-boot").start()

    def show_window():
        _win_show()

    def quit_app():
        log.info("GhostShell shutting down")
        os._exit(0)

    try:
        import webview

        # 3.4.0 — GPU-accelerate the WebView2 surface.  capabilities.py
        # classifies the GPU tier; webview2_gpu_args() returns Chromium
        # switches tuned to it (rasterization + zero-copy on discrete /
        # modern iGPU, clean software path on weak iGPU / VM / none —
        # half-accelerated states on buggy iGPU drivers are jankier than
        # SwiftShader).  The env var MUST be set BEFORE webview.start()
        # spins up the WebView2 host process; pywebview forwards it.
        try:
            from core import capabilities
            gpu_args = capabilities.webview2_gpu_args()
            existing = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
            os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
                (existing + " " + gpu_args).strip() if existing else gpu_args
            )
            caps = capabilities.get()
            log.info(f"WebView2 GPU policy ({caps.get('gpu_tier')}): {gpu_args}")
            if not caps.get("webview2_present"):
                log.warning("WebView2 runtime NOT detected — UI may fall back "
                            "to MSHTML.  Dependency check will auto-install it.")
        except Exception as e:
            log.warning(f"capability-based GPU policy failed (UI still works): {e}")

        # v2.9.5 — autostart mode opens the window hidden so we don't
        # steal focus when Windows is still bringing up the user's
        # desktop / startup apps.  User clicks the tray icon when ready.
        # 3.4.0 — gui='edgechromium' forces the modern WebView2 engine
        # instead of letting pywebview silently fall back to MSHTML on
        # systems where its auto-detection is shaky.
        _webview_window = webview.create_window(
            config.APP_NAME,
            f"http://127.0.0.1:{config.APP_PORT}",
            width=1320,
            height=860,
            frameless=True,
            easy_drag=False,
            min_size=(1024, 600),
            background_color="#0a0a0f",
            js_api=WindowJSAPI(),
            hidden=from_autostart,   # <-- start tray-only on autostart launch
        )
        if from_autostart:
            global _window_hidden
            _window_hidden = True
            log.info("Started hidden (autostart mode) — click tray icon to show")

        # Start tray icon in background
        tray_thread = threading.Thread(target=_create_tray_icon, args=(show_window, quit_app), daemon=True)
        tray_thread.start()

        # v3.4 — DevTools off in production builds.  gui='edgechromium'
        # pins the modern WebView2 engine; if the runtime is genuinely
        # absent pywebview raises and we fall through to browser mode
        # rather than silently rendering in MSHTML.
        try:
            webview.start(debug=False, gui="edgechromium")
        except Exception as e:
            log.warning(f"edgechromium start failed ({e}); letting pywebview pick a backend")
            webview.start(debug=False)

    except ImportError:
        log.warning("pywebview not installed — opening in browser mode")
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{config.APP_PORT}")
        # Run tray icon on main thread (blocking)
        _create_tray_icon(lambda: None, quit_app)


if __name__ == "__main__":
    main()
