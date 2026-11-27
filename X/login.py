import database.UserDB as dbimp
import os
import Instagram.schedule_video as sccc
import base64
import json
import hashlib
import secrets
import requests
from urllib.parse import urlencode
import  logging
from moviepy import VideoFileClip
import tempfile
from flask_cors import CORS
from flask import Flask, redirect ,request,  jsonify
import Drive.dep as dpp
from datetime import datetime, timezone, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from concurrent.futures import ThreadPoolExecutor
import authnew as au
import limit as lmmm
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("x_api")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 65 * 1024 * 1024  # means 65 MB is the maximum request size it could have 
serializer = URLSafeTimedSerializer(app.secret_key)
executor = ThreadPoolExecutor(max_workers=2)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
X_CLIENT_ID = os.getenv("x_client_id")
X_CLIENT_SECRET = os.getenv("x_client_secret")
X_REDIRECT_URI = os.getenv("x_redirect_uri")
BASE_URL = os.getenv("BASE_URL")
STATE_MAX_AGE = 600  # seconds
TWEET_URL = "https://api.twitter.com/2/tweets"
TABLE_NAME = "x"
SCOPE = "tweet.read users.read offline.access"
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
image_size = 7 * 1024 * 1024
video_size = 60 * 1024 * 1024
MEDIA_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
duation = 139

def upload_image(access_token, file_bytes):
    resp = requests.post( MEDIA_UPLOAD_URL, headers={"Authorization": f"Bearer {access_token}"}, files={"media": file_bytes} ).json()
    media_id = resp.get("media_id_string")
    if not media_id:
        logger.error(f"image upload failed: {resp}")
    return media_id

def refresh_x_token11( account_id):
    rows = dbimp.select_rows(TABLE_NAME,select="refresh_token",filters={"account_id": account_id})
    if not rows:
        logger.error(f"no x row found for the account {account_id}")
        return None
    refresh_token = rows[0]["refresh_token"]
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": X_CLIENT_ID}, ).json()
    new_access = resp.get("access_token")
    new_refresh = resp.get("refresh_token", refresh_token)
    seconds = resp.get("expires_in")
    if not new_access or not seconds:
        logger.error(f"token refresh failed for the account {account_id}: {resp}")
        return None
    expire_time = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    dbimp.update_rows( TABLE_NAME, {"access_token": new_access, "refresh_token": new_refresh, "token_expire": expire_time}, filters={"account_id": account_id})
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
        logger.error(f"video INIT failed: {init_resp}")
        return None
    chunk_size = 4 * 1024 * 1024
    for i, offset in enumerate(range(0, total_bytes, chunk_size)):
        chunk = file_bytes[offset:offset + chunk_size]
        app_resp = requests.post(MEDIA_UPLOAD_URL, headers=headers, data={ "command": "APPEND", "media_id": media_id, "segment_index": i }, files={"media": chunk})
        if app_resp.status_code >= 400:
            logger.error(f"video APPEND failed for the chunk {i} ({app_resp.status_code}): {app_resp.text}")
            return None
    fin_resp = requests.post(MEDIA_UPLOAD_URL, headers=headers, data={ "command": "FINALIZE", "media_id": media_id }).json()
    processing = fin_resp.get("processing_info")
    while processing and processing.get("state") in ("pending", "in_progress"):
        wait = processing.get("check_after_secs", 1)
        import time; time.sleep(wait)
        status_resp = requests.get(MEDIA_UPLOAD_URL, headers=headers, params={ "command": "STATUS", "media_id": media_id }).json()
        processing = status_resp.get("processing_info")
        if processing and processing.get("state") == "failed":
            logger.error(f"video processing failed: {processing}")
            return None
    return media_id

def get_video_duration(file_path):
    with VideoFileClip(file_path) as video:
        return video.duration

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
    resp = requests.get( f"https://api.twitter.com/2/users/{user_id}/tweets", headers={"Authorization": f"Bearer {access_token}"}, params={"max_results": max(5, min(n, 100)),  
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
        logger.error(f"post tweet failed ({resp.status_code}): {data}")
        return None
    tweet_id = data.get("data", {}).get("id")
    if not tweet_id:
        logger.error(f"post tweet returned no id: {data}")
        return None
    return tweet_id

def post_later(tpyee, account_id, text, text2 ,file_ids=None):
    account_ids = json.loads(account_id[0])
    rows = dbimp.select_rows(TABLE_NAME, select="id", filters={"account_id": account_ids})
    if not rows:
        logger.error(f"post_later found no x rows for {account_ids}")
        return False
    user_id = rows[0]["id"]
    service = dpp.get_drive_service(user_id)
    results = {}  
    drive_ids = []
    tmp_paths = []
    try:
        if tpyee in ("photo_tweet_later", "video_tweet_later"):
            if not file_ids:
                logger.error(f"post_later got no files for {tpyee}")
                return False
            drive_ids = json.loads(file_ids[0])
            typee = "photo_tweet" if tpyee == "photo_tweet_later" else "video_tweet"
            for drive_file_id in drive_ids:
                tmp_path, mimetype, name = dpp.download_drive_file_to_temp(service, drive_file_id)
                tmp_paths.append(tmp_path)
            for acc_id in account_ids:
                try:
                    access_token = refresh_x_token11(acc_id)
                    if not access_token:
                        results[acc_id] = (None, "token refresh failed")
                        continue
                    media_ids = []
                    for tmp_path in tmp_paths:
                        with open(tmp_path, "rb") as file:
                            bytesss = file.read()
                        media_id = upload_image(access_token, bytesss) if typee == "photo_tweet" else upload_video(access_token, bytesss)
                        if not media_id:
                            raise RuntimeError(f"media upload failed for {os.path.basename(tmp_path)}")
                        media_ids.append(media_id)
                    tweet_id, post_err = create_tweet_with_media(access_token, text, media_ids)
                    if not tweet_id:
                        logger.error(f"post_later failed to post for the account {acc_id}: {post_err}")
                        results[acc_id] = (None, post_err)
                        continue
                    if text2 == "true" :
                        tweet_query(acc_id, access_token, tweet_id, typee)
                    results[acc_id] = (tweet_id, None)
                except Exception as e:
                    logger.exception(f"post_later crashed for the account {acc_id}")
                    results[acc_id] = (None, str(e))
        elif tpyee == "tweet_later":
            for acc_id in account_ids:
                access_token = refresh_x_token11(acc_id)
                if not access_token:
                    results[acc_id] = (None, "token refresh failed")
                    continue
                tweet_id = _post_tweet(access_token, text)
                if not tweet_id:
                    results[acc_id] = (None, "post failed")
                    continue
                if text2 == "true" :
                    tweet_query(acc_id, access_token, tweet_id, "tweet")
                results[acc_id] = (tweet_id, None)
        else:
            logger.error(f"post_later got an unknown type {tpyee}")
            return False
        if drive_ids and all(r[0] for r in results.values()):
            for drive_file_id in drive_ids:
                deleteFile(service, drive_file_id)
        return results
    except Exception:
        logger.exception(f"post_later failed for {tpyee}")
        return False
    finally:
        for p in tmp_paths:
            if os.path.exists(p):
                os.remove(p)

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
    rows = dbimp.select_rows( TABLE_NAME, select="account_id,access_token,refresh_token,token_expire",filters={"id": user_id, "username": username} )
    if not rows:
        return None, (jsonify({"error": f"no x account linked for {username}"}), 404), None
    row = rows[0]
    account_id = row["account_id"]
    access_token = row["access_token"]
    token_expiry = row["token_expire"]
    if not access_token or not token_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400), account_id
    if isinstance(token_expiry, str):
        token_expiry = datetime.fromisoformat(token_expiry)
    if token_expiry.tzinfo is None:
        token_expiry = token_expiry.replace(tzinfo=timezone.utc)
    if token_expiry - datetime.now(timezone.utc) < timedelta(minutes=10):
        refresh_token = row.get("refresh_token")
        if refresh_token:
            refreshed = refresh_x_token(token, user_id, account_id, refresh_token)
            if refreshed:
                access_token = refreshed
    return access_token, None ,account_id

def tweet_query(user_id,access_token,media_id,typee):
    for i in range(7): 
        timesss = (datetime.now(timezone.utc) + timedelta(days=(i))).isoformat()
        sccc.insert__story1(user_id, timesss, access_token,media_id,typee)

def _authenticate(data,tokench):
    if not tokench["status"]:
        return None, None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200) , None
    user_id = tokench["user_id"]
    usernames = data.getlist("username") if hasattr(data, "getlist") else data.get("username")
    text = data.get("text", "")
    access_tokens = []
    account_ids = []
    analysis = data.get("analytics",False)
    analytic_bool = str(analysis).strip().lower() == "true"
    if not isinstance(usernames, list) or not usernames:
        return None, None, (jsonify({"error": "username must be a non empty list"}), 400) , None
    now = datetime.now(timezone.utc).isoformat()
    if not analytic_bool :
        counntttt = len(usernames) * 2 
    else :
        counntttt = len(usernames) * 16 # (2 for the post and 14 for the analytics)
    if not lmmm.checkk(tokench["token"],user_id,now, counntttt):
        return None, None, (jsonify({"error": "limit has been reached"}), 429) , None
    for username in usernames :
        access_token, err, account_id = get_access_token_by_username(tokench["token"], user_id, username)
        if err:
            return None, None, err ,None
        access_tokens.append(access_token)
        account_ids.append(account_id)
    return access_tokens, text, None , account_ids

def refresh_x_token(token,user_id, account_id, refresh_token):
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": X_CLIENT_ID}, ).json()
    new_access = resp.get("access_token")
    new_refresh = resp.get("refresh_token", refresh_token)
    seconds = resp.get("expires_in")
    if not new_access or not seconds:
        logger.error(f"token refresh failed for the account {account_id}: {resp}")
        return None
    expire_time = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    dbimp.update_rows( TABLE_NAME, {"access_token": new_access, "refresh_token": new_refresh, "token_expire": expire_time}, filters={"account_id": account_id,"id":user_id})
    return new_access

@app.route("/auth/login",methods=["POST"])
def x_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode( hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
    state = serializer.dumps({"user_id": user_id, "code_verifier": code_verifier})
    params = {"response_type": "code", "client_id": X_CLIENT_ID, "redirect_uri": X_REDIRECT_URI, "scope": SCOPE, "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",    }
    auth_url = AUTH_URL + "?" + urlencode(params)
    return jsonify({"auth_url": auth_url}) , 200

def clean(value):
    return (value or "").split("#", 1)[0].strip()

def store_x_account(token, user_id, access_token, username, account_id, timestamp, expire,refresh_token):
    dbimp.update_rows( TABLE_NAME, { "access_token": access_token, "refresh_token": refresh_token, "timestamp": timestamp, "token_expire": expire, "username": username, "account_id": account_id, }, filters={"id": user_id}, ) 
    dbimp.update_rows("users",{"x":True},{"user_id":user_id})

@app.route("/auth/callback", methods=["GET"])
def instagram_callback():
    code = clean(request.args.get("code"))
    state = clean(request.args.get("state"))
    if not code or not state:
        return jsonify({"error": "missing code or state"}), 400
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
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    token_resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={ "grant_type": "authorization_code", "code": code, "redirect_uri": X_REDIRECT_URI, "code_verifier": code_verifier, "client_id": X_CLIENT_ID,  }, ).json()
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    seconds = token_resp.get("expires_in")
    if not access_token or not seconds:
        logger.error(f"x token exchange failed for the user {user_id}: {token_resp}")
        return jsonify({"error": "token exchange failed"}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = requests.get( "https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {access_token}"}, params={"user.fields": "username"}, ).json()
    data = about.get("data", {})
    username = data.get("username")
    account_id = data.get("id")
    if not account_id:
        logger.error(f"x profile fetch failed for the user {user_id}: {about}")
        return jsonify({"error": "failed to fetch profile"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    try:
        store_x_account(token, user_id, access_token, username, account_id, timestamp, expire,refresh_token)
    except Exception as e:
        logger.exception(f"failed to store the x account for the user {user_id}")
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return redirect(homepage, code=302)

def save_files(files):
    saved = []
    for f in files:
        suffix = os.path.splitext(f.filename or "")[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            f.save(tmp.name)
            saved.append((tmp.name, f.mimetype))
    return saved

def remove_files(files):
    for tmp_path, mimetype in files:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.route("/post/text", methods=["POST"])
@limiter.limit("3 per minute")
def post_to_x_text():
    data = request.get_json(silent = True) or {}
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data,tokench)
    if err:
        return err
    if not text:
        return jsonify({"error": "text is required"}), 400
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    publish_now = str(publish).strip().lower() == "true"
    analysis = data.get("analytics",False)
    analytic_bool = str(analysis).strip().lower() == "true"
    if not publish_now :
        if timee is None:
            return jsonify({"error": "invalid or missing date/time"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(seconds=180)
        up = now + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
        try :
            list_account_ids = json.dumps(account_id)
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="tweet_later", text1=text,text2="true" if analytic_bool else "" , text3="", media_id="")
        except Exception:
            logger.exception(f"failed to schedule the tweet for {account_id} from the user {tokench['user_id']}")
            return jsonify({"error": "failed to schedule"}), 500
        return jsonify({"success": "added"}), 200
    try :
        executor.submit(tweet_job,text , account_id,access_token,analytic_bool)
    except Exception:
        logger.exception(f"failed to queue the tweet for {account_id} from the user {tokench['user_id']}")
        return jsonify({"error": "could not queue the job, try again"}), 503
    return jsonify({"success": "added"}), 200

def tweet_job(text,account_id,access_token,analytic_bool):
    for acccount ,acesss in zip(account_id,access_token) :
        try :
            tweet_id = _post_tweet(acesss, text)
            if not tweet_id:
                logger.error(f"failed to post tweet for the {acccount} ")
                continue
            if analytic_bool :
                tweet_query(acccount, acesss, tweet_id, "tweet")
        except Exception:
            logger.exception(f"tweet_job crashed for the {acccount}")

@app.route("/post/photo", methods=["POST"])
@limiter.limit("3 per minute")
def post_to_x_photo():
    data = request.form
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data, tokench)
    if err:
        return err
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    analysis = data.get("analytics",False)
    publish_now = str(publish).strip().lower() == "true"
    analytic_bool = str(analysis).strip().lower() == "true"
    if not publish_now:
        if timee is None:
            return jsonify({"error": "invalid time format"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(seconds=180)
        up = now + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    files = save_files(request.files.getlist("file"))  # the request is gone once we return, so only the saving happens here
    try :
        executor.submit(tweet_photo,tokench , publish_now , access_token,account_id , timee , text, analytic_bool , files)
    except Exception:
        logger.exception(f"failed to queue the photo post for {account_id} from the user {tokench['user_id']}")
        remove_files(files)
        return jsonify({"error": "could not queue the job, try again"}), 503
    return jsonify({"success": "added"}), 200

def tweet_photo(tokench , publish_now , access_token,account_id , timee , text, analytic_bool , files):
    try:
        if not files:
            logger.error(f"no files in the request to post in {account_id}")
            return
        if len(files) > 4:
            logger.error(f"too many files in the request to post in {account_id}")
            return
        non_images = [p for p, m in files if not (m or "").startswith("image/")]
        if non_images:
            logger.error(f"non image in the request to post in {account_id}")
            return
        for tmp_path, mimetype in files:
            if os.path.getsize(tmp_path) > image_size:
                logger.error(f"file_size of {os.path.basename(tmp_path)} exceeds the limit for {account_id}")
                return
        if not publish_now:
            drvie_id = []
            service = dpp.get_drive_service(tokench["user_id"]) 
            for tmp_path, mimetype in files:
                drive_file_id, drive_err = dpp.upload_path_to_drive( service, tmp_path, os.path.basename(tmp_path), mimetype )
                if drive_err:
                    logger.error(f"failed to save {os.path.basename(tmp_path)} for scheduling, because {drive_err}")
                    return
                drvie_id.append(drive_file_id)
            list_account_ids = json.dumps(account_id)
            drvie_ids = json.dumps(drvie_id)
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="photo_tweet_later", text1=text, text2="true" if analytic_bool else "", text3="", media_id=drvie_ids, )
            return
        for acccount, acesss in zip(account_id, access_token):
            media_ids = []
            for tmp_path, mimetype in files:  # media ids belong to the account that uploaded them
                with open(tmp_path, "rb") as fh:
                    bytesss = fh.read()
                media_id = upload_image(acesss, bytesss)
                if not media_id:
                    logger.error(f"unable to upload the photo for the account {acccount} for the user {tokench['user_id']}")
                    break
                media_ids.append(media_id)
            if len(media_ids) != len(files):
                continue
            tweet_id, post_err = create_tweet_with_media(acesss, text, media_ids)
            if post_err:
                logger.error(f"unable to post photo reson : {post_err},for the account {acccount} for the user {tokench['user_id']}")
                continue
            if analytic_bool :
                tweet_query(acccount, acesss, tweet_id, "photo_tweet")
    except Exception:
        logger.exception(f"tweet_photo crashed for {account_id} from the user {tokench['user_id']}")
    finally:
        remove_files(files)

@app.route("/post/video", methods=["POST"])
@limiter.limit("3 per minute")
def post_to_x_video():
    data = request.form
    token = data.get("token")
    tokench = au.process(token=token)
    access_token, text, err, account_id = _authenticate(data, tokench)
    if err:
        return err
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    publish_now = str(publish).strip().lower() == "true"
    analysis = data.get("analytics",False)
    analytic_bool = str(analysis).strip().lower() == "true"
    if not publish_now:
        if timee is None:
            return jsonify({"error": "invalid time format"}), 400
        now = datetime.now(timezone.utc)
        lb = now + timedelta(seconds=180)
        up = now + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    files = save_files(request.files.getlist("file"))  # the request is gone once we return, so only the saving happens here
    try :
        executor.submit(upload_video_job,publish_now,tokench,text,analytic_bool,timee,access_token,account_id,files)
    except Exception:
        logger.exception(f"failed to queue the video post for {account_id} from the user {tokench['user_id']}")
        remove_files(files)
        return jsonify({"error": "could not queue the job, try again"}), 503
    return jsonify({"success": "added"}), 200

def upload_video_job(publish_now,tokench,text,analytic_bool,timee,access_token,account_id,files):
    try:
        if not files:
            logger.error(f"no video to post for {account_id}")
            return
        if len(files) > 1:
            logger.error(f"more than one video to post for {account_id}")
            return
        tmp_path, mimetype = files[0]
        if not (mimetype or "").startswith("video/"):
            logger.error(f"not mimetype of the video in the file to post for {account_id}")
            return
        file_size_bytes = os.path.getsize(tmp_path)
        if get_video_duration(tmp_path) > duation or file_size_bytes > video_size:
            logger.error(f"duration or size exceed in the request files to post in {account_id}")
            return
        if not publish_now:
            service = dpp.get_drive_service(tokench["user_id"])
            drive_file_id, drive_err = dpp.upload_path_to_drive( service, tmp_path, os.path.basename(tmp_path), "video/mp4" )
            if drive_err:
                logger.error(f"failed to upload video to drive for {account_id} , because {drive_err}")
                return
            list_account_ids = json.dumps(account_id)
            sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token="", typeee="video_tweet_later", text1=text, text2="true" if analytic_bool else "", text3="", media_id=json.dumps([drive_file_id]), )
            return
        with open(tmp_path, "rb") as file:
            bytesss = file.read()
        for acccount, acesss in zip(account_id, access_token):
            media_id = upload_video(acesss, bytesss)  # media ids belong to the account that uploaded them
            if not media_id:
                logger.error(f"failed to upload video post in {acccount}")
                continue
            tweet_id, post_err = create_tweet_with_media(acesss, text, [media_id])
            if post_err:
                logger.error(f"unable to post video reson : {post_err},for the account {acccount} for the user {tokench['user_id']}")
                continue
            if analytic_bool :
                tweet_query(acccount, acesss, tweet_id, "video_tweet")
    except Exception:
        logger.exception(f"upload_video_job crashed for {account_id} from the user {tokench['user_id']}")
    finally:
        remove_files(files)

@app.route("/analytics/posts", methods=["POST"])
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
    try :
        tweet_ids = get_last_n_tweet_ids(account_id, access_token, n=20)
        if not tweet_ids:
            return {"error": "no posts found"}, 404
        results = fetch_tweet_metrics(tweet_ids, access_token)
    except Exception:
        logger.exception(f"x analytics failed for the account {account_id}")
        return jsonify({"error": "failed to fetch analytics"}), 502
    return jsonify(results)

if __name__ == "__main__":
    app.run(debug=True, port=5000)