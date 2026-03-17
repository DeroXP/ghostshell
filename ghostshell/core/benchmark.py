from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, List, Optional

from config import APPDATA_DIR
from core.utils import get_logger, log_event, ping

LOGGER = get_logger(__name__)

BENCH_DIR = Path(APPDATA_DIR) / "benchmarks"
HISTORY_FILE = BENCH_DIR / "history.json"

TARGETS = ("cpu", "memory", "disk", "network")
DEFAULT_DURATION = 2.0
CPU_BUFFER_BYTES = 1024 * 1024
MEM_BUFFER_BYTES = 64 * 1024 * 1024
DISK_BLOCK_BYTES = 8 * 1024 * 1024
DISK_MIN_TOTAL_BYTES = 128 * 1024 * 1024
NETWORK_HOSTS = ["1.1.1.1", "8.8.8.8", "cloudflare.com", "google.com"]


def run_benchmark(targets: Optional[List[str]] = None, duration_s: float = DEFAULT_DURATION) -> Dict[str, Any]:
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")

    requested_targets = _normalize_targets(targets)
    LOGGER.debug("Running benchmark for targets=%s duration=%.2fs", requested_targets, duration_s)

    log_event("benchmark", "run", "start", f"targets={','.join(requested_targets)} duration={duration_s}")
    timestamp = _iso_timestamp()
    results: Dict[str, Any] = {"timestamp": timestamp}
    previous_history = load_history(max_items=1)
    previous_entry = previous_history[-1] if previous_history else None

    if "cpu" in requested_targets:
        results["cpu"] = _benchmark_cpu(duration_s)
    if "memory" in requested_targets:
        results["memory"] = _benchmark_memory(duration_s)
    if "disk" in requested_targets:
        results["disk"] = _benchmark_disk(duration_s)
    if "network" in requested_targets:
        results["network"] = _benchmark_network()

    results["deltas"] = _compute_deltas(results, previous_entry) if previous_entry else {}

    saved = _append_history(results)
    results["saved"] = bool(saved)
    log_event("benchmark", "run", "end", json.dumps({k: v for k, v in results.items() if k != "deltas"}))
    return results


def load_history(max_items: int = 20) -> List[Dict[str, Any]]:
    try:
        if not HISTORY_FILE.exists():
            return []
        with HISTORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        if max_items and max_items > 0:
            return data[-max_items:]
        return data
    except Exception:
        return []


def clear_history() -> None:
    try:
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        log_event("benchmark", "clear_history", "success")
    except Exception as exc:
        log_event("benchmark", "clear_history", "error", str(exc))


def _normalize_targets(targets: Optional[List[str]]) -> List[str]:
    if not targets:
        return list(TARGETS)
    norm = []
    for t in targets:
        t2 = (t or "").strip().lower()
        if t2 in TARGETS and t2 not in norm:
            norm.append(t2)
    return norm or list(TARGETS)


def _benchmark_cpu(duration_s: float) -> Dict[str, Any]:
    action = "cpu"
    start = perf_counter()
    buf = bytearray(os.urandom(CPU_BUFFER_BYTES))
    count = 0
    try:
        while True:
            sha256(buf).digest()
            count += 1
            if perf_counter() - start >= duration_s:
                break
        total_mb = (count * CPU_BUFFER_BYTES) / (1024 * 1024)
        hashes_per_sec = count / duration_s
        mb_per_sec = total_mb / duration_s
        result = {"hashes_per_sec": round(hashes_per_sec, 2), "mb_per_sec": round(mb_per_sec, 2)}
        log_event("benchmark", action, "success", str(result))
        return result
    except Exception as exc:
        log_event("benchmark", action, "error", str(exc))
        return {"error": str(exc), "hashes_per_sec": None, "mb_per_sec": None}


def _benchmark_memory(duration_s: float) -> Dict[str, Any]:
    action = "memory"
    src = bytearray(b"A" * MEM_BUFFER_BYTES)
    dst = bytearray(MEM_BUFFER_BYTES)
    start = perf_counter()
    count = 0
    try:
        while True:
            dst[:] = src
            count += 1
            if perf_counter() - start >= duration_s:
                break
        total_mb = (count * MEM_BUFFER_BYTES) / (1024 * 1024)
        copies_per_sec = count / duration_s
        mb_per_sec = total_mb / duration_s
        result = {"copies_per_sec": round(copies_per_sec, 2), "mb_per_sec": round(mb_per_sec, 2)}
        log_event("benchmark", action, "success", str(result))
        return result
    except Exception as exc:
        log_event("benchmark", action, "error", str(exc))
        return {"error": str(exc), "copies_per_sec": None, "mb_per_sec": None}
    finally:
        # Encourage prompt GC of large buffers
        del src
        del dst


def _benchmark_disk(duration_s: float) -> Dict[str, Any]:
    action = "disk"
    _ensure_bench_dir()
    path = BENCH_DIR / "bench.bin"
    block = os.urandom(DISK_BLOCK_BYTES)
    written = 0
    write_mb_per_sec = None
    read_mb_per_sec = None

    try:
        start = perf_counter()
        with path.open("wb", buffering=0) as f:
            while written < DISK_MIN_TOTAL_BYTES and (perf_counter() - start) < duration_s:
                f.write(block)
                written += len(block)
            f.flush()
            os.fsync(f.fileno())
        elapsed = perf_counter() - start
        write_mb_per_sec = round((written / (1024 * 1024)) / max(elapsed, 1e-6), 2)
    except Exception as exc:
        log_event("benchmark", action, "error", f"write: {exc}")
        return {"error": f"write: {exc}", "write_mb_per_sec": None, "read_mb_per_sec": None, "total_mb": round(written / (1024 * 1024), 2)}

    try:
        read_bytes = 0
        start_r = perf_counter()
        with path.open("rb", buffering=0) as f:
            while True:
                chunk = f.read(DISK_BLOCK_BYTES)
                if not chunk:
                    break
                read_bytes += len(chunk)
        elapsed_r = perf_counter() - start_r
        read_mb_per_sec = round((read_bytes / (1024 * 1024)) / max(elapsed_r, 1e-6), 2)
    except Exception as exc:
        log_event("benchmark", action, "error", f"read: {exc}")
        return {"error": f"read: {exc}", "write_mb_per_sec": write_mb_per_sec, "read_mb_per_sec": None, "total_mb": round(written / (1024 * 1024), 2)}
    finally:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

    result = {
        "write_mb_per_sec": write_mb_per_sec,
        "read_mb_per_sec": read_mb_per_sec,
        "total_mb": round(written / (1024 * 1024), 2),
    }
    log_event("benchmark", action, "success", str(result))
    return result


def _benchmark_network() -> Dict[str, Any]:
    action = "network"
    by_host: List[Dict[str, Any]] = []
    best: Optional[int] = None
    try:
        for host in NETWORK_HOSTS:
            m = ping(host, count=4, timeout_ms=1000)
            entry = {"host": host, **m}
            by_host.append(entry)
            avg = m.get("avg")
            if isinstance(avg, (int, float)):
                val = int(avg)
                if best is None or val < best:
                    best = val
        result = {"best_avg_ms": best, "by_host": by_host}
        log_event("benchmark", action, "success", str(result))
        return result
    except Exception as exc:
        log_event("benchmark", action, "error", str(exc))
        return {"error": str(exc), "best_avg_ms": None, "by_host": by_host}


def _compute_deltas(current: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Optional[float]]:
    delta_map = {
        "cpu_mb_per_sec": ("cpu", "mb_per_sec"),
        "memory_mb_per_sec": ("memory", "mb_per_sec"),
        "disk_write_mb_per_sec": ("disk", "write_mb_per_sec"),
        "disk_read_mb_per_sec": ("disk", "read_mb_per_sec"),
        "network_best_avg_ms": ("network", "best_avg_ms"),
    }
    deltas: Dict[str, Optional[float]] = {}
    for key, (section, field) in delta_map.items():
        curr_section = current.get(section) or {}
        prev_section = previous.get(section) or {}
        curr_value = curr_section.get(field)
        prev_value = prev_section.get(field)
        if isinstance(curr_value, (int, float)) and isinstance(prev_value, (int, float)):
            deltas[key] = float(curr_value) - float(prev_value)
        else:
            deltas[key] = None
    return deltas


def _append_history(entry: Dict[str, Any]) -> bool:
    try:
        _ensure_bench_dir()
        history = load_history(max_items=10)
        history.append(entry)
        history = history[-10:]
        temp_fd, temp_path = tempfile.mkstemp(dir=BENCH_DIR, prefix="history_", suffix=".json")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                json.dump(history, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            shutil.move(temp_path, HISTORY_FILE)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return True
    except Exception as exc:
        LOGGER.exception("Failed to append benchmark history: %s", exc)
        return False


def _ensure_bench_dir() -> None:
    try:
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error("Failed to create benchmark directory: %s", exc)
        raise


def _iso_timestamp() -> str:
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    return ts.isoformat().replace("+00:00", "Z")
