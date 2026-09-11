import database.UserDB as dbimp
import os
import base64
import requests
from urllib.parse import urlencode
from moviepy import VideoFileClip
import Drive.dep as dpp
import json 
import tempfile
from flask_cors import CORS
import Instagram.schedule_video as sccc
from flask import Flask, request, redirect, jsonify
from datetime import datetime, timezone, timedelta , date , UTC
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import hashlib
import secrets
import time
import authnew as au
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
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
image_size = 10 * 1024 * 1024
STATE_MAX_AGE = 600  # seconds
PINTEREST_SCOPES = "boards:read,pins:read,pins:write,user_accounts:read"
MEDIA_REGISTER_URL = "https://api.pinterest.com/v5/media"
PIN_CREATE_URL = "https://api.pinterest.com/v5/pins"
PINTEREST_AUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
duation = 300
max_size = 100 * 1024 * 1024
METRIC_TYPES = "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK,ENGAGEMENT,USER_FOLLOW"

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, PINTEREST_TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

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

def refresh_pinterest_token11(access, username_id):
    rows = dbimp.select_rows_web(PINTEREST_TABLE_NAME , select="Refresh_token,Token_expire" , filters={"Account_id":username_id})
    if not rows:
        raise ValueError(f"No Pinterest token record found for user {username_id}")
    row = rows[0]
    refresh_token = row["Refresh_token"]
    expire = row["Token_expire"]
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
            dbimp.update_rows_web(PINTEREST_TABLE_NAME, { "Access_token": data["access_token"], "Refresh_token": data.get("refresh_token", refresh_token), "Token_expire": new_expiry.isoformat(), }, filters={"Account_id": username_id}, )
        except Exception as e:
            print(f"Failed to persist refreshed Pinterest token for user_i {e}")
        return data["access_token"]
    else:
        return access

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

def xcccc(user_id,access_token,media_id,typee):
    for i in range(7): 
        timesss = (datetime.now(timezone.utc) + timedelta(days=(i))).isoformat()
        sccc.insert__story1(user_id, timesss, access_token,media_id,typee)

def get_pinterest_pin_analytics_csv(username_id,pin_id, access_token):
    access_token = refresh_pinterest_token11(access_token,username_id)
    headers = {"Authorization": f"Bearer {access_token}"} 
    end_date = datetime.now(UTC)
    start_date = end_date - timedelta(hours=24)
    pin_resp = requests.get(f"{BASE_URL}/pins/{pin_id}", headers=headers)
    published_at = ""
    if pin_resp.ok:
        published_at = pin_resp.json().get("created_at", "")
    analytics_resp = requests.get( f"{BASE_URL}/pins/{pin_id}/analytics", headers=headers, params={ "start_date": start_date.strftime("%Y-%m-%d"), "end_date": end_date.strftime("%Y-%m-%d"), "metric_types": METRIC_TYPES, "app_types": "ALL", "split_field": "NO_SPLIT",},)
    metrics = {}
    if analytics_resp.ok:
        bucket = analytics_resp.json().get("all", {})
        metrics = bucket.get("lifetime_metrics") or bucket.get("summary_metrics") or {} 
    return f"{pin_id},{published_at},{metrics.get('IMPRESSION', 0)},{metrics.get('SAVE', 0)},{metrics.get('PIN_CLICK', 0)},{metrics.get('OUTBOUND_CLICK', 0)},{metrics.get('ENGAGEMENT', 0)},{metrics.get('USER_FOLLOW', 0)}"
  
def _authenticate(data):
    token = data.get("token")
    usernames = data.get("usernames")
    tokench = au.process(token=token)
    access_tokens = []
    Account_ids = []
    if not tokench["status"]:
        return None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200) , None , None
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return None, (jsonify({"error": "invalid user id"}), 401) , None , None
    if not username:
        return None, (jsonify({"error": "username is required"}), 400) , None , None
    for username in usernames :
        access_token, err ,Account_id= get_access_token_by_username(tokench["token"], user_id, username)
        if err:
            return None,  err , None , None
        access_tokens.append(access_token)
        Account_ids.append(Account_id)
    return access_tokens,  None, Account_ids , user_id

def get_access_token_by_username(token, user_id, username):
    rows = dbimp.select_rows( token, PINTEREST_TABLE_NAME, select="Account_id,Access_token,Refresh_token,Token_expire",filters={"id": user_id, "Username": username} )
    if not rows:
        return None, (jsonify({"error": "no x account linked for this username"}), 404) , None
    row = rows[0]
    access_token = row["Access_token"]
    raw_expiry = row["Token_expire"]
    Account_id = row["Account_id"]
    refresh_token = row["Refresh_token"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400) , None
    Token_expiry = datetime.fromisoformat(raw_expiry)
    access_token = refresh_pinterest_token(refresh_token, Token_expiry, access_token, token, user_id, username)
    return access_token, None , Account_id

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

def post_late(Account_id , access_token , title, description, board_id,  typee ,drive_file_id):
    Account_ids = json.loads(Account_id[0])
    Account_id = Account_ids[0]
    access_tokens = json.load(access_token[0])
    rows = dbimp.select_rows_web(PINTEREST_TABLE_NAME,select="id",filters={"Account_id":Account_id})
    if not rows :
        return False
    user_id = rows[0]["id"]
    service = dpp.get_drive_service(user_id)
    tmp_path, mimetype, name = download_drive_file_to_temp(service, drive_file_id)
    for access_token, Account_id in zip(access_tokens,Account_ids):
        access_token = refresh_pinterest_token11(access_token,Account_id)
        if typee == "photo":
            with open(tmp_path, "rb") as fh:
                photo_bytes = fh.read()
            b64_image = base64.b64encode(photo_bytes).decode("utf-8")
            url = "https://api.pinterest.com/v5/pins"
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            payload = { "board_id": board_id, "title": title, "description": description, "media_source": { "source_type": "image_base64",  "content_type": "image/jpeg", "data": b64_image, }, }
            try:
                resp = requests.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                return {"account": Account_id, "status": "posted", "response": resp.json()}
            except requests.HTTPError as e:
                error_detail = resp.text if resp is not None else str(e)
                return {"account": Account_id, "status": "failed", "error": error_detail}
            except requests.RequestException as e:
                return {"account": Account_id, "status": "failed", "error": str(e)}
        if typee == "video":
            try:
                resp = requests.post( MEDIA_REGISTER_URL, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}, json={"media_type": "video"},        )
                resp.raise_for_status()
                registration = resp.json()
            except requests.HTTPError as e:
                raise RuntimeError(f"registration failed: {e}") from e
            media_id = registration.get("media_id")
            upload_url = registration.get("upload_url")
            upload_parameters = registration.get("upload_parameters")
            if not media_id or not upload_url or not upload_parameters:
                raise RuntimeError(f"registration response missing required fields: {registration}")
            try:
                with open(tmp_path, "rb") as fh:
                    resp = requests.post(upload_url, data=upload_parameters, files={"file": fh})
                if not resp.ok:
                    raise RuntimeError(f"upload failed [{resp.status_code}]: {resp.text}")
            except OSError as e:
                raise RuntimeError(f"could not read file at {tmp_path}: {e}") from e
            deadline = time.time() + 60
            headers = {"Authorization": f"Bearer {access_token}"}
            status = None
            while time.time() < deadline:
                resp = requests.get(f"{MEDIA_REGISTER_URL}/{media_id}", headers=headers)
                resp.raise_for_status()
                status = resp.json().get("status")
                if status == "succeeded":
                    break
                if status == "failed":
                    raise RuntimeError(f"video processing failed for media_id={media_id}")
            else:
                raise RuntimeError(f"video processing timed out after {120}s for media_id={media_id}")
            try:
                resp = requests.post( PIN_CREATE_URL, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},json={ "title": title, "description": description, "board_id": board_id, "media_source": { "source_type": "video_id", "media_id": media_id, }, },)
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError as e:
                raise RuntimeError(f"pin creation failed: {e}") from e

def create_pinterest_video_pin_full(access_token, board_id, title, description, file_path,publish_now,timee,Account_id,user_id,typeee,mimetype ,timeout=60 ):
    if not publish_now :
        service = dpp.get_drive_service(user_id)
        drive_file_id, err = upload_path_to_drive(service, file_path, os.path.basename(file_path), mimetype)
        if err is not None:
            return {"account": Account_id, "status": "failed", "error": f"drive upload failed: {err}"}
        list_account_ids=json.dumps(Account_id)
        sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token=access_token, typeee=typeee, text1=title, text2=description , text3=board_id, media_id=drive_file_id,  )
        return {"account": Account_id, "status": "scheduled", "scheduled_time": timee.isoformat()}
    for access in access_token  : 
        try:
            resp = requests.post( MEDIA_REGISTER_URL, headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"}, json={"media_type": "video"},        )
            resp.raise_for_status()
            registration = resp.json()
        except requests.HTTPError as e:
            raise RuntimeError(f"registration failed: {e}") from e
        media_id = registration.get("media_id")
        upload_url = registration.get("upload_url")
        upload_parameters = registration.get("upload_parameters")
        if not media_id or not upload_url or not upload_parameters:
            raise RuntimeError(f"registration response missing required fields: {registration}")
        try:
            with open(file_path, "rb") as fh:
                resp = requests.post(upload_url, data=upload_parameters, files={"file": fh})
            if not resp.ok:
                raise RuntimeError(f"upload failed [{resp.status_code}]: {resp.text}")
        except OSError as e:
            raise RuntimeError(f"could not read file at {file_path}: {e}") from e
        deadline = time.time() + timeout
        headers = {"Authorization": f"Bearer {access}"}
        status = None
        while time.time() < deadline:
            resp = requests.get(f"{MEDIA_REGISTER_URL}/{media_id}", headers=headers)
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == "succeeded":
                break
            if status == "failed":
                raise RuntimeError(f"video processing failed for media_id={media_id}")
        else:
            raise RuntimeError(f"video processing timed out after {timeout}s for media_id={media_id}")
        try:
            resp = requests.post( PIN_CREATE_URL, headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},json={ "title": title, "description": description, "board_id": board_id, "media_source": { "source_type": "video_id", "media_id": media_id, }, },)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            raise RuntimeError(f"pin creation failed: {e}") from e

def upload_pin_from_file(access_token, title, board_id, publish_now, timee, user_id, upload_tmp_path, mimetype, Account_id, typeee,description=""):
    if not publish_now:
        service = dpp.get_drive_service(user_id)
        drive_file_id, err = upload_path_to_drive(service, upload_tmp_path, os.path.basename(upload_tmp_path), mimetype)
        if err is not None:
            return {"account": Account_id, "status": "failed", "error": f"drive upload failed: {err}"}
        list_account_ids=json.dumps(Account_id)
        sccc.insert_post( user_id=list_account_ids, scheduled_time=timee, access_token=access_token, typeee=typeee, text1=title, text2=description , text3=board_id, media_id=drive_file_id, )
        return {"account": Account_id, "status": "scheduled", "scheduled_time": timee.isoformat()}
    with open(upload_tmp_path, "rb") as fh:
        photo_bytes = fh.read()
    b64_image = base64.b64encode(photo_bytes).decode("utf-8")
    url = "https://api.pinterest.com/v5/pins"
    payload = { "board_id": board_id, "title": title, "description": description, "media_source": { "source_type": "image_base64",  "content_type": "image/jpeg", "data": b64_image, }, }
    for access  in access_token : 
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        try:
            resp = requests.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return { "status": "posted", "response": resp.json()}
        except requests.HTTPError as e:
            error_detail = resp.text if resp is not None else str(e)
            return { "status": "failed", "error": error_detail}
        except requests.RequestException as e:
            return { "status": "failed", "error": str(e)}

def upload_path_to_drive(service, file_path, filename, mimetype):
    make_public = True
    drive_file_id = None
    try:
        file_metadata = {"name": filename}
        media_upload = MediaFileUpload(file_path, mimetype=mimetype, resumable=True)
        created_file = service.files().create(body=file_metadata, media_body=media_upload, fields="id, name, webViewLink, webContentLink, mimeType",).execute()
        drive_file_id = created_file["id"]
        if make_public:
            service.permissions().create( fileId=drive_file_id, body={"type": "anyone", "role": "reader"},).execute()
        return drive_file_id, None
    except HttpError as e:
        if drive_file_id:
            try:
                service.files().delete(fileId=drive_file_id).execute()
            except HttpError:
                pass
        return None, str(e)

def generate_pkce_pair():
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge

def get_video_duration(file_path):
    with VideoFileClip(file_path) as video:
        return video.duration

@app.route("/auth/pinterest/login", methods=["GET", "POST"])
def pinterest_login():
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return jsonify({"error": "invalid user id"}), 401
    code_verifier, code_challenge = generate_pkce_pair()
    state = serializer.dumps({"user_id": user_id, "code_verifier": code_verifier})
    params = { "response_type": "code", "client_id": PINTEREST_APP_ID, "redirect_uri": PINTEREST_REDIRECT_URI, "scope": PINTEREST_SCOPES, "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256",}
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
    
@app.route("/pinterest/pins-with-metrics", methods=["GET"])
def pins_with_metrics():
    data = request.get_json(silent=True) or {}
    username = data.get("username")
    token = data.get("token")
    if not username or not token :
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

@app.route("/post/pinterest/photo", methods=["POST"])
def post_to_pinterest_photo():
    data = request.get_json(silent=True) or {}
    access_tokens ,err , Account_ids , user_id = _authenticate(data)
    if err:
        return err
    title = data.get("title")
    description = data.get("description", "")
    board_id = data.get("board_id")
    if not board_id:
        return jsonify({"error": "board_id is required"}), 400
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "at least one image file is required"}), 400
    if len(files) > 1:
        return jsonify({"error": "only one photo allowed"}), 400
    f = files[0]
    if not (f.mimetype or "").startswith("image/"):
        return jsonify({"error": "unsupported file type in upload"}), 400
    publish = data.get("publish", "true")
    timee_raw = data.get("time")
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=48)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    tmp_path = None
    try:
        suffix = os.path.splitext(f.filename or "")[1] or ".jpg"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        file_size_bytes = os.path.getsize(tmp_path)
        if file_size_bytes > image_size:
            return jsonify({"error": f"image {f.filename} exceeds max size"}), 400
        with open(tmp_path, "rb") as fh:
            try:
                pin = upload_pin_from_file(access_tokens, title, board_id, publish_now, timee, user_id, tmp_path, f.mimetype, Account_ids, "Pin_photo_later",description)
                for access_token , Account_id in zip(access_tokens,Account_ids):
                    xcccc(Account_id,access_token,pin.get("id"),"pin_photo")
            except requests.HTTPError as e:
                return jsonify({"error": "pin upload failed", "detail": str(e)}), 400
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    return jsonify({"success": True, "pin_id": pin.get("id")}), 200

@app.route("/post/pinterest/video", methods=["POST"])
def post_to_pinterest_video():
    data = request.get_json(silent=True) or {}
    access_tokens ,err , Account_ids , user_id = _authenticate(data)
    if err:
        return err
    title = data.get("title")
    description = data.get("description", "")
    board_id = data.get("board_id")
    publish = data.get("publish", "true")
    timee_raw = data.get("time")
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=48)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if not board_id:
        return jsonify({"error": "board_id is required"}), 400
    files = request.files.getlist("file")
    if not files:
        return jsonify({"error": "a video file is required"}), 400
    if len(files) > 1:
        return jsonify({"error": "only one video allowed per pin"}), 400
    f = files[0]
    if not (f.mimetype or "").startswith("video/"):
        return jsonify({"error": "unsupported file type in upload"}), 400
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        file_size_bytes = os.path.getsize(tmp_path)
        if get_video_duration(tmp_path) > duation or file_size_bytes > max_size:
            return jsonify({"error": "video exceeds allowed duration or size"}), 400
        try:
            pin = create_pinterest_video_pin_full( access_tokens, board_id, title, description, tmp_path,publish_now,timee ,Account_ids,user_id,"Pin_video_later",f.mimetype  )
            for access_token , Account_id in zip(access_tokens,Account_ids):
                xcccc(Account_id, access_token, pin.get("id"), "pin_video")
        except requests.HTTPError as e:
            return jsonify({"error": "pin upload failed", "detail": str(e)}), 400
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    return jsonify({"success": True, "pin_id": pin.get("id")}), 200