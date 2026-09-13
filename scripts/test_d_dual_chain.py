"""Workstream D verify: one fetch prices both chains — OFFLINE.

Copies kolfi.db to a scratch DB, monkeypatches fetch_window_sync with
synthetic RawMessages (solana base58 + robinhood 0x in the SAME messages),
runs run_backfill, and asserts chain partitioning. All call timestamps are
within the last 24h, so the 7d maturity guard defers pricing — the run
proves parse/persist/dedup without consuming API budget.

Run: env PYTHONPATH= .venv/bin/python scripts/test_d_dual_chain.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCRATCH = Path(tempfile.gettempdir()) / "kolfi_d_test.db"
shutil.copy(ROOT / "kolfi.db", SCRATCH)
os.environ["DB_PATH"] = str(SCRATCH)
os.environ["SCHEMA_FILE"] = "schema_unified.sql"
os.environ["PRICING_ENGINE"] = "7d"

import pipeline  # noqa: E402
from models import RawMessage  # noqa: E402

assert str(config_check := pipeline.settings.db_path) == str(SCRATCH), \
    f"settings did not pick up scratch DB: {config_check}"

SOL_ADDR = "3TYgKwkE2Y3rxdw9osLRSpxpXmSC1C1oo19W9KHspump"
RH_ADDR = "0x380b789970dccb4a0d81c818915db25a19b0fc80"
now = datetime.now(timezone.utc).replace(tzinfo=None)

msgs = [
    # Both chains in ONE message (666-style stock-pairing calls).
    RawMessage(channel_id=999001, message_id=1,
               text=f"new token pairs {SOL_ADDR} and also 0x-deep "
                    f"$brainrot {RH_ADDR}",
               timestamp=now - timedelta(hours=6)),
    # Solana-only message.
    RawMessage(channel_id=999001, message_id=2,
               text=f"$MOON call {SOL_ADDR[:-4]}Abcd",
               timestamp=now - timedelta(hours=5)),
    # Robinhood-only message + a bot-update duplicate that dedup must remove.
    RawMessage(channel_id=999001, message_id=3,
               text=f"$turret {RH_ADDR}",
               timestamp=now - timedelta(hours=4)),
    RawMessage(channel_id=999001, message_id=4,
               text=f"$turret still pumping {RH_ADDR}",
               timestamp=now - timedelta(hours=3)),
]


def fake_fetch(channel_ref, window_start, window_end, limit=None, progress_cb=None):
    if progress_cb:
        progress_cb(len(msgs))
    return (msgs, "D-test channel")


def main() -> int:
    import ingestion.telethon_fetcher as tf
    tf.fetch_window_sync = fake_fetch
    orig_import = pipeline._import_fetch_window_sync
    pipeline._import_fetch_window_sync = lambda: fake_fetch

    try:
        prog = pipeline.run_backfill(
            "@dtest_channel",
            window_start=now - timedelta(days=7),
            window_end=now,
            title="D-test channel",
            username="dtest_channel",
        )
    finally:
        pipeline._import_fetch_window_sync = orig_import

    conn = sqlite3.connect(SCRATCH)
    conn.row_factory = sqlite3.Row
    ch = conn.execute("SELECT id FROM channels WHERE username='dtest_channel'").fetchone()
    assert ch, "channel not created"
    rows = conn.execute(
        "SELECT chain, token_address, status, pending_reason, message_id "
        "FROM calls WHERE channel_id=? ORDER BY chain, call_timestamp",
        (ch["id"],),
    ).fetchall()
    by_chain = {}
    for r in rows:
        by_chain.setdefault(r["chain"], []).append(dict(r))

    print(f"progress: found={prog.found} priced={prog.priced} unpriceable={prog.unpriceable}")
    for chain, rs in sorted(by_chain.items()):
        print(f"  {chain}: {len(rs)} rows")
        for r in rs:
            print(f"    msg{r['message_id']} {r['token_address'][:16]}… "
                  f"{r['status']}/{r['pending_reason']}")

    ok = True
    sol = by_chain.get("sol", [])
    rh = by_chain.get("robinhood", [])
    # Expect 2 sol (msg1 addr + msg2 different addr), 1 rh (msg3 deduped into msg1's addr)
    if not sol:
        print("FAIL: no sol rows"); ok = False
    if not rh:
        print("FAIL: no robinhood rows"); ok = False
    if any(not r["token_address"].startswith("0x") for r in rh):
        print("FAIL: non-0x address in robinhood rows"); ok = False
    if any(r["token_address"].startswith("0x") for r in sol):
        print("FAIL: 0x address in sol rows"); ok = False
    # Duplicate RH call (msg4 same addr) must be deduped away: only 1 rh row.
    if len(rh) != 1:
        print(f"FAIL: expected 1 robinhood row after dedup, got {len(rh)}"); ok = False
    # All recent calls must be deferred, zero API pricing happened.
    if prog.priced or prog.unpriceable:
        print("FAIL: pricing API calls happened (expected all immature)"); ok = False
    if not all(r["status"] == "pending" for r in sol + rh):
        print("FAIL: not all rows pending"); ok = False
    # Ingestion runs: one row per chain produced calls.
    runs = conn.execute(
        "SELECT chain, calls_found FROM ingestion_runs WHERE channel_id=?",
        (ch["id"],),
    ).fetchall()
    print("ingestion_runs:", {r["chain"]: r["calls_found"] for r in runs})
    if {r["chain"] for r in runs} != {"sol", "robinhood"}:
        print("FAIL: ingestion_runs not split per chain"); ok = False

    print("PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    rc = main()
    SCRATCH.unlink(missing_ok=True)
    sys.exit(rc)
