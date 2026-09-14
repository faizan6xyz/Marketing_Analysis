import sqlite3
import os
import json
import time
import logging
from datetime import datetime, timedelta, timezone
import database.UserDB as dbimp
logger = logging.getLogger("rate_limit")
DB = "limit.db"

def _require(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

LIMIT_OF_PLAN = json.loads(_require("limit_of_plan"))

def get_conn():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read/write
    conn.execute("PRAGMA busy_timeout=10000")  # wait instead of erroring on lock contention
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    try:
        conn.execute(""" CREATE TABLE IF NOT EXISTS rate_limit ( id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, Start_time TEXT NOT NULL, End_time TEXT NOT NULL, request_count INTEGER NOT NULL DEFAULT 0 ) """)
        conn.execute(""" CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit (user_id, Start_time, End_time)  """)
        conn.commit()
    finally:
        conn.close()

def get_or_create_window(user_id, now_ts):
    conn = get_conn()
    try:
        cur = conn.execute( """SELECT request_count, id FROM rate_limit WHERE Start_time < ? AND End_time > ? AND user_id = ? """,(now_ts, now_ts, user_id), )
        row = cur.fetchone()
        if row:
            return row["request_count"], row["id"]
        start = datetime.now(timezone.utc).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        cur = conn.execute( "INSERT INTO rate_limit (user_id, Start_time, End_time, request_count, ) VALUES (?,?,?,?,?)", (user_id, start, end, 0),)
        conn.commit()
        return 0, cur.lastrowid
    finally:
        conn.close()

def try_increment(row_id, amount, limit):
    conn = get_conn()
    try:
        cur = conn.execute( "UPDATE rate_limit SET request_count = request_count + ? " "WHERE id = ? AND request_count + ? <= ?", (amount, row_id, amount, limit),)
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def checkk(token, user_id, now_ts, requestss: int) :
    rows = dbimp.select_rows(token, "users", select="Paid,Plan", filters={"user_id": user_id})
    if not rows:
        return False
    if not rows[0]["Paid"]:
        return False
    plan = rows[0]["Plan"]
    limit = LIMIT_OF_PLAN.get(plan)
    if limit is None:
        logger.warning("No rate limit configured for plan %r", plan)
        return False
    _, row_id = get_or_create_window(user_id, now_ts)
    return try_increment(row_id, requestss, int(limit))

def delete_by_time(timee):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM rate_limit WHERE End_time < ?", (timee,))
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
    while True:
        now = datetime.now(timezone.utc).isoformat()
        try:
            delete_by_time(now)
        except Exception:
            logger.exception("rate_limit cleanup failed")
        time.sleep(300)