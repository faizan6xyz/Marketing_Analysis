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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
STATE_MAX_AGE = 600
TABLE_NAME = "instagram"
SCOPE = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments"
MAX_SIZE = 300 * 1024 * 1024
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
IMAGE_EXTENSIONS = (".jpg", ".png","jpeg")
MAX_REEL_SECONDS = 15 * 60
MIN_REEL_SECONDS = 3
MAX_STORY_SECONDS = 60
MIN_STORY_SECONDS = 3
MAX_VIDEO_SECONDS = 60 * 60
MIN_VIDEO_SECONDS = 3
MAX_CROUSAL_SECONDS = 59
PHOTO_SIZE = 20 * 1024 * 1024
STORY_SIZE = 180 * 1024 * 1024
REEL_SIZE = 270 * 1024 * 1024
MAX_FAILURES = 3

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

def get_duration(file_path):
    ext = file_path.lower().split('.')[-1]
    if ext in IMAGE_EXTENSIONS:
        return 1 
    with VideoFileClip(file_path) as video:
        return video.duration   

def is_video_file(filename):
    return filename.lower().endswith(VIDEO_EXTENSIONS)

def cleanup_tmp(paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            logger.warning(f"could not remove temp file {path}")

def get_files_and_validate(request, media_type):
    uploaded_files = request.files.getlist("file")
    if not uploaded_files or all(f.filename == "" for f in uploaded_files):
        return None, None,None,None,"at least one file required (form-data field: file)"
    if media_type in ("reel", "photo", "story") and len(uploaded_files) > 1:
        return None, None,None,None,f"{media_type} allows only 1 file"
    if media_type == "carousel" and len(uploaded_files) > 10:
        return None, None,None,None,"carousel allows max 10 files"
    total_size = 0
    tmp_files = []
    durations = []
    sizes = []
    is_video = []
    def fail(message):
        cleanup_tmp([f["path"] for f in tmp_files])
        logger.warning(f"{media_type} validation failed: {message}")
        return None, None, None, None, message
    try:
        for uploaded_file in uploaded_files:
            if uploaded_file.filename == "":
                continue
            uploaded_file.stream.seek(0, os.SEEK_END)
            size = uploaded_file.stream.tell()
            uploaded_file.stream.seek(0)
            if size > MAX_SIZE:
                return fail("file exceeds max size")
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            tmp_files.append({"path": tmp_path, "filename": uploaded_file.filename, "mimetype": uploaded_file.mimetype})
            uploaded_file.save(tmp_path)
            file_size_on_disk = os.path.getsize(tmp_path)
            if media_type == "reel":
                duration = get_duration(tmp_path)
                if REEL_SIZE < file_size_on_disk or MAX_REEL_SECONDS < duration or duration < MIN_REEL_SECONDS :
                    return fail("reel failed size/duration validation")
                durations.append(duration)
                sizes.append(file_size_on_disk)
            elif media_type == "story":
                if is_video_file(uploaded_file.filename):
                    duration = get_duration(tmp_path)
                    if STORY_SIZE < file_size_on_disk or MAX_STORY_SECONDS < duration or MIN_STORY_SECONDS > duration:
                        return fail("story failed size/duration validation")
                    durations.append(duration)
                    is_video.append(True)
                sizes.append(file_size_on_disk)
            elif media_type == "photo":
                if PHOTO_SIZE < file_size_on_disk:
                    return fail("photo failed size validation")
                sizes.append(file_size_on_disk)
            elif media_type == "carousel":
                if PHOTO_SIZE < file_size_on_disk:
                    return fail("carousel item exceeds max size")
                if is_video_file(uploaded_file.filename):
                    duration = get_duration(tmp_path)
                    if MAX_CROUSAL_SECONDS < duration:
                        return fail("carousel video exceeds max duration")
                    durations.append(duration)
                    is_video.append(True)
                sizes.append(file_size_on_disk)
            total_size += file_size_on_disk
            if total_size > MAX_SIZE:
                return fail("total upload size exceeds limit")
        logger.info(f"{media_type} validated: {len(tmp_files)} file(s), {total_size} bytes")
        return tmp_files, is_video, durations, sizes, None
    except Exception:
        cleanup_tmp([f["path"] for f in tmp_files])
        logger.exception(f"{media_type} validation crashed")
        raise

def deleteFile(service ,file_id):
    if not service :
        return False
    if not file_id:
        return False
    try: 
        service.files().delete(fileId=file_id).execute()
    except Exception:
        logger.warning(f"failed to delete drive file {file_id}")
        return False
    return True

def rollback_drive(service, file_ids):
    if not file_ids:
        return
    logger.info(f"rolling back {len(file_ids)} drive file(s)")
    for file_id in file_ids:
        deleteFile(service, file_id)

def upload_files_to_drive(service, tmp_files, make_public=True):
    results = []
    uploaded_drive_ids = []
    try:
        for f in tmp_files:
            media_upload = MediaFileUpload(f["path"], mimetype=f["mimetype"], resumable=True)
            created_file = service.files().create( body={"name": f["filename"]} , media_body=media_upload, fields="id, name, webViewLink, webContentLink, mimeType", ).execute()
            drive_file_id = created_file["id"]
            uploaded_drive_ids.append(drive_file_id)
            if make_public:
                service.permissions().create( fileId=drive_file_id, body={"type": "anyone", "role": "reader"} ).execute()
            results.append({ "file_id": drive_file_id, "webViewLink": created_file.get("webViewLink"), "webContentLink": created_file.get("webContentLink"), "status": "uploaded", })
            logger.info(f"uploaded {f['filename']} to drive as {drive_file_id}")
        return results, uploaded_drive_ids, None
    except (HttpError, Exception):
        logger.exception("drive upload failed")
        rollback_drive(service, uploaded_drive_ids)
        return None, [], "drive upload failed"

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
        logger.info(f"refreshing instagram token for account {account_id}")
        access_token = uploadd.refresh_token(token, user_id, access_token)
    return access_token, None

def _as_list(value):
    if isinstance(value, list):
        return value
    return [value] if value else []
 
def _is_true(value):
    return str(value).strip().lower() == "true"
 
def _resolve_post_time(body):
    now = datetime.now(timezone.utc)
    if _is_true(body.get("publish")):
        return True, now, now, None
    post_time = parse_datetime(body.get("time") or {})
    if post_time is None:
        return False, now, None, "invalid or missing date/time"
    if post_time.tzinfo is None:
        post_time = post_time.replace(tzinfo=timezone.utc)
    if not (now + timedelta(minutes=5) <= post_time <= now + timedelta(days=4)):
        return False, now, None, "invalid time for posting"
    return False, now, post_time, None
 
def _load_rows(tokench):
    rows = dbimp.select_rows(TABLE_NAME,select="username,account_id",filters={"id": tokench["user_id"]},)
    return {r["username"]: r for r in rows}
 
def _run_jobs(label, usernames, rows_by_username, tokench, post_fn, tmp_files, token):
    service = None
    drive_ids = []
    try:
        logger.info(f"{label} job started for {len(usernames)} account(s)")
        try:
            service = dpp.authenticate_and_get_service(token)
            items, drive_ids, upload_err = upload_files_to_drive(service, tmp_files)
        finally:
            cleanup_tmp([f["path"] for f in tmp_files])
        if upload_err:
            logger.error(f"{label} job aborted: {upload_err}")
            return
        media_urls = [i["webContentLink"] for i in items]
        failures = 0
        successes = 0
        for user in usernames:
            if failures >= MAX_FAILURES:
                logger.warning(f"aborting {label} job after {MAX_FAILURES} failures")
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
                post_fn(access_token, row["account_id"], media_urls)
                successes += 1
                logger.info(f"{label} posted for {user}")
            except Exception:
                failures += 1
                logger.exception(f"unable to post {label} for {user}")
        logger.info(f"{label} job finished: {successes} ok, {failures} failed")
        if successes == 0:
            rollback_drive(service, drive_ids)
    except Exception:
        logger.exception(f"{label} job crashed")
        if service and drive_ids:
            rollback_drive(service, drive_ids)

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
    logger.info(f"instagram login url issued for user {user_id}")
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
        logger.warning("instagram callback state expired")
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature as e:
        logger.warning("instagram callback invalid state")
        return jsonify({"error": "invalid state"}), 400
    try:
        token_resp = requests.post("https://api.instagram.com/oauth/access_token",data={"client_id": IG_APP_ID, "client_secret": IG_APP_SECRET,"grant_type": "authorization_code","redirect_uri": IG_REDIRECT_URI, "code": code},timeout=10,).json()
        short_token = token_resp.get("access_token")
        if not short_token:
            logger.error("instagram token exchange failed")
            return jsonify({"error": "token exchange failed", "details": token_resp}), 400
        long_resp = requests.get("https://graph.instagram.com/access_token",params={"grant_type": "ig_exchange_token", "client_secret": IG_APP_SECRET,"access_token": short_token},timeout=10,).json()
        long_token = long_resp.get("access_token")
        seconds = long_resp.get("expires_in")
        if not long_token or not seconds:
            logger.error("instagram long token exchange failed")
            return jsonify({"error": "long token exchange failed", "details": long_resp}), 400
        about = requests.get("https://graph.instagram.com/me",params={"fields": "id,username,account_type,media_count", "access_token": long_token},timeout=10,).json()
    except requests.RequestException as e:
        logger.exception("instagram request failed")
        return jsonify({"error": "instagram request failed", "details": str(e)}), 502
    now = datetime.now(timezone.utc)
    db_token = au.jsonspoof(user_id=user_id, timestamp=now + timedelta(hours=1))
    try:
        store_instagram_account(db_token, user_id, long_token, about.get("username"), about.get("id"),now.isoformat(), (now + timedelta(seconds=seconds)).isoformat(),)
    except Exception as e:
        logger.exception("failed to save instagram token")
        return jsonify({"error": "failed to save token", "details": str(e)}), 500
    logger.info(f"instagram account {about.get('username')} connected for user {user_id}")
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
            logger.error(f"posts fetch error for {account_id}: {resp['error']}")
            return jsonify(resp), 400
        posts.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
        post_ids = [p["id"] for p in posts]
    workflows = sccc.get_workflows(post_ids)
    for p in posts:
        wf = workflows.get(p["id"])
        p["workflow"] = {"comment": wf[1], "message": wf[2]} if wf else None
    logger.info(f"fetched {len(posts)} posts for {account_id}")
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
            logger.error(f"stories fetch error for {account_id}: {resp['error']}")
            return jsonify(resp), 400   
        stories.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
    for story in stories:
        try:
            thumb_result = uploadd.get_media_thumbnail(access_token=access_token, media_id=story["id"])
        except Exception as e:
            logger.warning(f"thumbnail fetch failed for story {story['id']}: {e}")
            story["thumbnail_url"] = None
            continue
        if thumb_result["success"]:
            story["thumbnail_url"] = thumb_result["data"]
        else:
            story["thumbnail_url"] = None
    logger.info(f"fetched {len(stories)} stories for {account_id}")
    return jsonify({"count": len(stories), "stories": stories}), 200
 
@app.route("/upload/story", methods=["POST"])
def story():
    body = request.form
    usernames = _as_list(body.get("account_id"))
    token = body.get("token")
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not token:
        return jsonify({"error": "token are required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invalid"}), 400
    publish_now, now, post_time, err = _resolve_post_time(body)
    if err:
        return jsonify({"error": err}), 400
    tmp_files, is_video, duration, size, error = get_files_and_validate(request, "story")
    if error:
        return jsonify({"error": error}), 400
    if not lmmm.checkk(tokench["token"], tokench["user_id"], now.isoformat(), len(usernames)):
        cleanup_tmp([f["path"] for f in tmp_files])
        return jsonify({"error": "limit has been reached"}), 400
    rows_by_username = _load_rows(tokench)
    def post_fn(access_token, ig_user_id, media_urls):
        uploadd.post_story(access_token=access_token,ig_user_id=ig_user_id,media_size=size[0],media_url=media_urls[0],publish=publish_now,is_video=is_video,media_duration=duration,timmmm=post_time,)
    executor.submit(_run_jobs, "story", usernames, rows_by_username, tokench, post_fn, tmp_files, token)
    return jsonify({"success": "added"}), 200
 
@app.route("/upload/photo", methods=["POST"])
def photo():
    body = request.form
    usernames = _as_list(body.get("account_id"))
    token = body.get("token")
    caption = body.get("caption", "")
    comment = body.get("comment") or ""
    message = body.get("message") or ""
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invalid"}), 400
    publish_now, now, post_time, err = _resolve_post_time(body)
    if err:
        return jsonify({"error": err}), 400
    tmp_files, is_video, duration, size, error = get_files_and_validate(request, "photo")
    if error:
        return jsonify({"error": error}), 400
    if not lmmm.checkk(tokench["token"], tokench["user_id"], now.isoformat(), len(usernames)):
        cleanup_tmp([f["path"] for f in tmp_files])
        return jsonify({"error": "limit has been reached"}), 400
    rows_by_username = _load_rows(tokench)
    def post_fn(access_token, ig_user_id, media_urls):
        id_post = uploadd.post_photo(access_token=access_token,ig_user_id=ig_user_id,image_url=media_urls[0],caption=caption,media_size=size[0],publish=publish_now,timmmm=post_time,message=message,comment=comment,)
        if publish_now and comment and message:
            sccc.inert_workflow(id_post, message, comment)
    executor.submit(_run_jobs, "photo", usernames, rows_by_username, tokench, post_fn, tmp_files, token)
    return jsonify({"success": "added"}), 200
 
@app.route("/upload/reel", methods=["POST"])
def video():
    body = request.form
    usernames = _as_list(body.get("account_id"))
    token = body.get("token")
    caption = body.get("caption", "")
    comment = body.get("comment") or ""
    message = body.get("message") or ""
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not token:
        return jsonify({"error": "token is required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invalid"}), 400
    publish_now, now, post_time, err = _resolve_post_time(body)
    if err:
        return jsonify({"error": err}), 400
    tmp_files, is_video, duration, size, error = get_files_and_validate(request, "reel")
    if error:
        return jsonify({"error": error}), 400
    if not lmmm.checkk(tokench["token"], tokench["user_id"], now.isoformat(), len(usernames)):
        cleanup_tmp([f["path"] for f in tmp_files])
        return jsonify({"error": "limit has been reached"}), 400
    rows_by_username = _load_rows(tokench)
    def post_fn(access_token, ig_user_id, media_urls):
        id_post = uploadd.post_video(access_token=access_token,ig_user_id=ig_user_id,video_url=media_urls[0],media_size=size[0],caption=caption,publish=publish_now,as_reel=True,media_duration=duration,timmmm=post_time,message=message,comment=comment,)
        if publish_now and comment and message:
            sccc.inert_workflow(id_post, message, comment)
    executor.submit(_run_jobs, "reel", usernames, rows_by_username, tokench, post_fn, tmp_files, token)
    return jsonify({"success": "added"}), 200
 
@app.route("/upload/carousel", methods=["POST"])
def carousel():
    body = request.form
    usernames = _as_list(body.get("account_id"))
    token = body.get("token")
    caption = body.get("caption", "")
    comment = body.get("comment") or ""
    message = body.get("message") or ""
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not token:
        return jsonify({"error": "token is required"}), 400
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"success": False, "message": "token_invalid"}), 400
    publish_now, now, post_time, err = _resolve_post_time(body)
    if err:
        return jsonify({"error": err}), 400
    tmp_files, is_video, duration, size, error = get_files_and_validate(request, "carousel")
    if error:
        return jsonify({"error": error}), 400
    if not lmmm.checkk(tokench["token"], tokench["user_id"], now.isoformat(), len(usernames)):
        cleanup_tmp([f["path"] for f in tmp_files])
        return jsonify({"error": "limit has been reached"}), 400
    rows_by_username = _load_rows(tokench)
    def post_fn(access_token, ig_user_id, media_urls):
        id_post = uploadd.post_carousel(access_token=access_token,ig_user_id=ig_user_id,is_video=is_video,media_size=size,media_duration=duration,media_urls=media_urls,publish=publish_now,caption=caption,timmmm=post_time,message=message,comment=comment,)
        if publish_now and comment and message:
            sccc.inert_workflow(id_post, message, comment)
    executor.submit(_run_jobs, "carousel", usernames, rows_by_username, tokench, post_fn, tmp_files, token)
    return jsonify({"success": "added"}), 200

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
        logger.exception(f"unable to fetch insight for media {media_id}")
        return jsonify({"success": False, "message": f"Unable to fetch insight: {e}"}), 500
    if result["success"]:
        return jsonify({"success": True, "data": result["data"]}), 200
    else:
        logger.error(f"insight error for media {media_id}: {result['error']}")
        return jsonify({"success": False, "message": result["error"]}), 500

if __name__ == "__main__":
    app.run(port=5000, debug=True)