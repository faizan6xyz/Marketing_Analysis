from datetime import datetime, timezone
import time
import Instagram.upload as aaaa
import sqlite3
import X.login as x
import threads.login as thhh
import campaign as campp
import json
import pinterst.login as pin 
import Drive.dep as dpp
import youtube.login as you
DB = "schedule.db"

def get_conn():
    return sqlite3.connect(DB)

def is_valid_iso_format(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False

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
                    text1 TEXT ,
                    text2 TEXT ,
                    text3 TEXT  ) """)
    conn.execute(""" CREATE TABLE IF NOT EXISTS workflow (
                    id TEXT ,
                    message TEXT ,
                    comment TEXT ) """)
    conn.commit()
    conn.close()

def inert_workflow(id_ , message , comment ):
    conn = get_conn()
    conn.execute("INSERT INTO workflow ( id , comment , message) values (?,?,?) " ,( id_,comment,message ))
    conn.commit()
    conn.close()

def workflow_data(id_):
    conn = get_conn()
    cur = conn.execute("SELECT  message , comment FROM workdlow WHERE id = ? ",(id_))   # for the whatsapp theres no comment but for the gmail comment is the subject 
    rows = cur.fetchall()
    conn.close()
    return rows

def insert_time(user_id, container_id, scheduled_time, access_token):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    if is_valid_iso_format(scheduled_time):
        conn.execute("INSERT INTO schedule (user_id, container_id, time, type , access_token) VALUES (?, ?, ?, ?, ?)",(user_id, container_id, scheduled_time,"container",access_token))
        conn.commit()
        conn.close()

def insert_post(user_id, scheduled_time, access_token, typeee, text1 , text2 , text3 , media_id):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    if is_valid_iso_format(scheduled_time):
        conn.execute("INSERT INTO schedule (user_id, time, access_token , type , text1 , text2 , text3 ,media_id) VALUES (?, ?, ?, ?, ?, ? , ? , ?)",(user_id, scheduled_time,access_token,typeee,text1,text2,text3,media_id))
        conn.commit()
        conn.close()

def insert__story(user_id,  scheduled_time, access_token,media_id,hour,typee):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    if is_valid_iso_format(scheduled_time):
        conn.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id,hour) VALUES (?, ?, ?,?,?,?,?)",(user_id,  scheduled_time, access_token,typee,media_id,hour))
        conn.commit()
        conn.close()

def insert__story1(user_id,  scheduled_time, access_token,media_id,typee):    # time should be give in the isoformat iniitally as argument 
    conn = get_conn()
    if is_valid_iso_format(scheduled_time):    
        conn.execute("INSERT INTO schedule (user_id, time, access_token, type,media_id) VALUES (?, ?, ?, ?,?,?,?)",(user_id,  scheduled_time, access_token,typee,media_id))
        conn.commit()
        conn.close()

def get_containers_due(now):
    conn = get_conn()
    cur = conn.execute("SELECT id, container_id, access_token, user_id , type,media_id,hour,text1,text2,text3 FROM schedule WHERE time < ?", (now,))
    rows = cur.fetchall()
    conn.close()
    return rows

def update_container_schedule(container_id, sctime):
    conn = get_conn()
    if is_valid_iso_format(sctime):
        conn.execute("UPDATE schedule SET time = ? WHERE container_id = ?", (sctime, container_id))
        conn.commit()
        conn.close()

def delete_by_id(row_id):
    conn = get_conn()
    conn.execute("DELETE FROM schedule WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

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
                aaaa.post_photo(now,access_token, username_id,media_id, text3, 0, True)
                delete_by_id(row_id)
            if typess in ("Reel_later", "Video_later"):
                access_token = aaaa.access_tokenvali(username_id)
                if not access_token :
                    continue
                height, width, cover_url, media_duration = text2.split(",")
                aaaa.post_video(now,access_token, username_id, height, width, media_id, 0 , text3, text1, cover_url,True, media_duration )
                delete_by_id(row_id)
            if typess == "Carousel_later":
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
                delete_by_id(row_id)
            if typess == "container1":
                thhh.publish_threads_container_sc(user_id=username_id, access_token=access_tok, creation_id=container_id)
                delete_by_id(row_id)
            if typess == "carousal_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",")
                thhh.process_threads_carousel(access_token, username_id, media_id, text3, True, now, comment, message, text2)
                delete_by_id(row_id)
            if typess == "photo_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",")
                thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"photo",image_url=media_id )
                delete_by_id(row_id)
            if typess == "text_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",")
                thhh.process_threads_text_posts(access_token, username_id, True, now, text3, comment, message, text2,"text" )
                delete_by_id(row_id)
            if typess == "video_later1":
                expire  = datetime.now(timezone.utc)
                access_token = thhh.refresh_threads_token11(expire,access_token,username_id)
                comment , message = text1.split(",")
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
                campp.upload_lategmail(username_id , text1, text2, text3,media_id)
                delete_by_id(row_id)
            if typess == "message_later":
                campp.upload_latewhat(username_id , text1, text2, text3, media_id)
                delete_by_id(row_id)
            
        time.sleep(1)
# had to shift from the token to another method in which the token doesn't require to publish or do anything 



# add repeat of addition of scheledule of post1 type when every it deletes