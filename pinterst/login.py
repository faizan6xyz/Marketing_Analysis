import database.UserDB as dbimp
import os
import base64
import requests
from urllib.parse import urlencode
from moviepy import VideoFileClip
import tempfile
from flask_cors import CORS
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta , date
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import time
import authnew as au
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
PINTEREST_TABLE_NAME = "pinterest"
PINTEREST_APP_ID = os.environ.get("PINTEREST_APP_ID")
PINTEREST_APP_SECRET = os.environ.get("PINTEREST_APP_SECRET")
PINTEREST_REDIRECT_URI = os.environ.get("PINTEREST_REDIRECT_URI")
BASE_URL = os.environ.get("BASE_URL")
STATE_MAX_AGE = 600  # seconds
PINTEREST_SCOPES = "boards:read,pins:read,pins:write,user_accounts:read"
PINTEREST_AUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, PINTEREST_TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

@app.route("/auth/pinterest/login")
def pinterest_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return jsonify({"error": "invalid user id"}), 401
    state = serializer.dumps({"user_id": user_id})
    params = { "response_type": "code", "client_id": PINTEREST_APP_ID, "redirect_uri": PINTEREST_REDIRECT_URI, "scope": PINTEREST_SCOPES, "state": state,}
    auth_url = PINTEREST_AUTH_URL + "?" + urlencode(params)
    return redirect(auth_url)

@app.route("/auth/pinterest/callback")
def pinterest_callback():
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
    basic_auth = base64.b64encode(f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}".encode()).decode()
    token_resp = requests.post( PINTEREST_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={ "grant_type": "authorization_code", "code": code, "redirect_uri": PINTEREST_REDIRECT_URI, "code_verifier": code_verifier, }, ).json()
    access_token = token_resp.get("access_token")
    refresh_token = token_resp.get("refresh_token")
    seconds = token_resp.get("expires_in")
    if not access_token or not seconds:
        return jsonify({"error": "token exchange failed", "details": token_resp}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = requests.get( "https://api.pinterest.com/v5/user_account", headers={"Authorization": f"Bearer {access_token}"}, ).json()
    username = about.get("username")
    account_id = about.get("id") or about.get("account_id")
    if not account_id:
        return jsonify({"error": "failed to fetch profile", "details": about}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    payload = { "user_id": user_id, "account_id": account_id, "username": username, "expire": expire, "timestamp": timestamp, "token": token, "access": access_token, "refresh": refresh_token, }
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/pinterest/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/pinterest/callbackshi", methods=["POST"])
def pinterest_dataget():
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
    if not all([token, access_token, user_id, timestamp, expirey, account_id]):
        return jsonify({"error": "missing required fields"}), 400
    try:
        datetime.fromisoformat(timestamp)
        datetime.fromisoformat(expirey)
    except ValueError:
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows( token, PINTEREST_TABLE_NAME, { "Access_token": access_token, "Refresh_token": refresh_token, "Timestamp": timestamp, "Token_expire": expirey, "Username": username, "Account_id": account_id, }, filters={"id": user_id}, ) 
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200


def get_all_pins(access_token, page_size=100):
    pins = []
    bookmark = None
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        params = {"page_size": page_size}
        if bookmark:
            params["bookmark"] = bookmark
        resp = requests.get(f"{BASE_URL}/pins", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        pins.extend(data.get("items", []))
        bookmark = data.get("bookmark")
        if not bookmark:
            break   
    return pins


def get_pins_analytics(access_token, pin_ids, metric_types="IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"):
    headers = {"Authorization": f"Bearer {access_token}"}
    results = {}
    end_date = date.today()
    start_date = end_date - timedelta(days=30)
    for i in range(0, len(pin_ids), 100):
        chunk = pin_ids[i:i + 100]
        params = { "pin_ids": ",".join(chunk), "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "metric_types": metric_types, }
        resp = requests.get(f"{BASE_URL}/pins/analytics", headers=headers, params=params)
        resp.raise_for_status()
        results.update(resp.json())
    return results

def refresh_pinterest_token(refresh_token, expire, access, token, user_id, username):
    expire = datetime.fromisoformat(expire)
    if expire - datetime.now(timezone.utc) < timedelta(minutes=3):
        token_url = "https://api.pinterest.com/v5/oauth/token"
        credentials = f"{PINTEREST_APP_ID}:{PINTEREST_APP_SECRET}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()
        headers = {"Authorization": f"Basic {encoded_credentials}", "Content-Type": "application/x-www-form-urlencoded", }
        payload = { "grant_type": "refresh_token", "refresh_token": refresh_token, }
        resp = requests.post(token_url, headers=headers, data=payload)
        resp.raise_for_status()
        data = resp.json()
        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
        try:
            dbimp.update_rows( token, PINTEREST_TABLE_NAME, { "Access_token": data["access_token"], "Refresh_token": data.get("refresh_token", refresh_token), "Token_expire": new_expiry.isoformat(), }, filters={"Username": username, "id": user_id}, )
        except Exception as e:
            print(f"Failed to persist refreshed Pinterest token for user_id={user_id}: {e}")
        return data["access_token"]
    else:
        return access
    
@app.route("/pinterest/pins-with-metrics", methods=["GET"])
def pins_with_metrics():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    token = data.get("token")
    if not username or token :
        return jsonify({"status":"failed"}) , 400
    tokench = au.process(token=token)
    rows = dbimp.select_rows(tokench["token"],PINTEREST_TABLE_NAME,select="Access_token,Refresh_token,Token_expire",filters={"Username":username,"id":tokench["user_id"]})
    if not rows :
        return jsonify({"status":"failed"}) , 400
    row = rows[0]
    access_token = row["Access_token"]
    Refresh_token = row["Refresh_token"]
    Token_expire = row["Token_expire"]
    access_token = refresh_pinterest_token(Refresh_token, Token_expire, access_token, tokench["token"], tokench["user_id"], username)
    try:
        pins = get_all_pins(access_token)
        pin_ids = [p["id"] for p in pins]
        analytics = get_pins_analytics(access_token, pin_ids)
        combined = []
        for pin in pins:
            pin_id = pin["id"]
            combined.append({ "pin_id": pin_id, "title": pin.get("title"), "link": pin.get("link"), "created_at": pin.get("created_at"), "media_url": pin.get("media", {}).get("images", {}).get("originals", {}).get("url"), "metrics": analytics.get(pin_id, {}), })
        return jsonify({"count": len(combined), "pins": combined}) , 200
    except requests.HTTPError as e:
        return jsonify({"error": str(e), "response": e.response.text}), e.response.status_code

