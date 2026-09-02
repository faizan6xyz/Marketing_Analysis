import os
import json
import secrets
import tempfile
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import Instagram.schedule_video as sccc  
from flask import Flask, request, redirect, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import Flow
import Drive.dep as dpp
import database.UserDB as dbimp
import authnew as au
YOUTUBE_SCOPES = [ "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/drive.readonly",]
CLIENT_SECRETS_FILE = "client_secret.json"
BASE_URL = os.environ.get("baseurl")
STATE_MAX_AGE = 600  # seconds
MAX_VIDEO_SIZE = 30 * 1024 * 1024  # 30 MB
RESUMABLE_UPLOAD_MAX_RETRIES = 5
RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
MAX_UPLOAD_WORKERS = 4
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
Clientid = os.environ.get("client_id")
Clientsec = os.environ.get("client_secrect")
api_key = os.environ.get("api_key")
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],    allow_headers=["Content-Type", "Authorization", "Request-ID"],)
TABLE_NAME = "Youtube"
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
serializer = URLSafeTimedSerializer(app.secret_key)

def _to_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _expiry_to_iso(dt: datetime | None) -> str | None:
    if not dt:
        return None
    return _to_aware_utc(dt).isoformat()

def _iso_to_aware_utc(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    return _to_aware_utc(datetime.fromisoformat(iso_str))

def build_flow():
    return Flow.from_client_config( { "web": { "client_id": Clientid, "client_secret": Clientsec,"auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [f"{BASE_URL}/oauth/youtube/callback"], } }, scopes=YOUTUBE_SCOPES, redirect_uri=f"{BASE_URL}/oauth/youtube/callback", )

def credentials_from_json(creds_json: str) -> Credentials:
    data = json.loads(creds_json)
    creds = Credentials( token=data["token"], refresh_token=data.get("refresh_token"),token_uri=data["token_uri"], client_id=data["client_id"], client_secret=data["client_secret"],scopes=data.get("scopes"), )
    if data.get("expiry"):
        creds.expiry = datetime.fromisoformat(data["expiry"])
    return creds

def save_youtube_account(token, user_id, channel_id, channel_title, creds: Credentials):
    payload = { "user_id": user_id , "channel_id": channel_id, "channel_title": channel_title, "Access_token": creds.token, "Refresh_token": creds.refresh_token, "Token_expire": _expiry_to_iso(creds.expiry), "Timestamp": datetime.now(timezone.utc).isoformat(), }
    rows = dbimp.select_rows( token, TABLE_NAME, select="channel_id", filters={"user_id": user_id, "channel_id": channel_id}, )
    if rows:
        dbimp.update_rows(token, TABLE_NAME, payload, filters={"user_id": user_id, "channel_id": channel_id})
    else:
        dbimp.insert_rows(token, TABLE_NAME, payload)

def get_youtube_credentials_for_account(token, user_id, channel_id):
    rows = dbimp.select_rows( token, TABLE_NAME, select="Access_token,Refresh_token,Token_expire", filters={"user_id": user_id, "channel_id": channel_id}, )
    row = rows[0] if rows else None
    if not row:
        return None
    creds = Credentials( token=row["Access_token"], refresh_token=row["Refresh_token"], token_uri="https://oauth2.googleapis.com/token", client_id=Clientid, client_secret=Clientsec, scopes=YOUTUBE_SCOPES, )
    expiry = _iso_to_aware_utc(row.get("Token_expire"))
    needs_refresh = expiry is None or (expiry - datetime.now(timezone.utc) < timedelta(minutes=5))
    if needs_refresh and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        dbimp.update_rows( token, TABLE_NAME, {"Access_token": creds.token, "Token_expire": _expiry_to_iso(creds.expiry)}, filters={"user_id": user_id, "channel_id": channel_id}, )
    return creds

def xcccc(user_id, access_token, media_id, token, typee):
    for i in range(7):
        timesss = (datetime.now(timezone.utc) + timedelta(days=i)).isoformat()
        sccc.insert__story1(user_id, timesss, access_token, media_id, token, typee)

def get_valid_access_token(channel_id, token, client_id, client_secret, refresh_token, access_token, expires_at_iso=None):
    now = datetime.now(timezone.utc)
    expiry = _iso_to_aware_utc(expires_at_iso)
    if access_token and expiry and now < expiry - timedelta(seconds=60):
        return access_token
    resp = requests.post( "https://oauth2.googleapis.com/token", data={ "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token", }, timeout=10, )
    resp.raise_for_status()
    token_data = resp.json()
    new_access_token = token_data["access_token"]
    new_expiry_iso = (now + timedelta(seconds=token_data.get("expires_in", 3600))).isoformat()
    try:
        dbimp.update_rows( token, TABLE_NAME, {"Access_token": new_access_token, "Token_expire": new_expiry_iso},
            filters={"channel_id": channel_id}, )
    except Exception as e:
        print(f"token update failed for channel {channel_id}: {e}")
        return False
    return new_access_token

def shorts_schedule(channel_id, token, video_id, api_key, access_token, start_date="2026-01-01"):
    tokench = au.process(token=token)
    rows = dbimp.select_rows(tokench["token"], TABLE_NAME, select="Refresh_token,Token_expire", filters={"channel_id": channel_id})
    row = rows[0] if rows else None
    if not row:
        return False
    refresh_token = row["Refresh_token"]
    expires_at_iso = row["Token_expire"]
    access_token = get_valid_access_token(channel_id, token, Clientid, Clientsec, refresh_token, access_token, expires_at_iso)
    if not access_token:
        return False
    meta_resp = requests.get( "https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet,statistics", "id": video_id, "key": api_key}, timeout=10,).json()
    item = meta_resp.get("items", [{}])[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    analytics_resp = requests.get( "https://youtubeanalytics.googleapis.com/v2/reports", params={ "ids": "channel==MINE", "startDate": start_date, "endDate": datetime.now(timezone.utc).date().isoformat(), "metrics": "estimatedMinutesWatched,averageViewDuration,shares,impressions,subscribersGained",  "dimensions": "video", "filters": f"video=={video_id}", "access_token": access_token, },
        timeout=10, ).json()
    headers = [c["name"] for c in analytics_resp.get("columnHeaders", [])]
    row = analytics_resp.get("rows", [[]])[0] if analytics_resp.get("rows") else []
    metrics = dict(zip(headers, row))
    return ( f"{video_id},{snippet.get('publishedAt')},{stats.get('viewCount')},{stats.get('likeCount')}," f"{stats.get('commentCount')},{metrics.get('shares')},{metrics.get('estimatedMinutesWatched')}," f"{metrics.get('averageViewDuration')},{metrics.get('impressions')}," f"{metrics.get('impressionsClickThroughRate')},{metrics.get('subscribersGained')}")

def download_drive_file_to_temp(drive_service, file_id):
    meta = drive_service.files().get(fileId=file_id, fields="size,mimeType,name").execute()
    mimetype = meta.get("mimeType") or "video/*"
    name = meta.get("name") or "video"
    suffix = os.path.splitext(name)[1] or ".mp4"
    media_request = drive_service.files().get_media(fileId=file_id)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as tmp_file:
        downloader = MediaIoBaseDownload(tmp_file, media_request, chunksize=10 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return tmp_path, mimetype, name

def _upload_resumable_with_retry(request_):
    response = None
    retries = 0
    while response is None:
        try:
            _status, response = request_.next_chunk()
        except HttpError as e:
            if e.resp is not None and e.resp.status in RETRIABLE_STATUS_CODES and retries < RESUMABLE_UPLOAD_MAX_RETRIES:
                retries += 1
                time.sleep(min(2 ** retries, 30))
                continue
            raise
    return response

def upload_video_to_youtube_channel(creds, tmp_path, mimetype, title, description="", tags=None, category_id="22", privacy_status="public"):
    youtube_service = build("youtube", "v3", credentials=creds)
    media = MediaFileUpload(tmp_path, mimetype=mimetype, chunksize=10 * 1024 * 1024, resumable=True)
    body = {"snippet": { "title": title, "description": description, "tags": tags or [], "categoryId": category_id, },"status": {"privacyStatus": privacy_status}, }
    request_ = youtube_service.videos().insert(part="snippet,status", body=body, media_body=media)
    return _upload_resumable_with_retry(request_)

@app.route("/connect-youtube")
def connect_youtube():
    token = request.args.get("token")
    if not token:
        return jsonify({"status": False}), 403
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    state = serializer.dumps(user_id)
    flow = build_flow()
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
    flow = build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    creds_json = creds.to_json()
    youtube = build("youtube", "v3", credentials=creds)
    channel = youtube.channels().list(part="snippet", mine=True).execute()
    items = channel.get("items", [])
    if not items:
        return jsonify({"error": "no_youtube_channel", "detail": "authenticated Google account has no YouTube channel"}), 400
    channel_title = items[0]["snippet"]["title"]
    channel_id = items[0]["id"]
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    internal_token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    payload = {"user_id": user_id, "channel_id": channel_id, "channel_title": channel_title, "creds": creds_json, "token": internal_token,}
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
    channel_id = data.get("channel_id")
    channel_title = data.get("channel_title")
    creds_json = data.get("creds")
    if not token or not user_id or not channel_id or not creds_json:
        return jsonify({"status": False}), 403
    try:
        creds = credentials_from_json(creds_json)
        save_youtube_account(token, user_id, channel_id, channel_title, creds)
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 403
    return jsonify({"status": True}), 200

@app.route("/youtube/accounts", methods=["POST"])
@limiter.limit("10 per minute")
def list_youtube_accounts():
    token = request.form.get("token") or (request.get_json(silent=True) or {}).get("token")
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    rows = dbimp.select_rows(token, TABLE_NAME, select="channel_id,channel_title", filters={"user_id": user_id})
    return jsonify({"accounts": rows or []})

@app.route("/youtube/upload/short", methods=["POST"])
@limiter.limit("5 per minute")
def upload():
    token = request.form.get("token")
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    accounts = request.form.getlist("accounts")
    if not accounts:
        return jsonify({"error": "'accounts' is required (one or more connected channel ids)"}), 400
    service, err = dpp.authenticate_and_get_service(token)
    if err:
        return err
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return jsonify({"error": "file required (form-data field: file)"}), 400
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if size > MAX_VIDEO_SIZE:
        return jsonify({"error": f"file exceeds max size of {MAX_VIDEO_SIZE} bytes"}), 400
    parent_id = request.form.get("parent_id")
    platform = "Youtube"
    subfolder = "Upload"
    make_public = True
    try:
        if parent_id:
            parent_folder = parent_id
        else:
            platform_id, _ = dpp.get_or_create_folder(service, platform)
            sub_id, _ = dpp.get_or_create_folder(service, subfolder, parent_id=platform_id)
            parent_folder = sub_id
    except HttpError as e:
        return jsonify({"error": "drive folder setup failed", "detail": str(e)}), 400
    upload_tmp_path = None
    drive_file_id = None
    channel_tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            uploaded_file.save(tmp.name)
            upload_tmp_path = tmp.name
        file_metadata = {"name": uploaded_file.filename}
        if parent_folder:
            file_metadata["parents"] = [parent_folder]
        media_upload = MediaFileUpload(upload_tmp_path, mimetype=uploaded_file.mimetype, resumable=True)
        created_file = service.files().create( body=file_metadata, media_body=media_upload, fields="id, name, webViewLink, webContentLink, mimeType", ).execute()
        drive_file_id = created_file["id"]
        if make_public:
            service.permissions().create(fileId=drive_file_id, body={"type": "anyone", "role": "reader"}).execute()
        channel_tmp_path, mimetype, _name = download_drive_file_to_temp(service, drive_file_id)
    except HttpError as e:
        return jsonify({"error": "drive upload/download failed", "detail": str(e)}), 400
    finally:
        if upload_tmp_path and os.path.exists(upload_tmp_path):
            os.remove(upload_tmp_path)
    caption = request.form.get("caption") or ""
    description = request.form.get("description") or ""
    tags = [t.strip() for t in (request.form.get("tags") or "").split(",") if t.strip()]
    def _upload_one(channel_id):
        creds = get_youtube_credentials_for_account(token, user_id, channel_id)
        if not creds:
            return {"account": channel_id, "status": "failed", "error": "channel not connected"}
        try:
            response = upload_video_to_youtube_channel( creds, channel_tmp_path, mimetype, title=caption, description=description, tags=tags, )
            video_id = response.get("id")
            try:
                sccc(user_id, creds.token, video_id, token, "shorts")
            except Exception as e:
                print(f"xcccc scheduling failed for channel {channel_id}: {e}")
            return {"account": channel_id, "status": "uploaded", "youtube_video_id": video_id}
        except HttpError as e:
            return {"account": channel_id, "status": "failed", "error": str(e)}
        except Exception as e:
            return {"account": channel_id, "status": "failed", "error": str(e)}
    results = []
    try:
        with ThreadPoolExecutor(max_workers=min(MAX_UPLOAD_WORKERS, len(accounts))) as executor:
            futures = {executor.submit(_upload_one, ch): ch for ch in accounts}
            for future in as_completed(futures):
                results.append(future.result())
    finally:
        if channel_tmp_path and os.path.exists(channel_tmp_path):
            os.remove(channel_tmp_path)
        try:
            service.files().delete(fileId=drive_file_id).execute()
        except HttpError:
            pass
    return jsonify({"count": len(results), "results": results})

if __name__ == "__main__":
    app.run(debug=True, port=5000)