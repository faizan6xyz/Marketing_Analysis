import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import authnew as au
import database.UserDB as dbimp
import Drive.dep as dpp
import Gmail.Read_mails as gc
import Instagram.schedule_video as sccc
import limit as lmmm
import Whatsapp.new as whatt
load_dotenv()
MAX_MEDIA_ITEMS = 3
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_TARGETS = 2000
MIN_LEAD = timedelta(seconds=180)
MAX_LEAD = timedelta(hours=72)
PLATFORMS = ("gmail", "whatsapp")
SCHEDULE_DELIM = ",1234,+++x"
WA_ID_RE = re.compile(r"^\d{10,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = MAX_MEDIA_ITEMS * MAX_FILE_SIZE + 1024 * 1024
app.url_map.strict_slashes = False  # avoids slash redirects on preflight
frontend = os.environ.get("front_end")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("campaign_api")
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])

class BadRequest(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message, self.status, self.extra = message, status, extra

@app.errorhandler(BadRequest)
def _handle_bad_request(e):
    return jsonify({"error": e.message, **e.extra}), e.status

def guess_media_type(mime_type):
    if not mime_type:
        return None
    for prefix in ("image", "video", "audio"):
        if mime_type.startswith(prefix + "/"):
            return prefix
    return "document"

def parse_datetime(value, require_tz=True):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if require_tz and dt.tzinfo is None:
        return None
    return dt

def parse_json_list(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None

def personalize(body, name):
    return body.replace("{name}", name) if name else body

def _first(value):
    return value[0] if isinstance(value, (list, tuple)) else value

def _remove_files(paths):
    for p in paths:
        with suppress(OSError):
            os.remove(p)

def _get_account(table, user_id, account_id, select):
    rows = dbimp.select_rows(table, select=select, filters={"id": user_id, "account_id": account_id})
    return rows[0] if rows else None

def _ensure_wa_token(row, refresh):
    try:
        expiry = datetime.fromisoformat(row["token_expire"])
    except (TypeError, ValueError):
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry - datetime.now(timezone.utc) < timedelta(days=2):
        return refresh() or None
    return row["access_token"]

def _clean_recipients(platform, raw_targets, raw_names):
    targets = parse_json_list(raw_targets)
    names = parse_json_list(raw_names)
    if not targets:
        raise BadRequest("'target' must be a non-empty JSON list")
    if not names:
        raise BadRequest("'name' must be a non-empty JSON list")
    if len(targets) != len(names):
        raise BadRequest("'target' and 'name' must be the same length")
    if len(targets) > MAX_TARGETS:
        raise BadRequest(f"'target' exceeds max of {MAX_TARGETS}")
    pattern = WA_ID_RE if platform == "whatsapp" else EMAIL_RE
    cleaned = {}
    invalid = []
    for t, n in zip(targets, names):
        t = t.strip() if isinstance(t, str) else t
        if not isinstance(t, str) or not pattern.match(t):
            invalid.append(t)
            continue
        key = t.lower() if platform == "gmail" else t
        cleaned.setdefault(key, (t, n if isinstance(n, str) else ""))
    if invalid:
        raise BadRequest("invalid entries in 'target'", invalid=invalid[:10])
    return [v[0] for v in cleaned.values()], [v[1] for v in cleaned.values()]

def _save_uploads(saved_paths):
    files = [f for f in request.files.getlist("file") if f and f.filename]
    if not files:
        return [], []
    if len(files) > MAX_MEDIA_ITEMS:
        raise BadRequest(f"too many media files (max {MAX_MEDIA_ITEMS})")
    media, failed = [], []
    for f in files:
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_FILE_SIZE:
            failed.append({"filename": f.filename, "error": f"file exceeds max size of {MAX_FILE_SIZE} bytes"})
            continue
        try:
            suffix = os.path.splitext(f.filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                saved_paths.append(tmp.name)
                f.save(tmp)
        except Exception as e:
            failed.append({"filename": f.filename, "error": str(e)})
            continue
        media.append({ "path": tmp.name,"filename": f.filename,"mime_type": f.mimetype,"type": guess_media_type(f.mimetype),})
    if not media and failed:
        raise BadRequest("all file uploads failed", failed=failed)
    return media, failed

def _send_gmail(service, targets, names, subject, body, media):
    paths = [m["path"] for m in media]
    labels = [m.get("filename") or os.path.basename(m["path"]) for m in media]
    results = []
    for recipient, name in zip(targets, names):
        try:
            if paths:
                gc.send_message_with_attachments(service=service, to=recipient, subject=subject, body_text=body,attachment_paths=paths, attachment_labels=labels, name=name,)
            else:
                gc.send_message(service=service, to=recipient, subject=subject, body_text=body, name=name)
            results.append({"to": recipient, "status": "sent"})
        except Exception as e:
            logger.exception("campaign send failed for %s", recipient)
            results.append({"to": recipient, "status": "failed", "error": str(e)})
    return results

def _send_whatsapp(account_id, access_token, targets, names, body, media):
    results = []
    for recipient, name in zip(targets, names):
        try:
            whatt.send_whatsapp_message(PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=access_token,recipient_number=recipient, message_body=personalize(body, name),)
            for m in media:
                whatt.send_whatsapp_media(PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=access_token,recipient_number=recipient, msg_type=m["type"],path=m["path"], filename=m.get("filename"),)
            results.append({"to": recipient, "status": "sent"})
        except Exception as e:
            logger.exception("campaign send failed for %s", recipient)
            results.append({"to": recipient, "status": "failed", "error": str(e)})
    return results

def _finalize(account_id, platform, campaign_name, reply, subject, results):
    sent = [r["to"] for r in results if r["status"] == "sent"]
    if not sent:
        return sent
    ts = datetime.now(timezone.utc).isoformat()
    try:
        sccc.inert_workflow(campaign_name, reply, subject)
        dpp.append_to_file(user_id=account_id, platform=platform, filename="campaigns.txt",data_to_append="\n".join(f"{r},{campaign_name},{ts},," for r in sent),)
    except Exception:
        logger.exception("failed to record campaign %s", campaign_name)
    return sent

def _download_attachments(service, file_ids, paths):
    media = []
    for fid in file_ids:
        path, mimetype, name = dpp.download_drive_file_to_temp(service, fid)
        paths.append(path)
        media.append({"path": path, "filename": name, "type": guess_media_type(mimetype)})
    return media

def _scheduled_payload(text1, text3, file_id):
    raw = _first(text1)
    if isinstance(raw, str):
        raw = raw.split(SCHEDULE_DELIM, 1)[0]
    targets = json.loads(raw)
    body, names = json.loads(_first(text3))
    file_ids = json.loads(_first(file_id))
    return targets, body, names, file_ids

def upload_lategmail(account_id, text1, text2, text3, file_id, reply="", subject=""):
    targets, body, names, file_ids = _scheduled_payload(text1, text3, file_id)
    campaign_name = text2
    rows = dbimp.select_rows("gmail", select="id", filters={"account_id": account_id})
    if not rows:
        return False
    user_id = rows[0]["id"]
    paths = []
    try:
        gmail_service = gc.get_service_web(user_id, account_id)
        if not gmail_service:
            return False
        media = []
        if file_ids:
            media = _download_attachments(dpp.get_drive_service(user_id), file_ids, paths)
        results = _send_gmail(gmail_service, targets, names, campaign_name or "", body, media)
        return bool(_finalize(account_id, "gmail", campaign_name, reply, subject, results))
    finally:
        _remove_files(paths)

def upload_latewhat(account_id, text1, text2, text3, file_id, reply="", subject=""):
    targets, body, names, file_ids = _scheduled_payload(text1, text3, file_id)
    campaign_name = text2
    rows = dbimp.select_rows("whatsapp", select="id,access_token,token_expire", filters={"account_id": account_id})
    if not rows:
        return False
    row = rows[0]
    token = _ensure_wa_token(row, lambda: whatt.refresh_token_web(account_id, row["access_token"]))
    if not token:
        return False
    paths = []
    try:
        media = []
        if file_ids:
            media = _download_attachments(dpp.get_drive_service(row["id"]), file_ids, paths)
        results = _send_whatsapp(account_id, token, targets, names, body, media)
        return bool(_finalize(account_id, "whatsapp", campaign_name, reply, subject, results))
    finally:
        _remove_files(paths)

def _schedule(user_id, platform, account_id, scheduled_time, targets, names,  body, campaign_name, reply, subject, media):
    account = _get_account(platform, user_id, account_id, "account_id,access_token")
    if not account:
        raise BadRequest(f"{platform} account not connected", 401)
    drive_ids = []
    if media:
        service = dpp.get_drive_service(user_id)
        for m in media:
            name = os.path.basename(m.get("filename") or m["path"])
            fid, err = dpp.upload_path_to_drive(service, m["path"], name, m["mime_type"])
            if err is not None:
                raise BadRequest(f"drive upload failed: {err}", 502)
            drive_ids.append(fid)
    sccc.insert_post(
        user_id=account["account_id"],scheduled_time=scheduled_time,access_token=account["access_token"],typeee="email_later" if platform == "gmail" else "message_later",text1=f"{json.dumps(targets)}{SCHEDULE_DELIM}{reply}{SCHEDULE_DELIM}{subject}",text2=campaign_name,text3=json.dumps([body, names]),media_id=json.dumps(drive_ids),)
    return jsonify({"account": account_id, "status": "scheduled", "scheduled_time": scheduled_time.isoformat()}), 200

def _publish_gmail(user_id, account_id, targets, names, campaign_name, body, reply, subject, media):
    try:
        service = gc.get_service_web(user_id=user_id, account_id=account_id)
    except Exception:
        logger.exception("gmail service unavailable")
        service = None
    if not service:
        raise BadRequest("not connected", 401, connect_url="/connect-gmail")
    results = _send_gmail(service, targets, names, campaign_name, body, media)
    sent = _finalize(account_id, "gmail", campaign_name, reply, subject, results)
    if sent:
        try:
            label_id = gc.get_or_create_label(service, campaign_name)
            gc.create_filter(service,{"query": "from:(" + " OR ".join(sent) + ")"},{"addLabelIds": [label_id], "removeLabelIds": []},)
        except Exception:
            logger.exception("failed to create label/filter for %s", campaign_name)
    return results

def _publish_whatsapp(token, user_id, account_id, targets, names, campaign_name, body, reply, subject, media):
    row = _get_account("whatsapp", user_id, account_id, "access_token,account_id,token_expire")
    if not row:
        raise BadRequest("not connected", 401, connect_url="/connect-whatsapp")
    access_token = _ensure_wa_token(row, lambda: whatt.refresh_token(token, row["account_id"], row["access_token"]))
    if not access_token:
        raise BadRequest("token refresh failed, please reconnect WhatsApp", 502)
    results = _send_whatsapp(row["account_id"], access_token, targets, names, body, media)
    _finalize(row["account_id"], "whatsapp", campaign_name, reply, subject, results)
    return results

@app.route("/", methods=["POST"])
@limiter.limit("5 per minute")
def campaign():
    data = request.get_json(silent=True) or request.form.to_dict()
    token = data.get("token")
    if not token:
        raise BadRequest("'token' is required")
    platform = data.get("platform")
    platform = platform.strip().lower() if isinstance(platform, str) else None
    if platform not in PLATFORMS:
        raise BadRequest("'platform' must be 'gmail' or 'whatsapp'")
    auth = au.process(token=token)
    if not auth or not auth.get("status"):
        return jsonify({"status": "failed", "reason": (auth or {}).get("reason")}), 403
    user_id = auth.get("user_id")
    if not user_id or not dbimp.select_rows("users", select="user_id", filters={"user_id": user_id}):
        return jsonify({"status": False}), 403
    publish_now = str(data.get("publish")).strip().lower() == "true"
    scheduled_time = None
    if not publish_now:
        scheduled_time = parse_datetime(data.get("time"))
        if scheduled_time is None:
            raise BadRequest("invalid or missing date/time")
        now = datetime.now(timezone.utc)
        if not (now + MIN_LEAD <= scheduled_time <= now + MAX_LEAD):
            raise BadRequest("invalid time for the posting")
    campaign_name = str(data.get("campaign_name") or "").strip()
    body = data.get("body")
    reply = data.get("reply") or ""
    subject = data.get("subject") or ""
    account_id = data.get("account_id")
    if not campaign_name:
        raise BadRequest("'campaign_name' is required")
    if not isinstance(body, str) or not body.strip():
        raise BadRequest("'body' is required")
    if not account_id:
        raise BadRequest("'account_id' is required")
    targets, names = _clean_recipients(platform, data.get("target"), data.get("name"))
    if not lmmm.checkk(auth.get("token", token), user_id, datetime.now(timezone.utc).isoformat(), len(targets)):
        raise BadRequest("limit has been reached")
    saved_paths = []
    try:
        media, failed_uploads = _save_uploads(saved_paths)
        if platform == "whatsapp":
            for m in media:
                if m["type"] not in whatt.VALID_MEDIA_TYPES:
                    raise BadRequest(f"media 'type' must be one of {sorted(whatt.VALID_MEDIA_TYPES)}")
        if not publish_now:
            return _schedule(user_id, platform, account_id, scheduled_time, targets, names,body, campaign_name, reply, subject, media)
        if platform == "gmail":
            results = _publish_gmail(user_id, account_id, targets, names, campaign_name,body, reply, subject, media)
        else:
            results = _publish_whatsapp(token, user_id, account_id, targets, names, campaign_name, body, reply, subject, media)
        return jsonify({"count": len(results), "results": results, "failed_uploads": failed_uploads}), 200
    finally:
        _remove_files(saved_paths)