import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify , url_for
from flask_cors import CORS
import csv
import limit as lmmmm
import Instagram.schedule_video as sccc
from datetime import datetime, timezone, timedelta
import database.UserDB as dbimp
import authnew as au
from authlib.integrations.flask_client import OAuth
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("drive_api")
load_dotenv()
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS(app, origins=[frontend], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
TOKEN_TTL = timedelta(hours=2)
oauth = OAuth(app)
oauth.register( name="google", client_id=os.environ.get("GOOGLE_CLIENT_ID"), client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"), server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",client_kwargs={"scope": "openid email profile"}, )

def all_values(rows, key):
    if not rows:
        return []
    return [{"name": row.get(key),"account_id": str(row["account_id"]) if row.get("account_id") is not None else None}for row in rows]

@app.route("/oauth/google")
def login_google():
    redirect_uri = url_for("google_callback", _external=True) # redirets to "auth/google/callback"
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()  
    info = token["userinfo"]                       
    user = { "google_id": info["sub"], "email": info.get("email"), "name": info.get("name"), "picture": info.get("picture"), }
    if not dbimp.user_exist_check(user.get("email"), user.get("google_id")) : # using the google id to store this as password
        try:
            user_id = dbimp.add_new_user(user.get("email"), user.get("google_id"))
        except Exception as e :
            return jsonify({"status":False,"reason":"failed to store the user"}),202
    created_at = datetime.now(timezone.utc) + TOKEN_TTL 
    token = au.jsonspoof(user_id=user_id, timestamp=created_at)
    try:
        dbimp.insert_rows(  "users", { "user_id": user_id, "email": user.get("email"), "created_at": datetime.now(timezone.utc).isoformat(), })
    except Exception as e:
        logger.exception("failed to store set userd details")
    return jsonify({"Token": token}), 200

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    mail = body.get("email")
    passw = body.get("password")
    if not mail or not passw:
        return jsonify({"error": "email and password are required"}), 400
    try:
        user_id = dbimp.user_exist_check(email=mail, password=passw)
    except Exception as e:
        return jsonify({"error": "invalid credentials", "detail": str(e)}), 401
    if not user_id:
        return jsonify({"error": "invalid credentials"}), 401
    created_at = datetime.now(timezone.utc) + TOKEN_TTL
    token = au.jsonspoof(user_id=user_id, timestamp=created_at)
    return jsonify({"Token": token}), 200

@app.route("/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    mail = body.get("email")
    passw = body.get("password")
    if not mail or not passw:
        return jsonify({"error": "email and password are required"}), 400
    try:
        user_id = dbimp.add_new_user(email=mail,password=passw)
    except Exception as e:
        return jsonify({"error": "signup failed", "detail": str(e)}), 400
    if not user_id :
        return jsonify({"error": "invalid credentials"}), 401
    if user_id == "exist" :
        return jsonify({"error": "User Already exist "}), 401
    created_at = datetime.now(timezone.utc) + TOKEN_TTL 
    token = au.jsonspoof(user_id=user_id, timestamp=created_at)
    return jsonify({"Token": token}), 200

@app.route("/details", methods=["POST"])
def details():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 401
    user_id = tokench["user_id"]
    name = body.get("name")
    gmail = body.get("gmail")
    phone = body.get("phone")
    if not name or not gmail  or not phone:
        return jsonify({"status": False, "reason": "name, gmail, phone are all required"}), 400
    try:
        dbimp.update_rows( "users", {"name": name, "phone_number": phone, "gmail": gmail }, {"user_id": user_id})
    except Exception as e:
        return jsonify({"status": True, "Statusdb": False, "detail": str(e)}), 500
    return jsonify({"status": True, "Statusdb": True}), 200

@app.route("/check", methods=["POST"])
def check_status():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 401
    user_id = tokench["user_id"]
    if not user_id:
        return jsonify({"status": False, "reason": "Invalid user_id"}), 401
    try:
        data = dbimp.select_rows( "users", select="user_id", filters={"user_id": user_id})
    except Exception:
        return jsonify({"status": False, "reason": "unable to check db"}), 401
    if not data:
        return jsonify({"status": False, "reason": "user not found"}), 404
    return jsonify({"status": True, "reason": "verified","token":tokench["token"]}), 200

@app.route("/vrify", methods=["POST"])
def check_accounts():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 402
    user_id = tokench["user_id"]
    if not user_id:
        return jsonify({"status": False, "reason": "Invalid user_id"}), 401
    rows = dbimp.select_rows("users",select="gmail,gdrive,x,instagram,threads,pinterst,whatsapp,youtube",filters={"user_id":user_id})
    if not rows:
        return jsonify({"status": False, "reason": "Invalid user_id"}), 401
    data0 = data1 = data2 = data3 = data4 = data5 = data6 = data7 = None
    try:
        if rows[0]["instagram"]:
            data0 = dbimp.select_rows( "instagram", select="username,account_id", filters={"id": user_id})
        if rows[0]["gmail"]:
            data1 = dbimp.select_rows( "gmail", select="email,account_id", filters={"id": user_id})
        if rows[0]["gdrive"]:
            data2 = dbimp.select_rows( "drive", select="email", filters={"id": user_id})
        if rows[0]["whatsapp"]:
            data3 = dbimp.select_rows( "whatsapp", select="phone_no,account_id", filters={"id": user_id})
        if rows[0]["threads"]:
            data4 = dbimp.select_rows( "threads", select="username,account_id", filters={"id": user_id})
        if rows[0]["youtube"]:
            data5 = dbimp.select_rows( "youtube", select="channel_title,account_id", filters={"id": user_id})
        if rows[0]["pinterst"]:
            data6 = dbimp.select_rows( "pinterst", select="username,account_id", filters={"id": user_id})
        if rows[0]["x"]:
            data7 = dbimp.select_rows( "x", select="username,account_id", filters={"id": user_id})
    except Exception:
        app.logger.exception("Failed to fetch account data for user %s", user_id)
        return jsonify({"status": False, "reason": "unable to check db"}), 500
    merged = {"instagram": all_values(data0, "username"),"gmail": all_values(data1, "email"),"drive": all_values(data2, "email"),"whatsapp": all_values(data3, "phone_no"),"threads": all_values(data4, "username"),"youtube": all_values(data5, "channel_title"),"pinterest": all_values(data6, "username"),"x": all_values(data7, "username"),}
    return jsonify({"status": True, "data": merged}), 200   

@app.route("/limit", methods=["POST"])
def limit():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 402
    user_id = tokench["user_id"]
    if not user_id:
        return jsonify({"status": False, "reason": "Invalid user_id"}), 401
    now = datetime.now(timezone.utc).isoformat()
    current_count, row_id = lmmmm.get_or_create_window(user_id, now)
    if current_count :
        return jsonify({"status":True , "limit":current_count }) , 200 

@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/complaint", methods=["POST"])
def submit_complaint():
    body = request.get_json(silent=True) or {}
    complaint = body.get("complaint")
    token = body.get("token")
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 401
    if not isinstance(complaint, str) or not complaint.strip():
        return jsonify({"status": False, "error": "Complaint is required"}), 400
    if len(complaint) > 400:
        return jsonify({"status": False, "error": "Issue length too long"}), 400
    user_id = tokench["user_id"]
    with open("complaint.csv", "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([user_id, complaint, datetime.now(timezone.utc).isoformat()])
    return jsonify({"status": True, "reason": "Issue has been submitted"}), 200

@app.route("/add_workflow", methods=["POST"])
def update_workflow():
    body = request.get_json(silent=True) or {}
    id_ = body.get("id")
    comment = body.get("comment")
    message = body.get("message")
    token = body.get("token")
    tokench = au.process(token)
    if not tokench["status"]:
        return jsonify({"status": False, "reason": tokench["reason"]}), 401
    if not id_ or not comment or not message :
        return jsonify({"status": False, "reason": "parameter missing"}), 400
    if len(message) > 200 or len(comment) > 200:
        return jsonify({"status": False, "reason": "value too long"}), 413
    try:
        sccc.workflow_data_change(id_, message, comment)
    except Exception:
        return jsonify({"status": False, "reason": "server error"}), 500
    return jsonify({"status":True, "reason": "updated"}) ,200

if __name__ == "__main__":
    app.run(port=5000, debug=True)