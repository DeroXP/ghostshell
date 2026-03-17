import base64
import hashlib
import json
import secrets
import sqlite3
import subprocess
import threading
import time
from typing import Any, Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken

from config import (
    DB_FILE,
    VAULT_KDF_ITERATIONS,
    VAULT_AUTOLOCK_SECONDS,
    VAULT_MAX_ATTEMPTS,
    VAULT_LOCKOUT_MINUTES,
)
from core.utils import ensure_app_dirs, log_event

_CLIPBOARD_CLEAR_TIMER: Optional[threading.Timer] = None
_CLIPBOARD_LOCK = threading.Lock()


def copy_to_clipboard(text: str, clear_after_seconds: int = 30) -> bool:
    global _CLIPBOARD_CLEAR_TIMER
    text = text or ""
    script = "Set-Clipboard -Value @'\n{0}\n'@".format(text.replace("'", "''"))
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
        log_event("vault", "clipboard_copy", "success", f"chars={len(text)}")
    except Exception as exc:
        log_event("vault", "clipboard_copy", "error", str(exc))
        return False

    with _CLIPBOARD_LOCK:
        if _CLIPBOARD_CLEAR_TIMER:
            _CLIPBOARD_CLEAR_TIMER.cancel()
            _CLIPBOARD_CLEAR_TIMER = None
        if clear_after_seconds > 0:
            timer = threading.Timer(clear_after_seconds, _clear_clipboard)
            timer.daemon = True
            _CLIPBOARD_CLEAR_TIMER = timer
            timer.start()
    return True


def _clear_clipboard() -> None:
    global _CLIPBOARD_CLEAR_TIMER
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value ''"],
            check=True,
        )
        log_event("vault", "clipboard_clear", "success", "")
    except Exception as exc:
        log_event("vault", "clipboard_clear", "error", str(exc))
    finally:
        with _CLIPBOARD_LOCK:
            _CLIPBOARD_CLEAR_TIMER = None


class Vault:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._fernet: Optional[Fernet] = None
        self._unlocked_until: float = 0.0
        self._lock = threading.RLock()
        self._ensure_schema()

    @classmethod
    def open_or_init(cls) -> "Vault":
        ensure_app_dirs()
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        v = cls(conn)
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO settings(key, value) VALUES
                ('failed_attempts', '0'),
                ('lock_until', '0'),
                ('initialized', '1')
                """
            )
        return v

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pin(
                    pin_hash BLOB NOT NULL,
                    salt BLOB NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT,
                    username TEXT,
                    password BLOB,
                    url TEXT,
                    notes TEXT,
                    tags TEXT,
                    created_at INTEGER,
                    updated_at INTEGER
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta(
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

    # --- PIN & Key Management ---
    def is_initialized(self) -> bool:
        cur = self._conn.execute("SELECT value FROM settings WHERE key='initialized'")
        row = cur.fetchone()
        return bool(row and str(row[0]) == '1')

    def has_pin(self) -> bool:
        cur = self._conn.execute("SELECT COUNT(1) FROM pin")
        return bool(cur.fetchone()[0])

    def is_unlocked(self) -> bool:
        with self._lock:
            self.autolock_check()
            return bool(self._fernet) and time.time() <= self._unlocked_until

    def create_pin(self, pin: str) -> None:
        if not pin or not pin.isdigit() or not (4 <= len(pin) <= 8):
            raise ValueError("PIN must be 4-8 digits")
        if self.has_pin():
            raise ValueError("PIN already set")
        salt = secrets.token_bytes(16)
        pin_hash = bcrypt.hashpw(pin.encode("utf-8"), bcrypt.gensalt())
        key = self._derive_key(pin, salt)
        with self._conn:
            self._conn.execute("INSERT INTO pin(pin_hash, salt) VALUES(?, ?)", (pin_hash, salt))
        self._fernet = Fernet(key)
        self._touch()
        log_event("vault", "create_pin", "success", "PIN created")

    def verify_pin(self, pin: str) -> bool:
        cur = self._conn.execute("SELECT pin_hash FROM pin")
        row = cur.fetchone()
        if not row:
            return False
        ok = False
        try:
            ok = bcrypt.checkpw(pin.encode("utf-8"), row[0])
        except Exception:
            ok = False
        return ok

    def lock(self) -> None:
        with self._lock:
            self._fernet = None
            self._unlocked_until = 0
            log_event("vault", "lock", "success", "")

    def unlock(self, pin: str) -> bool:
        with self._lock:
            # Check lockout
            if self._is_locked_out():
                log_event("vault", "unlock", "locked", "Too many attempts")
                return False
            # Verify pin
            cur = self._conn.execute("SELECT pin_hash, salt FROM pin")
            row = cur.fetchone()
            if not row:
                return False
            pin_hash, salt = row[0], row[1]
            if not bcrypt.checkpw(pin.encode("utf-8"), pin_hash):
                self._increment_fail()
                log_event("vault", "unlock", "error", "Bad PIN")
                return False
            self._reset_fail()
            key = self._derive_key(pin, salt)
            try:
                self._fernet = Fernet(key)
                self._touch()
                log_event("vault", "unlock", "success", "")
                return True
            except Exception as exc:
                log_event("vault", "unlock", "error", str(exc))
                return False

    def autolock_check(self) -> None:
        with self._lock:
            if self._fernet and time.time() > self._unlocked_until:
                self.lock()

    # --- Entries ---
    def add_entry(self, service: str, username: str, password: str, url: Optional[str] = None, notes: Optional[str] = None, tags: Optional[str] = None) -> int:
        self._require_unlocked()
        now = int(time.time())
        enc = self._fernet.encrypt(password.encode("utf-8")) if self._fernet else b""
        with self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO entries(service, username, password, url, notes, tags, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (service, username, enc, url, notes, tags, now, now),
            )
            rowid = int(cur.lastrowid)
        self._touch()
        log_event("vault", "add_entry", "success", f"id={rowid}")
        return rowid

    def list_entries(self, query: Optional[str] = None, tag: Optional[str] = None) -> list[dict]:
        self._require_unlocked()
        sql = "SELECT id, service, username, url, notes, tags, created_at, updated_at FROM entries"
        params: list[Any] = []
        conds = []
        if query:
            conds.append("(service LIKE ? OR username LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if tag:
            conds.append("(tags LIKE ?)")
            params.append(f"%{tag}%")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY service ASC, username ASC"
        cur = self._conn.execute(sql, params)
        rows = [dict(row) for row in cur.fetchall()]
        self._touch()
        return rows

    def get_entry(self, id: int) -> Optional[dict]:
        self._require_unlocked()
        cur = self._conn.execute("SELECT * FROM entries WHERE id=?", (id,))
        row = cur.fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["password"] = self._fernet.decrypt(data["password"]).decode("utf-8") if self._fernet else ""
        except InvalidToken:
            data["password"] = ""
        self._touch()
        return data

    def update_entry(self, id: int, **fields) -> bool:
        self._require_unlocked()
        allowed = {"service", "username", "password", "url", "notes", "tags"}
        sets = []
        params: list[Any] = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "password" and v is not None:
                v = self._fernet.encrypt(str(v).encode("utf-8")) if self._fernet else None
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return False
        params.extend([int(time.time()), id])
        sql = f"UPDATE entries SET {', '.join(sets)}, updated_at=? WHERE id=?"
        with self._conn:
            cur = self._conn.execute(sql, params)
            ok = cur.rowcount > 0
        self._touch()
        log_event("vault", "update_entry", "success" if ok else "noop", f"id={id}")
        return ok

    def delete_entry(self, id: int) -> bool:
        self._require_unlocked()
        with self._conn:
            cur = self._conn.execute("DELETE FROM entries WHERE id=?", (id,))
            ok = cur.rowcount > 0
        self._touch()
        log_event("vault", "delete_entry", "success" if ok else "noop", f"id={id}")
        return ok

    # --- Export/Import ---
    def export_encrypted(self) -> bytes:
        self._require_unlocked()
        export = {
            "meta": {"exported_at": int(time.time()), "app": "GhostShell"},
            "entries": [],
        }
        cur = self._conn.execute("SELECT * FROM entries ORDER BY id ASC")
        for row in cur.fetchall():
            item = dict(row)
            item["password"] = base64.b64encode(item["password"] or b"").decode("ascii")
            export["entries"].append(item)
        blob = json.dumps(export).encode("utf-8")
        token = self._fernet.encrypt(blob) if self._fernet else b""
        log_event("vault", "export", "success", f"entries={len(export['entries'])}")
        return token

    def import_encrypted(self, blob: bytes) -> int:
        self._require_unlocked()
        try:
            data = self._fernet.decrypt(blob) if self._fernet else b"{}"
            payload = json.loads(data.decode("utf-8"))
            entries = payload.get("entries", [])
            with self._conn:
                self._conn.execute("DELETE FROM entries")
                count = 0
                for item in entries:
                    password_blob = base64.b64decode(item.get("password") or "")
                    self._conn.execute(
                        """
                        INSERT INTO entries(id, service, username, password, url, notes, tags, created_at, updated_at)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.get("id"),
                            item.get("service"),
                            item.get("username"),
                            password_blob,
                            item.get("url"),
                            item.get("notes"),
                            item.get("tags"),
                            item.get("created_at"),
                            item.get("updated_at"),
                        ),
                    )
                    count += 1
            self._touch()
            log_event("vault", "import", "success", f"entries={count}")
            return count
        except (InvalidToken, json.JSONDecodeError) as exc:
            log_event("vault", "import", "error", str(exc))
            return 0

    # --- Helpers ---
    def _derive_key(self, pin: str, salt: bytes) -> bytes:
        dk = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, VAULT_KDF_ITERATIONS, dklen=32)
        return base64.urlsafe_b64encode(dk)

    def _touch(self) -> None:
        self._unlocked_until = time.time() + VAULT_AUTOLOCK_SECONDS

    def _require_unlocked(self) -> None:
        self.autolock_check()
        if not self._fernet:
            raise PermissionError("Vault is locked")

    def _get_setting(self, key: str, default: str = "0") -> str:
        cur = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def _set_setting(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings(key,value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def _is_locked_out(self) -> bool:
        lock_until = int(self._get_setting("lock_until", "0"))
        return time.time() < lock_until

    def _increment_fail(self) -> None:
        fails = int(self._get_setting("failed_attempts", "0")) + 1
        if fails >= VAULT_MAX_ATTEMPTS:
            lock_secs = max(1, VAULT_LOCKOUT_MINUTES * 60)
            until = int(time.time() + lock_secs)
            self._set_setting("lock_until", str(until))
            self._set_setting("failed_attempts", "0")
            log_event("vault", "lockout", "engaged", f"until={until}")
        else:
            self._set_setting("failed_attempts", str(fails))

    def _reset_fail(self) -> None:
        self._set_setting("failed_attempts", "0")
        self._set_setting("lock_until", "0")
