from google.oauth2 import id_token
from google.auth.transport import requests as g_requests
import base64, json
import Drive.dep as dpp
import Gmail.Read_mails as gc
import database.UserDB as dbimp
from email.utils import parseaddr
import Drive.dep as dpp
from flask import Flask,request, jsonify
app = Flask(__name__)
GMAIL_TOPIC_NAME = 'projects/YOUR_PROJECT_ID/topics/gmail-notifications'
PUBSUB_SERVICE_ACCOUNT = "your-service-account@project.iam.gserviceaccount.com"

def verify_pubsub_jwt(req):
    auth_header = req.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return False
    token = auth_header.split(' ', 1)[1]
    try:
        claims = id_token.verify_oauth2_token(token, g_requests.Request())
        return claims.get('email') == PUBSUB_SERVICE_ACCOUNT
    except ValueError:
        return False

def stop_gmail_watch(service):
    service.users().stop(userId='me').execute()

def start_gmail_watch(service):
    request_body = { 'labelIds': ['INBOX'], 'topicName': 'projects/YOUR_PROJECT_ID/topics/gmail-notifications' }
    response = service.users().watch(userId='me', body=request_body).execute()   # this reutrns the historId and the expiretion in milliseconds
    return response

@app.route('/webhook/gmail', methods=['POST'])
def gmail_webhook():
    envelope = request.get_json()
    if not envelope or 'message' not in envelope:
        return jsonify({'error': 'bad request'}), 400
    message = envelope['message']
    data = json.loads(base64.b64decode(message['data']).decode('utf-8'))
    message_id = json.loads(base64.b64decode(message["messageId"]).decode('utf-8'))
    email_address = data['emailAddress']
    new_history_id = data['historyId']
    rows = dbimp.select_rows_web("Gmail"  ,select="Account_id,id", filters={"Email":email_address})
    if not rows : 
        return False
    account_id = rows[0]["Account_id"]
    user_id = rows[0]["id"]
    service = gc.get_service_web(user_id,account_id)
    msg = service.users().messages().get( userId='me', id=message_id, format='metadata',metadataHeaders=['From', 'Subject', 'Date']).execute()
    headers = {h['name']: h['value'] for h in msg['payload']['headers']}
    _, sender = parseaddr(headers.get('From', ''))

    df = dpp.read_csv_from_drive(account_id, "Gmail", "workflowmessage.json", as_json=True)
    df1 = dpp.read_csv_from_drive(account_id, "Gmail", "campains.txt", as_json=False)
    matches = df1.loc[df1['phone_no'] == sender, "capaign_name"]
    if matches.empty:
        return False
    else:
        campaign_id = matches.iloc[-1]
        response = df.get(campaign_id ,{}).get("reply")
        response_sub = df.get(campaign_id ,{}).get("subject")
        if response_sub and response :
            gc.send_message(service=service, to=sender, subject=response_sub or "", body_text=response)
    return 200

@app.route('/webhook/gmail', methods=['POST'])
def gmail_webhook():
    if not verify_pubsub_jwt(request):
        return jsonify({'error': 'unauthorized'}), 401
    envelope = request.get_json()
    if not envelope or 'message' not in envelope:
        return jsonify({'error': 'bad request'}), 400
    message = envelope['message']
    try:
        data = json.loads(base64.b64decode(message['data']).decode('utf-8'))
    except (KeyError, ValueError):
        return jsonify({'error': 'invalid payload'}), 400
    email_address = data.get('emailAddress')
    new_history_id = data.get('historyId')
    if not email_address or not new_history_id:
        return jsonify({'error': 'missing fields'}), 400
    rows = dbimp.select_rows_web( "Gmail", select="Account_id,id,LastHistoryId", filters={"Email": email_address})
    if not rows:
        return jsonify({'status': 'unknown account'}), 200
    account_id = rows[0]["Account_id"]
    user_id = rows[0]["id"]
    stored_history_id = rows[0].get("LastHistoryId") or new_history_id
    service = gc.get_service_web(user_id, account_id)
    try:
        history = service.users().history().list( userId='me', startHistoryId=stored_history_id, historyTypes=['messageAdded']).execute()
    except Exception as e:
        print(f"history.list failed for {email_address}: {e}")
        dbimp.update_rows_web("Gmail", {"LastHistoryId": new_history_id},{"Email": email_address})
        return jsonify({'status': 'history stale, resynced'}), 200
    df = dpp.read_csv_from_drive(account_id, "Gmail", "workflowmessage.json", as_json=True)
    df1 = dpp.read_csv_from_drive(account_id, "Gmail", "campains.txt", as_json=False)
    for record in history.get('history', []):
        for added in record.get('messagesAdded', []):
            msg_id = added['message']['id']
            msg = service.users().messages().get( userId='me', id=msg_id, format='metadata', metadataHeaders=['From', 'Subject', 'Date']).execute()
            headers = {h['name']: h['value'] for h in msg['payload']['headers']}
            _, sender_email = parseaddr(headers.get('From', ''))
            if not sender_email:
                continue
            matches = df1.loc[df1['email'] == sender_email, "capaign_name"]
            if matches.empty:
                continue
            campaign_id = matches.iloc[-1]
            reply_text = df.get(campaign_id, {}).get("reply")
            reply_subject = df.get(campaign_id, {}).get("subject")
            if reply_text and reply_subject:
                gc.send_message( service=service, to=sender_email, subject=reply_subject, body_text=reply_text)
    dbimp.update_rows_web( "Gmail",  {"LastHistoryId": new_history_id},{"Email": email_address})
    return jsonify({'status': 'ok'}), 200