import database.UserDB as dbimp
import os
import re
import time
import requests
from urllib.parse import urlencode
from flask_cors import CORS
import limit as lmmm
import json
from flask import Flask, request, jsonify, redirect
from datetime import datetime, timezone, timedelta
import Instagram.schedule_video as sccc
import Drive.dep as dpp
import logging
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from concurrent.futures import ThreadPoolExecutor
import authnew as au
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("threads_api")
app = Flask(__name__)
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
if not frontend:
    logger.error("front_end env var is not set, CORS will block every origin")
CORS(app, origins=[frontend] if frontend else [], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    logger.error("FLASK_SECRET_KEY is not set, using a temporary key (login state will break on restart / across workers)")
    app.secret_key = os.urandom(32).hex()
serializer = URLSafeTimedSerializer(app.secret_key)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
app.config["MAX_CONTENT_LENGTH"] = 65 * 1024 * 1024  # means 65 MB is the maximum request size it could have
THREADS_TABLE_NAME = "threads"
THREADS_APP_ID = os.environ.get("Threads_app_id")
executor = ThreadPoolExecutor(max_workers=4)
THREADS_APP_SECRET = os.environ.get("Threads_app_secrects")
THREADS_REDIRECT_URI = os.environ.get("THREADS_REDIRECT_URI")
BASE_URL = os.environ.get("BASE_URL")
MAX_FAILURE = 3
STATE_MAX_AGE = 600  # seconds
REQ_TIMEOUT = 15  # seconds, every outgoing request gets this
THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_manage_replies,threads_read_replies"
THREADS_AUTH_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_EXCHANGE_URL = "https://graph.threads.net/access_token"
THREADS_API_BASE = "https://graph.threads.net/v1.0"
THREADS_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
THREADS_ME_URL = "https://graph.threads.net/v1.0/me"

session = requests.Session()    
_SECRET_RE = re.compile(r"(access_token|client_secret)=[^&\s'\"]+")

def _safe(e):
    return _SECRET_RE.sub(r"\1=***", str(e))

def _call(method, url, **kw):
    try:
        resp = session.request(method, url, timeout=REQ_TIMEOUT, **kw)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        status = e.response.status_code if e.response is not None else "?"
        logger.error("threads api %s %s failed: %s %s", method.upper(), url.split("?")[0], status, _safe(body))
    except Exception as e:
        logger.error("threads api %s %s failed: %s", method.upper(), url.split("?")[0], _safe(e))
    return None

def _aware(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def _refresh(expire, access, filters):
    expire = _aware(expire)
    if expire is None or expire - datetime.now(timezone.utc) >= timedelta(days=1):
        return access
    data = _call("get", THREADS_REFRESH_URL, params={"grant_type": "th_refresh_token", "access_token": access})
    if not data or not data.get("access_token"):
        logger.error("threads token refresh failed for %s, keeping the old token", filters)
        return access
    new_access = data["access_token"]
    new_expiry = datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 5184000))
    try:
        dbimp.update_rows( THREADS_TABLE_NAME, { "access_token": new_access, "token_expire": new_expiry.isoformat(), }, filters=filters, )
        logger.info("threads token refreshed for %s", filters)
    except Exception as e:
        logger.error("Failed to persist refreshed Threads token for %s: %s", filters, e)
    return new_access

def refresh_threads_token(expire, access, token, user_id, username):
    return _refresh(expire, access, {"username": username, "id": user_id})

def refresh_threads_tokenww(expire, access, thread_user_id):
    if expire is None:  # caller does not know the expiry, take it from the db
        try:
            rows = dbimp.select_rows( THREADS_TABLE_NAME, select="token_expire", filters={"account_id": thread_user_id}, )
            expire = rows[0]["token_expire"] if rows else None
        except Exception as e:
            logger.error("unable to read token_expire for %s: %s", thread_user_id, e)
            return access
    return _refresh(expire, access, {"account_id": thread_user_id})

def refresh_threads_token11(expires, accesses, thread_user_ids):
    return [_refresh(expire, access, {"account_id": thread_user_id}) for expire, access, thread_user_id in zip(expires, accesses, thread_user_ids)]

def parse_aware_timestamp(timestamp: str) -> datetime:
    try:
        dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        logger.error("invalid timestamp %r", timestamp)
        return None
    if dt.tzinfo is None:
        logger.error("Timestamp '%s' has no timezone offset", timestamp)
        return None
    return dt.astimezone(timezone.utc)

def add_post_jobs(threads_user_id, access_token, thread_id, typee, comment, message):
    # jobs that must exist after every successful publish: 7 days of metrics fetch + the auto comment workflow
    xcccc(threads_user_id, access_token, thread_id, typee)
    if comment and message:
        try:
            sccc.inert_workflow(thread_id, message, comment)
        except Exception as e:
            logger.error("unable to add the comment workflow for %s: %s", thread_id, e)

def process_threads_carousel(access_tokens, threads_user_ids, items, text, publish_now, timee, comment, message, user_id):
    thread_ids = []
    for access_token, threads_user_id in zip(access_tokens, threads_user_ids):
        creation_id = create_threads_carousel(access_token, threads_user_id, items, caption=text)
        if not creation_id:
            logger.error("failed to create carousel container for %s", threads_user_id)
            continue
        if not wait_for_threads_container(access_token, creation_id, timeout=120):
            logger.error("carousel container %s failed to reach FINISHED state", creation_id)
            continue
        if not publish_now:
            try:
                sccc.insert_time(threads_user_id, creation_id, timee, access_token)
                thread_ids.append(creation_id)
            except Exception as e:
                logger.error("unable to schedule carousel %s: %s", creation_id, e)
            continue
        thread_id = publish_threads_container(access_token, threads_user_id, creation_id)
        if not thread_id:
            logger.error("failed to publish carousel thread for %s", threads_user_id)
            continue
        thread_ids.append(thread_id)
        add_post_jobs(threads_user_id, access_token, thread_id, "carousel1", comment, message)
    return thread_ids

def process_threads_text_posts(access_tokens, threads_user_ids, publish_now, timee, text, comment, message, user_id, tpyee, image_url=None, video_url=None):
    neww = datetime.now(timezone.utc) + timedelta(hours=24)
    newww = datetime.now(timezone.utc) + timedelta(days=5)
    tpyee = (tpyee or "text").lower()
    if tpyee == "image":
        media_id = image_url
    elif tpyee == "video":
        media_id = video_url
    else:
        media_id = None
    later = (not publish_now) and timee > neww and timee < newww  # container would expire before posting so the job creates it later
    thread_ids = []
    Failed_one = 0
    for access_token, threads_user_id in zip(access_tokens, threads_user_ids):
        if Failed_one > MAX_FAILURE:
            logger.error(f"max failure for the {user_id}")
            break
        if later:
            try:
                sccc.insert_post( user_id=threads_user_id, scheduled_time=timee, access_token=access_token, typeee=f"{tpyee}_later1", text1=f"{comment},1234,+++x{message}", text2=user_id, text3=text, media_id=media_id)
            except Exception as e:
                Failed_one += 1
                logger.error("unable to add the later job for %s: %s", threads_user_id, e)
            continue
        thread_id = _publish_threads_post(publish_now, timee, access_token, threads_user_id, tpyee, text=text, image_url=image_url if image_url else None, video_url=video_url if video_url else None)
        if not thread_id:
            Failed_one += 1
            continue
        thread_ids.append(thread_id)
        if publish_now:  # a scheduled post only has a creation id, no thread id to fetch metrics / reply to yet
            add_post_jobs(threads_user_id, access_token, thread_id, f"{tpyee}1", comment, message)
    return thread_ids

def get_thread_metrics_csv(user_id: str, media_id: str, access_token: str) -> str:
    access_token = refresh_threads_tokenww(None, access_token, user_id)
    base_url = THREADS_API_BASE
    media_metrics = ["views", "likes", "replies", "reposts", "shares"]
    media_resp = _call("get", f"{base_url}/{media_id}/insights", params={ "metric": ",".join(media_metrics), "access_token": access_token, }, )
    if media_resp is None:
        logger.error("unable to fetch metrics for %s", media_id)
        return None
    metrics = {m: 0 for m in media_metrics}
    for entry in media_resp.get("data", []):
        name = entry.get("name")
        values = entry.get("values", [])
        if name in metrics and values:
            metrics[name] = values[0].get("value", 0)
    followers_count = 0
    followers_resp = _call("get", f"{base_url}/{user_id}/threads_insights", params={ "metric": "followers_count", "access_token": access_token, }, )
    for entry in (followers_resp or {}).get("data", []):
        if entry.get("name") == "followers_count":
            values = entry.get("values")
            if values:
                followers_count = values[0].get("value", 0)
            else:  # followers_count comes back as total_value
                followers_count = (entry.get("total_value") or {}).get("value", 0)
    v, l, r, rp, s = (metrics["views"], metrics["likes"], metrics["replies"],
                       metrics["reposts"], metrics["shares"])
    return f"{media_id},{v},{l},{r},{rp},{s},{followers_count}"

def xcccc(user_id, access_token, media_id, typee):
    now = datetime.now(timezone.utc)
    for i in range(7):
        timesss = (now + timedelta(days=(i))).isoformat()
        try:
            sccc.insert__story1(user_id, timesss, access_token, media_id, typee)
        except Exception as e:
            logger.error("unable to add metrics job day %s for %s: %s", i, media_id, e)

def get_all_threads_media(access_token, account_id, page_size=100):
    media = []
    url = f"{THREADS_API_BASE}/{account_id}/threads"
    params = { "fields": "id,text,timestamp,permalink,media_type,media_url", "limit": page_size, "access_token": access_token, }
    while url:
        data = _call("get", url, params=params)
        if data is None:
            break
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
    def one(media_id):
        resp = _call("get", f"{THREADS_API_BASE}/{media_id}/insights", params={"metric": metric_types, "access_token": access_token})
        if resp is None:
            return media_id, {}
        return media_id, {m["name"]: (m.get("values") or [{}])[0].get("value") for m in resp.get("data", [])}
    if not media_ids:
        return {}
    with ThreadPoolExecutor(max_workers=5) as pool:  # was one call after another
        return dict(pool.map(one, media_ids))

def _publish_threads_post(publish, timee, access_token, threads_user_id, media_type, text, image_url=None, video_url=None):
    creation_id = create_threads_container( access_token, threads_user_id, media_type, text=text, image_url=image_url if image_url else None, video_url=video_url if video_url else None )
    if not creation_id:
        logger.error("failed to create container for %s", threads_user_id)
        return None
    if not wait_for_threads_container(access_token, creation_id, timeout=120 if media_type.upper() == "VIDEO" else 30):
        logger.error("container %s failed to reach FINISHED state", creation_id)
        return None
    if publish:
        thread_id = publish_threads_container(access_token, threads_user_id, creation_id)
        if not thread_id:
            logger.error("failed to publish thread for %s", threads_user_id)
        return thread_id
    try:
        sccc.insert_time(threads_user_id, creation_id, timee, access_token)
    except Exception as e:
        logger.error("unable to schedule container %s: %s", creation_id, e)
        return None
    return creation_id

def create_threads_container( access_token, threads_user_id, media_type, text=None, image_url=None, video_url=None, children_ids=None, is_carousel_item=False,):
    media_type = str(media_type or "").upper()
    valid_types = {"TEXT", "IMAGE", "VIDEO", "CAROUSEL"}
    if media_type not in valid_types:
        logger.error("media_type must be one of %s, got %r", valid_types, media_type)
        return None
    data = {"access_token": access_token}
    if media_type == "TEXT":
        if not text or not text.strip():
            logger.error("text is required for TEXT posts")
            return None
        data["media_type"] = "TEXT"
        data["text"] = text
    elif media_type == "IMAGE":
        if not image_url:
            logger.error("image_url is required for IMAGE posts")
            return None
        data["media_type"] = "IMAGE"
        data["image_url"] = image_url
        if text:
            data["text"] = text
        if is_carousel_item:
            data["is_carousel_item"] = "true"
    elif media_type == "VIDEO":
        if not video_url:
            logger.error("video_url is required for VIDEO posts")
            return None
        data["media_type"] = "VIDEO"
        data["video_url"] = video_url
        if text:
            data["text"] = text
        if is_carousel_item:
            data["is_carousel_item"] = "true"
    elif media_type == "CAROUSEL":
        if not children_ids or len(children_ids) < 2:
            logger.error("CAROUSEL requires at least 2 children_ids")
            return None
        if len(children_ids) > 20:
            logger.error("CAROUSEL supports at most 20 items")
            return None
        data["media_type"] = "CAROUSEL"
        data["children"] = ",".join(children_ids)
        if text:
            data["text"] = text
    resp = _call("post", f"{THREADS_API_BASE}/{threads_user_id}/threads", data=data)
    return resp.get("id") if resp else None

def create_threads_carousel(access_token, threads_user_id, items, caption=None):
    if not isinstance(items, list) or len(items) < 2 or len(items) > 20:
        logger.error("carousel requires between 2 and 20 items")
        return None
    child_ids = []
    for item in items:
        if not isinstance(item, dict):
            logger.error("bad carousel item %r", item)
            continue
        itype = str(item.get("type") or "").upper()
        child_id = create_threads_container( access_token, threads_user_id, media_type=itype, image_url=item.get("url") if itype == "IMAGE" else None, video_url=item.get("url") if itype == "VIDEO" else None, is_carousel_item=True, )
        if not child_id:
            logger.error("failed to create carousel child for %s", item)
            continue
        if itype == "VIDEO" and not wait_for_threads_container(access_token, child_id, timeout=120):
            logger.error("carousel video child %s did not finish", child_id)
            continue
        child_ids.append(child_id)
    if len(child_ids) < 2:
        logger.error("carousel has only %s valid children, need at least 2", len(child_ids))
        return None
    return create_threads_container( access_token, threads_user_id, media_type="CAROUSEL", text=caption, children_ids=child_ids, )

def publish_threads_container(access_token, threads_user_id, creation_id):
    resp = _call("post", f"{THREADS_API_BASE}/{threads_user_id}/threads_publish", data={ "creation_id": creation_id, "access_token": access_token, }, )
    return resp.get("id") if resp else None

def publish_threads_container_sc(access_token, threads_user_id, creation_id):
    access_token = refresh_threads_tokenww(None, access_token, threads_user_id)  # old code threw the refreshed token away
    return publish_threads_container(access_token, threads_user_id, creation_id)

def wait_for_threads_container(access_token, creation_id, timeout=30, interval=2):
    deadline = time.time() + timeout
    api_errors = 0
    while time.time() < deadline:
        info = _call("get", f"{THREADS_API_BASE}/{creation_id}", params={"fields": "status,error_message", "access_token": access_token}, )
        if info is None:
            api_errors += 1
            if api_errors >= 3:
                return False
        else:
            api_errors = 0
            status = info.get("status")
            if status == "FINISHED":
                return True
            if status in ("ERROR", "EXPIRED"):
                logger.error("container %s %s: %s", creation_id, status, info.get("error_message"))
                return False
        time.sleep(interval)
    logger.error("container %s did not finish processing in time", creation_id)
    return False

def get_access_token_by_username(token, user_id, username):
    try:
        rows = dbimp.select_rows( THREADS_TABLE_NAME, select="account_id,access_token,token_expire", filters={"id": user_id, "username": username} )
    except Exception as e:
        logger.error("db error while reading threads account %s: %s", username, e)
        return None, None, (jsonify({"error": "unable to read the account"}), 500)
    if not rows:
        return None, None, (jsonify({"error": "no threads account linked for this username"}), 404)
    row = rows[0]
    account_id = row["account_id"]
    access_token = row["access_token"]
    token_expiry = _aware(row["token_expire"])
    if not access_token or not token_expiry:
        return None, None, (jsonify({"error": "missing access_token"}), 400)
    # threads has no refresh_token, the access token itself is refreshed (only when < 1 day left)
    access_token = refresh_threads_token(token_expiry, access_token, token, user_id, username)
    return access_token, account_id, None

def _authenticate(data, tokench=None):
    token = data.get("token")
    usernames = data.getlist("username") if hasattr(data, "getlist") else data.get("username")  # form or json
    if isinstance(usernames, str):
        usernames = [usernames]
    if not usernames or not isinstance(usernames, list):
        return None, None, None, (jsonify({"error": "username is required"}), 400)
    usernames = list(dict.fromkeys(usernames))  # same account twice would post twice
    text = data.get("text", "")
    if tokench is None:
        tokench = au.process(token=token)
    if not tokench["status"]:
        return None, None, None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200)
    user_id = tokench["user_id"]
    now = datetime.now(timezone.utc).isoformat()
    try:
        allowed = lmmm.checkk(tokench["token"], user_id, now, len(usernames))
    except Exception as e:
        logger.error("limit check failed for %s: %s", user_id, e)
        return None, None, None, (jsonify({"error": "unable to check the limit"}), 500)
    if not allowed:
        return None, None, None, (jsonify({"error": "limit has been reached"}), 400)
    access_tokens = []
    threads_user_ids = []
    last_err = None
    for username in usernames:
        access_token, threads_user_id, err = get_access_token_by_username(tokench["token"], user_id, username)
        if err:
            logger.error("skipping %s for user %s", username, user_id)
            last_err = err
            continue  # one bad account should not stop the others
        access_tokens.append(access_token)
        threads_user_ids.append(threads_user_id)
    if not access_tokens:
        return None, None, None, last_err
    return access_tokens, threads_user_ids, text, None

def _resolve_time(publish_now, timee_raw):
    if publish_now:
        return datetime.now(timezone.utc), None
    timee = parse_datetime(timee_raw)
    if timee is None:
        return None, (jsonify({"error": "invalid type or missing date/time"}), 400)
    now = datetime.now(timezone.utc)
    lb = now + timedelta(minutes=5)
    up = now + timedelta(days=4)
    if timee < lb or timee > up:
        return None, (jsonify({"error": "invalid time for the posting"}), 400)
    return timee, None

@app.route("/auth/login", methods=["POST"])
def threads_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    state = serializer.dumps({"user_id": user_id})
    params = {"response_type": "code","client_id": THREADS_APP_ID,"redirect_uri": THREADS_REDIRECT_URI,"scope": THREADS_SCOPES,"state": state, }
    auth_url = THREADS_AUTH_URL + "?" + urlencode(params)
    return jsonify({"auth_url": auth_url}) , 200

def store_threads_account(token, user_id, access_token, username, account_id, timestamp, expire):
    dbimp.update_rows( THREADS_TABLE_NAME, {"access_token": access_token, "timestamp": timestamp, "token_expire": expire, "username": username,  "account_id": account_id, }, filters={"id": user_id}, )
    dbimp.update_rows("users",{"threads":True},{"user_id":user_id})

def clean(value):
    return (value or "").split("#", 1)[0].strip()

@app.route("/auth/callback", methods=["GET"])
def threads_callback():
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
    if not user_id:
        return jsonify({"error": "invalid state payload"}), 400
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    try:
        token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    except Exception as e:
        logger.error("unable to build the session token for %s: %s", user_id, e)
        return jsonify({"error": "internal error"}), 500
    short_resp = _call("post", THREADS_TOKEN_URL, data={ "client_id": THREADS_APP_ID, "client_secret": THREADS_APP_SECRET, "grant_type": "authorization_code", "redirect_uri": THREADS_REDIRECT_URI, "code": code, },) or {}
    short_lived_token = short_resp.get("access_token")
    threads_user_id = short_resp.get("user_id")
    if not short_lived_token:
        logger.error("threads token exchange failed for user %s", user_id)
        return jsonify({"error": "token exchange failed"}), 400
    exchange_resp = _call("get", THREADS_EXCHANGE_URL, params={ "grant_type": "th_exchange_token", "client_secret": THREADS_APP_SECRET, "access_token": short_lived_token,},) or {}
    access_token = exchange_resp.get("access_token")
    seconds = exchange_resp.get("expires_in")
    if not access_token or not seconds:
        logger.error("threads long-lived token exchange failed for user %s", user_id)
        return jsonify({"error": "long-lived token exchange failed"}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = _call("get", THREADS_ME_URL, params={"fields": "id,username", "access_token": access_token},) or {}
    username = about.get("username")
    account_id = about.get("id") or threads_user_id
    if not account_id:
        logger.error("threads profile fetch failed for user %s", user_id)
        return jsonify({"error": "failed to fetch profile"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    try:
        store_threads_account(token, user_id, access_token, username, account_id, timestamp, expire)
    except Exception as e:
        logger.error("threads account store failed for user %s: %s", user_id, e)
        return jsonify({"error": "token stored failed to save"}), 500
    logger.info("threads account %s connected for user %s", username, user_id)
    return redirect(homepage or frontend or "/", code=302)

@app.route("/posts-with-metrics", methods=["GET", "POST"])  # browsers cannot send a body with GET, POST works too
def posts_with_metrics():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    token = data.get("token")
    if not username or not token:
        return jsonify({"status": "failed"}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    try:
        rows = dbimp.select_rows( THREADS_TABLE_NAME, select="access_token,token_expire,account_id", filters={"username": username, "id": tokench["user_id"]}, )
    except Exception as e:
        logger.error("db error in posts-with-metrics for %s: %s", username, e)
        return jsonify({"status": "failed"}), 500
    if not rows:
        return jsonify({"status": "failed"}), 400
    row = rows[0]
    access_token = refresh_threads_token(row["token_expire"], row["access_token"], tokench["token"], tokench["user_id"], username)
    account_id = row["account_id"]
    media = get_all_threads_media(access_token, account_id)
    media_ids = [m["id"] for m in media]
    insights = get_threads_insights(access_token, media_ids)
    combined = []
    for m in media:
        mid = m["id"]
        combined.append({ "media_id": mid, "text": m.get("text"), "permalink": m.get("permalink"), "created_at": m.get("timestamp"), "media_url": m.get("media_url"), "metrics": insights.get(mid, {}),})
    return jsonify({"count": len(combined), "posts": combined}), 200

def _queue_post(data, image_url=None, video_url=None):
    tokench = au.process(token=data.get("token"))
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    publish_now = str(data.get("publish")).strip().lower() == "true"
    timee, err = _resolve_time(publish_now, data.get("time"))
    if err:
        return err
    executor.submit(post_text, data, publish_now, data.get("comment"), data.get("message"), tokench, timee, image_url, video_url)
    return jsonify({"success": True}), 200

def post_text(data, publish_now, comment, message, tokench, timee, image_url, video_url):
    try:
        access_tokens, threads_user_ids, text, err = _authenticate(data, tokench)
        if err:
            logger.error("unable to get the access token for %s", tokench["user_id"])
            return
        tpyee = "image" if image_url else "video" if video_url else "text"  # was always "text"
        thread_ids = process_threads_text_posts(access_tokens, threads_user_ids, publish_now, timee, text, comment, message, tokench["user_id"], tpyee, image_url if image_url else None, video_url if video_url else None)
        logger.info("%s post done for %s, ids=%s", tpyee, tokench["user_id"], thread_ids)
    except Exception:
        logger.exception("unable to post the %s for %s", "post", tokench.get("user_id"))

@app.route("/post/text", methods=["POST"])
def post_threads_text():
    data = request.get_json(silent=True) or {}
    return _queue_post(data)

@app.route("/post/image", methods=["POST"])
def post_threads_image():
    data = request.form
    image_url = data.get("image_url")
    if not image_url:
        return jsonify({"error": "image_url is required"}), 400
    return _queue_post(data, image_url=image_url)

@app.route("/post/video", methods=["POST"])
def post_threads_video():
    data = request.form
    video_url = data.get("video_url")
    if not video_url:
        return jsonify({"error": "video_url is required"}), 400
    return _queue_post(data, video_url=video_url)

def post_carousel(access_tokens, threads_user_ids, items, text, publish_now, timee, comment, message, tokench):
    try:
        neww = datetime.now(timezone.utc) + timedelta(hours=24)
        newww = datetime.now(timezone.utc) + timedelta(days=5)
        if not publish_now and timee > neww and timee < newww:
            combi = f"{comment},1234,+++x{message}"
            sccc.insert_post( user_id=json.dumps(threads_user_ids), schedled_time=timee, access_token="", typeee="carousal_later1", text1=combi, text2=tokench["user_id"], text3=text, media_id=json.dumps(items))
            logger.info("carousel job added for %s accounts of user %s", len(threads_user_ids), tokench["user_id"])
            return
        thread_ids = process_threads_carousel(access_tokens, threads_user_ids, items, text, publish_now, timee, comment, message, tokench["user_id"])
        logger.info("carousel done for %s, ids=%s", tokench["user_id"], thread_ids)
    except Exception:
        logger.exception("carousel post failed for %s", tokench.get("user_id"))

@app.route("/post/carousel", methods=["POST"])
def post_threads_carousel():
    data = request.form
    tokench = au.process(token=data.get("token"))
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    access_tokens, threads_user_ids, text, err = _authenticate(data, tokench)
    if err:
        return err
    items = data.get("items", [])
    if isinstance(items, str):  # form fields always come as a string
        try:
            items = json.loads(items)
        except ValueError:
            return jsonify({"error": "items must be a json list"}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"error": "items is required for carousel posts"}), 400
    if len(items) < 2 or len(items) > 20:
        return jsonify({"error": "carousel requires between 2 and 20 items"}), 400
    publish_now = str(data.get("publish")).strip().lower() == "true"
    timee, terr = _resolve_time(publish_now, data.get("time"))
    if terr:
        return terr
    executor.submit(post_carousel, access_tokens, threads_user_ids, items, text, publish_now, timee, data.get("comment"), data.get("message"), tokench)  # carousel can take minutes, so not in the request
    return jsonify({"success": True, "Added": len(threads_user_ids)}), 200

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)