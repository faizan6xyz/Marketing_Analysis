import Instagram.upload as aaaa
import X.login as x
import threads.login as thhh
import time
import campaign as campp
import json
import logging
import pinterst.login as pin 
import Drive.dep as dpp
import youtube.login as you
from dotenv import load_dotenv
load_dotenv()
import os
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from psycopg2 import sql
from psycopg2.extras import execute_values
from psycopg2.pool import ThreadedConnectionPool
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_pool = ThreadedConnectionPool( 1, 2, host=os.environ.get("DB_HOST", "localhost"),port=int(os.environ.get("DB_PORT", 5432)), dbname=os.environ.get("DB_NAME", "myapp_db"), user=os.environ.get("DB_USER", "myapp"), password=os.environ.get("DB_PASSWORD"), target_session_attrs="read-write",connect_timeout=3,)

@contextmanager
def get_conn():
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)

def is_valid_iso_format(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False

def _delete_by(cur,table, column, value):
    query = sql.SQL("DELETE FROM {} WHERE {} = %s").format(sql.Identifier(table), sql.Identifier(column))
    cur.execute(query, (value,))

def inert_workflow(cur,id_, message, comment):
    now = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    cur.execute("INSERT INTO workflow ( id , comment , message, time) values (%s,%s,%s,%s) ", (id_, comment, message, now))

def workflow_data_change(cur,id_, message, comment):
    cur.execute("SELECT message, comment FROM workflow WHERE id = %s", (id_,))
    rows = cur.fetchall()
    if rows:
        cur.execute("UPDATE workflow SET message = %s, comment = %s WHERE id = %s", (message, comment, id_))
    else:
        cur.execute("INSERT INTO workflow (id, message, comment) VALUES (%s, %s, %s)", (id_, message, comment))
    return rows

def workflow_10_days(cur,time):
    cur.execute("SELECT id FROM workflow WHERE time < %s", (time,))
    return cur.fetchall()

def delete_by_10days(cur,row_id):
    _delete_by(cur,"workflow", "id", row_id)

def workflow_data(cur,id_):
    cur.execute("SELECT message , comment FROM workflow WHERE id = %s ", (id_,))   # for the whatsapp theres no comment but for the gmail comment is the subject
    return cur.fetchall()

def insert_time(cur,user_id, container_id, scheduled_time, access_token):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    cur.execute("INSERT INTO schedule (user_id, container_id, time, type , access_token) VALUES (%s, %s, %s, %s, %s)", (user_id, container_id, scheduled_time, "container", access_token))

def insert_post(cur,user_id, scheduled_time, access_token, typeee, text1, text2, text3, media_id):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    cur.execute("INSERT INTO schedule (user_id, time, access_token , type , text1 , text2 , text3 ,media_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typeee, text1, text2, text3, media_id))

def insert_posts_bulk(cur,posts):
    rows = [p for p in posts if is_valid_iso_format(p[1])]
    if not rows:
        return
    execute_values( cur,"INSERT INTO schedule (user_id, time, access_token, type, text1, text2, text3, media_id) VALUES %s",rows,)

def insert__story(cur,user_id, scheduled_time, access_token, media_id, hour, typee):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    cur.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id,hour) VALUES (%s, %s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typee, media_id, hour))

def insert__story1(cur,user_id, scheduled_time, access_token, media_id, typee):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    cur.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id) VALUES (%s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typee, media_id))

def get_containers_due(cur,now):
    cur.execute("SELECT id, container_id, access_token, user_id , type,media_id,hour,text1,text2,text3 FROM schedule WHERE time < %s and taken = %s", (now,0))
    return cur.fetchall()
    
def get_failed_one(cur,now):
    tim = datetime.fromisoformat(now) - timedelta(minutes=30)
    cur.execute("SELECT id, container_id, access_token, user_id , type,media_id,hour,text1,text2,text3 FROM schedule WHERE time < %s and taken = %s ", (tim,1))
    return cur.fetchall()

def update_container_schedule(cur,container_id, sctime):
    if not is_valid_iso_format(sctime):
        return
    cur.execute("UPDATE schedule SET time = %s WHERE container_id = %s", (sctime, container_id))

def delete_by_id(cur,row_id):
    _delete_by(cur,"schedule", "id", row_id)

def set_taken_to(cur,ids):
    if not ids:
        return 0
    cur.execute("UPDATE schedule SET taken = 1 WHERE id = ANY(%s)", (list(ids),))
    return cur.rowcount

if __name__ == "__main__":
    while True:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with get_conn() as conn, conn.cursor() as cur:
                due = get_containers_due(cur,now)
                fail = get_failed_one(cur,now)
                seen = {row[0] for row in due}
                due.extend(row for row in fail if row[0] not in seen)    # no row can be processed twice
                set_taken_to(cur,[row[0] for row in due])
        except Exception:
            logging.exception("could not claim due rows")
            time.sleep(5)
            continue
        for row_id, container_id, access_tok, username_id , typess,media_id,hourss,text1,text2,text3 in due:
            try:
                if typess == "container":
                    aaaa.publish_container(user_id=username_id, access_token=access_tok, creation_id=container_id)
                elif typess == "Story_later":
                    access_token = aaaa.access_tokenvali(username_id)
                    if not access_token :
                        continue
                    aaaa.post_story(now,access_token, username_id, 0, media_id, text1, True, text2)
                elif typess == "Photo_later":
                    access_token = aaaa.access_tokenvali(username_id)
                    if not access_token :
                        continue
                    aaaa.post_photo(now,access_token, username_id,media_id, text3, 0, True)
                elif typess in ("Reel_later", "Video_later"):
                    access_token = aaaa.access_tokenvali(username_id)
                    if not access_token :
                        continue
                    cover_url, media_duration = text2.rsplit(",", 1)
                    aaaa.post_video(now,access_token, username_id, media_id, 0 , text3, text1, cover_url,True, media_duration )
                elif typess == "Carousel_later":
                    access_token = aaaa.access_tokenvali(username_id)
                    if not access_token :
                        continue
                    combi = json.loads(text2)
                    if len(combi) % 2 != 0:
                        continue
                    lengthof = len(combi) // 2
                    media_size = combi[:lengthof]
                    media_duration = combi[lengthof:]
                    aaaa.post_carousel(now,access_token, username_id,  media_size, media_duration, media_id, text1, text3, True)
                elif typess == "container1":
                    thhh.publish_threads_container_sc(user_id=username_id, access_token=access_tok, creation_id=container_id)
                elif typess == "carousal_later1":
                    expire  = datetime.now(timezone.utc)
                    access_token = thhh.refresh_threads_token11(expire,access_tok,username_id)
                    comment , message = text1.split(",1234,+++x")
                    thhh.process_threads_carousel(access_token, username_id, media_id, text3, True, now, comment, message, text2)
                elif typess == "photo_later1":
                    expire  = datetime.now(timezone.utc)
                    access_token = thhh.refresh_threads_token11(expire,access_tok,username_id)
                    comment , message = text1.split(",1234,+++x")
                    thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"photo",image_url=media_id )
                elif typess == "text_later1":
                    expire  = datetime.now(timezone.utc)
                    access_token = thhh.refresh_threads_token11(expire,access_tok,username_id)
                    comment , message = text1.split(",1234,+++x")
                    thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"text" )
                elif typess == "video_later1":
                    expire  = datetime.now(timezone.utc)
                    access_token = thhh.refresh_threads_token11(expire,access_tok,username_id)
                    comment , message = text1.split(",1234,+++x")
                    thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"video",video_url=media_id )
                elif typess == "story" :
                    content = aaaa.story_schedule(username_id,hourss,media_id,access_tok)
                    dpp.append_to_file(user_id=username_id, platform="Instagram", filename="reachanalysis.txt", data_to_append=content)
                elif typess in ("photo", "carousel", "video"):
                    content = aaaa.get_media_analytics(username_id,media_id,access_tok)
                    dpp.append_to_file(user_id=username_id, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                elif typess in ("text1", "photo1", "video1", "carousel1"):
                    content = thhh.get_thread_metrics_csv(username_id , media_id, access_tok)
                    dpp.append_to_file(user_id=username_id, platform="Threads", filename="postanalysis.txt", data_to_append=content)
                elif typess == "shorts": 
                    content = you.shorts_schedule(username_id, media_id, access_tok)
                    dpp.append_to_file(user_id=username_id, platform="Youtube", filename="postanalysis.txt", data_to_append=content)
                elif typess in ("tweet", "photo_tweet", "video_tweet"):
                    content = x.get_tweet_metrics(username_id ,media_id, access_tok)
                    dpp.append_to_file(user_id=username_id, platform="X", filename="postanalysis.txt", data_to_append=content)
                elif typess in ("tweet_later", "photo_tweet_later", "video_tweet_later"):
                    x.post_later(typess, username_id,text1 )
                elif typess in ("pin_photo", "pin_video"): 
                    content = pin.get_pinterest_pin_analytics_csv(username_id,media_id, access_tok)
                    dpp.append_to_file(user_id=username_id, platform="Pinterst", filename="postanalysis.txt", data_to_append=content)
                elif typess == "Shorts_later":
                    you.post_later(username_id, media_id, text1, text2, text3) 
                elif typess == "Pin_photo_later":
                    pin.post_late(username_id , access_tok , text1, text2, text3, "photo" ,media_id)
                elif typess == "Pin_video_later":
                    pin.post_late(username_id , access_tok , text1, text2, text3, "video" ,media_id)
                elif typess == "email_later":
                    text1 , reply , subject = text1.split(",1234,+++x")
                    campp.upload_lategmail(username_id , text1, text2, text3,media_id, reply , subject)
                elif typess == "message_later":
                    text1 , reply , subject = text1.split(",1234,+++x")
                    campp.upload_latewhat(username_id , text1, text2, text3, media_id, reply , subject)
                else:
                    logging.warning("row %s has unknown type %r, left in table", row_id, typess)
                    continue
                with get_conn() as conn, conn.cursor() as cur:
                    delete_by_id(cur,row_id)
            except Exception:
                logging.exception("row %s (%s) failed, will be retried after 30 min", row_id, typess)
        try:
            with get_conn() as conn, conn.cursor() as cur:
                rows = workflow_10_days(cur,now)
                for (row_id,) in rows :
                    delete_by_10days(cur,row_id)
        except Exception:
            logging.exception("workflow cleanup failed")
        time.sleep(5)