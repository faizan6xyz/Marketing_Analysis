import io
import os
import secrets
from flask import Flask, request, redirect, session, jsonify
import Drive.dep as dpp
import database.UserDB as dbimp
import authnew as au
from datetime import datetime, timezone, timedelta
from itsdangerous import  BadSignature, SignatureExpired
import requests
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload , MediaIoBaseUpload ,MediaIoBaseDownload
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from googleapiclient.errors import HttpError
from flask_cors import CORS
from google_auth_oauthlib.flow import Flow
from flask_limiter.util import get_remote_address
from flask_limiter import Limiter
YOUTUBE_SCOPES = [ "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/drive.readonly",]
CLIENT_SECRETS_FILE = "client_secret.json"
BASE_URL = os.environ.get("baseurl")
TOKEN_FILE = "token.json"
file_size = 30 * 1024 * 1024
STATE_MAX_AGE = 600  # seconds
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
Clientid = os.environ.get("client_id")
Clientsec = os.environ.get("client_secrect")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth2callback")
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])
TABLE_NAME = "Youtube"
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
serializer = URLSafeTimedSerializer(app.secret_key)

def credentials_to_dict(creds: Credentials) -> dict:
    return { "token": creds.token, "refresh_token": creds.refresh_token, "token_uri": creds.token_uri, "client_id": creds.client_id, "client_secret": creds.client_secret, "scopes": creds.scopes, }

def load_saved_credentials() -> Credentials | None:
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, YOUTUBE_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return creds
    return None

def build_flow():
    return Flow.from_client_config({"web": { "client_id": Clientid, "client_secret": Clientsec, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [REDIRECT_URI], }}, scopes=YOUTUBE_SCOPES, redirect_uri=REDIRECT_URI) 

def save_tokens(token, user_id, creds, email_addr=None):
    payload = {"Access_token": creds.token, "Refresh_token": creds.refresh_token, "Token_expire": creds.expiry.isoformat(), "Timestamp": datetime.now(timezone.utc).isoformat()}
    if email_addr:
        payload["Email"] = email_addr
    rows = dbimp.select_rows(token,TABLE_NAME, select="id", filters={"id": user_id})
    if rows:
        dbimp.update_rows(token,TABLE_NAME, payload, filters={"id": user_id})
    else:
        dbimp.insert_rows(token,TABLE_NAME, {"id": user_id, **payload})

def get_credentials() -> Credentials:
    creds = None
    if "credentials" in session:
        creds = Credentials(**session["credentials"])
        if creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
            session["credentials"] = credentials_to_dict(creds)
    if not creds or not creds.valid:
        creds = load_saved_credentials()
    if not creds or not creds.valid:
        raise RuntimeError("No valid credentials. Hit /login first.")
    return creds

class DriveStreamAsFile(io.RawIOBase):
    def __init__(self, drive_service, file_id):
        self._request = drive_service.files().get_media(fileId=file_id)
        self._buffer = io.BytesIO()
        self._downloader = MediaIoBaseDownload(self._buffer, self._request, chunksize=10 * 1024 * 1024)
        self._done = False

    def readable(self):
        return True

    def readinto(self, b):
        while self._buffer.tell() >= len(self._buffer.getvalue()) - len(b) and not self._done:
            _, self._done = self._downloader.next_chunk()
        data = self._buffer.getvalue()
        n = min(len(b), len(data))
        if n == 0:
            return 0
        b[:n] = data[:n]
        remaining = data[n:]
        self._buffer = io.BytesIO(remaining)
        self._buffer.seek(0, io.SEEK_END)
        return n

def get_drive_file_size(drive_service, file_id: str):
    meta = drive_service.files().get(fileId=file_id, fields="size,mimeType,name").execute()
    return int(meta["size"]), meta.get("mimeType", "video/*"), meta.get("name")

def upload_from_drive_to_youtube( creds, file_id: str, title: str, description: str = "", tags=None, category_id: str = "22", privacy_status: str = "private", ):
    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)
    size, mimetype, name = get_drive_file_size(drive, file_id)
    stream = DriveStreamAsFile(drive, file_id)
    media = MediaIoBaseUpload(stream, mimetype=mimetype, chunksize=10 * 1024 * 1024, resumable=True)
    body = { "snippet": { "title": title, "description": description, "tags": tags or [], "categoryId": category_id, }, "status": {"privacyStatus": privacy_status},  }
    request_ = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request_.next_chunk()
        if status:
            print(f"Uploading... {int(status.progress() * 100)}%")
    return response

@app.route("/connect-youtube")
def connect_youtube():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token:
        return jsonify({"status": False}), 403
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]})
    user_id = tokench["user_id"]
    state = serializer.dumps(user_id)
    flow = build_flow(scopes=YOUTUBE_SCOPES, redirect_uri=f"{BASE_URL}/oauth/youtube/callback")
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    return redirect(auth_url)


@app.route("/oauth/youtube/callback")
def youtube_oauth_callback():
    state = request.args.get("state")
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not state:
        return jsonify({"error": "missing state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    flow = build_flow(scopes=YOUTUBE_SCOPES, redirect_uri=f"{BASE_URL}/oauth/youtube/callback")
    flow.fetch_token(code=code)
    creds = flow.credentials
    creds_json = creds.to_json()
    youtube = build("youtube", "v3", credentials=creds)
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    channel = youtube.channels().list(part="snippet", mine=True).execute()
    items = channel.get("items", [])
    if not items:
        return jsonify({"error": "no_youtube_channel", "detail": "authenticated Google account has no YouTube channel"}), 400
    channel_title = items[0]["snippet"]["title"]
    channel_id = items[0]["id"]
    payload = { "user_id": user_id, "creds": creds_json, "channel_id": channel_id, "channel_title": channel_title, "token": token, }
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/youtube/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/youtube/callbackshi", methods=["POST"])
def youtube_callback_internal():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    user_id = data.get("user_id")
    creds = data.get("creds")
    channel_id = data.get("channel_id")
    if not token or not user_id or not channel_id or not creds:
        return jsonify({"status": False}), 403
    try:
        save_tokens(token, user_id, creds, channel_id)
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 403
    return jsonify({"status": True}), 200

@app.route("/youtube/upload/short", methods=["POST"])
def upload():
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id")
    title = data.get("title")
    if not file_id or not title:
        return jsonify({"error": "file_id and title are required"}), 400
    try:
        creds = get_credentials()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 401
    try:
        response = upload_from_drive_to_youtube( creds, file_id=file_id, title=title, description=data.get("description", ""), tags=data.get("tags"), category_id=data.get("category_id", "22"), privacy_status=data.get("privacy_status", "private"), )
    except HttpError as e:
        return jsonify({"error": "http_error", "detail": str(e)}), 502
    return jsonify({"status": "uploaded", "youtube_response": response})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
    # DRIVE_FILE_ID = "PASTE_DRIVE_FILE_ID_HERE"
    # try:
    #     result = upload_from_drive_to_youtube( DRIVE_FILE_ID, title="My Drive video", description="Uploaded straight from Drive, no local download", tags=["demo"], privacy_status="private", )
    # except HttpError as e:
    #     print(f"Failed: {e}")