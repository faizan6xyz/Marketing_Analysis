import re
import json
import database.UserDB as dbimp
import Instagram.schedule_video as sccc
import authnew as au
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import Drive.dep as dpp
import Whatsapp.new as whatt
import tempfile
import Gmail.Read_mails as gc
from flask import Flask, request, jsonify
from datetime import datetime, timezone, timedelta
import os
from flask_cors import CORS
import logging
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail_api")
MAX_MEDIA_ITEMS = 3
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TARGETS = 2000
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])
BASE_URL = ""

def guess_media_type(mime_type):
    if not mime_type:
        return None
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return "document"

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

def upload_latewhat(Account_id , text1, text2, text3, file_id):
    target = json.loads(text1[0])
    combin = json.loads(text3[0])
    names = combin[1]
    body = combin[0]
    campaign_name = text2
    file_id = json.loads(file_id[0])
    rows = dbimp.select_rows_web("Gmail",select="id",filters={"Account_id":Account_id})
    if not rows :
        return False
    user_id = rows[0]["id"]
    now = datetime.now(timezone.utc).isoformat()
    service = dpp.get_drive_service(user_id)
    attachment_paths = []
    attachment_labels = []
    mimetypes = []
    rows = dbimp.select_rows_web("Whatsapp", select="Access_token,Token_expire", filters={"Account_id": Account_id })
    row = rows[0] if rows else None
    if not row:
        return False
    acc = row["Access_token"]
    expire = row["Token_expire"]
    token_expiry = datetime.fromisoformat(expire)
    if token_expiry - datetime.now(timezone.utc) < timedelta(days=2):
        refreshed = whatt.refresh_token_web( Account_id, acc)
        if not refreshed:
            return False
        acc = refreshed
    for fileeee in file_id :
        tmp_path , mimetype,name = dpp.download_drive_file_to_temp(service,fileeee)
        attachment_paths.append(tmp_path)
        attachment_labels.append(name)
        mimetypes.append(mimetype)
    for recipient, recipient_name in zip(target, names):
        content = f"{recipient},{campaign_name},{now},,"
        personalized_body = body.replace("{name}", recipient_name) if recipient_name else body
        whatt.send_whatsapp_message(PHONE_NUMBER_ID=Account_id, ACCESS_TOKEN=acc, recipient_number=recipient, message_body=personalized_body)
        for typee , path , name in zip(mimetypes,attachment_paths,attachment_labels) :
            whatt.send_whatsapp_media( PHONE_NUMBER_ID=Account_id, ACCESS_TOKEN=acc, recipient_number=recipient, msg_type=typee, path=path,filename=name)
        dpp.append_to_file(user_id=Account_id, platform="Whatsapp", filename="campaigns.txt", data_to_append=content)

def upload_lategmail(Account_id , text1, text2, text3, file_id):
    target = json.loads(text1[0])
    combin = json.loads(text3[0])
    names = combin[1]
    body = combin[0]
    campaign_name = text2
    file_id = json.loads(file_id[0])
    rows = dbimp.select_rows_web("Gmail",select="id",filters={"Account_id":Account_id})
    if not rows :
        return False
    user_id = rows[0]["id"]
    now = datetime.now(timezone.utc).isoformat()
    service = dpp.get_drive_service(user_id)
    attachment_paths = []
    attachment_labels = []
    for fileeee in file_id :
        tmp_path , mimetype,name = dpp.download_drive_file_to_temp(service,fileeee)
        attachment_paths.append(tmp_path)
        attachment_labels.append(name)
    gmail_service = gc.get_service_web(user_id)
    for recipient, recipient_name in zip(target, names):
        content = f"{recipient},{campaign_name},{now},,"
        if attachment_labels and attachment_paths :
            gc.send_message_with_attachments( service=gmail_service, to=recipient, subject=campaign_name or "", body_text=body, attachment_paths=attachment_paths, attachment_labels=attachment_labels, name=recipient_name,)
        else:
            gc.send_message(service=gmail_service, to=recipient, subject=campaign_name or "", body_text=body, name=recipient_name)
        dpp.append_to_file(user_id=Account_id, platform="Gmail", filename="campaigns.txt", data_to_append=content)

@app.route('/campaign', methods=['POST'])
@limiter.limit("5 per minute")
def campaign():
    data = request.get_json(silent=True) or {}
    token = data.get("token")
    publish = data.get("publish")
    timee_raw = data.get("time")
    timee = parse_datetime(timee_raw)
    publish_now = str(publish).strip().lower() == "true"
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=72)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    platform = data.get("platform")
    saved_files = []
    failed_result = []
    media = []
    def cleanup_local_files():
        for f in saved_files:
            try:
                if os.path.exists(f["path"]):
                    os.remove(f["path"])
            except OSError:
                pass
    if "file" in request.files:
        uploaded_files = request.files.getlist("file")
        if not uploaded_files or all(f.filename == "" for f in uploaded_files):
            return jsonify({"error": "file required (form-data field: file)"}), 400
        if len(uploaded_files) > MAX_MEDIA_ITEMS:
            return jsonify({"error": f"too many media files (max {MAX_MEDIA_ITEMS})"}), 400
        for uploaded_file in uploaded_files:
            uploaded_file.stream.seek(0, os.SEEK_END)
            file_size = uploaded_file.stream.tell()
            uploaded_file.stream.seek(0)
            if file_size > MAX_FILE_SIZE:
                failed_result.append({"filename": uploaded_file.filename, "error": f"file exceeds max size of {MAX_FILE_SIZE} bytes"})
                continue
            tmp_path = None
            try:
                suffix = os.path.splitext(uploaded_file.filename or "")[1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    uploaded_file.save(tmp.name)
                    tmp_path = tmp.name
                saved_files.append({ "path": tmp_path, "filename": uploaded_file.filename, "mime_type": uploaded_file.mimetype, "size": file_size, })
            except Exception as e:
                if tmp_path and os.path.exists(tmp_path):
                    os.remove(tmp_path)
                failed_result.append({"filename": uploaded_file.filename, "error": str(e)})
        if uploaded_files and not saved_files and failed_result:
            return jsonify({"error": "all file uploads failed", "failed": failed_result}), 400
        media = [ { "path": f["path"],"filename": f["filename"], "type": guess_media_type(f["mime_type"]),"mime_type": f["mime_type"],} for f in saved_files]
    campaign_name = data.get("campaign_name")
    body = data.get("body")
    try:
        target = json.loads(data.get("target") or "[]")
        names = json.loads(data.get("name") or "[]")
    except json.JSONDecodeError:
        cleanup_local_files()
        return jsonify({"error": "'target' and 'name' must be valid JSON arrays"}), 400
    if len(media) > MAX_MEDIA_ITEMS:
        cleanup_local_files()
        return jsonify({"error": f"too many media files to send (max {MAX_MEDIA_ITEMS})"}), 400
    for m in media:
        if not isinstance(m, dict) or not m.get("path"):
            cleanup_local_files()
            return jsonify({"error": "each media item must reference a saved file"}), 400
    if not isinstance(platform, str) or platform.strip().lower() not in ("gmail", "whatsapp"):
        cleanup_local_files()
        return jsonify({"error": "'platform' must be 'gmail' or 'whatsapp'"}), 400
    platform = platform.strip().lower()
    if not isinstance(target, list) or not target:
        cleanup_local_files()
        return jsonify({"error": "'target' must be a non-empty list"}), 400
    if not isinstance(names, list) or not names:
        cleanup_local_files()
        return jsonify({"error": "'name' must be a non-empty list"}), 400
    if len(target) != len(names):
        cleanup_local_files()
        return jsonify({"error": "'target' and 'name' must be the same length"}), 400
    if len(target) > MAX_TARGETS:
        cleanup_local_files()
        return jsonify({"error": f"'target' exceeds max of {MAX_TARGETS}"}), 400
    if not isinstance(body, str) or not body.strip():
        cleanup_local_files()
        return jsonify({"error": "'body' is required"}), 400
    if platform == "whatsapp":
        WA_ID_RE = re.compile(r'^\d{10,15}$')
        invalid = [t for t in target if not isinstance(t, str) or not WA_ID_RE.match(t)]
        if invalid:
            cleanup_local_files()
            return jsonify({"error": "invalid entries in 'target'", "invalid": invalid[:10]}), 400
        for m in media:
            if m.get("type") not in whatt.VALID_MEDIA_TYPES:
                cleanup_local_files()
                return jsonify({"error": f"media 'type' must be one of {sorted(whatt.VALID_MEDIA_TYPES)}"}), 400
    elif platform == "gmail":
        EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
        invalid = [t for t in target if not isinstance(t, str) or not EMAIL_RE.match(t)]
        if invalid:
            cleanup_local_files()
            return jsonify({"error": "invalid entries in 'target'", "invalid": invalid[:10]}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        cleanup_local_files()
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    if not user_id:
        cleanup_local_files()
        return jsonify({"status": False}), 403
    db_rows = dbimp.select_rows(tokench["token"], "users", select="user_id", filters={"id": user_id})
    db_row = db_rows[0] if db_rows else None
    if not db_row or db_row.get("user_id") != user_id:
        cleanup_local_files()
        return jsonify({"status": False}), 403
    results = []
    if platform == "gmail":
        email = data.get("email")
        if not publish_now:
            service = dpp.get_drive_service(user_id)
            dr = []
            for m in media:
                drive_file_id, err = dpp.upload_path_to_drive(service, m["path"], os.path.basename(m["path"]), m["mime_type"])
                if err is not None:
                    cleanup_local_files()
                    return jsonify({"status": "failed", "error": f"drive upload failed: {err}"}), 502
                dr.append(drive_file_id)
            rows = dbimp.select_rows( tokench["token"], "Gmail", select="Account_id,Access_token", filters={"id": user_id, "Email": email}, )
            if not rows:
                cleanup_local_files()
                return jsonify({"status": "failed", "reason": "gmail account not connected"}), 401
            Account_id = rows[0]["Account_id"]
            access_token = rows[0]["Access_token"]  
            camp = json.dumps(target)
            new_body  = [body , recipient_names]
            bodyy = json.dumps(new_body)
            file_id = json.dumps(dr)
            sccc.insert_post( user_id=Account_id, scheduled_time=timee, access_token=access_token, typeee="email_later", text1=camp, text2=campaign_name, text3=bodyy, media_id=file_id, )
            cleanup_local_files()  
            return jsonify({"account": Account_id, "status": "scheduled", "scheduled_time": timee.isoformat()}), 200
        try:
            gmail_service = gc.get_service(token=token, user_id=user_id)
        except Exception:
            cleanup_local_files()
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        if not gmail_service:
            cleanup_local_files()
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        rows = dbimp.select_rows(tokench["token"], "Gmail", select="Account_id", filters={"id": user_id, "email": email})
        if not rows:
            cleanup_local_files()
            return jsonify({"error": "gmail account not found for this email"}), 404
        account_id = rows[0]["Account_id"]
        attachment_paths = [m["path"] for m in media] if media else None
        attachment_labels = [m.get("filename") for m in media] if media else None
        if attachment_labels and any(l is None for l in attachment_labels):
            attachment_labels = None
        for recipient, recipient_name in zip(target, names):
            now = datetime.now(timezone.utc).isoformat()
            content = f"{recipient},{campaign_name},{now},,"
            try:
                if media:
                    gc.send_message_with_attachments( service=gmail_service, to=recipient, subject=campaign_name or "", body_text=body, attachment_paths=attachment_paths, attachment_labels=attachment_labels, name=recipient_name,)
                else:
                    gc.send_message(service=gmail_service, to=recipient, subject=campaign_name or "", body_text=body, name=recipient_name)
                dpp.append_to_file(user_id=account_id, platform=platform, filename="campaigns.txt", data_to_append=content)
                results.append({"to": recipient, "status": "sent"})
            except Exception as e:
                logger.exception("campaign send failed for %s", recipient)
                results.append({"to": recipient, "status": "failed", "error": str(e)})
    elif platform == "whatsapp":
        number = data.get("number")
        if not publish_now:
            service = dpp.get_drive_service(user_id)
            dr = []
            for m in media:
                drive_file_id, err = dpp.upload_path_to_drive(service, m["path"], os.path.basename(m["path"]), m["mime_type"])
                if err is not None:
                    cleanup_local_files()
                    return jsonify({"status": "failed", "error": f"drive upload failed: {err}"}), 502
                dr.append(drive_file_id)
            rows = dbimp.select_rows( tokench["token"], "Whatsapp",select="Account_id,Access_token", filters={"id": user_id, "Phone_no": number}, )
            if not rows:
                cleanup_local_files()
                return jsonify({"status": "failed", "reason": "whatsapp account not connected"}), 401
            Account_id = rows[0]["Account_id"]
            access_token = rows[0]["Access_token"]  
            camp = json.dumps(target)
            new_body  = [body , recipient_names]
            recipient_names = json.dumps(names)     
            file_id = json.dumps(dr)
            sccc.insert_post( user_id=Account_id, scheduled_time=timee, access_token=access_token,typeee="message_later", text1=camp, text2=recipient_names, text3=bodyy, media_id=file_id,)
            cleanup_local_files()
            return jsonify({"account": Account_id, "status": "scheduled", "scheduled_time": timee.isoformat()}), 200
        rows = dbimp.select_rows(tokench["token"], "Whatsapp", select="Access_token,Account_id,Token_expire", filters={"Phone_no": number, "id": user_id})
        row = rows[0] if rows else None
        if not row:
            cleanup_local_files()
            return jsonify({"error": "not connected", "connect_url": "/connect-whatsapp"}), 401
        account_id = row["Account_id"]
        acc = row["Access_token"]
        expire = row["Token_expire"]
        try:
            token_expiry = datetime.fromisoformat(expire)
        except (TypeError, ValueError):
            cleanup_local_files()
            return jsonify({"error": "invalid stored token expiry"}), 500
        if token_expiry - datetime.now(timezone.utc) < timedelta(days=2):
            refreshed = whatt.refresh_token(token, account_id, acc)
            if not refreshed:
                cleanup_local_files()
                return jsonify({"error": "token refresh failed, please reconnect WhatsApp"}), 502
            acc = refreshed
        for recipient, recipient_name in zip(target, names):
            now = datetime.now(timezone.utc).isoformat()
            content = f"{recipient},{campaign_name},{now},,"
            try:
                personalized_body = body.replace("{name}", recipient_name) if recipient_name else body
                whatt.send_whatsapp_message(PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=acc, recipient_number=recipient, message_body=personalized_body)
                for m in media:
                    whatt.send_whatsapp_media( PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=acc, recipient_number=recipient, msg_type=m["type"], path=m["path"], caption=m.get("caption"), filename=m.get("filename"),)
                dpp.append_to_file(user_id=account_id, platform=platform, filename="campaigns.txt", data_to_append=content)
                results.append({"to": recipient, "status": "sent"})
            except Exception as e:
                logger.exception("campaign send failed for %s", recipient)
                results.append({"to": recipient, "status": "failed", "error": str(e)})
    cleanup_local_files()
    return jsonify({"count": len(results), "results": results, "failed_uploads": failed_result}), 200