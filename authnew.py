import base64
import string
import secrets
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
import random
import os
import database.UserDB as dbimp
SECRET_KEY = os.environ["SECRET_KEY"].encode("utf-8")

def random_text(limit):
    password = [ secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits),]
    characters = string.ascii_letters + string.digits
    password.extend(secrets.choice(characters) for _ in range(limit))
    random.SystemRandom().shuffle(password)
    return ''.join(password)

def jsonspoof(user_id, timestamp):
    user_id1 = base64.b64encode(user_id.encode("utf-8")).decode("utf-8")
    time1 = base64.b64encode(str(timestamp).encode("utf-8")).decode("utf-8")
    signature = hmac.new(SECRET_KEY, f"{user_id}.{timestamp}".encode("utf-8"), hashlib.sha256).hexdigest()
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
    expected_sign = hmac.new(SECRET_KEY, f"{user_id}.{time}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sign, sign):
        return {"status" : False, "reason" : "invalid signature"}
    if datetime.now(timezone.utc) > datetime.fromisoformat(time) :
        time = datetime.now(timezone.utc) + timedelta(hours=1)
        token_new = jsonspoof(user_id=user_id , timestamp=time)
        dbimp.update_token_by_token(token=token,new_token=token_new)
        update = dbimp.update_rows(token_new,"users" , {"Token":token_new},{"user_id":user_id})
        if not update:
            return "unable to update"
        return {"status" : True , "token" : token_new , "user_id":user_id }
    return {"status" : True , "token" : token , "user_id":user_id }

def paidcheck(token , user_id):  # this will be used in the analytics and locks the premium features 
    if not user_id :
        return False
    rows = dbimp.select_rows(token , "users" , select="Payment_check" , filters={"user_id":user_id})
    row = rows[0] if rows[0] else None
    pay = str(row["Payment_check"]).strip().lower() == "false"
    if pay :
        return False
    return True


# time = datetime.now(timezone.utc) + timedelta(hours=1)
# x = '451d8b58-4575-4b7b-9158-cb39dc3aed1e'
# print(jsonspoof(user_id=x,timestamp=time))
# print(jp(jsonspoof(user_id=x,timestamp=time)))
# print(process("NDUxZDhiNTgtNDU3NS00YjdiLTkxNTgtY2IzOWRjM2FlZDFl.MjAyNi0wOC0yNiAxMTozMDo1Ny42ODYwNzMrMDA6MDA=.N2E0YzU2NzkwN2Y0ZjMwZWMzZDhmMTNhZmY5MzFkOTVhMjIzOWM1MmI5OGFkYTljYjVmMzFjZTM1YzUxZGI1ZA=="))