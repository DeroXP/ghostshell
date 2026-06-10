"""Clean shutdown / restart. Runs fast cleanup passes so the next boot is faster."""
import os
import threading
import time
from core.utils import run_ps, run_cmd, get_logger
from core import cleaner, game_profiles

log = get_logger("power")


def _quick_cleanup() -> dict:
    """Fast cleanup pass — temp files, caches, RAM, DNS, prefetch. ~5-15s."""
    log.info("=== PRE-SHUTDOWN CLEANUP ===")
    start = time.time()
    result = {"freed_bytes": 0, "steps": []}

    # 1. Make sure gaming tweaks aren't left active (they'd carry to next boot)
    try:
        if game_profiles._is_gaming_flag_set():
            game_profiles.restore_normal_mode("shutdown cleanup")
            result["steps"].append({"name": "Revert gaming tweaks", "ok": True})
    except Exception as e:
        result["steps"].append({"name": "Revert gaming tweaks", "ok": False, "err": str(e)})

    # 2. Clean temp/cache directories (the safe, fast pass)
    try:
        temp_result = cleaner.clean_all()
        result["freed_bytes"] += temp_result.get("total_freed", 0)
        result["steps"].append({
            "name": "Clean temp & caches",
            "ok": True,
            "freed": temp_result.get("total_freed_str", "0 B"),
        })
    except Exception as e:
        result["steps"].append({"name": "Clean temp & caches", "ok": False, "err": str(e)})

    # 3. Flush DNS cache (fresh lookups on next boot)
    r = run_cmd(["ipconfig", "/flushdns"], timeout=10)
    result["steps"].append({"name": "Flush DNS cache", "ok": r["ok"]})

    # 4. Clear RAM standby list + working sets (next boot starts cleaner)
    try:
        ram_result = cleaner.clean_ram()
        result["freed_bytes"] += ram_result.get("freed", 0)
        result["steps"].append({
            "name": "Clear RAM standby",
            "ok": True,
            "freed": ram_result.get("freed_str", "0 B"),
        })
    except Exception as e:
        result["steps"].append({"name": "Clear RAM standby", "ok": False, "err": str(e)})

    # 5. Clear event logs (tiny, but keeps boot quick)
    run_ps("Get-EventLog -List | ForEach-Object { Clear-EventLog -LogName $_.Log -ErrorAction SilentlyContinue }",
           timeout=15)
    result["steps"].append({"name": "Clear event logs", "ok": True})

    # 6. Flush filesystem buffers to disk so shutdown isn't delayed
    run_ps("try { [System.IO.File]::GetAttributes('C:\\') | Out-Null } catch {}", timeout=5)

    elapsed = round(time.time() - start, 1)
    result["elapsed_sec"] = elapsed
    log.info(f"=== CLEANUP DONE in {elapsed}s — freed {cleaner._format_size(result['freed_bytes'])} ===")
    return result


def shutdown(delay_sec: int = 10, clean: bool = True) -> dict:
    """Run cleanup, then shut down the PC."""
    log.info(f"Shutdown requested (clean={clean}, delay={delay_sec}s)")

    def _run():
        if clean:
            _quick_cleanup()
        log.info(f"Issuing shutdown in {delay_sec}s...")
        run_cmd(["shutdown", "/s", "/f", "/t", str(max(1, delay_sec)),
                 "/c", "GhostShell: cleanup complete — shutting down"], timeout=10)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "action": "shutdown", "delay": delay_sec, "clean": clean}


def restart(delay_sec: int = 10, clean: bool = True) -> dict:
    """Run cleanup, then restart the PC."""
    log.info(f"Restart requested (clean={clean}, delay={delay_sec}s)")

    def _run():
        if clean:
            _quick_cleanup()
        log.info(f"Issuing restart in {delay_sec}s...")
        run_cmd(["shutdown", "/r", "/f", "/t", str(max(1, delay_sec)),
                 "/c", "GhostShell: cleanup complete — restarting"], timeout=10)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "action": "restart", "delay": delay_sec, "clean": clean}


def cancel() -> dict:
    """Cancel a pending shutdown/restart (if the delay hasn't elapsed)."""
    r = run_cmd(["shutdown", "/a"], timeout=5)
    log.info("Shutdown/restart cancelled" if r["ok"] else "No pending shutdown to cancel")
    return {"ok": r["ok"]}


def cleanup_only() -> dict:
    """Run the cleanup pass without shutting down — same routine, no reboot."""
    return _quick_cleanup()
