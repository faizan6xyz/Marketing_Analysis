import os
from dotenv import load_dotenv
from supabase import create_client, Client
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime, timezone
import database.UserDB as dbimp
import authnew as au
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in your environment or .env file")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)
frontend = os.environ.get("front_end")
CORS( app, origins=[frontend], methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  allow_headers=["Content-Type", "Authorization","Request-ID"])

def all_values(rows, key):
    if not rows:
        return []
    return [row.get(key) for row in rows]

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    mail = body.get("email")
    passw = body.get("password")
    if not mail or not passw:
        return jsonify({"error": "email and password are required"}), 400
    try:
        res = supabase.auth.sign_in_with_password({"email": mail, "password": passw})
        if res.user is None:
            return jsonify({"error": "invalid credentials"}), 401
    except Exception as e:
        return jsonify({"error": "invalid credentials", "detail": str(e)}), 401
    created_at = datetime.now(timezone.utc).isoformat()
    token = au.jsonspoof(user_id=res.user.id, timestamp=created_at)
    try:
        rows = dbimp.select_rows(token, "users", select="Token", filters={"user_id": res.user.id})
    except Exception as e:
        return jsonify({"error": "failed to fetch user record", "detail": str(e)}), 500
    if not rows:
        return jsonify({"error": "user record not found"}), 500
    return jsonify({"user_id": res.user.id, "Token": rows[0]["Token"]}), 200

@app.route("/signup", methods=["POST"])
def signup():
    body = request.get_json(silent=True) or {}
    mail = body.get("email")
    passw = body.get("password")
    if not mail or not passw:
        return jsonify({"error": "email and password are required"}), 400
    try:
        res = supabase.auth.sign_up({"email": mail, "password": passw})
    except Exception as e:
        return jsonify({"error": "signup failed", "detail": str(e)}), 400
    if res.user is None:
        return jsonify({"message": "signup started, check email to confirm"}), 202
    created_at = datetime.now(timezone.utc).isoformat()
    token = au.jsonspoof(user_id=res.user.id, timestamp=created_at)
    dbimp.insert_user(email=mail, password=passw, token=token)
    insert_error = None
    try:
        insert = dbimp.insert_rows(token, "users", {"user_id": res.user.id, "created_at": created_at, "Token": token})
    except Exception as e:
        insert = None
        insert_error = str(e)
    if not insert:
        return jsonify({"Token": token, "Statusdb": False, "detail": insert_error}), 200
    return jsonify({"Token": token, "Statusdb": True, "next": "/details"}), 200

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
    address = body.get("address")
    profession = body.get("profession")
    if not name or not gmail or not address or not phone:
        return jsonify({"status": False, "reason": "name, gmail, phone, and address are all required"}), 400
    try:
        dbimp.update_rows(tokench["token"], "users", {"Name": name, "Phone_number": phone, "Address": address, "Gmail": gmail, "Profession": profession}, {"user_id": user_id})
    except Exception as e:
        return jsonify({"status": True, "Statusdb": False, "detail": str(e)}), 500
    return jsonify({"status": True, "Statusdb": True}), 200

@app.route("/check", methods=["GET"])
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
        data = dbimp.select_rows(tokench["token"], "users", select="user_id", filters={"user_id": user_id})
    except Exception:
        return jsonify({"status": False, "reason": "unable to check db"}), 401
    if not data:
        return jsonify({"status": False, "reason": "user not found"}), 404
    return jsonify({"status": True, "reason": "verified"}), 200

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
    try:
        data0 = dbimp.select_rows(tokench["token"], "Instagram", select="Username", filters={"id": user_id})
        data1 = dbimp.select_rows(tokench["token"], "Gmail", select="Email", filters={"id": user_id})
        data2 = dbimp.select_rows(tokench["token"], "Drive", select="Email", filters={"id": user_id})
        data4 = dbimp.select_rows(tokench["token"], "Whatsapp", select="Phone_no", filters={"id": user_id})
        data5 = dbimp.select_rows(tokench["token"], "Linkedin", select="Username", filters={"id": user_id})
    except Exception:
        app.logger.exception("Failed to fetch account data for user %s", user_id)
        return jsonify({"status": False, "reason": "unable to check db"}), 500
    merged = {
        "instagram": all_values(data0, "Account_id"),
        "gmail": all_values(data1, "Email"),
        "drive": all_values(data2, "Email"),
        "whatsapp": all_values(data4, "Account_id"),
        "linkedin": all_values(data5, "Account_id")
    }
    return jsonify({"status": True, "data": merged}), 200

if __name__ == "__main__":
    app.run(port=5000, debug=True)
    # user_id = '451d8b58-4575-4b7b-9158-cb39dc3aed1e'
    # token = "NDUxZDhiNTgtNDU3NS00YjdiLTkxNTgtY2IzOWRjM2FlZDFl.MjAyNi0wOC0zMCAxMTo1ODowOS41ODg0NTYrMDA6MDA=.NGFiYTVmNGEyYjAxZjM4ZDBmM2M2OTVhODcyMTg2OTQwOTg2OGU0ZmRlNjY2M2ZiNWJhODMzZThiYmJmYjc0ZQ=="
    # data0 = dbimp.select_rows(token, "Instagram", select="Username", filters={"id": user_id})
    # print(data0)