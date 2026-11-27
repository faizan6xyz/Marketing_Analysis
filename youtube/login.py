import os
import limit as lmmm
import json
import tempfile
import time
from datetime import datetime, timezone, timedelta
import requests
import Instagram.schedule_video as sccc
from flask import Flask, request, jsonify , redirect
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import google.auth.transport.requests
import isodate
from moviepy import VideoFileClip
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import Flow
import database.UserDB as dbimp
import authnew as au
import Drive.dep as dpp
from concurrent.futures import ThreadPoolExecutor
import logging
from dotenv import load_dotenv
load_dotenv()
YOUTUBE_SCOPES = [ "https://www.googleapis.com/auth/youtube.upload","https://www.googleapis.com/auth/youtube.readonly","https://www.googleapis.com/auth/drive.readonly","https://www.googleapis.com/auth/yt-analytics.readonly",]
CLIENT_SECRETS_FILE = "client_secret.json"
BASE_URL = os.environ.get("BASE_URL")
STATE_MAX_AGE = 600  # seconds
MAX_VIDEO_SIZE = 30 * 1024 * 1024  # 30 MB
RESUMABLE_UPLOAD_MAX_RETRIES = 3
RETRIABLE_STATUS_CODES = (500, 502, 503, 504)
MAX_UPLOAD_WORKERS = 4
YOUTUBE_API_KEY = os.getenv("Youtube_api_key")
YOUTUBE_DATA_URL = "https://www.googleapis.com/youtube/v3"
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("drive_api")
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 31 * 1024 * 1024  # means 31 MB is the maximum request size it could have 
executor = ThreadPoolExecutor(max_workers=2)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
Clientid = os.environ.get("client_id")
Clientsec = os.environ.get("client_secrect")
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
TABLE_NAME = "youtube"
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
serializer = URLSafeTimedSerializer(app.secret_key)
MAX_SHORT_SECONDS = 180
MAX_FAILURE = 3
ALLOWED_VIDEO_MIMETYPES = {"video/mp4", "video/quicktime", "video/webm"}
session = requests.Session()    

def get_video_duration(file_path):
    with VideoFileClip(file_path) as video:
        return video.duration

def get_file_and_upload_to_drive(service):
    uploaded_file = request.files.get("file")
    if uploaded_file is None or uploaded_file.filename == "":
        return None, jsonify({"error": "no file provided"}), 400
    if uploaded_file.mimetype not in ALLOWED_VIDEO_MIMETYPES:
        return None, jsonify({"error": f"unsupported file type: {uploaded_file.mimetype}"}), 400
    uploaded_file.stream.seek(0, os.SEEK_END)
    uploaded_file.stream.seek(0)
    make_public = True
    tmp_path = None
    drive_file_id = None
    try:
        suffix = os.path.splitext(uploaded_file.filename)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            uploaded_file.save(tmp.name)
            tmp_path = tmp.name
        file_metadata = {"name": uploaded_file.filename}
        media_upload = MediaFileUpload(tmp_path, mimetype=uploaded_file.mimetype, resumable=True)
        created_file = service.files().create(body=file_metadata, media_body=media_upload,fields="id, name, webViewLink, webContentLink, mimeType",).execute()
        drive_file_id = created_file["id"]
        if make_public:
            service.permissions().create( fileId=drive_file_id, body={"type": "anyone", "role": "reader"},).execute()
        return drive_file_id, None, None
    except HttpError as e:
        if drive_file_id:
            try:
                service.files().delete(fileId=drive_file_id).execute()
            except HttpError:
                pass
        return None, jsonify({"error": str(e)}), 500
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

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
    return Flow.from_client_config({"web": { "client_id": Clientid, "client_secret": Clientsec, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",   "redirect_uris": [f"{BASE_URL}/youtube/auth/callback"],}}, scopes=YOUTUBE_SCOPES, redirect_uri=f"{BASE_URL}/youtube/auth/callback",autogenerate_code_verifier=True)

def save_youtube_account_web( user_id, channel_id, channel_title, creds: Credentials):
    payload = { "id": user_id, "account_id": channel_id, "channel_title": channel_title, "access_token": creds.token, "refresh_token": creds.refresh_token, "token_expire": _expiry_to_iso(creds.expiry), "timestamp": datetime.now(timezone.utc).isoformat(), }
    dbimp.update_rows("users",{"youtube":True},{"user_id":user_id})
    rows = dbimp.select_rows( TABLE_NAME, select="account_id", filters={"id": user_id, "account_id": channel_id},    )
    if rows:
        dbimp.update_rows(TABLE_NAME, payload, filters={"id": user_id, "account_id": channel_id})
    else:
        dbimp.insert_rows( TABLE_NAME, payload)

def get_youtube_credentials_for_account(token, user_id, channel_id):
    rows = dbimp.select_rows( TABLE_NAME, select="access_token,refresh_token,token_expire",filters={"id": user_id, "account_id": channel_id},)
    row = rows[0] if rows else None
    if not row:
        return None
    creds = Credentials( token=row["access_token"], refresh_token=row["refresh_token"], token_uri="https://oauth2.googleapis.com/token", client_id=Clientid, client_secret=Clientsec,scopes=YOUTUBE_SCOPES,    )
    expiry = _iso_to_aware_utc(row.get("token_expire"))
    needs_refresh = expiry is None or (expiry - datetime.now(timezone.utc) < timedelta(minutes=5))
    if needs_refresh and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        dbimp.update_rows( TABLE_NAME, {"access_token": creds.token, "token_expire": _expiry_to_iso(creds.expiry)},filters={"id": user_id, "account_id": channel_id},        )
    return creds

def get_youtube_credentials_for_account_web(channel_id):
    rows = dbimp.select_rows( TABLE_NAME, select="access_token,refresh_token,token_expire", filters={"account_id": channel_id}, )
    row = rows[0] if rows else None
    if not row:
        return None
    creds = Credentials( token=row["access_token"], refresh_token=row["refresh_token"], token_uri="https://oauth2.googleapis.com/token", client_id=Clientid, client_secret=Clientsec, scopes=YOUTUBE_SCOPES,)
    expiry = _iso_to_aware_utc(row.get("token_expire"))
    needs_refresh = expiry is None or (expiry - datetime.now(timezone.utc) < timedelta(minutes=5))
    if needs_refresh and creds.refresh_token:
        creds.refresh(google.auth.transport.requests.Request())
        dbimp.update_rows( TABLE_NAME, {"access_token": creds.token, "token_expire": _expiry_to_iso(creds.expiry)}, filters={"account_id": channel_id},)
    return creds

def shorts_query(user_id, access_token, media_id, typee):
    for i in range(7):
        timesss = (datetime.now(timezone.utc) + timedelta(days=i)).isoformat()
        sccc.insert__story1(user_id, timesss, access_token, media_id, typee) 

def get_valid_access_token(channel_id, token, client_id, client_secret, refresh_token, access_token, expires_at_iso=None):
    now = datetime.now(timezone.utc)
    expiry = _iso_to_aware_utc(expires_at_iso)
    if access_token and expiry and now < expiry - timedelta(seconds=60):
        return access_token
    resp = session.post( "https://oauth2.googleapis.com/token", data={ "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token", }, timeout=10,     )
    resp.raise_for_status()
    token_data = resp.json()
    new_access_token = token_data["access_token"]
    new_expiry_iso = (now + timedelta(seconds=token_data.get("expires_in", 3600))).isoformat()
    try:
        dbimp.update_rows( TABLE_NAME, {"access_token": new_access_token, "token_expire": new_expiry_iso}, filters={"account_id": channel_id}, )
    except Exception as e:
        logger.error(f"token update failed for channel {channel_id}: {e}")
    return new_access_token

def get_valid_access_token11(channel_id,client_id, client_secret, refresh_token, access_token,expires_at_iso):
    now = datetime.now(timezone.utc)
    expiry = _iso_to_aware_utc(expires_at_iso)
    if access_token and expiry and now < expiry - timedelta(seconds=60):
        return access_token
    resp = session.post( "https://oauth2.googleapis.com/token", data={ "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token", }, timeout=10,     )
    resp.raise_for_status()
    token_data = resp.json()
    new_access_token = token_data["access_token"]
    new_expiry_iso = (now + timedelta(seconds=token_data.get("expires_in", 3600))).isoformat()
    try:
        dbimp.update_rows(TABLE_NAME, {"access_token": new_access_token, "token_expire": new_expiry_iso}, filters={"account_id": channel_id}, )
    except Exception as e:
        logger.error(f"token update failed for channel {channel_id}: {e}")
    return new_access_token

def shorts_schedule(channel_id, video_id, access_token):
    start_date = (datetime.now(timezone.utc) - timedelta(hours=24)).date().isoformat()
    rows = dbimp.select_rows( TABLE_NAME, select="refresh_token,token_expire", filters={"account_id": channel_id})
    row = rows[0] if rows else None
    if not row:
        return False
    refresh_token = row["refresh_token"]
    expires_at_iso = row["token_expire"]
    access_token = get_valid_access_token11(channel_id,Clientid, Clientsec, refresh_token, access_token, expires_at_iso)
    if not access_token:
        return False
    meta_resp = session.get( "https://www.googleapis.com/youtube/v3/videos", params={"part": "snippet,statistics", "id": video_id, "key": YOUTUBE_API_KEY},timeout=10,    ).json()
    item = (meta_resp.get("items") or [{}])[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    analytics_resp = session.get( "https://youtubeanalytics.googleapis.com/v2/reports", params={ "ids": "channel==MINE", "startDate": start_date, "endDate": datetime.now(timezone.utc).date().isoformat(), "metrics": "estimatedMinutesWatched,averageViewDuration,shares,subscribersGained", "dimensions": "video", "filters": f"video=={video_id}", "access_token": access_token,}, timeout=10,     ).json()
    headers = [c["name"] for c in analytics_resp.get("columnHeaders", [])]
    row = analytics_resp.get("rows", [[]])[0] if analytics_resp.get("rows") else []
    metrics = dict(zip(headers, row))
    return (f"{video_id},{snippet.get('publishedAt')},{stats.get('viewCount')},{stats.get('likeCount')},"f"{stats.get('commentCount')},{metrics.get('shares')},{metrics.get('estimatedMinutesWatched')},"f"{metrics.get('averageViewDuration')},{metrics.get('impressions')}," f"{metrics.get('impressionsClickThroughRate')},{metrics.get('subscribersGained')}")

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

def _upload_resumable_with_retry(request_):
    response = None
    retries = 0
    while response is None:
        try:
            _status, response = request_.next_chunk()
        except HttpError as e:
            if e.resp is not None and e.resp.status in RETRIABLE_STATUS_CODES and retries < RESUMABLE_UPLOAD_MAX_RETRIES:
                retries += 1
                time.sleep(min(2 ** retries,16))
                continue
            raise
        except (ConnectionError, TimeoutError, OSError):
            if retries < RESUMABLE_UPLOAD_MAX_RETRIES:
                retries += 1
                time.sleep(min(2 ** retries,16))
                continue
            raise
    return response

def post_later(channel_id, file_id, text1, text2, text3):
    channel_ids = json.loads(channel_id[0])
    chann = channel_ids[0]
    rows = dbimp.select_rows(TABLE_NAME,select="id",filters={"account_id":chann})
    if not rows :
        return False
    user_id = rows[0]["id"]
    drive_service = dpp.get_drive_service(user_id)
    tmp_path, mimetype, name = dpp.download_drive_file_to_temp(drive_service, file_id)
    if not tmp_path:
        logger.error(f"post_later: unable to download drive file {file_id}")
        return False
    video_ids = []
    try:
        for channel_id in channel_ids :
            creds = get_youtube_credentials_for_account_web(channel_id)
            if not creds:
                logger.error(f"post_later: no credentials found for channel {channel_id}")
                continue
            try:
                response = upload_video_to_youtube_channel( creds, tmp_path, mimetype, title=text1, description=text2, tags=text3, )
                video_id = response.get("id")
            except Exception as e:
                logger.error(f"post_later: upload failed for channel {channel_id}: {e}")
                continue
            video_ids.append(video_id)
            try:
                shorts_query(channel_id, creds.token, video_id, "shorts")
            except Exception as e:
                logger.error(f"shorts_query scheduling failed for channel {channel_id}: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
        rollback_drive(service=drive_service,file_ids=[file_id])
    return video_ids[0] if video_ids else False

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

def rollback_drive(service, file_ids):
    if not file_ids:
        return
    if isinstance(file_ids, str):
        file_ids = [file_ids]
    logger.info(f"rolling back {len(file_ids)} drive file(s)")
    for file_id in file_ids:
        deleteFile(service, file_id)

def _publish_to_channel(token, user_id, channel_id, upload_tmp_path, mimetype, caption, description, tags):
    creds = get_youtube_credentials_for_account(token, user_id, channel_id)
    if not creds:
        logger.error(f"no credentials for channel {channel_id}")
        return None
    try:
        response = upload_video_to_youtube_channel(creds, upload_tmp_path, mimetype, title=caption, description=description, tags=tags)
        video_id = response.get("id")
    except Exception:
        logger.exception(f"upload failed for channel {channel_id}")
        return None
    try:
        shorts_query(channel_id, creds.token, video_id, "shorts")
    except Exception as e:
        logger.warning(f"shorts_query scheduling failed for channel {channel_id}: {e}")
    return video_id

def _upload_one_account(account_ids, token, user_id, upload_tmp_path, mimetype, caption, description, tags, publish_now, timee):
    if not publish_now:
        service = dpp.get_drive_service(user_id)
        created_file, err = dpp.upload_path_to_drive( service, upload_tmp_path, os.path.basename(upload_tmp_path), mimetype, )
        if err is not None or not created_file:
            logger.error("unable to upload to the drive")
            return False
        channel_id  = json.dumps(account_ids)
        sccc.insert_post( user_id=channel_id, scheduled_time=timee, access_token="", typeee="Shorts_later", text1=caption, text2=description, text3=tags, media_id=created_file.get("id"), )
        return {"account": channel_id, "status": "scheduled", "scheduled_time": timee.isoformat()}
    failed_one = 0
    video_ids = []
    for account_id in account_ids :
        if failed_one > MAX_FAILURE :
            logger.error(f"failed to upload the shorts for user {user_id} , done with {results}")
            return False
        results = _publish_to_channel(token, user_id, account_id, upload_tmp_path, mimetype, caption, description, tags)
        if not results :
            logger.error(f"failed to upload the shorts for account_id {account_id}")
        video_ids.append(results)
    return video_ids 

def upload_video_to_youtube_channel(creds, tmp_path, mimetype, title, description="", tags=None, category_id="22", privacy_status="public"):
    youtube_service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    media = MediaFileUpload(tmp_path, mimetype=mimetype, chunksize=10 * 1024 * 1024, resumable=True)
    body = { "snippet": {"title": title, "description": description, "tags": tags or [], "categoryId": category_id},
        "status": {"privacyStatus": privacy_status}, }
    request_ = youtube_service.videos().insert(part="snippet,status", body=body, media_body=media)
    return _upload_resumable_with_retry(request_)

@app.route("/auth/login", methods=['POST'])
def connect_youtube():
    body = request.get_json(silent=True) or {}
    token = body.get("token") or request.args.get("token")
    if not token:
        return jsonify({"status": False}), 403
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    state = serializer.dumps(user_id)
    flow = build_flow()
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    try:
        try:
            dbimp.delete_rows("tableverify", {"id": user_id, "platform": "youtube"})
        except Exception:
            pass
        dbimp.insert_rows( "tableverify",{"id": user_id, "platform": "youtube", "code": flow.code_verifier})
    except Exception:
        logger.exception("failed to store pkce verifier")
        return jsonify({"status": False, "error": "could not start login"}), 500
    return jsonify({"status": True, "auth_url": auth_url}), 200

@app.route("/auth/callback", methods=['GET'])
def youtube_oauth_callback():
    state = request.args.get("state")
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "missing code", "details": request.args.get("error")}), 400
    if not state:
        return jsonify({"error": "missing state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    rows = dbimp.select_rows("tableverify", select="code", filters={"id": user_id, "platform": "youtube"})
    if not rows:
        return jsonify({"error": "no login in progress, please reconnect"}), 400
    verifier = rows[0]["code"]
    try:  
        dbimp.delete_rows("tableverify", {"id": user_id, "platform": "youtube"})
    except Exception:
        logger.warning(f"unable to delete verifier for {user_id} youtube")
    try:
        flow = build_flow()
        flow.code_verifier = verifier
        flow.fetch_token(code=code)
    except Exception:
        logger.exception("token exchange failed")
        return jsonify({"error": "token exchange failed, please reconnect"}), 401
    creds = flow.credentials
    try:
        youtube = build("youtube", "v3", credentials=creds)
        channel = youtube.channels().list(part="snippet", mine=True).execute()
    except Exception:
        logger.exception("youtube channel lookup failed")
        return jsonify({"error": "youtube error"}), 502
    items = channel.get("items", [])
    if not items:
        return jsonify({"error": "no_youtube_channel","detail": "authenticated Google account has no YouTube channel"}), 400
    channel_title = items[0]["snippet"]["title"]
    channel_id = items[0]["id"]
    try:
        save_youtube_account_web(user_id, channel_id, channel_title, creds)
    except Exception as e:
        logger.exception("save_youtube_account failed")
        return jsonify({"status": False, "error": "could not save account"}), 403
    return redirect(homepage, code=302)

@app.route("/upload/short", methods=["POST"])
@limiter.limit("5 per minute")
def upload():
    data = request.form
    token = data.get("token")
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    publish = data.get("publish", "true")
    timee_raw = data.get("time")
    publish_now = str(publish).strip().lower() == "true"
    all_files = [f for _, f in request.files.items(multi=True)]
    if len(all_files) != 1:
        return jsonify({"error": "exactly one file is allowed per upload (form-data field: file)"}), 400
    accounts = data.getlist("account_id")
    if not accounts :
            return jsonify({"error": "accounts must be a there"}), 400
    if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
        return jsonify({"error": "accounts must be a list"}), 400
    accounts = list(dict.fromkeys(a.strip() for a in accounts if a.strip()))
    if not accounts:
        return jsonify({"error": "'accounts' is required (one or more connected channel ids)"}), 400
    now_dt = datetime.now(timezone.utc)
    timee = None
    if not publish_now:
        timee = parse_datetime(timee_raw)
        if timee is None:
            return jsonify({"error": "invalid or missing date/time"}), 400
        lb = now_dt + timedelta(seconds=180)
        up = now_dt + timedelta(hours=72)
        if timee < lb or timee > up:
            return jsonify({"error": "invalid time for the posting"}), 400
    if not lmmm.checkk(tokench["token"],user_id,now_dt.isoformat(),len(accounts)):
        return jsonify({"error": "limit has been reached"}) ,400
    caption = data.get("caption") or ""
    description = data.get("description") or ""
    tags = [t.strip() for t in (data.get("tags") or "").split(",") if t.strip()]
    try:
        executor.submit(upload_short_job,request.files.get("file") , caption, description, tags, accounts, token, user_id, publish_now, timee)
    except Exception:
        logger.exception(f"failed to queue the upload for shorts for {accounts} from the user {user_id}")
    return jsonify({"status":True}), 200
    
def upload_short_job(request , caption, description, tags, accounts, token, user_id, publish_now, timee):
    uploaded_file = request.files.get("file")
    if not uploaded_file or uploaded_file.filename == "":
        return jsonify({"error": "file required (form-data field: file)"}), 400
    if uploaded_file.mimetype not in ALLOWED_VIDEO_MIMETYPES:
        return jsonify({"error": f"unsupported file type: {uploaded_file.mimetype}"}), 400
    uploaded_file.stream.seek(0, os.SEEK_END)
    size = uploaded_file.stream.tell()
    uploaded_file.stream.seek(0)
    if size > MAX_VIDEO_SIZE:
        return jsonify({"error": f"file exceeds max size of {MAX_VIDEO_SIZE} bytes"}), 400
    try:
        suffix = os.path.splitext(uploaded_file.filename)[1] or ".mp4"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            uploaded_file.save(tmp.name)
            upload_tmp_path = tmp.name
    except :
        logger.error(f"failed to save the tempfile for the user {user_id}")
        if upload_tmp_path and os.path.exists(upload_tmp_path):
            os.remove(upload_tmp_path)
    try:
        if get_video_duration(upload_tmp_path) > MAX_SHORT_SECONDS:
            logger.info(f"video exceeds max short length of {MAX_SHORT_SECONDS} seconds for user {user_id}")
            return
        x = _upload_one_account(accounts, token, user_id, upload_tmp_path, uploaded_file.mimetype, caption, description, tags, publish_now, timee)
        if not  x :
            logger.info(f"fail to post the user shorts {accounts}")
    except Exception:
        logger.exception(f"upload job crashed for user {user_id}")
    finally:
        if upload_tmp_path and os.path.exists(upload_tmp_path):
            os.remove(upload_tmp_path)

@app.route("/shorts", methods=["POST"])
@limiter.limit("10 per minute")
def fetch_shorts():
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token")
    account_name = request.args.get("name") or (request.get_json(silent=True) or {}).get("name")
    if not token or not account_name:
        return jsonify({"error": "'token' and 'name' are required"}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    rows = dbimp.select_rows( TABLE_NAME, select="account_id", filters={"id": tokench["user_id"], "channel_title": account_name},    )
    if not rows:
        return jsonify({"error": "account not found"}), 404
    channel_id = rows[0]["account_id"]
    channel_resp = session.get( f"{YOUTUBE_DATA_URL}/channels", params={"part": "contentDetails", "id": channel_id, "key": YOUTUBE_API_KEY}, timeout=10, )
    channel_resp.raise_for_status()
    channel_items = channel_resp.json().get("items", [])
    if not channel_items:
        return jsonify({"error": "channel not found on youtube"}), 404
    uploads_playlist_id = channel_items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    video_ids = []
    page_token = None
    while True:
        params = {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50, "key": YOUTUBE_API_KEY}
        if page_token:
            params["pageToken"] = page_token
        playlist_resp = session.get(f"{YOUTUBE_DATA_URL}/playlistItems", params=params, timeout=10)
        playlist_resp.raise_for_status()
        data = playlist_resp.json()
        video_ids.extend(item["contentDetails"]["videoId"] for item in data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    shorts = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i: i + 50]
        videos_resp = session.get( f"{YOUTUBE_DATA_URL}/videos", params={"part": "snippet,contentDetails,statistics", "id": ",".join(batch), "key": YOUTUBE_API_KEY}, timeout=10, )
        videos_resp.raise_for_status()
        for item in videos_resp.json().get("items", []):
            seconds = int(isodate.parse_duration(item["contentDetails"]["duration"]).total_seconds())
            if seconds <= MAX_SHORT_SECONDS:
                shorts.append({ "video_id": item["id"], "published_at": item["snippet"]["publishedAt"], "caption": item["snippet"]["title"], "thumbnail": item["snippet"]["thumbnails"]["high"]["url"], "views": item["statistics"].get("viewCount", 0), "likes": item["statistics"].get("likeCount", 0), "comments": item["statistics"].get("commentCount", 0), "duration_seconds": seconds,})
    return jsonify({"shorts": shorts})

if __name__ == "__main__":
    app.run(debug=True, port=5000)