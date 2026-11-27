import database.UserDB as dbimp
import os
import base64
import requests
import logging
from urllib.parse import urlencode
from moviepy import VideoFileClip
import Drive.dep as dpp
import json
import tempfile
from flask_cors import CORS
import Instagram.schedule_video as sccc
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import hashlib
import secrets
import time
import limit as lmmm
import authnew as au
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pinterest_api")
app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=4)
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # means 50 MB is the maximum request size it could have 
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
PINTEREST_TABLE_NAME = "pinterest"
PINTEREST_APP_ID = os.environ.get("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = os.environ.get("PINTEREST_APP_SECRET")
PINTEREST_REDIRECT_URI = os.environ.get("PINTEREST_REDIRECT_URI")
BASE_URL = os.environ.get("BASE_URL")
image_size = 10 * 1024 * 1024
STATE_MAX_AGE = 600
PINTEREST_SCOPES = "boards:read,pins:read,pins:write,user_accounts:read"
PINTEREST_API_BASE = "https://api.pinterest.com/v5"
MEDIA_REGISTER_URL = f"{PINTEREST_API_BASE}/media"
PIN_CREATE_URL = f"{PINTEREST_API_BASE}/pins"
PINTEREST_USER_URL = f"{PINTEREST_API_BASE}/user_account"
PINTEREST_AUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = f"{PINTEREST_API_BASE}/oauth/token"
duation = 300
max_size = 100 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = max_size + 1024 * 1024  # reject oversized bodies early
METRIC_TYPES = "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK,ENGAGEMENT,USER_FOLLOW"
HTTP_TIMEOUT = 10
UPLOAD_TIMEOUT = 120
MAX_FAILURE = 3
VIDEO_PROCESS_TIMEOUT = 180
VIDEO_POLL_INTERVAL = 2
REFRESH_MARGIN = timedelta(minutes=5)
STORY_REPEAT_DAYS = 7
session = requests.Session()    

def _utcnow():
    return datetime.now(timezone.utc)

def _to_aware_utc(value):
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def _remove_quietly(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            logger.warning(f"could not remove {path}")

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

def pin_query(user_id,access_token,media_id,typee):
    base = _utcnow()
    for i in range(STORY_REPEAT_DAYS):
        sccc.insert__story1(user_id, (base + timedelta(days=i)).isoformat(), access_token,media_id,typee)

def _refresh_core(refresh_token, expire, access, filters):
    expire = _to_aware_utc(expire)
    if expire is not None and expire - _utcnow() >= REFRESH_MARGIN:
        return access
    if not refresh_token:
        return access
    encoded_credentials = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    headers = {"Authorization": f"Basic {encoded_credentials}", "Content-Type": "application/x-www-form-urlencoded", }
    payload = { "grant_type": "refresh_token", "refresh_token": refresh_token, }
    try:
        resp = session.post(PINTEREST_TOKEN_URL, headers=headers, data=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        new_access = data["access_token"]
        new_expiry = _utcnow() + timedelta(seconds=data["expires_in"])
    except (requests.RequestException, ValueError, KeyError):
        logger.exception(f"pinterest token refresh failed for {filters}")
        return access
    try:
        dbimp.update_rows(PINTEREST_TABLE_NAME, { "access_token": new_access, "refresh_token": data.get("refresh_token", refresh_token), "token_expire": new_expiry.isoformat(), }, filters=filters, )
    except Exception:
        logger.exception(f"failed to persist refreshed pinterest token for {filters}")
    return new_access

def refresh_pinterest_token11(access, username_id):
    rows = dbimp.select_rows(PINTEREST_TABLE_NAME , select="refresh_token,token_expire" , filters={"account_id":username_id})
    if not rows:
        raise ValueError(f"No Pinterest token record found for user {username_id}")
    row = rows[0]
    return _refresh_core(row["refresh_token"], row["token_expire"], access, {"account_id": username_id})

def refresh_pinterest_token(refresh_token, expire, access, token, user_id, username):
    return _refresh_core(refresh_token, expire, access, {"username": username, "id": user_id})

def get_fresh_token_for_account(account_id):
    rows = dbimp.select_rows(PINTEREST_TABLE_NAME, select="access_token,refresh_token,token_expire", filters={"account_id": account_id})
    if not rows:
        return None
    row = rows[0]
    return _refresh_core(row["refresh_token"], row["token_expire"], row["access_token"], {"account_id": account_id})

def get_all_pins(access_token, page_size=100):
    pins = []
    bookmark = None
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        params = {"page_size": page_size}
        if bookmark:
            params["bookmark"] = bookmark
        resp = session.get(f"{PINTEREST_API_BASE}/pins", headers=headers, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        pins.extend(data.get("items", []))
        bookmark = data.get("bookmark")
        if not bookmark:
            break
    return pins

def get_pinterest_pin_analytics_csv(username_id,pin_id, access_token):
    access_token = refresh_pinterest_token11(access_token,username_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    end_date = _utcnow()
    start_date = end_date - timedelta(hours=24)
    pin_resp = session.get(f"{PINTEREST_API_BASE}/pins/{pin_id}", headers=headers, timeout=HTTP_TIMEOUT)
    published_at = ""
    if pin_resp.ok:
        published_at = pin_resp.json().get("created_at", "")
    else:
        logger.warning(f"pin lookup failed for {pin_id} [{pin_resp.status_code}]: {pin_resp.text}")
    analytics_resp = session.get( f"{PINTEREST_API_BASE}/pins/{pin_id}/analytics", headers=headers, params={ "start_date": start_date.strftime("%Y-%m-%d"), "end_date": end_date.strftime("%Y-%m-%d"), "metric_types": METRIC_TYPES, "app_types": "ALL", "split_field": "NO_SPLIT",}, timeout=HTTP_TIMEOUT)
    metrics = {}
    if analytics_resp.ok:
        bucket = analytics_resp.json().get("all", {})
        metrics = bucket.get("lifetime_metrics") or bucket.get("summary_metrics") or {}
    else:
        logger.warning(f"pin analytics failed for {pin_id} [{analytics_resp.status_code}]: {analytics_resp.text}")
    return f"{pin_id},{published_at},{metrics.get('IMPRESSION', 0)},{metrics.get('SAVE', 0)},{metrics.get('PIN_CLICK', 0)},{metrics.get('OUTBOUND_CLICK', 0)},{metrics.get('ENGAGEMENT', 0)},{metrics.get('USER_FOLLOW', 0)}"

def get_pins_analytics(access_token, pin_ids, metric_types="IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"):
    headers = {"Authorization": f"Bearer {access_token}"}
    results = {}
    end_date = _utcnow().date()
    start_date = end_date - timedelta(days=30)
    for i in range(0, len(pin_ids), 100):
        chunk = pin_ids[i:i + 100]
        params = { "pin_ids": ",".join(chunk), "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "metric_types": metric_types, }
        resp = session.get(f"{PINTEREST_API_BASE}/pins/analytics", headers=headers, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        results.update(resp.json())
    return results

def get_access_token_by_username(token, user_id, username):
    rows = dbimp.select_rows( PINTEREST_TABLE_NAME, select="account_id,access_token,refresh_token,token_expire",filters={"id": user_id, "username": username} )
    if not rows:
        return None, (jsonify({"error": "no pinterest account linked for this username"}), 404) , None
    row = rows[0]
    access_token = row["access_token"]
    raw_expiry = row["token_expire"]
    account_id = row["account_id"]
    refresh_token = row["refresh_token"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400) , None
    refreshed = refresh_pinterest_token(refresh_token, raw_expiry, access_token, token, user_id, username)
    if refreshed == access_token and _to_aware_utc(raw_expiry) <= _utcnow():
        logger.warning(f"pinterest token expired for user {user_id} username {username}")
        return None, (jsonify({"error": "pinterest token expired, please reconnect"}), 401) , None
    return refreshed, None , account_id

def _get_tokench(data):
    token = data.get("token")
    if not token or not isinstance(token, str):
        return None, (jsonify({"error": "'token' is required"}), 400)
    tokench = au.process(token=token)
    if not tokench["status"]:
        return None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 403)
    return tokench, None

def _authenticate(data, tokench):
    usernames = data.getlist("username")
    if len(usernames) == 1 and isinstance(usernames[0], str) and usernames[0].strip().startswith("["):
        try:
            usernames = json.loads(usernames[0])
        except ValueError:
            return None , (jsonify({"error": "username is not the list"}),400) , None , None
    if not usernames:
        return None, (jsonify({"error": "username is required"}), 400) , None , None
    if not isinstance(usernames, list):
        return None , (jsonify({"error": "username is not the list"}),400) , None , None
    usernames = list(dict.fromkeys(u.strip() for u in usernames if isinstance(u, str) and u.strip()))
    if not usernames:
        return None, (jsonify({"error": "username is required"}), 400) , None , None
    access_tokens = []
    account_ids = []
    for username in usernames :
        access_token, err ,account_id= get_access_token_by_username(tokench["token"], tokench["user_id"], username)
        if err:
            return None,  err , None , None
        access_tokens.append(access_token)
        account_ids.append(account_id)
    return access_tokens,  None, account_ids , tokench["user_id"]

def _limit_ok(tokench, count):
    return lmmm.checkk(tokench["token"], tokench["user_id"], _utcnow().isoformat(), count)

def _parse_schedule(data):
    publish_now = str(data.get("publish", "true")).strip().lower() == "true"
    if publish_now:
        return True, _utcnow(), None
    timee = parse_datetime(data.get("time"))
    if timee is None:
        return None, None, (jsonify({"error": "invalid or missing date/time"}), 400)
    now = _utcnow()
    if timee < now + timedelta(seconds=180) or timee > now + timedelta(hours=72):
        return None, None, (jsonify({"error": "invalid time for the posting"}), 400)
    return False, timee, None

def _failed(resp, e):
    if resp is not None:
        return {"status": "failed", "error": resp.text}
    return {"status": "failed", "error": str(e)}

def _create_image_pin(access, board_id, title, description, photo_bytes, content_type):
    payload = { "board_id": board_id, "title": title, "description": description, "media_source": { "source_type": "image_base64",  "content_type": content_type or "image/jpeg", "data": base64.b64encode(photo_bytes).decode("utf-8"), }, }
    payload = {k: v for k, v in payload.items() if v is not None}
    headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    resp = None
    try:
        resp = session.post(PIN_CREATE_URL, headers=headers, json=payload, timeout=UPLOAD_TIMEOUT)
        resp.raise_for_status()
        return {"status": "posted", "pin_id": resp.json().get("id")}
    except requests.HTTPError as e:
        logger.error(f"failed upload for the pin {resp} , {e}")
        return _failed(None, e)
    except requests.RequestException as e:
        logger.error(f"failed upload for the pin {resp} , {e}")
        return _failed(None, e)

def _create_video_pin(access, board_id, title, description, file_path, timeout=VIDEO_PROCESS_TIMEOUT):
    json_headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
    resp = None
    try:
        resp = session.post( MEDIA_REGISTER_URL, headers=json_headers, json={"media_type": "video"}, timeout=HTTP_TIMEOUT )
        resp.raise_for_status()
        registration = resp.json()
        media_id = registration.get("media_id")
        upload_url = registration.get("upload_url")
        upload_parameters = registration.get("upload_parameters")
        if not media_id or not upload_url or not upload_parameters:
            raise RuntimeError(f"registration response missing required fields: {registration}")
        with open(file_path, "rb") as fh:
            resp = session.post(upload_url, data=upload_parameters, files={"file": fh}, timeout=UPLOAD_TIMEOUT)
        if not resp.ok:
            raise RuntimeError(f"upload failed [{resp.status_code}]: {resp.text}")
        deadline = time.time() + timeout
        headers = {"Authorization": f"Bearer {access}"}
        while time.time() < deadline:
            resp = session.get(f"{MEDIA_REGISTER_URL}/{media_id}", headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == "succeeded":
                break
            if status == "failed":
                raise RuntimeError(f"video processing failed for media_id={media_id}")
            time.sleep(VIDEO_POLL_INTERVAL)
        else:
            raise RuntimeError(f"video processing timed out after {timeout}s for media_id={media_id}")
        payload = { "title": title, "description": description, "board_id": board_id, "media_source": { "source_type": "video_id", "media_id": media_id, }, }
        payload = {k: v for k, v in payload.items() if v is not None}
        resp = session.post(PIN_CREATE_URL, headers=json_headers, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return {"status": "posted", "pin_id": resp.json().get("id")}
    except requests.HTTPError as e:
        return _failed(resp, e)
    except (RuntimeError, requests.RequestException, OSError) as e:
        return {"status": "failed", "error": str(e)}

def post_late(account_id , access_token , title, description, board_id,  typee ,drive_file_id):
    if typee not in ("photo", "video"):
        logger.error(f"post_late: unsupported type {typee}")
        return False
    raw = account_id[0] if isinstance(account_id, (list, tuple)) else account_id
    account_ids = json.loads(raw)
    if not account_ids:
        logger.error("post_late: no account ids")
        return False
    rows = dbimp.select_rows(PINTEREST_TABLE_NAME,select="id",filters={"account_id":account_ids[0]})
    if not rows :
        logger.error(f"post_late: no pinterest row for account {account_ids[0]}")
        return False
    user_id = rows[0]["id"]
    service = dpp.get_drive_service(user_id)
    tmp_path, mimetype, name = dpp.download_drive_file_to_temp(service, drive_file_id)
    if not tmp_path:
        logger.error(f"post_late: unable to download drive file {drive_file_id}")
        return False
    results = []
    failed_one = 0
    try:
        photo_bytes = None
        if typee == "photo":
            with open(tmp_path, "rb") as fh:
                photo_bytes = fh.read()
        for acc_id in account_ids:
            if failed_one > MAX_FAILURE :
                logger.error(f"failed post later due to max failure limit from user {user_id}, passed ones are : {results}")
                return False
            access = get_fresh_token_for_account(acc_id)
            if not access:
                failed_one +=1
                logger.error(f"post_late: no token found for account {acc_id}")
                results.append({"account": acc_id, "status": "failed", "error": "no token found"})
                continue
            if typee == "photo":
                res = _create_image_pin(access, board_id, title, description, photo_bytes, mimetype)
            else:
                res = _create_video_pin(access, board_id, title, description, tmp_path)
            res["account"] = acc_id 
            if res["status"] != "posted":
                failed_one +=1
                logger.error(f"post_late: {typee} pin failed for account {acc_id}: {res.get('error')}")
            results.append(res)
    except Exception:
        logger.exception("post_late crashed")
        return False
    finally:
        deleteFile(service ,drive_file_id)
        _remove_quietly(tmp_path)
    return results

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

def _schedule_pin(access_token, title, description, board_id, timee, user_id, file_path, mimetype, account_id, typeee):
    service = dpp.get_drive_service(user_id)
    drive_file_id, err = dpp.upload_path_to_drive(service, file_path, os.path.basename(file_path), mimetype)
    if err is not None or not drive_file_id:
        logger.error(f"drive upload failed for user {user_id}: {err}")
        return [{"account": account_id, "status": "failed", "error": f"drive upload failed: {err}"}]
    sccc.insert_post( user_id=json.dumps(account_id), scheduled_time=timee, access_token="", typeee=typeee, text1=title, text2=description , text3=board_id, media_id=drive_file_id, )
    return [{"account": account_id, "status": "scheduled", "scheduled_time": timee.isoformat()}]

def create_pinterest_video_pin_full(access_token, board_id, title, description, file_path,publish_now,timee,account_id,user_id,typeee,mimetype ,timeout=VIDEO_PROCESS_TIMEOUT ):
    if not publish_now :
        return _schedule_pin(access_token, title, description, board_id, timee, user_id, file_path, mimetype, account_id, typeee)
    results = []
    failed_one = 0
    for access, acc_id in zip(access_token, account_id):
        if failed_one > MAX_FAILURE :
            logger.error(f"failed post later due to max failure limit from user {user_id} , passed ones are : {results}")
            return False
        res = _create_video_pin(access, board_id, title, description, file_path, timeout)
        if res["status"] != "posted":
            failed_one +=1
            logger.error(f"video pin failed for account {acc_id}: {res.get('error')}")
        res["account"] = acc_id  
        results.append(res)
    return results

def upload_pin_from_file(access_token, title, board_id, publish_now, timee, user_id, upload_tmp_path, mimetype, account_id, typeee,description=""):
    if not publish_now:
        return _schedule_pin(access_token, title, description, board_id, timee, user_id, upload_tmp_path, mimetype, account_id, typeee)
    with open(upload_tmp_path, "rb") as fh:
        photo_bytes = fh.read()
    results = []
    failed_one = 0
    for access, acc_id in zip(access_token, account_id):
        if failed_one > MAX_FAILURE :
            logger.error(f"failed post later due to max failure limit from user {user_id} , passed ones are : {results}")
            return False
        res = _create_image_pin(access, board_id, title, description, photo_bytes, mimetype)
        if res["status"] != "posted":
            failed_one +=1
            logger.error(f"video pin failed for account {acc_id}: {res.get('error')}")
        res["account"] = acc_id 
        results.append(res)
    return results

def _finish(results, publish_now, access_tokens, kind, user_id=None):
    if not publish_now:
        for r in results:
            if r["status"] == "failed":
                logger.error(f"scheduling {kind} failed for user {user_id}: {r.get('error')}")
        return
    for res, access_token in zip(results, access_tokens):
        if res["status"] != "posted":
            logger.error(f"{kind} post failed for account {res.get('account')} user {user_id}: {res.get('error')}")
            continue
        logger.info(f"{kind} posted for account {res['account']} pin {res.get('pin_id')}")
        if res.get("pin_id"):
            try:
                pin_query(res["account"], access_token, res["pin_id"], kind)
            except Exception:
                logger.exception(f"pin_query scheduling failed for {res['account']}")

def upload_pin_job(kind, access_tokens, title, description, board_id, publish_now, timee, user_id, account_ids,mime_prefix, suffix_default, size_limit, check_video):
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": f"a {mime_prefix} file is required"}), 400
    if len(files) > 1:
        return jsonify({"error": f"only one {mime_prefix} allowed per pin"}), 400
    f = files[0]
    if not (f.mimetype or "").startswith(f"{mime_prefix}/"):
        return jsonify({"error": "unsupported file type in upload"}), 400
    tmp_path = None
    try:
        suffix = os.path.splitext(f.filename or "")[1] or suffix_default
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        if os.path.getsize(tmp_path) > size_limit:
            _remove_quietly(tmp_path)
            return jsonify({"error": f"{mime_prefix} exceeds max size"}), 400
        if check_video:
            try:
                video_length = get_video_duration(tmp_path)
            except Exception:
                logger.exception("could not read video duration")
                _remove_quietly(tmp_path)
                return jsonify({"error": "invalid video file"}), 400
            if video_length > duation:
                _remove_quietly(tmp_path)
                return jsonify({"error": "video exceeds allowed duration or size"}), 400
    except Exception:
        logger.exception(f"failed to queue {kind} upload for user {user_id}")
        _remove_quietly(tmp_path)
    try:
        if kind == "pin_photo":
            results = upload_pin_from_file(access_tokens, title, board_id, publish_now, timee, user_id, tmp_path, f.mimetype, account_ids, "Pin_photo_later", description)
        else:
            results = create_pinterest_video_pin_full(access_tokens, board_id, title, description, tmp_path, publish_now, timee, account_ids, user_id, "Pin_video_later", f.mimetype)
        _finish(results, publish_now, access_tokens, kind, user_id)
    except Exception:
        logger.exception(f"{kind} upload job crashed for user {user_id} accounts {account_ids}")
    finally:
        _remove_quietly(tmp_path)

def generate_pkce_pair():
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge

def get_video_duration(file_path):
    with VideoFileClip(file_path) as video:
        return video.duration

@app.route("/auth/login", methods=["POST"])
def pinterest_login():
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token")
    tokench, err = _get_tokench({"token": token})
    if err:
        return err
    user_id = tokench["user_id"]
    code_verifier, code_challenge = generate_pkce_pair()
    state = serializer.dumps(user_id)
    try:
        try:
            dbimp.delete_rows("tableverify", {"id": user_id, "platform": "pinterest"})
        except Exception:
            pass
        dbimp.insert_rows("tableverify", {"id": user_id, "platform": "pinterest", "code": code_verifier})
    except Exception:
        logger.exception("failed to store pkce verifier")
        return jsonify({"status": False, "error": "could not start login"}), 500
    params = { "response_type": "code", "client_id": PINTEREST_APP_ID, "redirect_uri": PINTEREST_REDIRECT_URI, "scope": PINTEREST_SCOPES, "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",}
    auth_url = PINTEREST_AUTH_URL + "?" + urlencode(params)
    return jsonify({"auth_url": auth_url}) , 200

def clean(value):
    return (value or "").split("#", 1)[0].strip()

def store_pinterest_account(user_id, access_token, username, account_id, timestamp, expire,refresh_token):
    payload = { "id": user_id, "access_token": access_token, "refresh_token": refresh_token, "timestamp": timestamp, "token_expire": expire, "username": username, "account_id": account_id, }
    filters = {"id": user_id, "account_id": account_id}
    if dbimp.select_rows(PINTEREST_TABLE_NAME, select="account_id", filters=filters):
        if not refresh_token:
            payload.pop("refresh_token")
        dbimp.update_rows(PINTEREST_TABLE_NAME, payload, filters=filters)
    else:
        dbimp.insert_rows(PINTEREST_TABLE_NAME, payload)
    dbimp.update_rows("users",{"pinterst":True},{"user_id":user_id})

@app.route("/auth/callback", methods=["GET"])
def pinterest_callback():
    code = clean(request.args.get("code"))
    state = clean(request.args.get("state"))
    if not code or not state:
        return jsonify({"error": "missing code or state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    rows = dbimp.select_rows("tableverify", select="code", filters={"id": user_id, "platform": "pinterest"})
    if not rows:
        return jsonify({"error": "no login in progress, please reconnect"}), 400
    code_verifier = rows[0]["code"]
    try:
        dbimp.delete_rows("tableverify", {"id": user_id, "platform": "pinterest"})
    except Exception:
        logger.warning(f"unable to delete verifier for {user_id} pinterest")
    basic_auth = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    try:
        token_resp = session.post( PINTEREST_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={ "grant_type": "authorization_code", "code": code, "redirect_uri": PINTEREST_REDIRECT_URI, "code_verifier": code_verifier, }, timeout=HTTP_TIMEOUT ).json()
    except (requests.RequestException, ValueError):
        logger.exception("pinterest token exchange failed")
        return jsonify({"error": "token exchange failed, please reconnect"}), 502
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    seconds = token_resp.get("expires_in")
    if not access_token or not seconds:
        logger.error(f"pinterest token exchange returned no token for user {user_id}: {token_resp}")
        return jsonify({"error": "token exchange failed", "details": token_resp}), 400
    expire = (_utcnow() + timedelta(seconds=seconds)).isoformat()
    try:
        about = session.get( PINTEREST_USER_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=HTTP_TIMEOUT ).json()
    except (requests.RequestException, ValueError):
        logger.exception("pinterest profile fetch failed")
        return jsonify({"error": "failed to fetch profile"}), 502
    username = about.get("username")
    account_id = about.get("id") or about.get("account_id")
    if not account_id:
        logger.error(f"pinterest profile has no account id for user {user_id}: {about}")
        return jsonify({"error": "failed to fetch profile", "details": about}), 400
    timestamp = _utcnow().isoformat()
    try:
        store_pinterest_account(user_id, access_token, username, account_id, timestamp, expire,refresh_token)
    except Exception as e:
        logger.exception("store_pinterest_account failed")
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return redirect(homepage, code=302)

@app.route("/pins-with-metrics", methods=["POST"])
def pins_with_metrics():
    data = request.get_json(silent=True) or {}
    username = request.args.get("username") or data.get("username")
    token = request.args.get("token") or data.get("token")
    if not username or not token :
        return jsonify({"status":"failed"}) , 400
    tokench, err = _get_tokench({"token": token})
    if err:
        return err
    rows = dbimp.select_rows(PINTEREST_TABLE_NAME,select="access_token,refresh_token,token_expire",filters={"username":username,"id":tokench["user_id"]})
    if not rows :
        return jsonify({"status":"failed"}) , 400
    row = rows[0]
    access_token = refresh_pinterest_token(row["refresh_token"], row["token_expire"], row["access_token"], tokench["token"], tokench["user_id"], username)
    try:
        pins = get_all_pins(access_token)
        analytics = get_pins_analytics(access_token, [p["id"] for p in pins])
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else 502
        logger.error(f"pinterest pins fetch failed [{status}]: {e.response.text if e.response is not None else e}")
        return jsonify({"error": str(e), "response": e.response.text if e.response is not None else None}), status
    except requests.RequestException:
        logger.exception("pinterest pins fetch failed")
        return jsonify({"error": "pinterest api error"}), 502
    combined = []
    for pin in pins:
        pin_id = pin["id"]
        combined.append({ "pin_id": pin_id, "title": pin.get("title"), "link": pin.get("link"), "created_at": pin.get("created_at"), "media_url": ((pin.get("media") or {}).get("images") or {}).get("originals", {}).get("url"), "metrics": analytics.get(pin_id, {}), })
    return jsonify({"count": len(combined), "pins": combined}) , 200

@app.route("/post/photo", methods=["POST"])
def post_to_pinterest_photo():
    data = request.form
    tokench, err = _get_tokench(data)
    if err:
        return err
    publish_now, timee, err = _parse_schedule(data)
    if err:
        return err
    title = data.get("title")
    description = data.get("description", "")
    board_id = data.get("board_id")
    access_tokens ,err , account_ids , user_id = _authenticate(data, tokench)
    if err:
        return err
    if not board_id:
        return jsonify({"error": "board_id is required"}), 400
    if not _limit_ok(tokench, len(account_ids)):
        return jsonify({"error": "limit has been reached"}) ,400
    try:
        executor.submit(upload_pin_job, "pin_photo", access_tokens, title, description, board_id, publish_now, timee, tokench["user_id"],account_ids, "image", ".jpg", image_size, False)
    except Exception:
        logger.exception(f"failed to queue the upload for photo pin for {data.getlist("username")} from the user {user_id}")
    return jsonify({"success": "added"}), 200

@app.route("/post/video", methods=["POST"])
def post_to_pinterest_video():
    data = request.form
    tokench, err = _get_tokench(data)
    if err:
        return err
    publish_now, timee, err = _parse_schedule(data)
    if err:
        return err
    title = data.get("title")
    description = data.get("description", "")
    board_id = data.get("board_id")
    access_tokens ,err , account_ids , user_id = _authenticate(data, tokench)
    if err:
        return err
    if not board_id:
        return jsonify({"error": "board_id is required"}), 400
    if not _limit_ok(tokench, len(account_ids)):
        return jsonify({"error": "limit has been reached"}) ,400
    try :
        executor.submit(upload_pin_job, "pin_video", access_tokens, title, description, board_id, publish_now, timee, tokench["user_id"], account_ids,"video", ".mp4", max_size, True)
    except Exception:
        logger.exception(f"failed to queue the upload for video pin for {data.getlist("username")} from the user {user_id}")
    return jsonify({"success": "added"}), 200

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_ENV") == "development", port=5000)