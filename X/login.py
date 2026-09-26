import database.UserDB as dbimp
import os
import Instagram.schedule_video as sccc
import base64
import json
import hashlib
import secrets
import requests
from urllib.parse import urlencode
from moviepy import VideoFileClip
import tempfile
from flask_cors import CORS
from flask import Flask, request, redirect, jsonify
import Drive.dep as dpp
from datetime import datetime, timezone, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import authnew as au
import limit as lmmm
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
X_CLIENT_ID = os.getenv("X_CLIENT_ID")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
X_REDIRECT_URI = os.getenv("X_REDIRECT_URI")
STATE_MAX_AGE = 600  # seconds
TWEET_URL = "https://api.twitter.com/2/tweets"
TABLE_NAME = "X"
SCOPE = "tweet.read users.read offline.access"
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
BASE_URL = ""
image_size = 5 * 1024 * 1024
video_size = 500 * 1024 * 1024
MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
duation = 139

def upload_image(access_token, file_bytes):
    resp = requests.post( MEDIA_UPLOAD_URL, headers={"Authorization": f"Bearer {access_token}"}, files={"media": file_bytes} ).json()
    return resp.get("media_id_string")

def refresh_x_token11( account_id):
    rows = dbimp.select_rows_web(TABLE_NAME,select="Refresh_token",filters={"Account_id": account_id})
    if not rows:
        return None
    refresh_token = rows[0]["Refresh_token"]
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": X_CLIENT_ID}, ).json()
    new_access = resp.get("access_token")
    new_refresh = resp.get("refresh_token", refresh_token)
    seconds = resp.get("expires_in")
    if not new_access or not seconds:
        return None
    expire_time = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    dbimp.update_rows_web( TABLE_NAME, {"Access_token": new_access, "Refresh_token": new_refresh, "Token_expire": expire_time}, filters={"Account_id": account_id})
    return new_access

def parse_aware_timestamp(timestamp: str) -> datetime:
    dt = datetime.fromisoformat(timestamp)
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp '{timestamp}' has no timezone offset")
    return dt.astimezone(timezone.utc)

def get_tweet_metrics(x_user_id, tweet_id, access_token):
    access_token = refresh_x_token11(x_user_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    tweet_url = f"https://api.x.com/2/tweets/{tweet_id}"
    tweet_params = {"tweet.fields": "created_at,public_metrics"}
    resp = requests.get(tweet_url, headers=headers, params=tweet_params, timeout=10)
    resp.raise_for_status()
    t = resp.json()["data"]
    pm = t.get("public_metrics", {})
    return f"{t.get('id')},{t.get('created_at')},{pm.get('impression_count')},{pm.get('like_count')},{pm.get('retweet_count')},{pm.get('reply_count')},{pm.get('bookmark_count')}"

def upload_video(access_token, file_bytes, mime_type="video/mp4"):
    total_bytes = len(file_bytes)
    headers = {"Authorization": f"Bearer {access_token}"}
    init_resp = requests.post(MEDIA_UPLOAD_URL, headers=headers, data={ "command": "INIT", "media_type": mime_type, "total_bytes": total_bytes, "media_category": "tweet_video" }).json()
    media_id = init_resp.get("media_id_string")
    if not media_id:
        return None
    chunk_size = 4 * 1024 * 1024
    for i, offset in enumerate(range(0, total_bytes, chunk_size)):
        chunk = file_bytes[offset:offset + chunk_size]
        requests.post(MEDIA_UPLOAD_URL, headers=headers, data={ "command": "APPEND", "media_id": media_id, "segment_index": i }, files={"media": chunk})
    fin_resp = requests.post(MEDIA_UPLOAD_URL, headers=headers, data={ "command": "FINALIZE", "media_id": media_id }).json()
    processing = fin_resp.get("processing_info")
    while processing and processing.get("state") in ("pending", "in_progress"):
        wait = processing.get("check_after_secs", 1)
        import time; time.sleep(wait)
        status_resp = requests.get(MEDIA_UPLOAD_URL, headers=headers, params={ "command": "STATUS", "media_id": media_id }).json()
        processing = status_resp.get("processing_info")
        if processing and processing.get("state") == "failed":
            return None
    return media_id

def get_video_duration(file_path):
    with VideoFileClip(file_path) as video:
        return video.duration

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

def create_tweet_with_media(access_token, text, media_ids):
    resp = requests.post( "https://api.twitter.com/2/tweets", headers={ "Authorization": f"Bearer {access_token}", "Content-Type": "application/json", }, json={ "text": text or "", "media": {"media_ids": media_ids}, }, )
    if resp.status_code not in (200, 201):
        return None, resp.text
    return resp.json().get("data", {}).get("id"), None

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

def get_last_n_tweet_ids(user_id: str, access_token: str, n: int = 20) -> list[str]:
    resp = requests.get( f"https://api.twitter.com/2/users/{user_id}/tweets", headers={"Authorization": f"Bearer {access_token}"}, params={"max_results": min(n, 100),  
            "tweet.fields": "created_at", }, )
    resp.raise_for_status()
    data = resp.json()
    return [tweet["id"] for tweet in data.get("data", [])]

def _post_tweet(access_token, text, media_ids=None):
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    resp = requests.post( TWEET_URL, headers={ "Authorization": f"Bearer {access_token}", "Content-Type": "application/json",}, json=payload,    )
    data = resp.json()
    if resp.status_code >= 400:
        return None
    tweet_id = data.get("data", {}).get("id")
    if not tweet_id:
        return None
    return tweet_id

def post_later(tpyee, Account_id, text, text2 ,file_ids=None):
    Account_ids = json.loads(Account_id[0])
    rows = dbimp.select_rows_web("X", select="id", filters={"Account_id": Account_ids})
    if not rows:
        return False
    user_id = rows[0]["id"]
    service = dpp.get_drive_service(user_id)
    results = {}  
    if tpyee in ("photo_tweet_later", "video_tweet_later"):
        if not file_ids:
            return False
        file_ids = json.loads(file_ids[0])
        media_ids_by_account = {acc_id: [] for acc_id in Account_ids}
        tokens_by_account = {acc_id: refresh_x_token11(acc_id) for acc_id in Account_ids}
        for drive_file_id in file_ids:
            tmp_path, mimetype, name = dpp.download_drive_file_to_temp(service, drive_file_id)
            try:
                for acc_id in Account_ids:
                    access_token = tokens_by_account[acc_id]
                    with open(tmp_path, "rb") as file:
                        if tpyee == "photo_later":
                            media_id = upload_image(access_token, file.read())
                            if text2 == "true" :
                                xcccc(acc_id, access_token, tweet_id, "photo_tweet")
                        else: 
                            media_id = upload_video(access_token, file.read())
                            if text2 == "true" :
                                xcccc(acc_id, access_token, tweet_id, "photo_tweet")
                    media_ids_by_account[acc_id].append(media_id)
            finally:
                os.remove(tmp_path)
        for acc_id, media_ids in media_ids_by_account.items():
            access_token = tokens_by_account[acc_id]
            tweet_id, post_err = create_tweet_with_media(access_token, text, media_ids)
            results[acc_id] = (tweet_id, post_err)
    elif tpyee == "tweet_later":
        for acc_id in Account_ids:
            access_token = refresh_x_token11(acc_id)
            tweet_id = _post_tweet(access_token, text)
            if text2 == "true" :
                xcccc(acc_id, access_token, tweet_id, "tweet")
            results[acc_id] = (tweet_id, None)
    else:
        return False
    return results

def fetch_tweet_metrics(tweet_ids: list[str], access_token: str) -> list[dict]:
    resp = requests.get( "https://api.twitter.com/2/tweets",  headers={"Authorization": f"Bearer {access_token}"}, params={ "ids": ",".join(tweet_ids), "tweet.fields": "created_at,public_metrics,non_public_metrics,organic_metrics",   }, )
    resp.raise_for_status()
    data = resp.json()
    results = []
    for tweet in data.get("data", []):
        public = tweet.get("public_metrics", {})
        organic = tweet.get("organic_metrics", {})
        results.append( { "tweet_id": tweet["id"], "created_at": tweet.get("created_at"),"retweets": public.get("retweet_count", 0), "replies": public.get("reply_count", 0), "likes": public.get("like_count", 0),"quotes": public.get("quote_count", 0),"impressions": organic.get("impression_count", 0),"profile_clicks": organic.get("user_profile_clicks", 0), "url_clicks": organic.get("url_link_clicks", 0), })
    return results

def get_access_token_by_username(token, user_id, username):
    rows = dbimp.select_rows( token, TABLE_NAME, select="Account_id,Access_token,Refresh_token,Token_expire",filters={"id": user_id, "Username": username} )
    if not rows:
        return None, (jsonify({"error": "no x account linked for this username"}), 404), None
    row = rows[0]
    account_id = row["Account_id"]
    access_token = row["Access_token"]
    raw_expiry = row["Token_expire"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400), account_id
    Token_expiry = datetime.fromisoformat(raw_expiry)
    if Token_expiry.tzinfo is None:
        Token_expiry = Token_expiry.replace(tzinfo=timezone.utc)
    if Token_expiry - datetime.now(timezone.utc) < timedelta(minutes=10):
        refresh_token = row.get("Refresh_token")
        if refresh_token:
            refreshed = refresh_x_token(token, user_id, account_id, refresh_token)
            if refreshed:
                access_token = refreshed
    return access_token, None ,account_id

def xcccc(user_id,access_token,media_id,typee):
    for i in range(7): 
        timesss = (datetime.now(timezone.utc) + timedelta(days=(i))).isoformat()
        sccc.insert__story1(user_id, timesss, access_token,media_id,typee)

def _authenticate(data,tokench):
    usernames = data.get("username")
    text = data.get("text", "")
    access_tokens = []
    account_ids = []
    analysis = data.get("anal",False)
    anal = str(analysis).strip().lower() == "true"
    if not isinstance(usernames, list):
            return None , (jsonify({"error": "username is not the list"}),400) , None , None 
    now = datetime.now(timezone.utc).isoformat()
    if not anal :
        counntttt = len(username) * 2 
        if not lmmm.checkk(tokench["token"],user_id,now, counntttt):
            return jsonify({"error": "limit has been reached"}) ,400
    else :
        counntttt = len(username) * 2 + len(username) * 14
        if not lmmm.checkk(tokench["token"],user_id,now, counntttt):
            return jsonify({"error": "limit has been reached"}) ,400
    if not tokench["status"]:
        return None, None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200) , None
    user_id = tokench["user_id"]
    if not usernames:
        return None, None, (jsonify({"error": "username is required"}), 400) , None
    for username in usernames :
        access_token, err, account_id = get_access_token_by_username(tokench["token"], user_id, username)
        access_tokens.append(access_token)
        account_ids.append(account_id)
    if err:
        return None, None, err ,None
    return access_tokens, text, None , account_ids

def refresh_x_token(token,user_id, account_id, refresh_token):
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": X_CLIENT_ID}, ).json()
    new_access = resp.get("access_token")
    new_refresh = resp.get("refresh_token", refresh_token)
    seconds = resp.get("expires_in")
    if not new_access or not seconds:
        return None
    expire_time = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    dbimp.update_rows(token, TABLE_NAME, {"Access_token": new_access, "Refresh_token": new_refresh, "Token_expire": expire_time}, filters={"Account_id": account_id,"id":user_id})
    return new_access

@app.route("/auth/x/login")
def x_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return jsonify({"error": "invalid user id"}), 401
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode( hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    state = serializer.dumps({"user_id": user_id, "code_verifier": code_verifier})
    params = {"response_type": "code", "client_id": X_CLIENT_ID, "redirect_uri": X_REDIRECT_URI, "scope": SCOPE, "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",    }
    auth_url = AUTH_URL + "?" + urlencode(params)
    return redirect(auth_url)

@app.route("/auth/x/callback")
def x_callback():
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
    code_verifier = state_data.get("code_verifier")
    if not user_id or not code_verifier:
        return jsonify({"error": "invalid state payload"}), 400
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    if not check_user_id(token, user_id):
        return jsonify({"error": "invalid user id"}), 400
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    token_resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={ "grant_type": "authorization_code", "code": code, "redirect_uri": X_REDIRECT_URI, "code_verifier": code_verifier, "client_id": X_CLIENT_ID,  }, ).json()
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    seconds = token_resp.get("expires_in")
    if not access_token or not seconds:
        return jsonify({"error": "token exchange failed", "details": token_resp}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = requests.get( "https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {access_token}"}, params={"user.fields": "username"}, ).json()
    data = about.get("data", {})
    username = data.get("username")
    account_id = data.get("id")
    if not account_id:
        return jsonify({"error": "failed to fetch profile", "details": about}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    payload = { "user_id": user_id, "account_id": account_id, "username": username, "expire": expire, "timestamp": timestamp, "token": token, "access": access_token, "refresh": refresh_token, }
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/x/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/x/callbackshi", methods=["POST"])
def x_dataget():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    access_token = data.get("access")
    refresh_token = data.get("refresh")
    user_id = data.get("user_id")
    timestamp = data.get("timestamp")
    expirey = data.get("expire")
    username = data.get("username")
    account_id = data.get("account_id")
    if not all([token, access_token, user_id, timestamp, expirey, username, account_id]):
        return jsonify({"error": "missing required fields"}), 400
    try:
        timestamp = parse_aware_timestamp(timestamp)
        expirey = parse_aware_timestamp(expirey)
    except (ValueError, TypeError):
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows( token, TABLE_NAME, { "Access_token": access_token, "Refresh_token": refresh_token, "Timestamp": timestamp, "Token_expire": expirey, "Username": username, "Account_id": account_id, }, filters={"id": user_id}, ) 
        dbimp.update_rows(token,"users",{"X":True},{"user_id":user_id})
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200

@app.route("/post/x/text", methods=["POST"])
def post_to_x_text():
    data = request.get_json(silent = True) or {}
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data,tokench)
    if not text:
        return jsonify({"error": "text is required"}), 400
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    publish_now = str(publish).strip().lower() == "true"
    analysis = data.get("anal",False)
    anal = str(analysis).strip().lower() == "true"
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=72)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if err:
        return err
    if not publish_now :
        list_account_ids = json.dumps(account_id)
        if anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="tweet_later", text1=text, text2="true" , text3="", media_id="")
        elif not anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="tweet_later", text1=text, text2="" , text3="", media_id="")
    for acccount ,acesss in zip(account_id,access_token) :
        tweet_id = _post_tweet(acesss, text)
        if not tweet_id:
            continue
        if anal :
            xcccc(acccount, acesss, tweet_id, "tweet")
    return jsonify({"success": True, "post_id": tweet_id}), 200

@app.route("/post/x/photo", methods=["POST"])
def post_to_x_photo():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data, tokench)
    if err:
        return err
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "at least one image file is required"}), 400
    if len(files) > 4:
        return jsonify({"error": "maximum 4 photos allowed per post"}), 400
    non_images = [f for f in files if not (f.mimetype or "").startswith("image/")]
    if non_images:
        return jsonify({"error": "unsupported file type in upload"}), 400
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    analysis = data.get("anal",False)
    publish_now = str(publish).strip().lower() == "true"
    anal = str(analysis).strip().lower() == "true"
    if not publish_now:
        if timee is None:
            return jsonify({"error": "invalid time format"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(seconds=180)
        up = now + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    media_ids = []
    drvie_id = []
    service = dpp.get_drive_service(tokench["user_id"]) if not publish_now else None
    for f in files:
        tmp_path = None
        media_id = None
        try:
            suffix = os.path.splitext(f.filename or "")[1] or ".jpg"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = tmp.name
                f.save(tmp_path)
            file_size_bytes = os.path.getsize(tmp_path)
            if file_size_bytes > image_size:
                return jsonify({"error": f"image {f.filename} exceeds max size"}), 400
            if not publish_now:
                drive_file_id, drive_err = dpp.upload_path_to_drive( service, tmp_path, os.path.basename(tmp_path), f.mimetype )
                if drive_err:
                    return jsonify({"error": f"failed to save {f.filename} for scheduling"}), 400
                drvie_id.append(drive_file_id)
                continue
            with open(tmp_path, "rb") as fh:
                bytesss = fh.read()
            media_id = upload_image(access_token[0], bytesss)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        if not media_id:
            return jsonify({"error": f"image upload failed for {f.filename}"}), 400
        media_ids.append(media_id)
    if not publish_now:
        list_account_ids = json.dumps(account_id)
        drvie_ids = json.dumps(drvie_id)
        if anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="photo_tweet_later", text1=text, text2="true", text3="", media_id=drvie_ids, )
        elif not anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="photo_tweet_later", text1=text, text2="", text3="", media_id=drvie_ids, )
        return jsonify({"success": True}), 200
    results = []
    for acccount, acesss in zip(account_id, access_token):
        tweet_id, post_err = create_tweet_with_media(acesss, text, media_ids)
        if post_err:
            results.append({"account": acccount, "success": False, "error": post_err})
            continue
        if anal :
            xcccc(acccount, acesss, tweet_id, "photo_tweet")
        results.append({"account": acccount, "success": True, "post_id": tweet_id})
    return jsonify({"success": True, "results": results, "media_ids": media_ids}), 200

@app.route("/post/x/video", methods=["POST"])
def post_to_x_video():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data, tokench)
    if err:
        return err
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "a video file is required"}), 400
    if len(files) > 1:
        return jsonify({"error": "only one video allowed per post"}), 400
    f = files[0]
    if not (f.mimetype or "").startswith("video/"):
        return jsonify({"error": "unsupported file type in upload"}), 400
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    publish_now = str(publish).strip().lower() == "true"
    analysis = data.get("anal",False)
    anal = str(analysis).strip().lower() == "true"
    if not publish_now:
        if timee is None:
            return jsonify({"error": "invalid time format"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(seconds=180)
        up = now + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    tmp_path = None
    media_id = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        file_size_bytes = os.path.getsize(tmp_path)
        if get_video_duration(tmp_path) > duation or file_size_bytes > video_size:
            return jsonify({"error": "video exceeds allowed duration or size"}), 400
        if not publish_now:
            service = dpp.get_drive_service(tokench["user_id"])
            drive_file_id, drive_err = dpp.upload_path_to_drive( service, tmp_path, os.path.basename(tmp_path), "video/mp4" )
            if drive_err:
                return jsonify({"error": "failed to save video for scheduling"}), 400
            list_account_ids = json.dumps(account_id)
        if anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="video_later", text1=text, text2="true", text3="", media_id=drive_file_id, )
        elif not anal :
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="video_later", text1=text, text2="", text3="", media_id=drive_file_id, )
            return jsonify({"success": True}), 200
        with open(tmp_path, "rb") as file:
            bytesss = file.read()
        media_id = upload_video(access_token[0], bytesss, f.mimetype)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    if not media_id:
        return jsonify({"error": "video upload failed"}), 400
    results = []
    for acccount, acesss in zip(account_id, access_token):
        tweet_id, post_err = create_tweet_with_media(acesss, text, [media_id])
        if post_err:
            results.append({"account": acccount, "success": False, "error": post_err})
            continue
        if anal :
            xcccc(acccount, acesss, tweet_id, "video_tweet")
        results.append({"account": acccount, "success": True, "post_id": tweet_id})
    return jsonify({"success": True, "results": results, "media_ids": [media_id]}), 200

@app.route("/x/analytics/posts", methods=["GET"])
@limiter.limit("10 per minute")
def fetch_x_post_analytics():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    username = data.get("name")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    access_token, err , account_id = get_access_token_by_username(tokench["token"], tokench["user_id"], username)
    if err:
        return err
    tweet_ids = get_last_n_tweet_ids(account_id, access_token, n=20)
    if not tweet_ids:
        return {"error": "no posts found"}, 404
    results = fetch_tweet_metrics(tweet_ids, access_token)
    return results
