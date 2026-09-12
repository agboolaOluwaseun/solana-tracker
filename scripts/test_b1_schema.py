"""B1 verification: unified schema init + pipeline stoploss upsert shape works."""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DB_PATH"] = "/tmp/k2.db"
os.environ["SCHEMA_FILE"] = "schema_unified.sql"
if os.path.exists("/tmp/k2.db"):
    os.remove("/tmp/k2.db")

from db import get_connection, init_db  # noqa: E402

init_db()
conn = get_connection()
conn.execute(
    "INSERT INTO channels (telegram_channel_id,username,title,window_start,window_end)"
    " VALUES (1,'x666calls','666','2026-04-01','2026-09-01')"
)
conn.execute(
    "INSERT INTO calls (id,channel_id,chain,message_id,token_address,call_timestamp,status)"
    " VALUES (1,1,'sol',100,'So11111111111111111111111111111111111111112',"
    "'2026-04-01 10:00:00','priced')"
)

# Exact shape used by pipeline.persist_stoploss_result:
ins = (
    "INSERT INTO stoploss_results (call_id,entry_price_usd,peak_price_usd,status,computed_at)"
    " VALUES (?,?,?,?,?)"
    " ON CONFLICT(call_id) DO UPDATE SET peak_price_usd=excluded.peak_price_usd,"
    " status=excluded.status"
)
conn.execute(ins, (1, 0.01, 0.015, "pending", "2026-04-01 10:00:00"))
conn.execute(ins, (1, 0.01, 0.099, "WON", "2026-04-01 10:00:00"))

cur = conn.execute(
    "SELECT status, peak_price_usd FROM stoploss_results WHERE call_id=1"
)
vals = dict(cur.fetchone())
print("row:", vals)
n = conn.execute("SELECT COUNT(*) AS n FROM stoploss_results").fetchone()["n"]
print("stoploss row count after insert+upsert:", n)
assert vals["status"] == "WON" and vals["peak_price_usd"] == 0.099 and n == 1
print("B1 VERIFIED: schema init OK; pipeline upsert shape into stoploss_results OK")
