import requests
import os
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
import requests
import database.UserDB as dbimp
import upload as uploadd
import authnew as au
import Drive.dep as dp
from flask import Flask, request, jsonify
app = Flask(__name__)
VERIFY_TOKEN = os.getenv("IG_VERIFY_TOKEN")
APP_SECRET = os.getenv("IG_APP_SECRET")
table = "Instagram"

@app.route("/instagram/comments/", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return verify_webhook()
    return receive_webhook()

def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

def receive_webhook():
    if not verify_signature(request):
        return "Invalid signature", 403
    data = request.get_json()
    if data.get("object") != "instagram":
        return jsonify({"status": "ignored"}), 200
    for entry in data.get("entry", []):
        ig_account_id = entry.get("id")
        from_user_id = None
        media_id = None
        for change in entry.get("changes", []):
            if change.get("field") != "comments":
                continue
            value = change.get("value")
            if not value:
                continue
            from_user_id = value.get("from", {}).get("id")
            media_id = value.get("media", {}).get("id")
        if not from_user_id or not media_id:
            continue  # nothing usable in this entry, move to next
        access_rows = dbimp.select_rows_web(table, select="id,File", filters={"Account_id": ig_account_id})
        access = access_rows[0] if access_rows else None
        if not access:
            return jsonify({"error": "no access record found for account"}), 404
        user_id = access["id"]
        expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
        token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
        df = dp.read_csv_from_drive(token, "Instagram", "workflowcomment.json", as_json=True)
        dfid = df.get(media_id, {})
        reply = dfid.get("reply")
        if not reply:
            return jsonify({"error": "from_user_id and reply are required"}), 400
        result = uploadd.send_message(from_user_id, reply, token)  # <- fixed: use token, not access_token list
        if not result["success"]:
            return jsonify(result), 400
        return jsonify(result), 200
    return jsonify({"status": "no matching comment changes"}), 200

def verify_signature(req):
    signature = req.headers.get("X-Hub-Signature-256", "")
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), req.data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)

def handle_comment_event(account_id, value):
    comment_id = value.get("id")
    from_user_id = value.get("from", {}).get("id")
    from_username = value.get("from", {}).get("username")
    media_id = value.get("media", {}).get("id")
    return {"account_id": account_id, "comment_id": comment_id, "from_user_id": from_user_id, "from_username": from_username,  "media_id": media_id,}

def subscribe_page_to_webhooks(page_id, page_access_token): # run it once for the subscription
    url = f"https://graph.facebook.com/v25.0/{page_id}/subscribed_apps"   # webhook require facebook api and link the facebook account with it 
    resp = requests.post( url,params={"subscribed_fields": "comments", "access_token": page_access_token},)
    return resp.json()

if __name__ == "__main__":
    app.run(port=5000, debug=True)
