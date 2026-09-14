import sqlite3 
import database.UserDB as dbimp
DB = "limit.db"

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    conn = get_conn()
    conn.execute(""" CREATE TABLE IF NOT EXISTS rate_limit ( user_id TEXT PRIMARY KEY , Start_time TEXT , End_time TEXT , request_count INTEGER , platform TEXT ) """)
    conn.commit()
    conn.close()

def check_data(user_id,time,platform):  # time is send by the server not the client so no validation of the time required
    conn = get_conn()
    cur = conn.execute( """SELECT request_count FROM rate_limit WHERE start_time < ? AND end_time > ? AND user_id = ? and platform = ? """,(time,time,user_id,platform))
    rows = cur.fetchall()
    conn.close()
    return rows

def checkk(token,user_id,tablename,time,platform):
    rows  = dbimp.select_rows(token,tablename , select="Payment_check",filters={"user_id":user_id})
    if not rows :
        False
    payment = rows[0]["Payment_check"]
    pay = str(payment).strip().lower() == "true"
    row = check_data(user_id,time,platform)
    counttt = row[0]