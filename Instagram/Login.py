from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
import tempfile
import os
import Instagram.schedule_video as sccc
import database.UserDB as dbimp
import requests
from urllib.parse import urlencode
from flask_cors import CORS
from flask import Flask, request, jsonify , redirect
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import limit as lmmm
import Drive.dep as dpp
import authnew as au
from moviepy import VideoFileClip
import Instagram.upload as uploadd
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
load_dotenv()
import logging
executor = ThreadPoolExecutor(max_workers=4)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("drive_api")
app = Flask(__name__)
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
IG_APP_ID = os.getenv("IG_APP_ID")
IG_APP_SECRET = os.getenv("IG_APP_SECRET")
IG_REDIRECT_URI = os.getenv("IG_REDIRECT_URI")
INSTAGRAM_AUTH_URL = "https://www.instagram.com/oauth/authorize"
STATE_MAX_AGE = 600  # seconds
TABLE_NAME = "instagram"
SCOPE = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments"
# BASE_URL = os.getenv("BASE_URL")
MAX_SIZE = 400 * 1024 * 1024
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
reel_duraltion = 300
sotry_duration = 59
min_sorty_duation = 3
min_reel_duration = 3
crousal_duration = 59
photo_szie = 20 * 1024 * 1024
sotry_szie = 180 * 1024 * 1024
reel_szie = 270 * 1024 * 1024

def parse_datetime(value: str, require_tz: bool = True):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if require_tz and dt.tzinfo is None:
        return None
    return dt

def _validate_int(value, field_name="value") -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}: {value!r}")

def get_duration(file_path):
    ext = file_path.lower().split('.')[-1]
    if ext in ('jpg', 'jpeg', 'png', 'bmp'):
        return 1  # static images have no natural duration
    with VideoFileClip(file_path) as video:
        return video.duration

def _coerce_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def is_video_file(filename):
    return filename.lower().endswith(VIDEO_EXTENSIONS)

def get_files_and_upload_to_drive(service, media_type):
    uploaded_files = request.files.getlist("file")
    if not uploaded_files or all(f.filename == "" for f in uploaded_files):
        return None, jsonify({"error": "at least one file required (form-data field: file)"}), 400
    if media_type in ("reel", "photo", "story") and len(uploaded_files) > 1:
        return None, jsonify({"error": f"{media_type} allows only 1 file"}), 400
    if media_type == "carousel" and len(uploaded_files) > 20:
        return None, jsonify({"error": "carousel allows max 20 files"}), 400
    total_size = 0
    results = []
    tmp_paths = []
    uploaded_drive_ids = []
    make_public = True
    try:
        for uploaded_file in uploaded_files:
            if uploaded_file.filename == "":
                continue
            uploaded_file.stream.seek(0, os.SEEK_END)
            size = uploaded_file.stream.tell()
            uploaded_file.stream.seek(0)
            if size > MAX_SIZE:
                results.append({"filename": uploaded_file.filename, "status": "failed", "error": "file exceeds max size"})
                continue
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                uploaded_file.save(tmp.name)
                tmp_path = tmp.name
                tmp_paths.append(tmp_path)
            file_size_on_disk = os.path.getsize(tmp_path)
            if media_type == "reel":
                duration = get_duration(tmp_path)
                if reel_szie < file_size_on_disk or reel_duraltion < duration or duration < min_reel_duration:
                    return None, jsonify({"error": "reel failed size/duration validation"}), 400
            elif media_type == "story":
                duration = get_duration(tmp_path)
                if sotry_szie < file_size_on_disk or sotry_duration < duration:
                    return None, jsonify({"error": "story failed size/duration validation"}), 400
            elif media_type == "photo":
                if photo_szie < file_size_on_disk:
                    return None, jsonify({"error": "photo failed size validation"}), 400
            elif media_type == "carousel":
                if photo_szie < file_size_on_disk:
                    return None, jsonify({"error": "carousel item exceeds max size"}), 400
                if is_video_file(uploaded_file.filename):
                    duration = get_duration(tmp_path)
                    if crousal_duration < duration:
                        return None, jsonify({"error": "carousel video exceeds max duration"}), 400
            total_size += file_size_on_disk
            if total_size > MAX_SIZE:
                return None, jsonify({"error": "total upload size exceeds limit"}), 400
            try:
                file_metadata = {"name": uploaded_file.filename}
                media_upload = MediaFileUpload(tmp_path, mimetype=uploaded_file.mimetype, resumable=True)
                created_file = service.files().create( body=file_metadata , media_body=media_upload, fields="id, name, webViewLink, webContentLink, mimeType", ).execute()
                drive_file_id = created_file["id"]
                uploaded_drive_ids.append(drive_file_id)
                if make_public:
                    service.permissions().create( fileId=drive_file_id, body={"type": "anyone", "role": "reader"} ).execute()
                results.append({ "file_id": drive_file_id, "webViewLink": created_file.get("webViewLink"), "status": "uploaded", })
            except HttpError as e:
                results.append({"status": "failed", "error": str(e)})
        return results, None, None
    except Exception:
        for file_id in uploaded_drive_ids:
            try:
                service.files().delete(fileId=file_id).execute()
            except HttpError:
                pass
        raise
    finally:
        for path in tmp_paths:
            if path and os.path.exists(path):
                os.remove(path)

def deleteFile(service ,file_id):
    if not service :
        return False
    if not file_id:
        return False
    try: 
        service.files().delete(fileId=file_id).execute()
    except:
        return False
    return True

def get_authenticated_access_token(user_id, token, account_id):
    rows = dbimp.select_rows(TABLE_NAME,select="access_token,token_expire",filters={"account_id": account_id, "id": user_id})
    if not rows:
        return None, (jsonify({"error": "no instagram account linked"}),404)
    row = rows[0]
    access_token = row["access_token"]
    raw_expiry = row["token_expire"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}),400)
    if isinstance(raw_expiry, str):
        token_expiry = datetime.fromisoformat(raw_expiry)
    else:
        token_expiry = raw_expiry
    if token_expiry.tzinfo is None:
        token_expiry = token_expiry.replace(tzinfo=timezone.utc)
    if token_expiry - datetime.now(timezone.utc) < timedelta(days=2):
        access_token = uploadd.refresh_token(token, user_id, access_token)
    return access_token, None

def post_story_job(tokench, usernames, rows_by_username, media_size, media_url,publish_now, video_type, duration, post_time):
    try:
        failures = 0
        for user in usernames:
            if failures >= 3:
                logger.warning("aborting story job after 3 failures")
                break
            row = rows_by_username.get(user)
            if row is None:
                failures += 1
                logger.warning(f"no account row for {user}")
                continue
            access_token, err = get_authenticated_access_token(tokench["user_id"], tokench["token"], user)
            if err:
                failures += 1
                logger.warning(f"access token error for {user}: {err}")
                continue
            try:
                uploadd.post_story(
                    access_token=access_token, ig_user_id=row["account_id"],
                    media_size=media_size, media_url=media_url, publish=publish_now,
                    is_video=video_type, media_duration=duration, timmmm=post_time)
            except Exception:
                failures += 1
                logger.exception(f"unable to post story for {user}")
    except Exception:
        logger.exception("story job crashed")

@app.route("/auth/login", methods=["POST"])
def instagram_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    state = serializer.dumps(user_id)
    params = {"client_id": IG_APP_ID,"redirect_uri": IG_REDIRECT_URI,"scope": SCOPE,"response_type": "code","state": state,}
    auth_url = INSTAGRAM_AUTH_URL + "?" + urlencode(params)
    return jsonify({"auth_url": auth_url}) , 200

def store_instagram_account(token, user_id, access_token, username, account_id, timestamp, expire):
    dbimp.insert_rows( TABLE_NAME,{"access_token": access_token, "timestamp": timestamp, "token_expire": expire,"username": username, "account_id": account_id,"id": user_id},)
    dbimp.update_rows( "users", {"instagram": True}, {"user_id": user_id})

def clean(value):
    return (value or "").split("#", 1)[0].strip()

@app.route("/auth/callback", methods=["GET"])
def instagram_callback():
    code = clean(request.args.get("code"))
    state = clean(request.args.get("state"))
    if not code or not state:
        return jsonify({"error": "missing code or state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature as e:
        return jsonify({"error": "invalid state"}), 400
    try:
        token_resp = requests.post("https://api.instagram.com/oauth/access_token",data={"client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,"grant_type": "authorization_code","redirect_uri": IG_REDIRECT_URI, "code": code},timeout=10,).json()
        short_token = token_resp.get("access_token")
        if not short_token:
            return jsonify({"error": "token exchange failed", "details": token_resp}), 400
        long_resp = requests.get("https://graph.instagram.com/access_token",params={"grant_type": "ig_exchange_token", "client_secret": IG_APP_SECRET,"access_token": short_token},timeout=10,).json()
        long_token = long_resp.get("access_token")
        seconds = long_resp.get("expires_in")
        if not long_token or not seconds:
            return jsonify({"error": "long token exchange failed", "details": long_resp}), 400
        about = requests.get("https://graph.instagram.com/me",params={"fields": "id,username,account_type,media_count", "access_token": long_token},timeout=10,).json()
    except requests.RequestException as e:
        return jsonify({"error": "instagram request failed", "details": str(e)}), 502
    now = datetime.now(timezone.utc)
    db_token = au.jsonspoof(user_id=user_id, timestamp=now + timedelta(hours=1))
    try:
        store_instagram_account(db_token, user_id, long_token, about.get("username"), about.get("id"),now.isoformat(), (now + timedelta(seconds=seconds)).isoformat(),)
    except Exception as e:
        return jsonify({"error": "failed to save token", "details": str(e)}), 500
    return redirect(homepage, code=302)

@app.route("/posts/", methods=["POST"])
def get_instagram_posts():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    token = body.get("token")
    tokench = au.process(token=token)
    access_token, err = get_authenticated_access_token(tokench["user_id"], tokench["token"], account_id)
    if err:
        return err
    fields = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count"
    url = "https://graph.instagram.com/me/media"
    params = {"fields": fields, "access_token": access_token}
    posts = []
    while url:
        resp = requests.get(url, params=params).json()
        if "error" in resp:
            return jsonify(resp), 400
        posts.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
        post_ids = [p["id"] for p in posts]
    workflows = sccc.get_workflows(post_ids)
    for p in posts:
        wf = workflows.get(p["id"])
        p["workflow"] = {"comment": wf[1], "message": wf[2]} if wf else None
    return jsonify({"count": len(posts), "post_ids": post_ids, "posts": posts}), 200

@app.route("/stories/", methods=["POST"])
def get_instagram_stories():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    token = body.get("token")
    tokench = au.process(token=token)
    access_token, err = get_authenticated_access_token(tokench["user_id"],tokench["token"],account_id)
    if err: return err
    fields = "id,media_type,media_url,timestamp"
    url = "https://graph.instagram.com/me/stories"
    params = {"fields": fields, "access_token": access_token}
    stories = []
    while url:
        resp = requests.get(url, params=params).json()
        if "error" in resp:
            return jsonify(resp), 400
        stories.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
    for story in stories:
        try:
            thumb_result = uploadd.get_media_thumbnail(access_token=access_token, media_id=story["id"])
        except Exception as e:
            story["thumbnail_url"] = None
            continue
        if thumb_result["success"]:
            story["thumbnail_url"] = thumb_result["data"]
        else:
            story["thumbnail_url"] = None
    return jsonify({"count": len(stories), "stories": stories}), 200

@app.route("/upload/story", methods=["POST"])
def story():
    body = request.get_json(silent=True) or {}
    account_ids = body.get("account_id")
    token = body.get("token")
    media_size = _coerce_int(body.get("media_size"))
    duration = _coerce_int(body.get("duration"))
    video_type = str(body.get("is_video")).strip().lower() == "true"
    publish_now = str(body.get("publish")).strip().lower() == "true"
    if not isinstance(account_ids, list):
        account_ids = [account_ids] if account_ids else []
    if not account_ids:
        return jsonify({"error": "at least one username is required"}), 400
    if media_size is None or not token:
        return jsonify({"error": "media_size and token are required"}), 400
    if video_type and duration is None:
        return jsonify({"error": "duration is required for video"}), 400
    try:
        _validate_int(media_size)
        if duration is not None:
            _validate_int(duration)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invalid"}), 400
    now = datetime.now(timezone.utc)
    if publish_now:
        post_time = now
    else:
        post_time = parse_datetime(body.get("time") or {})
        if post_time is None:
            return jsonify({"error": "invalid or missing date/time"}), 400
        if post_time.tzinfo is None:
            post_time = post_time.replace(tzinfo=timezone.utc)
        if not (now + timedelta(minutes=5) <= post_time <= now + timedelta(days=4)):
            return jsonify({"error": "invalid time for posting"}), 400
    rows = dbimp.select_rows(TABLE_NAME, select="username,account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {r["username"]: r for r in rows}
    service = dpp.authenticate_and_get_service(token)
    drive_data, error, code = get_files_and_upload_to_drive(service=service, type="story")
    if not drive_data or drive_data[0].get("error"):
        return jsonify({"error": error or "drive upload failed"}), code or 500
    if not lmmm.checkk(tokench["token"], tokench["user_id"], now.isoformat(), len(account_ids)):
        return jsonify({"error": "limit has been reached"}), 400
    media_url = drive_data[0]["webContentLink"]  # direct URL, must be publicly accessible
    executor.submit(post_story_job, tokench, account_ids, rows_by_username,media_size, media_url, publish_now, video_type, duration, post_time)
    return jsonify({"success": "added"}), 200

@app.route("/upload/photo", methods=["POST"])
def photo():
    data = request.get_json(silent=True) or {}
    media_size = data.get("media_size")
    publish = data.get("publish")
    caption = data.get("caption", "")
    timee_raw = data.get("time") or {}
    token = data.get("token")
    comment = data.get("comment")  or ""
    message = data.get("message") or ""
    account_id = data.get("account_id")
    if not isinstance(account_id, list):
        account_id = [account_id] if account_id else []
    if not account_id:
        return jsonify({"error": "at least one username is required"}), 400
    if media_size is None:
        return jsonify({"error": "url and media size is required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invlaid"}), 400
    media_size = _coerce_int(media_size)
    service = dpp.authenticate_and_get_service(token)
    data , error , code  = get_files_and_upload_to_drive(service=service,type="photo")
    if not data or data[0]["error"] :
        return jsonify({"error":error}) , code
    media_url = data[0]["webViewLink"]
    if media_size is None:
        return jsonify({"success": False, "message": "Unable to post photo. due media size int value"}), 400
    try:
        _validate_int(media_size)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    publish_now = str(publish).strip().lower() == "true"
    if not publish_now :
        timee = parse_datetime(timee_raw)
        if timee is None:
            return jsonify({"error": "invalid type or missing date/time"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(minutes=5)
        up = now + timedelta(days=4)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    else :
        timee = datetime.now(timezone.utc)
    if not lmmm.checkk(tokench["token"],tokench["user_id"],now.isoformat(),len(account_id)):
        return jsonify({"error": "limit has been reached"}) ,400
    rows = dbimp.select_rows( TABLE_NAME, select="username,account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["username"]: row for row in rows}
    results = []
    for user in account_id:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["account_id"]
        access_token, err = get_authenticated_access_token(tokench["user_id"],tokench["token"],user)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_photo(access_token=access_token,ig_user_id=account_id,image_url=media_url,caption=caption,media_size=media_size,publish=publish_now,timmmm=timee,message=message , comment=comment)
            if publish_now  and comment and message:
                sccc.inert_workflow(id_post ,message , comment)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post photo: {e}"})
            continue
        results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
        overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/upload/reel", methods=["POST"])
def video():
    data = request.get_json(silent=True) or {}
    media_size = data.get("media_size")
    publish = data.get("publish")
    caption = data.get("caption", "")
    duration = data.get("duration")
    timee_raw = data.get("time") or {}
    token = data.get("token")
    comment = data.get("comment") or ""
    message = data.get("message") or ""
    account_id = data.get("account_id")
    if not isinstance(account_id, list):
        account_id = [account_id] if account_id else []
    if not account_id:
        return jsonify({"error": "at least one username is required"}), 400
    if media_size is None:
        return jsonify({"error": "url and media size is required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invlaid"}), 400
    media_size = _coerce_int(media_size)
    as_reeel = True
    duration = _coerce_int(duration)
    service = dpp.authenticate_and_get_service(token)
    data , error , code  = get_files_and_upload_to_drive(service=service,type="reel") 
    if not data or data[0]["error"] :
        return jsonify({"error":error}) , code
    media_url = data[0]["webViewLink"]
    if None in (media_size, duration):
        return jsonify({"success": False, "message": "Unable to post video. due to media size / duration / is not int"}), 400
    try:
        _validate_int(media_size)
        _validate_int(duration)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    publish_now = str(publish).strip().lower() == "true"
    if not publish_now :
        timee = parse_datetime(timee_raw)
        if timee is None:
            return jsonify({"error": "invalid type or missing date/time"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(minutes=5)
        up = now + timedelta(days=4)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    else :
        timee = datetime.now(timezone.utc)
    if not lmmm.checkk(tokench["token"],tokench["user_id"],now.isoformat(),len(account_id)):
        return jsonify({"error": "limit has been reached"}) ,400
    rows = dbimp.select_rows( TABLE_NAME, select="username,account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["username"]: row for row in rows}
    results = []
    for user in account_id:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["account_id"]
        access_token, err = get_authenticated_access_token(tokench["user_id"],tokench["token"],user)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_video(access_token=access_token,ig_user_id=account_id,video_url=media_url,media_size=media_size,caption=caption,publish=publish_now,as_reel=as_reeel,media_duration=duration,timmmm=timee,message=message , comment=comment)
            if publish_now  and comment and message:
                sccc.inert_workflow(id_post ,message , comment)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post video: {e}"})
            continue
        results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
    overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/upload/carousel", methods=["POST"])
def carousel():
    data = request.get_json(silent=True) or {}
    publish = data.get("publish")
    caption = data.get("caption", "")
    media_size = data.get("media_size", [])
    media_duration = data.get("media_duration", [])
    is_video = data.get("is_video", [])
    timee_raw = data.get("time") or {}
    token = data.get("token")
    comment = data.get("comment") or ""
    message = data.get("message") or ""
    account_id = data.get("account_id")
    if not isinstance(account_id, list):
        account_id = [account_id] if account_id else []
    if not account_id:
        return jsonify({"error": "at least one username is required"}), 400
    if not is_video or not media_size or not media_duration:
        return jsonify({"success": False, "message": "media_urls, is_video, media_size, and media_duration are all required"}), 400
    if len(media_size) == len(media_duration):
        return jsonify({"success": False, "message": "media_urls, is_video, media_size, and media_duration must all be the same length"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invlaid"}), 400
    is_videoo = [str(p).strip().lower() == "true" for p in is_video]
    media_sizee = [_coerce_int(p) for p in media_size]
    if any(v is None for v in media_sizee):
        return jsonify({"success": False, "message": "one or more media_size values are not valid ints"}), 400
    media_durationn = [_coerce_int(p) for p in media_duration]
    if any(v is None for v in media_durationn):
        return jsonify({"success": False, "message": "one or more media_duration values are not valid ints"}), 400
    service = dpp.authenticate_and_get_service(token)
    data , error , code  = get_files_and_upload_to_drive(service=service,type="carousel")
    if not data or data[0]["error"] :
        return jsonify({"error":error}) , code
    media_urls = [item["webViewLink"] for item in data]
    publish_now = str(publish).strip().lower() == "true"
    if not publish_now :
        timee = parse_datetime(timee_raw)
        if timee is None:
            return jsonify({"error": "invalid type or missing date/time"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(minutes=5)
        up = now + timedelta(days=4)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    else :
        timee = datetime.now(timezone.utc)
    if not lmmm.checkk(tokench["token"],tokench["user_id"],now.isoformat(),len(account_id)):
        return jsonify({"error": "limit has been reached"}) ,400
    rows = dbimp.select_rows( TABLE_NAME, select="username,account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["username"]: row for row in rows}
    results = []
    for user in account_id:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["account_id"]
        access_token, err = get_authenticated_access_token(tokench["user_id"],tokench["token"],user)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_carousel(access_token=access_token,ig_user_id=account_id,is_video=is_videoo,media_size=media_sizee,media_duration=media_durationn,media_urls=media_urls,publish=publish_now,caption=caption,timmmm=timee,message=message , comment=comment )
            if publish_now  and comment and message:
                sccc.inert_workflow(id_post ,message , comment)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post carousel: {e}"})
            continue
        results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
    overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/insight", methods=["POST"])
def insight():
    body = request.get_json(silent=True) or {}
    is_story = body.get("is_story")
    media_id = body.get("media_id")
    account_id = body.get("account_id")
    token = body.get("token")
    tokench = au.process(token=token)
    access_token, err = get_authenticated_access_token(tokench["user_id"],tokench["token"],account_id)
    if err: return err
    is_story = str(is_story).strip().lower() == "true" if is_story else False
    try:
        result = uploadd.get_media_insights(access_token=access_token, media_id=media_id , story = is_story)
    except Exception as e:
        return jsonify({"success": False, "message": f"Unable to fetch insight: {e}"}), 500
    if result["success"]:
        return jsonify({"success": True, "data": result["data"]}), 200
    else:
        return jsonify({"success": False, "message": result["error"]}), 500

if __name__ == "__main__":
    app.run(port=5000, debug=True)
#  account id will be hideen and checks by the db 