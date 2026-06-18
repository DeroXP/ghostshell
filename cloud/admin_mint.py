"""Admin CLI for license operations — used during Phase B development
before Stripe is wired up.

This script connects directly to Postgres (via DATABASE_URL from
settings.py / .env) and lets you:

  • mint            — create a fresh license + print the plaintext key
  • list            — show all licenses (status + slot count)
  • show ID         — show a single license + its devices
  • revoke ID       — admin-revoke a license (won't refund money)
  • refund ID       — mark a license as refunded (simulates a Stripe refund)
  • forget-trial FP — delete a trial row by fingerprint hash hex (lets you
                      re-test the trial flow without changing hardware)

Usage examples:
    python admin_mint.py mint --email test@example.com
    python admin_mint.py list
    python admin_mint.py show 1
    python admin_mint.py revoke 1 --reason "test cleanup"
    python admin_mint.py refund 1
    python admin_mint.py forget-trial <hex>

Run this from the cloud/ directory so settings.py finds .env.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import asyncpg

from security import (
    generate_license_key,
    hash_email,
    hash_license_key,
)
from settings import settings


async def _conn() -> asyncpg.Connection:
    return await asyncpg.connect(settings.database_url)


# ─── Commands ────────────────────────────────────────────────────────────
async def cmd_mint(args: argparse.Namespace) -> int:
    """Create a fresh license without going through Stripe."""
    key = generate_license_key()
    key_hash   = hash_license_key(key)
    email_hash = hash_email(args.email) if args.email else None

    db = await _conn()
    try:
        row = await db.fetchrow(
            """INSERT INTO licenses
                  (key_hash, email_hash, status, currency, amount_cents)
               VALUES ($1, $2, 'active', $3, $4)
               RETURNING id, paid_at""",
            key_hash, email_hash, args.currency, args.amount_cents,
        )
    finally:
        await db.close()

    print()
    print(f"  License ID:   {row['id']}")
    print(f"  License key:  {key}")
    print(f"  Email:        {args.email or '(none)'}")
    print(f"  Paid at:      {row['paid_at']}")
    print()
    print("  Save the license key — it cannot be recovered if lost.")
    print("  Paste it into the Vispora client to activate.")
    return 0


async def cmd_list(args: argparse.Namespace) -> int:
    db = await _conn()
    try:
        rows = await db.fetch(
            """SELECT l.id, l.status, l.paid_at, l.refunded_at,
                      l.revoked_at, l.amount_cents, l.currency,
                      COUNT(ld.id) FILTER (WHERE ld.deactivated_at IS NULL) AS slots_used
                 FROM licenses l
            LEFT JOIN license_devices ld ON ld.license_id = l.id
             GROUP BY l.id
             ORDER BY l.id"""
        )
    finally:
        await db.close()

    if not rows:
        print("(no licenses yet)")
        return 0

    print(f"{'id':<5} {'status':<10} {'slots':<7} {'amount':<10} paid_at")
    print("-" * 60)
    for r in rows:
        amt = (f"{(r['amount_cents'] or 0) / 100:.2f} {r['currency'] or '?'}"
               if r['amount_cents'] else "(unset)")
        slots = f"{r['slots_used']}/{settings.license_max_devices}"
        print(f"{r['id']:<5} {r['status']:<10} {slots:<7} {amt:<10} {r['paid_at']}")
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    db = await _conn()
    try:
        lic = await db.fetchrow(
            """SELECT id, status, paid_at, refunded_at, revoked_at,
                      revoke_reason, currency, amount_cents
                 FROM licenses WHERE id = $1""",
            args.license_id,
        )
        if not lic:
            print(f"No license with id={args.license_id}", file=sys.stderr)
            return 1

        devs = await db.fetch(
            """SELECT slot_no, gpu_model_norm, cpu_family, ram_gb_bucket,
                      activated_at, last_seen, deactivated_at
                 FROM license_devices
                WHERE license_id = $1
                ORDER BY slot_no""",
            args.license_id,
        )
    finally:
        await db.close()

    print(f"License {lic['id']}")
    print(f"  status:     {lic['status']}")
    print(f"  paid_at:    {lic['paid_at']}")
    print(f"  refunded:   {lic['refunded_at']}")
    print(f"  revoked:    {lic['revoked_at']} {('(' + lic['revoke_reason'] + ')') if lic['revoke_reason'] else ''}")
    print(f"  amount:     {(lic['amount_cents'] or 0) / 100:.2f} {lic['currency'] or '?'}")
    print()
    if not devs:
        print("  no devices activated")
        return 0
    print(f"  {'slot':<5} {'gpu':<25} {'cpu':<20} {'ram':<6} last_seen")
    print("  " + "-" * 75)
    for d in devs:
        suffix = "" if d["deactivated_at"] is None else " (deactivated)"
        print(f"  {d['slot_no']:<5} "
              f"{(d['gpu_model_norm'] or '-'):<25} "
              f"{(d['cpu_family'] or '-'):<20} "
              f"{(d['ram_gb_bucket'] or '-'):<6} "
              f"{d['last_seen']}{suffix}")
    return 0


async def cmd_revoke(args: argparse.Namespace) -> int:
    db = await _conn()
    try:
        result = await db.execute(
            """UPDATE licenses
                  SET status = 'revoked',
                      revoked_at = NOW(),
                      revoke_reason = $2
                WHERE id = $1 AND status = 'active'""",
            args.license_id, args.reason,
        )
    finally:
        await db.close()
    if result.endswith(" 0"):
        print(f"License {args.license_id} not found or not active",
              file=sys.stderr)
        return 1
    print(f"License {args.license_id} revoked: {args.reason}")
    return 0


async def cmd_refund(args: argparse.Namespace) -> int:
    db = await _conn()
    try:
        result = await db.execute(
            """UPDATE licenses
                  SET status = 'refunded',
                      refunded_at = NOW()
                WHERE id = $1 AND status = 'active'""",
            args.license_id,
        )
    finally:
        await db.close()
    if result.endswith(" 0"):
        print(f"License {args.license_id} not found or not active",
              file=sys.stderr)
        return 1
    print(f"License {args.license_id} marked as refunded")
    return 0


async def cmd_forget_trial(args: argparse.Namespace) -> int:
    """Delete a trial row by its device_fp_hash (hex).  Useful when you
    want to re-test the trial flow on the same hardware without touching
    physical IDs.

    Get the hash hex from the cloud logs (it shows up in trial-start
    error messages) or from the trials table directly.
    """
    try:
        fp_bytes = bytes.fromhex(args.fp_hex)
        if len(fp_bytes) != 32:
            print("fp_hex must decode to exactly 32 bytes", file=sys.stderr)
            return 1
    except ValueError:
        print("fp_hex must be valid hex", file=sys.stderr)
        return 1

    db = await _conn()
    try:
        result = await db.execute(
            "DELETE FROM trials WHERE device_fp_hash = $1", fp_bytes,
        )
    finally:
        await db.close()
    if result.endswith(" 0"):
        print(f"No trial found for fp={args.fp_hex}", file=sys.stderr)
        return 1
    print(f"Trial for fp={args.fp_hex[:12]}... deleted")
    return 0


# ─── Entry point ─────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(
        prog="admin_mint",
        description="GhostShell-Cloud admin CLI (Phase B dev tool, no Stripe)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("mint", help="create a license")
    sp.add_argument("--email", default=None)
    sp.add_argument("--currency", default="USD")
    sp.add_argument("--amount-cents", type=int, default=2000)
    sp.set_defaults(fn=cmd_mint)

    sp = sub.add_parser("list", help="list all licenses")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="show one license + its devices")
    sp.add_argument("license_id", type=int)
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("revoke", help="revoke a license")
    sp.add_argument("license_id", type=int)
    sp.add_argument("--reason", default="manual revocation")
    sp.set_defaults(fn=cmd_revoke)

    sp = sub.add_parser("refund", help="mark a license as refunded")
    sp.add_argument("license_id", type=int)
    sp.set_defaults(fn=cmd_refund)

    sp = sub.add_parser("forget-trial",
                        help="delete a trial row by device_fp_hash hex")
    sp.add_argument("fp_hex", help="64-char hex string")
    sp.set_defaults(fn=cmd_forget_trial)

    args = p.parse_args()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
