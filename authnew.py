import base64
import string
import secrets
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
import os
import database.UserDB as dbimp
from dotenv import load_dotenv
load_dotenv()
SECRET_KEY = os.environ.get("SECRET_KEY")

def jsonspoof(user_id, timestamp):
    user_id1 = base64.b64encode(user_id.encode("utf-8")).decode("utf-8")
    time1 = base64.b64encode(str(timestamp).encode("utf-8")).decode("utf-8")
    signature = hmac.new(SECRET_KEY.encode("utf-8"), f"{user_id}.{timestamp}".encode("utf-8"), hashlib.sha256).hexdigest()
    signature1 = base64.b64encode(signature.encode("utf-8")).decode("utf-8")
    return f"{user_id1}.{time1}.{signature1}"

def jp(token):
    token = token.split(".")
    user_id = base64.b64decode(token[0]).decode('utf-8') if token[0] else False
    time = base64.b64decode(token[1]).decode('utf-8') if token[1] else False
    signature = base64.b64decode(token[2]).decode('utf-8') if token[2] else False   
    if not user_id or not time or not signature :
        return False
    return {"user" : user_id , "time" : time , "sign" : signature }

def process(token):
    if not token.strip():
        return {"status" : False , "reason": "Token is needed"}
    decd = jp(token)
    if not decd:
        return {"status" : False, "reason" : "token is malformed"}
    user_id = decd["user"] if decd["user"] else False
    time = decd["time"] if decd["time"] else False
    sign = decd["sign"] if decd["sign"] else False
    if not user_id or not time or not sign :
        return {"status" : False, "reason" : "user_id,time,sign of them is missing"}
    expected_sign = hmac.new(SECRET_KEY.encode("utf-8"), f"{user_id}.{time}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sign, sign):
        return {"status" : False, "reason" : "invalid signature"}
    now = datetime.now(timezone.utc)
    token_time = datetime.fromisoformat(time)
    if now > token_time and now - token_time < timedelta(hours=2):
        time = datetime.now(timezone.utc) + timedelta(minutes=20)
        token_new = jsonspoof(user_id=user_id , timestamp=time)
        return {"status" : True , "token" : token_new , "user_id":user_id }
    if now > token_time and now - token_time >= timedelta(hours=2):
        return {"status" : False, "reason" : "token expired"}
    return {"status" : True , "token" : token , "user_id":user_id }
