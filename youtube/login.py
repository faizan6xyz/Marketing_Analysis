import io
import os
import json
import secrets
from flask import Flask, request, redirect, session, jsonify, url_for
import google_auth_oauthlib.flow
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
SCOPES = [ "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/drive.readonly",]
CLIENT_SECRETS_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "http://localhost:5000/oauth2callback")

def credentials_to_dict(creds: Credentials) -> dict:
    return { "token": creds.token, "refresh_token": creds.refresh_token, "token_uri": creds.token_uri, "client_id": creds.client_id, "client_secret": creds.client_secret, "scopes": creds.scopes, }


def load_saved_credentials() -> Credentials | None:
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return creds
    return None

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

def upload_from_drive_to_youtube(
    creds, file_id: str, title: str, description: str = "", tags=None, category_id: str = "22", privacy_status: str = "private", ):
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

@app.route("/login")
def login():
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, redirect_uri=REDIRECT_URI )
    authorization_url, state = flow.authorization_url( access_type="offline", include_granted_scopes="true", prompt="consent", )
    session["state"] = state
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    state = session.get("state")
    if not state or state != request.args.get("state"):
        return jsonify({"error": "invalid_state"}), 400
    flow = google_auth_oauthlib.flow.Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=SCOPES, state=state, redirect_uri=REDIRECT_URI )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    session["credentials"] = credentials_to_dict(creds)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    return jsonify({"status": "authenticated"})

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