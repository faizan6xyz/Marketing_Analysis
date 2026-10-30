from datetime import datetime, timezone, timedelta
import os
import Instagram.schedule_video as sccc
from flask import Flask, request, redirect, jsonify
from itsdangerous import URLSafeTimedSerializer
from Whatsapp.new import ( VERIFY_TOKEN, TABLE_NAME , log,  is_valid_signature,  get_user_for_phone_number_id, send_whatsapp_message )
import database.UserDB as dbimp
import authnew as au
import Drive.dep as dp
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()
BASE_URL = ""
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
serializer = URLSafeTimedSerializer(app.secret_key)
STATE_MAX_AGE = 600  # seconds


@app.route("/webhook/whatsapp", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    log.warning("Webhook verification attempt failed (bad mode/verify token).")
    return "Verification failed", 403

@app.route("/webhook/whatsapp", methods=["POST"])  # add a reply to message for every incoming message
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
            token = au.jsonspoof(user_id=user_id, timestamp=timee) # 
            for msg in messages:
                try:
                    sender_wa_id = msg.get("from")
                    msg_type = msg.get("type")
                    body = msg.get("text", {}).get("body") if msg_type == "text" else None
                    if body and body.strip().lower() == "no":
                        send_whatsapp_message(PHONE_NUMBER_ID=my_phone_number_id,ACCESS_TOKEN=acc,recipient_number=sender_wa_id,message_body="Thanks for replying , We recived your message ",)
                    if body and body.strip().lower() == "yes":
                        campaigns_df = dp.read_csv_from_drive(my_phone_number_id, "Whatsapp", "campains.txt", as_json=False)
                        matches = campaigns_df.loc[campaigns_df['phone_no'] == sender_wa_id, "capaign_name"]
                        if matches.empty:
                            log.warning(f"No campaign match found for {sender_wa_id} on 'yes' reply.")
                        else:
                            campaign_id = matches.iloc[-1]
                            reply , comment = sccc.workflow_data(campaign_id)
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