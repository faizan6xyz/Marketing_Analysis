import requests
import os
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
import database.UserDB as dbimp
import threads.login as thhh
import authnew as au
import Drive.dep as dp
from flask import Flask, request, jsonify
app = Flask(__name__)
VERIFY_TOKEN = os.getenv("TH_VERIFY_TOKEN")
APP_SECRET = os.getenv("TH_APP_SECRET")
table = "Threads"

def create_threads_post(text, ACCESS_TOKEN, THREADS_USER_ID, reply_to_id=None):
    container_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads"
    container_params = {"media_type": "TEXT", "text": text, "access_token": ACCESS_TOKEN}
    if reply_to_id:
        container_params["reply_to_id"] = reply_to_id
    container_resp = requests.post(container_url, params=container_params)
    if container_resp.status_code != 200:
        return {"success": False, "error": container_resp.json(), "stage": "create_container"}
    creation_id = container_resp.json().get("id")
    if not creation_id:
        return {"success": False, "error": container_resp.json(), "stage": "no_creation_id"}
    publish_url = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}/threads_publish"
    publish_params = {"creation_id": creation_id, "access_token": ACCESS_TOKEN}
    publish_resp = requests.post(publish_url, params=publish_params)
    if publish_resp.status_code != 200:
        return {"success": False, "error": publish_resp.json(), "stage": "publish"}
    return {"success": True, "data": publish_resp.json()}

@app.route("/threads/comments/", methods=["GET", "POST"])
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
    if data.get("object") != "threads":
        return jsonify({"status": "ignored"}), 200
    results = []
    for entry in data.get("entry", []):
        th_account_id = entry.get("id")
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
        access_rows = dbimp.select_rows_web( table, select="id,Access_token,", filters={"Account_id": th_account_id} )
        access = access_rows[0] if access_rows else None
        if not access:
            results.append({"entry": th_account_id, "error": "no access record found for account"})
            continue
        user_id = access["id"]
        Token_expire = datetime.fromisoformat(access["Token_expire"])
        expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
        token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)    # dont need to create either retrieve or jsut use the web one
        df = dp.read_csv_from_drive(token, "Threads", "workflowcomment.json", as_json=True)
        dfid = df.get(media_id, {})
        reply = dfid.get("reply")
        if not reply:
            results.append({"entry": th_account_id, "error": "no reply configured for this media_id"})
            continue
        access_token = thhh.refresh_threads_tokenww(Token_expire, access, th_account_id)
        result = create_threads_post(reply, access_token, th_account_id, from_user_id)
        if not result["success"]:
            results.append({"entry": th_account_id, "error": result})
        else:
            results.append({"entry": th_account_id, "success": True, "data": result["data"]})
    if not results:
        return jsonify({"status": "no matching comment changes"}), 200
    return jsonify({"status": "processed", "results": results}), 200

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
    return { "account_id": account_id, "comment_id": comment_id, "from_user_id": from_user_id, "from_username": from_username, "media_id": media_id,    }

def subscribe_page_to_webhooks(user_id, access_token):  # run once per Threads user
    url = f"https://graph.threads.net/v1.0/{user_id}/webhook_subscriptions"
    resp = requests.post(url, params={"subscribed_fields": "comments", "access_token": access_token})
    return resp.json()

if __name__ == "__main__":
    app.run(port=5001, debug=True)