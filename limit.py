import os
import json
from contextlib import contextmanager
import time
import logging
from datetime import datetime, timedelta, timezone
import database.UserDB as dbimp
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("rate_limit")
from psycopg2 import sql
from database.UserDB import pool_read , pool_write 

def _require(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

LIMIT_OF_PLAN = json.loads(_require("limit_of_plan"))

@contextmanager
def get_conn_write():
    conn = pool_write.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool_write.putconn(conn)

@contextmanager
def get_conn_read():
    conn = pool_read.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool_read.putconn(conn)

# require the pool of the read 
def get_or_create_window(user_id, now_ts):
    with get_conn_read() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (str(user_id),))
            cur.execute( sql.SQL( "SELECT {count}, {id} FROM {t} " "WHERE {start} < %s AND {end} > %s AND {user} = %s").format(sql.Identifier("request_count"), sql.Identifier("id"), sql.Identifier("rate_limit"), sql.Identifier("start_time"), sql.Identifier("end_time"), sql.Identifier("user_id")), (now_ts, now_ts, user_id), )
            row = cur.fetchone()
            if row:
                return row[0], row[1]
            start = datetime.now(timezone.utc)
            end = start + timedelta(days=30)
            cur.execute( sql.SQL( "INSERT INTO {t} ({user}, {start}, {end}, {count}) " "VALUES (%s, %s, %s, %s) RETURNING {id}" ).format(sql.Identifier("rate_limit"), sql.Identifier("user_id"), sql.Identifier("start_time"), sql.Identifier("end_time"), sql.Identifier("request_count"), sql.Identifier("id")), (user_id, start, end, 0), )
            return 0, cur.fetchone()[0]

def try_increment(row_id, amount, limit):
    with get_conn_write() as conn:
        with conn.cursor() as cur:
            cur.execute( sql.SQL( "UPDATE {t} SET {count} = {count} + %s " "WHERE {id} = %s AND {count} + %s <= %s" ).format(sql.Identifier("rate_limit"), sql.Identifier("request_count"), sql.Identifier("id")), (amount, row_id, amount, limit),)
            return cur.rowcount > 0

def checkk(token, user_id, now_ts, requestss: int):
    if requestss < 0:
        return False
    rows = dbimp.select_rows(token, "users", select="Paid,Plan", filters={"user_id": user_id})
    if not rows:
        return False
    if not rows[0]["Paid"]:
        return False
    plan = rows[0]["Plan"]
    limit = LIMIT_OF_PLAN.get(plan)
    if limit is None:
        return False
    _, row_id = get_or_create_window(user_id, now_ts)
    return try_increment(row_id, requestss, int(limit))

def delete_by_time(timee):
    with get_conn_write() as conn:
        with conn.cursor() as cur:
            cur.execute( sql.SQL("DELETE FROM {t} WHERE {end} < %s").format(sql.Identifier("rate_limit"), sql.Identifier("end_time")),(timee,), )

if __name__ == "__main__":
    while True:
        now = datetime.now(timezone.utc).isoformat()
        try:
            delete_by_time(now)
        except Exception:
            logger.exception("rate_limit cleanup failed")
        time.sleep(300)