import os
import re
import logging
from flask import Flask, request, jsonify ,redirect
from datetime import datetime, timezone, timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import Gmail.Read_mails as gc  # rename to match your actual module filename
import authnew as au
from googleapiclient.discovery import build
import requests
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail_api")
API_KEY = os.environ.get("GMAIL_API_KEY")  # set this in env, required
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_MB = 15
app.config['MAX_CONTENT_LENGTH'] = MAX_ATTACHMENT_MB * MAX_ATTACHMENTS * 1024 * 1024
ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.doc', '.docx', '.xlsx', '.csv', '.txt', '.zip'}
UPLOAD_ROOT = os.path.abspath('uploads')
ATTACH_ROOT = os.path.abspath('attachments')
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
STATE_MAX_AGE = 600  # seconds
MESSAGE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{5,50}$')
USER_ID_RE = re.compile(r'^[a-zA-Z0-9_.@-]{1,100}$')
PUBSUB_VERIFICATION_TOKEN = os.environ.get("PUBSUB_VERIFICATION_TOKEN")  # set this in env
LABEL_NAME_RE = re.compile(r'^[\w\s/.-]{1,100}$')
BASE_URL = os.environ.get("baseurl")
limiter = Limiter(get_remote_address, app=app, default_limits=["60 per minute"])

def get_valid_user_id(token):
    if not token:
        return None, None
    tokench = au.process(token=token)
    if not tokench["status"]:
        return None, None
    token = tokench["token"]
    user_id = tokench['user_id']
    if not user_id or not token or not USER_ID_RE.match(user_id):
        return None, None
    return user_id, token

def safe_error(e, status=400):
    logger.exception("request failed")
    return jsonify({"error": "request failed"}), status

def is_allowed_file(filename):
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS

@app.route("/connect-gmail")
def connect_gmail():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    if not token :
        return jsonify({"status":False}) , 403
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    state = serializer.dumps(user_id)
    flow = gc.build_flow()
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state)
    return redirect(auth_url)

@app.route("/oauth/gmail/callback")
def gmail_oauth_callback():
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
    flow = gc.build_flow()
    flow.fetch_token(code=code)
    creds = flow.credentials
    creds_json = creds.to_json()  # Credentials has a to_json() method
    service = build('gmail', 'v1', credentials=creds)
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    email_addr = service.users().getProfile(userId='me').execute()['emailAddress']
    payload = {"user_id": user_id,"creds": creds_json,"email": email_addr, "token":token}
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/gmail/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/gmail/callbackshi", methods=["POST"])
def oauth_callbac():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    user_id = data.get("user_id")
    creds = data.get("creds")
    email = data.get("email")
    if not token or not user_id or not email or not creds :
        return jsonify({"status":False}),403
    try:
        gc.save_tokens(token, user_id, creds, email)
    except Exception as e:
        return jsonify({"status": False, "error": str(e)}), 403
    return jsonify({"status":True}),200

@app.route('/messages', methods=['GET'])
@limiter.limit("30 per minute")
def list_messages():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    user_id ,token  = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token :
        return jsonify({"status":False}) , 403
    query = body.get('q', 'is:unread')[:200]
    try:
        max_results = int(body.get('max_results', 10))
    except ValueError:
        return jsonify({"error": "'max_results' must be an integer"}), 400
    max_results = max(1, min(max_results, 100))
    all_pages = body.get('all_pages', 'false').lower() == 'true'
    try:
        service = gc.get_service(token=token,user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        messages = gc.list_messages(service, query=query, max_results=max_results, all_pages=all_pages, verbose=False)
        return jsonify({"count": len(messages), "messages": messages})
    except Exception as e:
        return safe_error(e)

@app.route('/filters', methods=['GET'])
@limiter.limit("30 per minute")
def list_filters():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    user_id ,token  = get_valid_user_id(token)

    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token :
        return jsonify({"status":False}) , 403
    try:
        service = gc.get_service(token=token,user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        return jsonify(gc.list_filters(service))
    except Exception as e:
        return safe_error(e)

@app.route('/filters/create', methods=['POST'])
@limiter.limit("15 per minute")
def create_filter():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    user_id ,token  = get_valid_user_id(token)

    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token :
        return jsonify({"status":False}) , 403
    data = request.get_json(silent=True) or {}
    criteria = data.get('criteria')
    action = data.get('action')
    if not isinstance(criteria, dict) or not isinstance(action, dict):
        return jsonify({"error": "'criteria' and 'action' must be objects"}), 400
    if "from" in criteria and isinstance(criteria["from"], str):
        criteria["from"] = criteria["from"].replace(",", " OR ")
    try:
        service = gc.get_service(token=token,user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        result = gc.create_filter(service, criteria, action) # this reutrn the filter_id which is the result["id"]
        return jsonify(result)
    except Exception as e:
        return safe_error(e)

@app.route('/labels', methods=['GET'])
@limiter.limit("30 per minute")
def list_labels():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    user_id ,token  = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token :
        return jsonify({"status":False}) , 403
    try:
        service = gc.get_service(token=token,user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        return jsonify(gc.list_labels(service))
    except Exception as e:
        return safe_error(e)

@app.route('/labels/create', methods=['POST'])
@limiter.limit("15 per minute")
def create_label():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    user_id ,token  = get_valid_user_id(token)

    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token :
        return jsonify({"status":False}) , 403
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    if not name or not LABEL_NAME_RE.match(name):
        return jsonify({"error": "valid 'name' is required"}), 400
    list_visibility = data.get('list_visibility', 'labelShow')
    label_visibility = data.get('label_visibility', 'labelShow')
    if list_visibility not in ('labelShow', 'labelHide'):
        list_visibility = 'labelShow'
    if label_visibility not in ('labelShow', 'labelShowIfUnread', 'labelHide'):
        label_visibility = 'labelShow'
    try:
        service = gc.get_service(token=token,user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        result = gc.create_label(service, name, list_visibility, label_visibility)
        return jsonify(result)
    except Exception as e:
        return safe_error(e)

@app.route('/messages/read', methods=['POST'])
@limiter.limit("60 per minute")
def mark_as_read():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    message_ids = body.get("message_id")
    user_id, token = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token:
        return jsonify({"status": False}), 403
    if not message_ids:
        return jsonify({"error": "at least one valid 'message_id' is required"}), 400
    invalid = [m for m in message_ids if not MESSAGE_ID_RE.match(m)]
    if invalid:
        return jsonify({"error": "invalid message_id(s)", "invalid": invalid}), 400
    try:
        service = gc.get_service(token=token, user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        results = {}
        errors = {}
        for mid in message_ids:
            try:
                results[mid] = gc.mark_as_read(service, mid)
            except Exception as inner_e:
                errors[mid] = str(inner_e)
        return jsonify({"results": results, "errors": errors})
    except Exception as e:
        return safe_error(e)

@app.route('/messages/unread', methods=['POST'])
@limiter.limit("60 per minute")
def mark_as_unread():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    message_ids = body.get("message_id")
    user_id, token = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token:
        return jsonify({"status": False}), 403
    if not message_ids:
        return jsonify({"error": "at least one valid 'message_id' is required"}), 400
    invalid = [m for m in message_ids if not MESSAGE_ID_RE.match(m)]
    if invalid:
        return jsonify({"error": "invalid message_id(s)", "invalid": invalid}), 400
    try:
        service = gc.get_service(token=token, user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        results = {}
        errors = {}
        for mid in message_ids:
            try:
                results[mid] = gc.mark_as_unread(service, mid)
            except Exception as inner_e:
                errors[mid] = str(inner_e)
        return jsonify({"results": results, "errors": errors})
    except Exception as e:
        return safe_error(e)

@app.route('/filters/delete', methods=['DELETE'])
@limiter.limit("15 per minute")
def delete_filter():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    filter_ids = body.get("filter_id")
    user_id, token = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token:
        return jsonify({"status": False}), 403
    if not filter_ids:
        return jsonify({"error": "at least one valid 'filter_id' is required"}), 400
    invalid = [f for f in filter_ids if not MESSAGE_ID_RE.match(f)]
    if invalid:
        return jsonify({"error": "invalid filter_id(s)", "invalid": invalid}), 400
    try:
        service = gc.get_service(token=token, user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        deleted = []
        errors = {}
        for fid in filter_ids:
            try:
                gc.delete_filter(service, fid)
                deleted.append(fid)
            except Exception as inner_e:
                errors[fid] = str(inner_e)
        return jsonify({"deleted": deleted, "errors": errors})
    except Exception as e:
        return safe_error(e)

@app.route('/labels/count', methods=['GET'])
@limiter.limit("30 per minute")
def get_label_count():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    label_ids = body.get("label_id")
    user_id, token = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token:
        return jsonify({"status": False}), 403
    if not label_ids:
        return jsonify({"error": "at least one valid 'label_id' is required"}), 400
    try:
        service = gc.get_service(token=token, user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        results = {}
        errors = {}
        for lid in label_ids:
            try:
                label = service.users().labels().get(userId="me", id=lid).execute()
                results[lid] = {"id": label.get("id"),"name": label.get("name"),"messages_total": label.get("messagesTotal", 0),"messages_unread": label.get("messagesUnread", 0),"threads_total": label.get("threadsTotal", 0),"threads_unread": label.get("threadsUnread", 0),}
            except Exception as inner_e:
                errors[lid] = str(inner_e)
        return jsonify({"results": results, "errors": errors})
    except Exception as e:
        return safe_error(e)

@app.route('/labels/messages', methods=['GET'])
@limiter.limit("30 per minute")
def get_label_messages():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    label_ids = body.get("label_id")
    user_id, token = get_valid_user_id(token)
    if not user_id:
        return jsonify({"error": "valid 'user_id' is required"}), 400
    if not token:
        return jsonify({"status": False}), 403
    if not label_ids:
        return jsonify({"error": "at least one valid 'label_id' is required"}), 400
    max_results = body.get("max_results", default=20, type=int)
    max_results = max(1, min(max_results, 100))
    page_token = body.get("page_token")
    include_details = body.get("include_details", "false").lower() == "true"
    try:
        service = gc.get_service(token=token, user_id=user_id)
        if not service:
            return jsonify({"error": "not connected", "connect_url": "/connect-gmail"}), 401
        results = {}
        errors = {}
        for lid in label_ids:
            try:
                list_kwargs = {"userId": "me", "labelIds": [lid], "maxResults": max_results}
                if page_token:
                    list_kwargs["pageToken"] = page_token
                resp = service.users().messages().list(**list_kwargs).execute()
                messages = resp.get("messages", [])
                if include_details:
                    detailed = []
                    for m in messages:
                        msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",metadataHeaders=["Subject", "From", "Date"]).execute()
                        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                        detailed.append({"id": msg.get("id"),"threadId": msg.get("threadId"),"snippet": msg.get("snippet"),"subject": headers.get("Subject"),"from": headers.get("From"),"date": headers.get("Date"),})
                    messages = detailed
                results[lid] = {"messages": messages,"result_size_estimate": resp.get("resultSizeEstimate", 0),"next_page_token": resp.get("nextPageToken"),}
            except Exception as inner_e:
                errors[lid] = str(inner_e)
        return jsonify({"results": results, "errors": errors})
    except Exception as e:
        return safe_error(e)

if __name__ == '__main__':
    os.makedirs(UPLOAD_ROOT, exist_ok=True)
    os.makedirs(ATTACH_ROOT, exist_ok=True)
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode, port=int(os.environ.get("PORT", 5000)))