from datetime import datetime, timezone, timedelta
import time
import Instagram.upload as aaaa
import sqlite3
import Drive.dep as dpp

DB = "schedule.db"

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    conn = get_conn()
    conn.execute(""" CREATE TABLE IF NOT EXISTS schedule (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT ,
                    time TEXT ,
                    type TEXT ,
                    container_id TEXT ,
                    access_token TEXT ,
                     media_id TEXT ,
                     hour INTEGER ,
                     token TEXT) """)
    conn.commit()
    conn.close()

def insert_time(user_id, container_id, scheduled_time, access_token):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    conn.execute("INSERT INTO schedule (user_id, container_id, time, type , access_token) VALUES (?, ?, ?, ?, ?)",(user_id, container_id, scheduled_time,"container",access_token))
    conn.commit()
    conn.close()

def insert__story(user_id,  scheduled_time, access_token,media_id,hour,token,typee):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    conn.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id,hour,token) VALUES (?, ?, ?, ?,?,?,?,?)",(user_id,  scheduled_time, access_token,typee,media_id,hour,token))
    conn.commit()
    conn.close()

def insert__story1(user_id,  scheduled_time, access_token,media_id,token,typee):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    conn.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id,token) VALUES (?, ?, ?, ?,?,?,?,?)",(user_id,  scheduled_time, access_token,typee,media_id,token))
    conn.commit()
    conn.close()

def get_containers_due(now):
    conn = get_conn()
    cur = conn.execute("SELECT id, container_id, access_token, user_id , type,media_id,hour,token FROM schedule WHERE time < ?", (now,))
    rows = cur.fetchall()
    conn.close()
    return rows

def update_container_schedule(container_id, sctime):
    conn = get_conn()
    conn.execute("UPDATE schedule SET time = ? WHERE container_id = ?", (sctime, container_id))
    conn.commit()
    conn.close()

def delete_by_id(row_id):
    conn = get_conn()
    conn.execute("DELETE FROM schedule WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

init_db()
# timmm = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
# insert_time("sdidfbdzdf", "fgfgfd", timmm, "dsajhbdhf")

if __name__ == "__main__":
    while True:
        now = datetime.now(timezone.utc).isoformat()
        due = get_containers_due(now)
        for row_id, container_id, access_tok, user_id , typess,media_id,hourss,token in due:
            if typess == "container":
                aaaa.publish_container(user_id=user_id, access_token=access_tok, creation_id=container_id)
                print(row_id,container_id,access_tok,user_id)
                delete_by_id(row_id)
            if typess == "story" :
                content = aaaa.story_schedule(hourss,media_id,access_tok)
                dpp.append_to_file(token=token, platform="Instagram", filename="reachanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "photo1":
                content = aaaa.get_media_analytics(media_id,access_tok)
                dpp.append_to_file(token=token, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "carousel1":
                content = aaaa.get_media_analytics(media_id,access_tok)
                dpp.append_to_file(token=token, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "video1":
                content = aaaa.get_media_analytics(media_id,access_tok)
                dpp.append_to_file(token=token, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
        time.sleep(1)


# add repeat of addition of scheledule of post1 type when every it deletes