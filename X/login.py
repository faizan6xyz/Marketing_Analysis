import database.UserDB as dbimp
import os
import base64
import hashlib
import secrets
import requests
from urllib.parse import urlencode
from flask_cors import CORS
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta
import authnew as au
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
X_CLIENT_ID = os.getenv("X_CLIENT_ID")
X_CLIENT_SECRET = os.getenv("X_CLIENT_SECRET")
X_REDIRECT_URI = os.getenv("X_REDIRECT_URI")
STATE_MAX_AGE = 600  # seconds
TABLE_NAME = "X"
SCOPE = "tweet.read users.read offline.access"
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
BASE_URL = ""

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

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

def generate_pkce_pair():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("utf-8")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
    return code_verifier, code_challenge

def get_authenticated_access_token(account_id):
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200)
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return None, (jsonify({"error": "invalid user id"}), 401)
    rows = dbimp.select_rows(tokench["token"], TABLE_NAME, select="Access_token,Refresh_token,Token_expire", filters={"Account_id": account_id})
    if not rows:
        return None, (jsonify({"error": "no x account linked"}), 404)
    row = rows[0]
    access_token = row["Access_token"]
    raw_expiry = row["Token_expire"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400)
    Token_expiry = datetime.fromisoformat(raw_expiry)
    if Token_expiry.tzinfo is None:
        Token_expiry = Token_expiry.replace(tzinfo=timezone.utc)
    if Token_expiry - datetime.now(timezone.utc) < timedelta(minutes=10):
        refresh_token = row.get("Refresh_token")
        if refresh_token:
            access_token = refresh_x_token(tokench["token"], user_id, account_id, refresh_token)
    return access_token, None

def refresh_x_token(token, user_id, account_id, refresh_token):
    basic_auth = base64.b64encode(f"{X_CLIENT_ID}:{X_CLIENT_SECRET}".encode()).decode()
    resp = requests.post( TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic_auth}"}, data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": X_CLIENT_ID}, ).json()
    new_access = resp.get("access_token")
    new_refresh = resp.get("refresh_token", refresh_token)
    seconds = resp.get("expires_in")
    if not new_access or not seconds:
        return None
    expire_time = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    dbimp.update_rows(token, TABLE_NAME, {"Access_token": new_access, "Refresh_token": new_refresh, "Token_expire": expire_time}, filters={"Account_id": account_id})
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
    code_verifier, code_challenge = generate_pkce_pair()
    state = serializer.dumps({"user_id": user_id, "code_verifier": code_verifier})
    params = { "response_type": "code", "client_id": X_CLIENT_ID, "redirect_uri": X_REDIRECT_URI, "scope": SCOPE,"state": state,"code_challenge": code_challenge, "code_challenge_method": "S256",}
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
        datetime.fromisoformat(timestamp)
        datetime.fromisoformat(expirey)
    except ValueError:
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows( token, TABLE_NAME, { "Access_token": access_token, "Refresh_token": refresh_token, "Timestamp": timestamp, "Token_expire": expirey, "Username": username, "Account_id": account_id, }, filters={"id": user_id}, ) 
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200