from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
import os
import requests
from flask import Flask, request, jsonify , redirect
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from Whatsapp.new import ( WA_APP_ID, WA_REDIRECT_URI, GRAPH_VERSION, SCOPE, APP_SECRET,  TABLE_NAME , log )
import database.UserDB as dbimp
import authnew as au
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()
BASE_URL = ""
app = Flask(__name__)
frontend = os.environ.get("front_end")
homepage = os.environ.get("homepage")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
serializer = URLSafeTimedSerializer(app.secret_key)
STATE_MAX_AGE = 600  # seconds

@app.route("/auth/login",methods=["POST"])
def whatsapp_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]}) , 403
    user_id = tokench['user_id']
    if not user_id:
        return jsonify({"error": "user_id is required"}), 400
    state = serializer.dumps(user_id)
    params = {"client_id": WA_APP_ID,"redirect_uri": WA_REDIRECT_URI,"scope": SCOPE,"response_type": "code","state": state,}
    auth_url = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?" + urlencode(params)
    return jsonify({"auth_url": auth_url}) , 200


def clean(value):
    return (value or "").split("#", 1)[0].strip()

def store_whatsapp_account(token, user_id, access_token, phone_no, account_id, timestamp, expire):
    dbimp.update_rows(TABLE_NAME,{"access_token": access_token,"timestamp": timestamp ,"token_expire": expire ,"account_id": account_id ,"phone_no": phone_no,},filters={"id": user_id},)
    dbimp.update_rows( "users", {"whatsapp": True}, {"user_id": user_id})

@app.route("/auth/callback", methods=["GET"])
def instagram_callback():
    code = clean(request.args.get("code"))
    state = clean(request.args.get("state"))
    if not code or not state:
        return jsonify({"error": "missing code or state"}), 400
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
    try:
        store_whatsapp_account(token, user_id, long_token, display_number, phone_number_id, times, expp)
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return redirect(homepage, code=302)

if __name__ == "__main__":
    # debug=False in production — the Werkzeug debugger is an RCE risk if exposed.
    app.run(host="0.0.0.0", port=5000, debug=False)