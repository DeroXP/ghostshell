"""Encrypted password vault with PIN protection."""
import os
import json
import time
import hashlib
import hmac
import base64
import secrets
import string
import sqlite3
from datetime import datetime
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from config import VAULT_DB_PATH, VAULT_PBKDF2_ITERATIONS, VAULT_MAX_PIN_ATTEMPTS, VAULT_LOCKOUT_SECONDS, VAULT_AUTO_LOCK_SECONDS, APPDATA_DIR
from core.utils import get_logger

log = get_logger("vault")

# ═══════════════════════════════════════════════════════════════════════════
# Vault state — kept in memory, never persisted
# ═══════════════════════════════════════════════════════════════════════════
_vault_state = {
    "unlocked": False,
    "fernet": None,  # Fernet instance for this session
    "last_activity": 0,
    "failed_attempts": 0,
    "lockout_until": 0,
}


def _derive_key(pin: str, salt: bytes) -> bytes:
    """Derive a Fernet key from PIN + salt via PBKDF2."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=VAULT_PBKDF2_ITERATIONS,
    )
    key = base64.urlsafe_b64encode(kdf.derive(pin.encode("utf-8")))
    return key


def _hash_pin(pin: str, salt: bytes) -> str:
    """Hash the PIN for verification (separate from encryption key derivation)."""
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, VAULT_PBKDF2_ITERATIONS).hex()


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(VAULT_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(VAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    """Create tables if they don't exist."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vault_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS vault_entries (
            id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


_init_db()


# ═══════════════════════════════════════════════════════════════════════════
# PIN Management
# ═══════════════════════════════════════════════════════════════════════════
def is_pin_set() -> bool:
    """Check if a PIN has been configured."""
    conn = _get_db()
    row = conn.execute("SELECT value FROM vault_meta WHERE key = 'pin_hash'").fetchone()
    conn.close()
    return row is not None


def setup_pin(pin: str) -> dict:
    """First-time PIN setup. Creates encryption key."""
    if len(pin) < 4 or len(pin) > 8 or not pin.isdigit():
        return {"ok": False, "err": "PIN must be 4-8 digits"}

    if is_pin_set():
        return {"ok": False, "err": "PIN already configured. Reset vault to change."}

    # Generate salts
    pin_salt = secrets.token_bytes(32)
    key_salt = secrets.token_bytes(32)

    # Hash PIN for verification
    pin_hash = _hash_pin(pin, pin_salt)

    # Derive encryption key
    enc_key = _derive_key(pin, key_salt)

    # v3 — write all three rows in ONE transaction.  Without this, a
    # crash between the first INSERT (pin_hash) and the third (key_salt)
    # leaves `is_pin_set()` returning True but `unlock()` exploding on
    # the missing key_salt — vault becomes unrecoverable without a reset.
    conn = _get_db()
    try:
        with conn:    # sqlite3 contextmanager = BEGIN / COMMIT (or ROLLBACK on raise)
            conn.execute("INSERT OR REPLACE INTO vault_meta (key, value) VALUES (?, ?)",
                         ("pin_hash", pin_hash))
            conn.execute("INSERT OR REPLACE INTO vault_meta (key, value) VALUES (?, ?)",
                         ("pin_salt", base64.b64encode(pin_salt).decode()))
            conn.execute("INSERT OR REPLACE INTO vault_meta (key, value) VALUES (?, ?)",
                         ("key_salt", base64.b64encode(key_salt).decode()))
    finally:
        conn.close()

    # Unlock immediately
    _vault_state["unlocked"] = True
    _vault_state["fernet"] = Fernet(enc_key)
    _vault_state["last_activity"] = time.time()
    _vault_state["failed_attempts"] = 0

    log.info("Vault PIN configured and vault unlocked")
    return {"ok": True}


def unlock(pin: str) -> dict:
    """Unlock the vault with PIN."""
    now = time.time()

    # Check lockout
    if _vault_state["lockout_until"] > now:
        remaining = int(_vault_state["lockout_until"] - now)
        return {"ok": False, "err": f"Vault locked. Try again in {remaining}s", "locked_out": True}

    if not is_pin_set():
        return {"ok": False, "err": "No PIN set. Configure vault first."}

    conn = _get_db()
    pin_hash_stored = conn.execute("SELECT value FROM vault_meta WHERE key = 'pin_hash'").fetchone()["value"]
    pin_salt = base64.b64decode(conn.execute("SELECT value FROM vault_meta WHERE key = 'pin_salt'").fetchone()["value"])
    key_salt = base64.b64decode(conn.execute("SELECT value FROM vault_meta WHERE key = 'key_salt'").fetchone()["value"])
    conn.close()

    # Verify PIN — constant-time compare even though local attack
    # surface is limited, this is cheap and removes timing-attack class
    pin_hash_attempt = _hash_pin(pin, pin_salt)
    if not hmac.compare_digest(pin_hash_attempt, pin_hash_stored):
        _vault_state["failed_attempts"] += 1
        remaining = VAULT_MAX_PIN_ATTEMPTS - _vault_state["failed_attempts"]
        if _vault_state["failed_attempts"] >= VAULT_MAX_PIN_ATTEMPTS:
            _vault_state["lockout_until"] = now + VAULT_LOCKOUT_SECONDS
            _vault_state["failed_attempts"] = 0
            log.warning(f"Vault locked out for {VAULT_LOCKOUT_SECONDS}s after too many attempts")
            return {"ok": False, "err": f"Too many attempts. Locked for {VAULT_LOCKOUT_SECONDS // 60} minutes.", "locked_out": True}
        log.warning(f"Wrong PIN. {remaining} attempts remaining")
        return {"ok": False, "err": f"Wrong PIN. {remaining} attempts remaining."}

    # PIN correct — derive encryption key
    enc_key = _derive_key(pin, key_salt)
    _vault_state["unlocked"] = True
    _vault_state["fernet"] = Fernet(enc_key)
    _vault_state["last_activity"] = time.time()
    _vault_state["failed_attempts"] = 0

    log.info("Vault unlocked")
    return {"ok": True}


def lock():
    """Lock the vault."""
    _vault_state["unlocked"] = False
    _vault_state["fernet"] = None
    log.info("Vault locked")


def is_unlocked() -> bool:
    """Check if vault is unlocked (including auto-lock timeout)."""
    if not _vault_state["unlocked"]:
        return False
    # Auto-lock check
    if time.time() - _vault_state["last_activity"] > VAULT_AUTO_LOCK_SECONDS:
        lock()
        return False
    return True


def _touch():
    """Update last activity timestamp."""
    _vault_state["last_activity"] = time.time()


def _encrypt(data: str) -> str:
    return _vault_state["fernet"].encrypt(data.encode("utf-8")).decode("utf-8")


def _decrypt(data: str) -> str:
    return _vault_state["fernet"].decrypt(data.encode("utf-8")).decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# CRUD Operations
# ═══════════════════════════════════════════════════════════════════════════
def add_entry(service: str, username: str, password: str,
              url: str = "", notes: str = "", category: str = "General") -> dict:
    """Add a new vault entry."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    entry_id = secrets.token_hex(8)
    entry_data = json.dumps({
        "service": service,
        "username": username,
        "password": password,
        "url": url,
        "notes": notes,
        "category": category,
    })
    encrypted = _encrypt(entry_data)
    now = datetime.now().isoformat()

    conn = _get_db()
    conn.execute(
        "INSERT INTO vault_entries (id, data, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (entry_id, encrypted, now, now)
    )
    conn.commit()
    conn.close()

    log.info(f"Vault entry added: {service}")
    return {"ok": True, "id": entry_id}


def get_entries(search: str = "", category: str = "") -> dict:
    """Get all vault entries (decrypted)."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked", "entries": []}
    _touch()

    conn = _get_db()
    rows = conn.execute("SELECT id, data, created_at, updated_at FROM vault_entries ORDER BY updated_at DESC").fetchall()
    conn.close()

    entries = []
    had_decrypt_errors = False
    for row in rows:
        try:
            decrypted = _decrypt(row["data"])
            entry = json.loads(decrypted)
            entry["id"] = row["id"]
            entry["created_at"] = row["created_at"]
            entry["updated_at"] = row["updated_at"]

            # Filter by search
            if search:
                s = search.lower()
                if s not in entry["service"].lower() and s not in entry["username"].lower():
                    continue
            if category and entry.get("category", "") != category:
                continue

            # Mask password for listing (frontend can request full)
            entry["password_masked"] = "•" * min(len(entry["password"]), 12)
            entries.append(entry)
        except (InvalidToken, json.JSONDecodeError) as e:
            log.warning(f"Failed to decrypt entry {row['id']}: {e}")
            had_decrypt_errors = True
            continue

    # had_decrypt_errors lets the caller tell "vault is empty" apart from
    # "some/all entries failed to decrypt" (e.g. unlocked with a PIN that
    # doesn't match the key these rows were encrypted under) — without it,
    # {entries: [], count: 0} looks identical in both cases.
    return {"ok": True, "entries": entries, "count": len(entries),
            "had_decrypt_errors": had_decrypt_errors}


def get_entry_password(entry_id: str) -> dict:
    """Get the actual password for a specific entry (for copy-to-clipboard)."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    conn = _get_db()
    row = conn.execute("SELECT data FROM vault_entries WHERE id = ?", (entry_id,)).fetchone()
    conn.close()

    if not row:
        return {"ok": False, "err": "Entry not found"}

    try:
        decrypted = json.loads(_decrypt(row["data"]))
        return {"ok": True, "password": decrypted["password"]}
    except Exception:
        return {"ok": False, "err": "Decryption failed"}


def update_entry(entry_id: str, **kwargs) -> dict:
    """Update a vault entry. Pass only the fields to update."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    conn = _get_db()
    row = conn.execute("SELECT data FROM vault_entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "err": "Entry not found"}

    try:
        entry = json.loads(_decrypt(row["data"]))
    except Exception:
        conn.close()
        return {"ok": False, "err": "Decryption failed"}

    # Update fields
    for key in ("service", "username", "password", "url", "notes", "category"):
        if key in kwargs and kwargs[key] is not None:
            entry[key] = kwargs[key]

    encrypted = _encrypt(json.dumps(entry))
    now = datetime.now().isoformat()
    conn.execute("UPDATE vault_entries SET data = ?, updated_at = ? WHERE id = ?",
                 (encrypted, now, entry_id))
    conn.commit()
    conn.close()

    log.info(f"Vault entry updated: {entry.get('service', entry_id)}")
    return {"ok": True}


def delete_entry(entry_id: str) -> dict:
    """Delete a vault entry."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    conn = _get_db()
    conn.execute("DELETE FROM vault_entries WHERE id = ?", (entry_id,))
    conn.commit()
    conn.close()

    log.info(f"Vault entry deleted: {entry_id}")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════
# Password Generator
# ═══════════════════════════════════════════════════════════════════════════
def generate_password(length: int = 20, uppercase: bool = True, lowercase: bool = True,
                      digits: bool = True, symbols: bool = True,
                      exclude_ambiguous: bool = False) -> str:
    """Generate a cryptographically secure random password."""
    chars = ""
    if uppercase:
        chars += string.ascii_uppercase
    if lowercase:
        chars += string.ascii_lowercase
    if digits:
        chars += string.digits
    if symbols:
        chars += "!@#$%^&*()-_=+[]{}|;:,.<>?"

    if exclude_ambiguous:
        ambiguous = "0O1lI|"
        chars = "".join(c for c in chars if c not in ambiguous)

    if not chars:
        chars = string.ascii_letters + string.digits

    length = max(8, min(128, length))

    # Ensure at least one char from each selected category
    password = []
    if uppercase:
        pool = string.ascii_uppercase
        if exclude_ambiguous:
            pool = "".join(c for c in pool if c not in "OI")
        password.append(secrets.choice(pool))
    if lowercase:
        pool = string.ascii_lowercase
        if exclude_ambiguous:
            pool = "".join(c for c in pool if c not in "l")
        password.append(secrets.choice(pool))
    if digits:
        pool = string.digits
        if exclude_ambiguous:
            pool = "".join(c for c in pool if c not in "01")
        password.append(secrets.choice(pool))
    if symbols:
        password.append(secrets.choice("!@#$%^&*()-_=+[]{}|;:,.<>?"))

    # Fill remaining length
    while len(password) < length:
        password.append(secrets.choice(chars))

    # Shuffle
    pw_list = list(password)
    for i in range(len(pw_list) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        pw_list[i], pw_list[j] = pw_list[j], pw_list[i]

    return "".join(pw_list)


# ═══════════════════════════════════════════════════════════════════════════
# Export / Import
# ═══════════════════════════════════════════════════════════════════════════
def export_vault(export_pin: str) -> dict:
    """Export all entries as an encrypted file."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    entries_result = get_entries()
    if not entries_result["ok"]:
        return entries_result

    # Re-encrypt with export PIN
    export_salt = secrets.token_bytes(32)
    export_key = _derive_key(export_pin, export_salt)
    export_fernet = Fernet(export_key)

    payload = json.dumps(entries_result["entries"])
    encrypted = export_fernet.encrypt(payload.encode("utf-8"))

    export_data = {
        "version": 1,
        "salt": base64.b64encode(export_salt).decode(),
        "data": encrypted.decode("utf-8"),
        "count": len(entries_result["entries"]),
        "exported_at": datetime.now().isoformat(),
    }

    export_path = os.path.join(APPDATA_DIR, f"vault_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.gsvault")
    with open(export_path, "w") as f:
        json.dump(export_data, f)

    log.info(f"Vault exported: {export_path}")
    return {"ok": True, "path": export_path, "count": len(entries_result["entries"])}


def import_vault(file_path: str, import_pin: str) -> dict:
    """Import entries from an encrypted export file."""
    if not is_unlocked():
        return {"ok": False, "err": "Vault is locked"}
    _touch()

    try:
        with open(file_path, "r") as f:
            export_data = json.load(f)
    except Exception as e:
        return {"ok": False, "err": f"Failed to read file: {e}"}

    export_salt = base64.b64decode(export_data["salt"])
    export_key = _derive_key(import_pin, export_salt)
    export_fernet = Fernet(export_key)

    try:
        decrypted = export_fernet.decrypt(export_data["data"].encode("utf-8"))
        entries = json.loads(decrypted)
    except InvalidToken:
        return {"ok": False, "err": "Wrong PIN for this export file"}
    except Exception as e:
        return {"ok": False, "err": f"Decryption failed: {e}"}

    imported = 0
    for entry in entries:
        result = add_entry(
            service=entry.get("service", ""),
            username=entry.get("username", ""),
            password=entry.get("password", ""),
            url=entry.get("url", ""),
            notes=entry.get("notes", ""),
            category=entry.get("category", "General"),
        )
        if result["ok"]:
            imported += 1

    log.info(f"Imported {imported} entries from vault export")
    return {"ok": True, "imported": imported}


def reset_vault() -> dict:
    """Completely reset the vault — destroys all data."""
    lock()
    conn = _get_db()
    conn.execute("DELETE FROM vault_entries")
    conn.execute("DELETE FROM vault_meta")
    conn.commit()
    conn.close()
    log.info("Vault reset — all data destroyed")
    return {"ok": True}
