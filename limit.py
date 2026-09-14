import sqlite3 
from datetime import datetime, timedelta , timezone
import database.UserDB as dbimp
import os
import json
import time
DB = "limit.db"

def _require(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

limit_count = json.loads(_require("limit_of_plan"))

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    conn = get_conn()
    conn.execute(""" CREATE TABLE IF NOT EXISTS rate_limit ( user_id TEXT PRIMARY KEY , Start_time TEXT , End_time TEXT , request_count INTEGER , platform TEXT ) """)
    conn.commit()
    conn.close()

def check_data(user_id, time, platform):
    conn = get_conn()
    try:
        cur = conn.execute("""SELECT request_count FROM rate_limit WHERE start_time < ? AND end_time > ? AND user_id = ? AND platform = ?""",(time, time, user_id, platform), )
        rows = cur.fetchall()
        if not rows:
            start = datetime.now(timezone.utc).isoformat()
            end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            conn.execute( "INSERT INTO rate_limit (user_id, Start_time, End_time, request_count, platform) VALUES (?,?,?,?,?)", (user_id, start, end, 0, platform),)
            conn.commit()
            return [(0,)]
        return rows
    finally:
        conn.close()

def checkk(token,user_id,time,platform, requestss : int):
    rows  = dbimp.select_rows(token,"users" , select="Paid,Plan",filters={"user_id":user_id}) 
    if not rows :
        False
    Paid = rows[0]["Paid"]
    Plan = rows[0]["Plan"]
    if Paid :
        row = check_data(user_id,time,platform)
        counttt = row[0]
        ccccc = int(limit_count[Plan])
        if ccccc > counttt + requestss :
            return True 
        else :
            return False
    return False

def delete_by_time(timee):
    conn = get_conn()
    conn.execute("delete from rate_limit where End_time < ?", (timee,))
    conn.commit()
    conn.close()

if __name__ == "__main__":
    while True:
        now = datetime.now(timezone.utc).isoformat()
        delete_by_time(now)
        time.sleep(300)  