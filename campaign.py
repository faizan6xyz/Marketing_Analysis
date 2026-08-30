import re
import database.UserDB as dbimp
import authnew as au
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import Drive.dep as dpp
import Whatsapp.login as what
import Gmail.Read_mails as gc
from flask import Flask,request , jsonify 
from datetime import datetime , timezone , timedelta
import os
from flask_cors import CORS
import logging
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])
app.secret_key = os.environ["FLASK_SECRET_KEY"]
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail_api")
MAX_MEDIA_ITEMS = 4
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])

@app.route('/campaign', methods=['POST'])
@limiter.limit("5 per minute")
def campaign():
    data = request.get_json(silent=True) or {}
    if not data:
        return jsonify({"status": False}), 403
    token = data.get("token")
    platform = data.get("platofrm")
    campaign_id = data.get("campain_id")
    campaign_name = data.get("campain_name")
    target = data.get("target")
    body = data.get("body")
    names = data.get("name")
    media = data.get("media") or []
    if not isinstance(media, list):
        return jsonify({"error": "'media' must be a list"}), 400
    if len(media) > MAX_MEDIA_ITEMS:
        return jsonify({"error": f"too many media files to send (max {MAX_MEDIA_ITEMS})"}), 400
    for m in media:
        if not isinstance(m, dict) or not m.get("link"):
            return jsonify({"error": "each media item must be an object with a 'link'"}), 400
    if not token:
        return jsonify({"error": "'token' is required"}), 400
    if not isinstance(platform, str) or platform.strip().lower() not in ("gmail", "whatsapp"):
        return jsonify({"error": "'platform' must be 'gmail' or 'whatsapp'"}), 400
    platform = platform.strip().lower()
    if not isinstance(target, list) or not target:
        return jsonify({"error": "'target' must be a non-empty list"}), 400
    if not isinstance(names, list) or not names:
        return jsonify({"error": "'name' must be a non-empty list"}), 400
    if len(target) != len(names):
        return jsonify({"error": "'target' and 'name' must be the same length"}), 400
    MAX_TARGETS = 2000
    if len(target) > MAX_TARGETS:
        return jsonify({"error": f"'target' exceeds max of {MAX_TARGETS}"}), 400
    if not isinstance(body, str) or not body.strip():
        return jsonify({"error": "'body' is required"}), 400
    if platform == "whatsapp":
        WA_ID_RE = re.compile(r'^\d{10,15}$')
        invalid = [t for t in target if not isinstance(t, str) or not WA_ID_RE.match(t)]
        if invalid:
            return jsonify({"error": "invalid entries in 'target'", "invalid": invalid[:10]}), 400
        for m in media:
            if m.get("type") not in what.VALID_MEDIA_TYPES:
                return jsonify({"error": f"media 'type' must be one of {sorted(what.VALID_MEDIA_TYPES)}"}), 400
    elif platform == "gmail":
        EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
        invalid = [t for t in target if not isinstance(t, str) or not EMAIL_RE.match(t)]
        if invalid:
            return jsonify({"error": "invalid entries in 'target'", "invalid": invalid[:10]}), 400
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 403
    user_id = tokench["user_id"]
    if not user_id:
        return jsonify({"status": False}), 403
    db_rows = dbimp.select_rows(token, "users", select="user_id", filters={"id": user_id})
    db_row = db_rows[0] if db_rows else None
    if not db_row or db_row.get("user_id") != user_id:
        return jsonify({"status": False}), 403
    results = []
    if platform == "gmail":
        try:
            service = gc.get_service(token=token, user_id=user_id)
        except Exception:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        drive_links = [m["link"] for m in media] if media else None
        link_labels = [m.get("filename") for m in media] if media else None
        if link_labels and any(l is None for l in link_labels):
            link_labels = None  # only use labels if every item has one
        for recipient, recipient_name in zip(target, names):
            now = datetime.now(timezone.utc).isoformat()
            content = f"{recipient},{campaign_id},{campaign_name},{now},,"
            try:
                if media:
                    gc.send_message_with_attachments(service=service, to=recipient, subject=campaign_name or "", body_text=body, drive_links=drive_links,link_labels=link_labels, name=recipient_name,)
                else:
                    gc.send_message(service=service, to=recipient, subject=campaign_name or "", body_text=body, name=recipient_name)
                dpp.append_to_file(token=token, platform=platform, filename="campaigns.txt", data_to_append=content)
                results.append({"to": recipient, "status": "sent"})
            except Exception as e:
                logger.exception("campaign send failed for %s", recipient)
                results.append({"to": recipient, "status": "failed", "error": str(e)})
    elif platform == "whatsapp":
        rows = dbimp.select_rows(token, "Whatsapp", select="Access_token,Account_id,Token_expire", filters={"id": user_id})
        row = rows[0] if rows else None
        if not row: 
            return jsonify({"error": "not connected", "connect_url": "/connect-whatsapp"}), 401
        account_id = row["Account_id"]
        acc = row["Access_token"]
        expire = row["Token_expire"]
        try:
            token_expiry = datetime.fromisoformat(expire)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid stored token expiry"}), 500
        if token_expiry - datetime.now(timezone.utc) < timedelta(days=2):
            refreshed = what.refresh_token(token, user_id, acc)
            if not refreshed:
                return jsonify({"error": "token refresh failed, please reconnect WhatsApp"}), 502
            acc = refreshed
        for recipient, recipient_name in zip(target, names):
            now = datetime.now(timezone.utc).isoformat()
            content = f"{recipient},{campaign_id},{campaign_name},{now},,"
            try:
                personalized_body = body.replace("{name}", recipient_name) if recipient_name else body
                what.send_whatsapp_message(PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=acc, recipient_number=recipient, message_body=personalized_body)
                for m in media:
                    what.send_whatsapp_media( PHONE_NUMBER_ID=account_id, ACCESS_TOKEN=acc, recipient_number=recipient,msg_type=m["type"], link=m["link"],caption=m.get("caption"), filename=m.get("filename"),  )
                dpp.append_to_file(token=token, platform=platform, filename="campaigns.txt", data_to_append=content)
                results.append({"to": recipient, "status": "sent"})
            except Exception as e:
                logger.exception("campaign send failed for %s", recipient)
                results.append({"to": recipient, "status": "failed", "error": str(e)})
    return jsonify({"count": len(results), "results": results})