import database.UserDB as dbimp
import os
import base64
import requests
from urllib.parse import urlencode
from flask_cors import CORS
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta, date
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
THREADS_APP_ID = os.environ.get("THREADS_APP_ID")
THREADS_APP_SECRET = os.environ.get("THREADS_APP_SECRET")
THREADS_REDIRECT_URI = os.environ.get("THREADS_REDIRECT_URI")
BASE_URL = os.environ.get("BASE_URL")
STATE_MAX_AGE = 600  # seconds
THREADS_SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_read_replies"
THREADS_AUTH_URL = "https://threads.net/oauth/authorize"
THREADS_TOKEN_URL = "https://graph.threads.net/oauth/access_token"
THREADS_EXCHANGE_URL = "https://graph.threads.net/access_token"
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

def get_threads_insights(access_token, media_ids, metric_types="views,likes,replies,reposts,quotes,shares"):
    results = {}
    for media_id in media_ids:
        resp = requests.get( f"https://graph.threads.net/v1.0/{media_id}/insights", params={"metric": metric_types, "access_token": access_token}, ) 
        resp.raise_for_status()
        data = resp.json().get("data", [])
        results[media_id] = {m["name"]: m.get("values", [{}])[0].get("value") for m in data}
    return results

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
    if not username or token:
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