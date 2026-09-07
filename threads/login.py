import database.UserDB as dbimp
import os
import time
import requests
from urllib.parse import urlencode
from flask_cors import CORS
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta
import Instagram.schedule_video as sccc
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import authnew as au
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
THREADS_TABLE_NAME = "threads"
THREADS_APP_ID = os.environ.get("Threads_app_id")
THREADS_APP_SECRET = os.environ.get("Threads_app_secrects")
THREADS_REDIRECT_URI = os.environ.get("THREADS_REDIRECT_URI")
BASE_URL = os.environ.get("BASE_URL")
STATE_MAX_AGE = 600  # seconds
THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_read_replies"
THREADS_AUTH_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_EXCHANGE_URL = "https://graph.threads.net/access_token"
THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
THREADS_ME_URL = "https://graph.threads.net/v1.0/me"

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, THREADS_TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

def refresh_threads_token(expire, access, token, user_id, username):
    expire = datetime.fromisoformat(expire)
    if expire - datetime.now(timezone.utc) < timedelta(days=1):
        resp = requests.get( THREADS_REFRESH_URL, params={"grant_type": "th_refresh_token", "access_token": access}, )
        resp.raise_for_status()
        data = resp.json()
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
        new_access = data["access_token"]
        try:
            dbimp.update_rows( token, THREADS_TABLE_NAME, { "Access_token": new_access, "Token_expire": new_expiry.isoformat(), }, filters={"Username": username, "id": user_id}, )
        except Exception as e:
            print(f"Failed to persist refreshed Threads token for user_id={user_id}: {e}")
        return new_access
    else:
        return access

def get_all_threads_media(access_token, account_id, page_size=100):
    media = []
    url = f"https://graph.threads.net/v1.0/{account_id}/threads"
    params = { "fields": "id,text,timestamp,permalink,media_type,media_url", "limit": page_size, "access_token": access_token, }
    while url:
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        media.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None  # 'next' already has all query params baked in
    return media

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

def get_threads_insights(access_token, media_ids, metric_types="views,likes,replies,reposts,quotes,shares"):
    results = {}
    for media_id in media_ids:
        resp = requests.get( f"https://graph.threads.net/v1.0/{media_id}/insights", params={"metric": metric_types, "access_token": access_token}, ) 
        resp.raise_for_status()
        data = resp.json().get("data", [])
        results[media_id] = {m["name"]: m.get("values", [{}])[0].get("value") for m in data}
    return results

def _publish_threads_post(publish,time,access_token, threads_user_id, media_type, text, **kwargs):
    creation_id = create_threads_container( access_token, threads_user_id, media_type, text=text, **kwargs )
    if not creation_id:
        raise RuntimeError("failed to create container")
    is_published = wait_for_threads_container(access_token, creation_id)
    if not is_published:
        raise RuntimeError("container failed to reach FINISHED state")
    if publish :
        thread_id = publish_threads_container(access_token, threads_user_id, creation_id)
        if not thread_id:
            raise RuntimeError("failed to publish thread")
    if not publish:
        sccc.insert_time(threads_user_id,creation_id,time,access_token)
        return creation_id
    return thread_id

def create_threads_container( access_token, threads_user_id, media_type,text=None, image_url=None, video_url=None, children_ids=None, is_carousel_item=False,):
    media_type = media_type.upper()
    valid_types = {"TEXT", "IMAGE", "VIDEO", "CAROUSEL"}
    if media_type not in valid_types:
        raise ValueError(f"media_type must be one of {valid_types}, got {media_type!r}")
    data = {"access_token": access_token}
    if media_type == "TEXT":
        if not text or not text.strip():
            raise ValueError("text is required for TEXT posts")
        data["media_type"] = "TEXT"
        data["text"] = text
    elif media_type == "IMAGE":
        if not image_url:
            raise ValueError("image_url is required for IMAGE posts")
        data["media_type"] = "IMAGE"
        data["image_url"] = image_url
        if text:
            data["text"] = text
        if is_carousel_item:
            data["is_carousel_item"] = "true"
    elif media_type == "VIDEO":
        if not video_url:
            raise ValueError("video_url is required for VIDEO posts")
        data["media_type"] = "VIDEO"
        data["video_url"] = video_url
        if text:
            data["text"] = text
        if is_carousel_item:
            data["is_carousel_item"] = "true"
    elif media_type == "CAROUSEL":
        if not children_ids or len(children_ids) < 2:
            raise ValueError("CAROUSEL requires at least 2 children_ids")
        if len(children_ids) > 20:
            raise ValueError("CAROUSEL supports at most 20 items")
        data["media_type"] = "CAROUSEL"
        data["children"] = ",".join(children_ids)
        if text:
            data["text"] = text
    resp = requests.post(f"{THREADS_API_BASE}/{threads_user_id}/threads", data=data)
    resp.raise_for_status()
    return resp.json().get("id")

def create_threads_carousel(access_token, threads_user_id, items, caption=None):
    if len(items) < 2 or len(items) > 20:
        raise ValueError("carousel requires between 2 and 20 items")
    child_ids = []
    for item in items:
        child_id = create_threads_container( access_token,threads_user_id, media_type=item["type"], image_url=item.get("url") if item["type"] == "IMAGE" else None, video_url=item.get("url") if item["type"] == "VIDEO" else None,is_carousel_item=True, )
        if not child_id:
            raise RuntimeError(f"failed to create carousel child for {item}")
        child_ids.append(child_id)
    return create_threads_container( access_token, threads_user_id, media_type="CAROUSEL", text=caption, children_ids=child_ids, )

def publish_threads_container(access_token, threads_user_id, creation_id):
    resp = requests.post( f"{THREADS_API_BASE}/{threads_user_id}/threads_publish", data={ "creation_id": creation_id, "access_token": access_token, }, )
    resp.raise_for_status()
    return resp.json().get("id")  

def publish_threads_container_sc(token,access_token, threads_user_id, creation_id):
    tokench = au.process(token=token)
    expire  = datetime.now(timezone.utc).isoformat()
    refresh_threads_token(expire, access_token, token, tokench["user_id"], threads_user_id)
    resp = requests.post( f"{THREADS_API_BASE}/{threads_user_id}/threads_publish", data={ "creation_id": creation_id, "access_token": access_token, }, )
    resp.raise_for_status()
    return resp.json().get("id")  

def wait_for_threads_container(access_token, creation_id, timeout=30, interval=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get( f"{THREADS_API_BASE}/{creation_id}", params={"fields": "status,error_message", "access_token": access_token}, )
        resp.raise_for_status()
        info = resp.json()
        status = info.get("status")
        if status == "FINISHED":
            return True
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(info.get("error_message") or f"container {status.lower()}")
        time.sleep(interval)
    raise TimeoutError("container did not finish processing in time")

def get_access_token_by_username(token, user_id, username):
    rows = dbimp.select_rows( token, THREADS_TABLE_NAME, select="Account_id,Access_token,Refresh_token,Token_expire",filters={"id": user_id, "Username": username} )
    if not rows:
        return None,None, (jsonify({"error": "no x account linked for this username"}), 404)
    row = rows[0]
    account_id = row["Account_id"]
    access_token = row["Access_token"]
    raw_expiry = row["Token_expire"]
    if not access_token or not raw_expiry:
        return None, None,(jsonify({"error": "missing access_token"}), 400)
    Token_expiry = datetime.fromisoformat(raw_expiry)
    if Token_expiry.tzinfo is None:
        Token_expiry = Token_expiry.replace(tzinfo=timezone.utc)
    if Token_expiry - datetime.now(timezone.utc) < timedelta(minutes=10):
        refresh_token = row.get("Refresh_token")
        if refresh_token:
            refreshed = refresh_threads_token(raw_expiry, access_token, token, user_id, username)
            if refreshed:
                access_token = refreshed
    return access_token,account_id, None

def _authenticate(request):
    token = request.form.get("token")
    username = request.form.get("username")
    text = request.form.get("text", "")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return None,None, None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200)
    user_id = tokench["user_id"]
    if not username:
        return None,None,None, (jsonify({"error": "username is required"}), 400)
    access_token,threads_user_id , err = get_access_token_by_username(tokench["token"], user_id, username)
    if err:
        return None, None, err
    return access_token, threads_user_id , text, None

@app.route("/auth/threads/login")
def threads_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return jsonify({"error": "invalid user id"}), 401
    state = serializer.dumps({"user_id": user_id})
    params = { "response_type": "code", "client_id": THREADS_APP_ID, "redirect_uri": THREADS_REDIRECT_URI, "scope": THREADS_SCOPES, "state": state, }
    auth_url = THREADS_AUTH_URL + "?" + urlencode(params)
    return redirect(auth_url)

@app.route("/auth/threads/callback")
def threads_callback():
    error = request.args.get("error")
    if error:
        return jsonify({"error": error, "description": request.args.get("error_description")}), 400
    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not state:
        return jsonify({"error": "missing state"}), 400
    try:
        state_data = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    user_id = state_data.get("user_id")
    if not user_id:
        return jsonify({"error": "invalid state payload"}), 400
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    if not check_user_id(token, user_id):
        return jsonify({"error": "invalid user id"}), 400
    short_resp = requests.post(THREADS_TOKEN_URL, data={ "client_id": THREADS_APP_ID, "client_secret": THREADS_APP_SECRET, "grant_type": "authorization_code", "redirect_uri": THREADS_REDIRECT_URI, "code": code, },).json()
    short_lived_token = short_resp.get("access_token")
    threads_user_id = short_resp.get("user_id")
    if not short_lived_token:
        return jsonify({"error": "token exchange failed", "details": short_resp}), 400
    exchange_resp = requests.get( THREADS_EXCHANGE_URL, params={ "grant_type": "th_exchange_token", "client_secret": THREADS_APP_SECRET, "access_token": short_lived_token,},).json()
    access_token = exchange_resp.get("access_token")
    seconds = exchange_resp.get("expires_in")
    if not access_token or not seconds:
        return jsonify({"error": "long-lived token exchange failed", "details": exchange_resp}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = requests.get( THREADS_ME_URL, params={"fields": "id,username", "access_token": access_token},).json() 
    username = about.get("username")
    account_id = about.get("id") or threads_user_id
    if not account_id:
        return jsonify({"error": "failed to fetch profile", "details": about}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    payload = { "user_id": user_id, "account_id": account_id, "username": username,"expire": expire, "timestamp": timestamp, "token": token, "access": access_token, }
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/threads/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/threads/callbackshi", methods=["POST"])
def threads_dataget():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    access_token = data.get("access")
    user_id = data.get("user_id")
    timestamp = data.get("timestamp")
    expirey = data.get("expire")
    username = data.get("username")
    account_id = data.get("account_id")
    if not all([token, access_token, user_id, timestamp, expirey, account_id]):
        return jsonify({"error": "missing required fields"}), 400
    try:
        datetime.fromisoformat(timestamp)
        datetime.fromisoformat(expirey)
    except ValueError:
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows( token, THREADS_TABLE_NAME, {"Access_token": access_token, "Timestamp": timestamp, "Token_expire": expirey, "Username": username,  "Account_id": account_id, }, filters={"id": user_id}, )
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200

@app.route("/threads/posts-with-metrics", methods=["GET"])
def posts_with_metrics():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    token = data.get("token")
    if not username or not token:
        return jsonify({"status": "failed"}), 400
    tokench = au.process(token=token)
    rows = dbimp.select_rows( tokench["token"], THREADS_TABLE_NAME, select="Access_token,Token_expire,Account_id", filters={"Username": username, "id": tokench["user_id"]}, )
    if not rows:
        return jsonify({"status": "failed"}), 400
    row = rows[0]
    access_token = row["Access_token"]
    Token_expire = row["Token_expire"]
    account_id = row["Account_id"]
    access_token = refresh_threads_token(Token_expire, access_token, tokench["token"], tokench["user_id"], username)
    try:
        media = get_all_threads_media(access_token, account_id)
        media_ids = [m["id"] for m in media]
        insights = get_threads_insights(access_token, media_ids)
        combined = []
        for m in media:
            mid = m["id"]
            combined.append({ "media_id": mid, "text": m.get("text"), "permalink": m.get("permalink"), "created_at": m.get("timestamp"), "media_url": m.get("media_url"), "metrics": insights.get(mid, {}),})
        return jsonify({"count": len(combined), "posts": combined}), 200
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "response": e.response.text}), e.response.status_code

@app.route("/post/threads/text", methods=["POST"])
def post_threads_text():
    data = request.get_json(silent=True) or {}
    publish = data.get("publish")
    timee_raw = data.get("time") or {}
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    access_token, threads_user_id, text, err = _authenticate(request)
    if err:
        return err
    try:
        thread_id = _publish_threads_post(publish_now,timee,access_token, threads_user_id, "TEXT", text=text)
    except (requests.HTTPError, ValueError, RuntimeError) as e:
        return jsonify({"error": "thread post failed", "detail": str(e)}), 400
    return jsonify({"success": True, "thread_id": thread_id}), 200

@app.route("/post/threads/image", methods=["POST"])
def post_threads_image():
    access_token, threads_user_id, text, err = _authenticate(request)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    image_url = data.get("image_url")
    publish = data.get("publish")
    timee_raw = data.get("time") or {}
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if not image_url:
        return jsonify({"error": "image_url is required"}), 400
    try:
        thread_id = _publish_threads_post(publish_now,timee, access_token, threads_user_id, "IMAGE", text=text, image_url=image_url)
    except (requests.HTTPError, ValueError, RuntimeError) as e:
        return jsonify({"error": "thread post failed", "detail": str(e)}), 400
    return jsonify({"success": True, "thread_id": thread_id}), 200

@app.route("/post/threads/video", methods=["POST"])
def post_threads_video():
    access_token, threads_user_id, text, err = _authenticate(request)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    video_url = data.get("video_url")
    publish = data.get("publish")
    timee_raw = data.get("time") or {}
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if not video_url:
        return jsonify({"error": "video_url is required"}), 400
    try:
        thread_id = _publish_threads_post( publish_now,timee,access_token, threads_user_id, "VIDEO", text=text, video_url=video_url )
    except (requests.HTTPError, ValueError, RuntimeError) as e:
        return jsonify({"error": "thread post failed", "detail": str(e)}), 400
    return jsonify({"success": True, "thread_id": thread_id}), 200

@app.route("/post/threads/carousel", methods=["POST"])
def post_threads_carousel():
    access_token, threads_user_id, text, err = _authenticate(request)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    items = data.get("items", [])
    publish = data.get("publish")
    timee_raw = data.get("time") or {}
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if not items:
        return jsonify({"error": "items is required for carousel posts"}), 400
    try:
        creation_id = create_threads_carousel(access_token, threads_user_id, items, caption=text)
        if not creation_id:
            raise RuntimeError("failed to create carousel container")
        is_published = wait_for_threads_container(access_token, creation_id)
        if not is_published:
            raise RuntimeError("carousel container failed to reach FINISHED state")
        if publish_now :
            thread_id = publish_threads_container(access_token, threads_user_id, creation_id)
            if not thread_id:
                raise RuntimeError("failed to publish carousel thread")
        if not publish:
            sccc.insert_time(threads_user_id,creation_id,time,access_token)
            return creation_id
    except (requests.HTTPError, ValueError, RuntimeError) as e:
        return jsonify({"error": "thread post failed", "detail": str(e)}), 400
    return jsonify({"success": True, "thread_id": thread_id}), 200