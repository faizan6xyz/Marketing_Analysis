import Instagram.upload as aaaa
import X.login as x
import threads.login as thhh
import campaign as campp
import json
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
_pool = ThreadedConnectionPool( 1, 10, host=os.environ.get("DB_HOST", "localhost"),port=int(os.environ.get("DB_PORT", 5432)), dbname=os.environ.get("DB_NAME", "myapp_db"), user=os.environ.get("DB_USER", "myapp"), password=os.environ.get("DB_PASSWORD"),)

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

def close_pool():
    _pool.closeall()

def is_valid_iso_format(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False

def _delete_by(table: str, column: str, value):
    query = sql.SQL("DELETE FROM {} WHERE {} = %s").format(sql.Identifier(table), sql.Identifier(column))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, (value,))

def init_db():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(""" CREATE TABLE IF NOT EXISTS schedule ( id SERIAL PRIMARY KEY, user_id TEXT , time TEXT , type TEXT , container_id TEXT , access_token TEXT , media_id TEXT , hour INTEGER , text1 TEXT , text2 TEXT , text3 TEXT ) """)
        cur.execute(""" CREATE TABLE IF NOT EXISTS workflow ( id TEXT PRIMARY KEY , time TEXT , message TEXT , comment TEXT ) """)

def inert_workflow(id_, message, comment):
    now = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO workflow ( id , comment , message, time) values (%s,%s,%s,%s) ", (id_, comment, message, now))

def workflow_data_change(id_, message, comment):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT message, comment FROM workflow WHERE id = %s", (id_,))
        rows = cur.fetchall()
        if rows:
            cur.execute("UPDATE workflow SET message = %s, comment = %s WHERE id = %s", (message, comment, id_))
        else:
            cur.execute("INSERT INTO workflow (id, message, comment) VALUES (%s, %s, %s)", (id_, message, comment))
        return rows

def workflow_10_days(time):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM workflow WHERE time < %s", (time,))
        return cur.fetchall()

def delete_by_10days(row_id):
    _delete_by("workflow", "id", row_id)

def workflow_data(id_):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT message , comment FROM workflow WHERE id = %s ", (id_,))   # for the whatsapp theres no comment but for the gmail comment is the subject
        return cur.fetchall()

def insert_time(user_id, container_id, scheduled_time, access_token):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (user_id, container_id, time, type , access_token) VALUES (%s, %s, %s, %s, %s)", (user_id, container_id, scheduled_time, "container", access_token))


def insert_post(user_id, scheduled_time, access_token, typeee, text1, text2, text3, media_id):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (user_id, time, access_token , type , text1 , text2 , text3 ,media_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typeee, text1, text2, text3, media_id))

def insert_posts_bulk(posts):
    rows = [p for p in posts if is_valid_iso_format(p[1])]
    if not rows:
        return
    with get_conn() as conn, conn.cursor() as cur:
        execute_values( cur,"INSERT INTO schedule (user_id, time, access_token, type, text1, text2, text3, media_id) VALUES %s",rows,)

def insert__story(user_id, scheduled_time, access_token, media_id, hour, typee):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id,hour) VALUES (%s, %s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typee, media_id, hour))

def insert__story1(user_id, scheduled_time, access_token, media_id, typee):    # time should be give in the isoformat iniitally as argument
    if not is_valid_iso_format(scheduled_time):
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id) VALUES (%s, %s, %s, %s, %s)", (user_id, scheduled_time, access_token, typee, media_id))

def get_containers_due(now):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, container_id, access_token, user_id , type,media_id,hour,text1,text2,text3 FROM schedule WHERE time < %s", (now,))
        return cur.fetchall()

def update_container_schedule(container_id, sctime):
    if not is_valid_iso_format(sctime):
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE schedule SET time = %s WHERE container_id = %s", (sctime, container_id))

def delete_by_id(row_id):
    _delete_by("schedule", "id", row_id)

init_db()

if __name__ == "__main__":
    while True:
        now = datetime.now(timezone.utc).isoformat()
        due = get_containers_due(now)
        for row_id, container_id, access_tok, username_id , typess,media_id,hourss,text1,text2,text3 in due:
            if typess == "container":
                aaaa.publish_container(user_id=username_id, access_token=access_tok, creation_id=container_id)
                delete_by_id(row_id)
            if typess == "Story_later":
                access_token = aaaa.access_tokenvali(username_id)
                if not access_token :
                    continue
                aaaa.post_story(now,access_token, username_id, 0, media_id, text1, True, text2)
                delete_by_id(row_id)
            if typess == "Photo_later":
                access_token = aaaa.access_tokenvali(username_id)
                if not access_token :
                    continue
                comment , message = access_tok.split(",1234,+++x")
                aaaa.post_photo(now,access_token, username_id,media_id, text3, 0, True)
                delete_by_id(row_id)
            if typess in ("Reel_later", "Video_later"):
                access_token = aaaa.access_tokenvali(username_id)
                if not access_token :
                    continue
                cover_url, media_duration = text2.split(",")
                comment , message = access_tok.split(",1234,+++x")
                aaaa.post_video(now,access_token, username_id, media_id, 0 , text3, text1, cover_url,True, media_duration )
                delete_by_id(row_id)
            if typess == "Carousel_later":
                access_token = aaaa.access_tokenvali(username_id)
                if not access_token :
                    continue
                comment , message = access_tok.split(",1234,+++x")
                combi = json.loads(text2)
                if len(combi) % 2 != 0:
                    continue
                lengthof = len(combi) // 2
                media_size = combi[:lengthof]
                media_duration = combi[lengthof:]
                aaaa.post_carousel(now,access_token, username_id,  media_size, media_duration, media_id, text1, text3, True)
                delete_by_id(row_id)
            if typess == "container1":
                thhh.publish_threads_container_sc(user_id=username_id, access_token=access_tok, creation_id=container_id)
                delete_by_id(row_id)
            if typess == "carousal_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",1234,+++x")
                thhh.process_threads_carousel(access_token, username_id, media_id, text3, True, now, comment, message, text2)
                delete_by_id(row_id)
            if typess == "photo_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",1234,+++x")
                thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"photo",image_url=media_id )
                delete_by_id(row_id)
            if typess == "text_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",1234,+++x")
                thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"text" )
                delete_by_id(row_id)
            if typess == "video_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",1234,+++x")
                thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"video",video_url=media_id )
                delete_by_id(row_id)
            if typess == "story" :
                content = aaaa.story_schedule(username_id,hourss,media_id,access_tok)
                dpp.append_to_file(user_id=username_id, platform="Instagram", filename="reachanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "photo":
                content = aaaa.get_media_analytics(username_id,media_id,access_tok)
                dpp.append_to_file(user_id=username_id, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "carousel":
                content = aaaa.get_media_analytics(username_id,media_id,access_tok)
                dpp.append_to_file(user_id=username_id, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "video":
                content = aaaa.get_media_analytics(username_id,media_id,access_tok)
                dpp.append_to_file(user_id=username_id, platform="Instagram", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "text1" :
                content = thhh.get_thread_metrics_csv(username_id , media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Threads", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "photo1" :
                content = thhh.get_thread_metrics_csv(username_id , media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Threads", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "video1" :
                content = thhh.get_thread_metrics_csv(username_id , media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Threads", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "carousel1" :
                content = thhh.get_thread_metrics_csv(username_id , media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Threads", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "shorts": 
                content = you.shorts_schedule(username_id, media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Youtube", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "tweet":
                content = x.get_tweet_metrics(username_id ,media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="X", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "photo_tweet" :
                content = x.get_tweet_metrics(username_id ,media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="X", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "video_tweet":
                content = x.get_tweet_metrics(username_id ,media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="X", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "tweet_later":
                x.post_later("tweet_later", username_id,text1 )
                delete_by_id(row_id)
            if typess == "photo_tweet_later":
                x.post_later("photo_tweet_later", username_id,text1 )
                delete_by_id(row_id)
            if typess == "video_tweet_later":
                x.post_later("video_tweet_later", username_id,text1 )
                delete_by_id(row_id)
            if typess == "pin_photo": 
                content = pin.get_pinterest_pin_analytics_csv(username_id,media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Pinterst", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "pin_video": 
                content = pin.get_pinterest_pin_analytics_csv(username_id,media_id, access_tok)
                dpp.append_to_file(user_id=username_id, platform="Pinterst", filename="postanalysis.txt", data_to_append=content)
                delete_by_id(row_id)
            if typess == "Shorts_later":
                you.post_later(username_id, media_id, text1, text2, text3) 
                delete_by_id(row_id)
            if typess == "Pin_photo_later":
                pin.post_late(username_id , access_tok , text1, text2, text3, "photo" ,media_id)
                delete_by_id(row_id)
            if typess == "Pin_video_later":
                pin.post_late(username_id , access_tok , text1, text2, text3, "video" ,media_id)
                delete_by_id(row_id)
            if typess == "email_later":
                text1 , reply , subject = text1.split(",1234,+++x")
                campp.upload_lategmail(username_id , text1, text2, text3,media_id, reply , subject)
                delete_by_id(row_id)
            if typess == "message_later":
                text1 , reply , subject = text1.split(",1234,+++x")
                campp.upload_latewhat(username_id , text1, text2, text3, media_id, reply , subject)
                delete_by_id(row_id)
        rows = workflow_10_days(now)
        for row_id in rows :
            delete_by_10days(row_id)


# need to add the comment and message for the autmation in the sqlite after the schedule post for every single platform


# had to shift from the token to another method in which the token doesn't require to publish or do anything 

# add repeat of addition of scheledule of post1 type when every it deletes