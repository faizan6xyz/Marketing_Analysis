from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
import os
import requests
from flask import Flask, request, redirect, jsonify
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from Whatsapp.new import ( WA_APP_ID, WA_REDIRECT_URI, GRAPH_VERSION, SCOPE, APP_SECRET, VERIFY_TOKEN, TABLE_NAME, VALID_MEDIA_TYPES, log,  InvalidPhoneNumberError, MessageTooLongError, FileTooLargeError, is_valid_signature, require_api_key, ensure_csv_exists, ensure_excel_exists, process_single_message, get_user_for_phone_number_id, check_user_id, refresh_token, send_whatsapp_message, send_whatsapp_media, send_whatsapp_location, send_whatsapp_reply_buttons, send_whatsapp_list, )
import database.UserDB as dbimp
import authnew as au
import Drive.dep as dp
from flask_cors import CORS
BASE_URL = ""
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
serializer = URLSafeTimedSerializer(app.secret_key)
STATE_MAX_AGE = 600  # seconds

@app.route("/auth/whatsapp/login")
def whatsapp_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    if not check_user_id(tokench["token"],user_id):
        return jsonify({"error": "invalid user id"}), 400
    state = serializer.dumps(user_id)
    params = {"client_id": WA_APP_ID,"redirect_uri": WA_REDIRECT_URI,"scope": SCOPE,"response_type": "code","state": state,}
    auth_url = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?" + urlencode(params)
    return redirect(auth_url)

@app.route("/auth/whatsapp/callback")
def whatsapp_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not state:
        return jsonify({"error": "missing state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please restart login"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    token_resp = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={"client_id": WA_APP_ID,"client_secret": APP_SECRET,"redirect_uri": WA_REDIRECT_URI,"code": code,},).json()
    short_token = token_resp.get("access_token")
    if not short_token:
        log.error(f"Short-token exchange failed for user {user_id}: {token_resp}")
        return jsonify({"error": "token exchange failed", "details": token_resp}), 400
    long_resp = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={"grant_type": "fb_exchange_token","client_id": WA_APP_ID,"client_secret": APP_SECRET,"fb_exchange_token": short_token,},).json()
    long_token = long_resp.get("access_token")
    seconds = long_resp.get("expires_in")
    if not long_token or not seconds:
        log.error(f"Long-token exchange failed for user {user_id}: {long_resp}")
        return jsonify({"error": "token exchange failed", "details": long_resp}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    debug = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/debug_token",params={"input_token": long_token, "access_token": f"{WA_APP_ID}|{APP_SECRET}"}, ).json()
    granular_scopes = debug.get("data", {}).get("granular_scopes", [])
    waba_ids = []
    for scope in granular_scopes:
        if scope.get("scope") == "whatsapp_business_management":
            waba_ids.extend(scope.get("target_ids", []))
    if not waba_ids:
        log.warning(f"OAuth completed for user {user_id} but no WABA was granted.")
        return jsonify({"error": "no WhatsApp Business Account was authorized"}), 422
    waba_id = waba_ids[0]
    phones = requests.get( f"https://graph.facebook.com/{GRAPH_VERSION}/{waba_id}/phone_numbers", params={"access_token": long_token},).json()
    numbers = phones.get("data", [])
    if not numbers:
        log.warning(f"OAuth completed for user {user_id}, WABA {waba_id} has no phone numbers.")
        return jsonify({"error": "no phone number found on this WhatsApp Business Account"}), 422
    phone_number_id = numbers[0].get("id")
    display_number = numbers[0].get("display_phone_number")
    times = datetime.now(timezone.utc).isoformat()
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    expp = expire_time.isoformat()
    payload = {"user_id": user_id,"access": long_token,"timestamp": times , "token":token , "account_id":phone_number_id,"phone_no":display_number,"expire":expp}
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/whatsapp/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/whatsapp/callbackshi", methods=["POST"])
def oauth_callbac():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    user_id = data.get("user_id")
    access = data.get("access")
    account_id = data.get("account_id")
    phone_no = data.get("phone_no")
    expire = data.get("expire")
    timestamp = data.get("timestamp")
    if not token or not user_id or not access or not account_id or not phone_no or not expire or not timestamp :
        return jsonify({"error": "missing required fields"}), 400
    try:
        datetime.fromisoformat(timestamp)
        datetime.fromisoformat(expire)
    except ValueError:
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows(token,TABLE_NAME,{"Access_token": access,"Timestamp": timestamp ,"Token_expire": expire ,"Account_id": account_id ,"Phone_no": phone_no,},filters={"id": user_id},)
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    log.warning("Webhook verification attempt failed (bad mode/verify token).")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])  # add a reply to message for every incoming message
def receive_webhook_message():
    if not is_valid_signature(request):
        log.error("Rejected webhook POST: invalid or missing signature.")
        return jsonify({"status": "invalid signature"}), 403
    data = request.get_json(silent=True) or {}
    entries = data.get("entry", [])
    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])
            my_phone_number_id = value.get("metadata", {}).get("phone_number_id")
            user_row = get_user_for_phone_number_id(my_phone_number_id)
            if not user_row:
                log.warning(f"Rejected webhook payload: unrecognized phone_number_id ({my_phone_number_id}).")
                continue
            user_id = user_row["id"]
            files = dbimp.select_rows_web(TABLE_NAME, select="File,Access_token", filters={"Account_id": my_phone_number_id})
            file = files[0]["File"] if files else None
            acc = files[0]["Access_token"] if files else None
            if not file or not acc:
                log.warning(f"No campaign file/access token found for phone_number_id ({my_phone_number_id}).")
                continue
            timee = datetime.now(timezone.utc) + timedelta(hours=1)
            token = au.jsonspoof(user_id=user_id, timestamp=timee)
            for msg in messages:
                try:
                    sender_wa_id = msg.get("from")
                    msg_type = msg.get("type")
                    body = msg.get("text", {}).get("body") if msg_type == "text" else None
                    if body and body.strip().lower() == "no":
                        send_whatsapp_message(PHONE_NUMBER_ID=my_phone_number_id,ACCESS_TOKEN=acc,recipient_number=sender_wa_id,message_body="Thanks for replying , We recived your message ",)
                    if body and body.strip().lower() == "yes":
                        workflow_map = dp.read_csv_from_drive(token, "Whatsapp", "workflowmessage.json", as_json=True)
                        campaigns_df = dp.read_csv_from_drive(token, "Whatsapp", "campains.txt", as_json=False)
                        matches = campaigns_df.loc[campaigns_df['phone_no'] == sender_wa_id, "campaign_id"]
                        if matches.empty:
                            log.warning(f"No campaign match found for {sender_wa_id} on 'yes' reply.")
                        else:
                            campaign_id = matches.iloc[-1]
                            reply = workflow_map.get(campaign_id, {}).get("reply")
                            if reply:
                                send_whatsapp_message(PHONE_NUMBER_ID=my_phone_number_id,ACCESS_TOKEN=acc,recipient_number=sender_wa_id,message_body=reply,)
                            else:
                                log.warning(f"No reply template found for campaign_id={campaign_id}.")
                    ss = dp.mark_status_done(token, "Whatsapp", "campains.txt", [sender_wa_id], "recieve_time", "phone_no")
                    log.info(f"mark_status_done result for {sender_wa_id}: {ss}")
                except Exception as e:
                    log.exception(f"Failed processing message from {msg.get('from')}: {e}")
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    # debug=False in production — the Werkzeug debugger is an RCE risk if exposed.
    app.run(host="0.0.0.0", port=5000, debug=False)