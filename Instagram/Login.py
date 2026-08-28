import database.UserDB as dbimp
import os
import requests
from urllib.parse import urlencode
from flask import Flask, request, redirect, jsonify
from supabase import create_client, Client
from datetime import datetime, timezone, timedelta
import time
import authnew as au
import Instagram.upload as uploadd
import Instagram.schedule_video as scccc
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
serializer = URLSafeTimedSerializer(app.secret_key)
IG_APP_ID = os.getenv("IG_APP_ID")
IG_APP_SECRET = os.getenv("IG_APP_SECRET")
IG_REDIRECT_URI = os.getenv("IG_REDIRECT_URI")
mail = os.environ.get("email")
passw = os.environ.get("pass")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
STATE_MAX_AGE = 600  # seconds
TABLE_NAME = "Instagram"
SCOPE = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_comments"
BASE_URL = ""

def check_user_id(token, uuser_id):
    rows = dbimp.select_rows(token, TABLE_NAME, select="id", filters={"id": uuser_id})
    exist = rows[0] if rows else None
    if not exist:
        return False
    return True

def parse_datetime(value: str, require_tz: bool = True):
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if require_tz and dt.tzinfo is None:
        return None
    return dt

def _validate_int(value, field_name="value") -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}: {value!r}")

def _coerce_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def refresh_token(token, user_id, access_token):
    resp = requests.get("https://graph.instagram.com/refresh_access_token",params={"grant_type": "ig_refresh_token", "access_token": access_token},).json()
    new_token = resp.get("access_token")
    seconds = resp.get("expires_in")
    if new_token and seconds:
        new_expire = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        dbimp.update_rows(token,TABLE_NAME,{"Access_token": new_token, "Token_expire": new_expire.isoformat()},filters={"id": user_id},)
        return new_token
    return access_token

def get_authenticated_access_token(account_id):
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 200)
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return None, (jsonify({"error": "invalid user id"}), 401)
    rows = dbimp.select_rows(tokench["token"], TABLE_NAME, select="Access_token,Token_expire", filters={"Account_id": account_id})
    if not rows:
        return None, (jsonify({"error": "no instagram account linked"}), 404)
    row = rows[0]
    access_token = row["Access_token"]
    raw_expiry = row["Token_expire"]
    if not access_token or not raw_expiry:
        return None, (jsonify({"error": "missing access_token"}), 400)
    Token_expiry = datetime.fromisoformat(raw_expiry)
    if Token_expiry.tzinfo is None:
        Token_expiry = Token_expiry.replace(tzinfo=timezone.utc)
    if Token_expiry - datetime.now(timezone.utc) < timedelta(days=2):
        access_token = refresh_token(tokench["token"], user_id, access_token)
    return access_token, None

@app.route("/auth/instagram/login")
def instagram_login():
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"]:
        return jsonify({"status": "failed", "reason": tokench["reason"]}), 200
    user_id = tokench["user_id"]
    if not check_user_id(tokench["token"], user_id):
        return jsonify({"error": "invalid user id"}), 401
    state = serializer.dumps(user_id)
    params = {"client_id": IG_APP_ID,"redirect_uri": IG_REDIRECT_URI,"scope": SCOPE,"response_type": "code","state": state,}
    auth_url = "https://www.instagram.com/oauth/authorize?" + urlencode(params)
    return redirect(auth_url)


@app.route("/auth/instagram/callback")
def instagram_callback():
    code = request.args.get("code")
    state = request.args.get("state")
    if not code:
        return jsonify({"error": "missing code"}), 400
    if not state:
        return jsonify({"error": "missing state"}), 400
    try:
        user_id = serializer.loads(state, max_age=STATE_MAX_AGE)
    except SignatureExpired:
        return jsonify({"error": "state expired, please reconnect"}), 400
    except BadSignature:
        return jsonify({"error": "invalid state"}), 400
    expiry_ts = datetime.now(timezone.utc) + timedelta(hours=1)
    token = au.jsonspoof(user_id=user_id, timestamp=expiry_ts)
    if not check_user_id(token, user_id):
        return jsonify({"error": "invalid user id"}), 400
    token_resp = requests.post("https://api.instagram.com/oauth/access_token",data={"client_id": IG_APP_ID,"client_secret": IG_APP_SECRET,"grant_type": "authorization_code","redirect_uri": IG_REDIRECT_URI,"code": code,},).json()
    short_token = token_resp.get("access_token")
    if not short_token:
        return jsonify({"error": "token exchange failed", "details": token_resp}), 400
    long_resp = requests.get("https://graph.instagram.com/access_token",params={"grant_type": "ig_exchange_token","client_secret": IG_APP_SECRET,"access_token": short_token,},).json()
    long_token = long_resp.get("access_token")
    seconds = long_resp.get("expires_in")
    if not long_token or not seconds:
        return jsonify({"error": "token exchange failed", "details": long_resp}), 400
    expire_time = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    about = requests.get("https://graph.instagram.com/me",params={"fields": "id,username,account_type,media_count", "access_token": long_token},).json()
    account = about.get("username")
    account_id = about.get("id")
    timestamp = datetime.now(timezone.utc).isoformat()
    expire = expire_time.isoformat()
    payload = {"user_id": user_id,"account_id": account_id,"username": account,"expire": expire,"timestamp": timestamp,"token": token,"access": long_token,}
    signed_payload = serializer.dumps(payload)
    resp = requests.post(f"{BASE_URL}/auth/instagram/callbackshi", json={"data": signed_payload}, timeout=5)
    return (resp.content, resp.status_code, resp.headers.items())

@app.route("/auth/instagram/callbackshi", methods=["POST"])
def dataget():
    raw = request.get_json(silent=True) or {}
    try:
        data = serializer.loads(raw.get("data"), max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"status": False, "error": "invalid or expired payload"}), 403
    token = data.get("token")
    access_token = data.get("access")
    user_id = data.get("user_id")
    timestamp = data.get("timestamp")
    expirey = data.get("expire")
    username = data.get("username")
    account_id = data.get("account_id")
    if not all([token, access_token, user_id, timestamp, expirey, username, account_id]):
        return jsonify({"error": "missing required fields"}), 400
    try:
        datetime.fromisoformat(timestamp)
        datetime.fromisoformat(expirey)
    except ValueError:
        return jsonify({"error": "invalid timestamp/expire format"}), 400
    try:
        dbimp.update_rows(token,TABLE_NAME,{"Access_token": access_token,"Timestamp": timestamp,"Token_expire": expirey,"Username": username,"Account_id": account_id},filters={"id": user_id},)
    except Exception as e:
        return jsonify({"error": "token stored failed to save", "details": str(e)}), 500
    return jsonify({"status": "ok"}), 200

@app.route("/instagram/posts/")
def get_instagram_posts():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    fields = "id,caption,media_type,media_url,permalink,timestamp,like_count,comments_count"
    url = "https://graph.instagram.com/me/media"
    params = {"fields": fields, "access_token": access_token}
    posts = []
    while url:
        resp = requests.get(url, params=params).json()
        if "error" in resp:
            return jsonify(resp), 400
        posts.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
    for post in posts:
        try:
            thumb_result = uploadd.get_media_thumbnail(access_token=access_token,media_id=post["id"])
        except Exception as e:
            post["thumbnail_url"] = None
            continue
        if thumb_result["success"]:
            post["thumbnail_url"] = thumb_result["data"]
        else:
            post["thumbnail_url"] = None
    return jsonify({"count": len(posts), "posts": posts}) , 200

@app.route("/instagram/stories/")
def get_instagram_stories():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    fields = "id,media_type,media_url,timestamp"
    url = "https://graph.instagram.com/me/stories"
    params = {"fields": fields, "access_token": access_token}
    stories = []
    while url:
        resp = requests.get(url, params=params).json()
        if "error" in resp:
            return jsonify(resp), 400
        stories.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
    for story in stories:
        try:
            thumb_result = uploadd.get_media_thumbnail(access_token=access_token, media_id=story["id"])
        except Exception as e:
            story["thumbnail_url"] = None
            continue
        if thumb_result["success"]:
            story["thumbnail_url"] = thumb_result["data"]
        else:
            story["thumbnail_url"] = None
    return jsonify({"count": len(stories), "stories": stories}), 200

@app.route("/instagram/comments/")
def get_instagram_comments():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    media_id = body.get("media_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    fields = "id,text,username,timestamp,like_count"
    url = f"https://graph.instagram.com/{media_id}/comments"
    params = {"fields": fields, "access_token": access_token}
    comments = []
    while url:
        resp = requests.get(url, params=params).json()
        if "error" in resp:
            return jsonify(resp), 400
        comments.extend(resp.get("data", []))
        url = resp.get("paging", {}).get("next")
        params = None
    return jsonify({"count": len(comments), "comments": comments})

@app.route("/instagram/upload/story", methods=["POST"])
def story():
    data = request.get_json(silent=True) or {}
    media_url = data.get("media_url")
    is_video = data.get("is_video")
    media_size = data.get("media_size")
    publish = data.get("publish")
    duration = data.get("duration")
    usernames = data.get("username")  # expected: list of usernames
    timee_raw = data.get("time") or {}
    token = data.get("token")
    if not isinstance(usernames, list):
        usernames = [usernames] if usernames else []
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not media_url or media_size is None or not token:
        return jsonify({"error": "url and media size is required"}), 400
    tokench = au.process(token)
    media_size = _coerce_int(media_size)
    duration = _coerce_int(duration)
    if media_size is None or duration is None:
        return jsonify({"success": False, "message": "Unable to post story. due to duration / media size int value"}), 400
    try:
        _validate_int(media_size)
        _validate_int(duration)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    video_type = str(is_video).strip().lower() == "true"
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid type or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    rows = dbimp.select_rows(token, TABLE_NAME, select="Username,Account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["Username"]: row for row in rows}
    results = []
    for user in usernames:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["Account_id"]
        access_token, err = get_authenticated_access_token(account_id)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_story(access_token=access_token,ig_user_id=account_id,media_size=media_size,media_url=media_url,publish=publish_now,is_video=video_type,media_duration=duration,timmmm=timee,)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post story: {e}"})
            continue
        if id_post:
            uploadd.scccc(user_id=tokench["user_id"], access_token=access_token,media_id=id_post,token=tokench["token"],typee="story",)
            results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
        else:
            results.append({"username": user, "account_id": account_id, "success": False, "message": "Unable to post story."})
    overall_success = any(r["success"] for r in results)
    status_code = 200 if overall_success else 500
    return jsonify({"success": overall_success, "results": results}), status_code

@app.route("/instagram/upload/photo", methods=["POST"])
def photo():
    data = request.get_json(silent=True) or {}
    media_url = data.get("media_url")
    media_size = data.get("media_size")
    publish = data.get("publish")
    caption = data.get("caption", "")
    timee_raw = data.get("time") or {}
    token = data.get("token")
    usernames = data.get("username")
    if not isinstance(usernames, list):
        usernames = [usernames] if usernames else []
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not media_url or media_size is None:
        return jsonify({"error": "url and media size is required"}), 400
    tokench = au.process(token)
    media_size = _coerce_int(media_size)
    if media_size is None:
        return jsonify({"success": False, "message": "Unable to post photo. due media size int value"}), 400
    try:
        _validate_int(media_size)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    rows = dbimp.select_rows(token, TABLE_NAME, select="Username,Account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["Username"]: row for row in rows}
    results = []
    for user in usernames:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["Account_id"]
        access_token, err = get_authenticated_access_token(account_id)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_photo(access_token=access_token,ig_user_id=account_id,image_url=media_url,caption=caption,media_size=media_size,publish=publish_now,timmmm=timee,)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post photo: {e}"})
            continue
        if id_post:
            uploadd.xcccc(user_id=tokench["user_id"], access_token=access_token, media_id=id_post, token=tokench["token"], typee="photo1")
            results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
        else:
            results.append({"username": user, "account_id": account_id, "success": False, "message": "Unable to post photo."})
    overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/instagram/upload/video", methods=["POST"])
def video():
    data = request.get_json(silent=True) or {}
    media_url = data.get("media_url")
    cover_url = data.get("cover_url")
    media_size = data.get("media_size")
    publish = data.get("publish")
    caption = data.get("caption", "")
    as_reel = data.get("as_reel")
    height = data.get("height")
    width = data.get("width")
    duration = data.get("duration")
    timee_raw = data.get("time") or {}
    token = data.get("token")
    usernames = data.get("username")
    if not isinstance(usernames, list):
        usernames = [usernames] if usernames else []
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not media_url or media_size is None:
        return jsonify({"error": "url and media size is required"}), 400
    tokench = au.process(token)
    media_size = _coerce_int(media_size)
    duration = _coerce_int(duration)
    width = _coerce_int(width)
    height = _coerce_int(height)
    if None in (media_size, duration, width, height):
        return jsonify({"success": False, "message": "Unable to post video. due to media size / duration / width / height is not int"}), 400
    try:
        _validate_int(media_size)
        _validate_int(duration)
        _validate_int(width)
        _validate_int(height)
    except ValueError as e:
        return jsonify({"success": False, "message": str(e)}), 400
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    as_reeel = str(as_reel).strip().lower() == "true"
    if cover_url and not as_reeel:
        return jsonify({"success": False, "message": "cover_url is only supported when as_reel is true"}), 400
    rows = dbimp.select_rows(token, TABLE_NAME, select="Username,Account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["Username"]: row for row in rows}
    results = []
    for user in usernames:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["Account_id"]
        access_token, err = get_authenticated_access_token(account_id)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_video(access_token=access_token,ig_user_id=account_id,video_url=media_url,media_size=media_size,caption=caption,publish=publish_now,cover_url=cover_url if as_reeel else None, as_reel=as_reeel,media_duration=duration,width=width,height=height,timmmm=timee,)
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post video: {e}"})
            continue
        if id_post:
            uploadd.xcccc(user_id=tokench["user_id"], access_token=access_token, media_id=id_post, token=tokench["token"], typee="video1")
            results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
        else:
            results.append({"username": user, "account_id": account_id, "success": False, "message": "Unable to post video."})
    overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/instagram/upload/carousel", methods=["POST"])
def carousel():
    data = request.get_json(silent=True) or {}
    publish = data.get("publish")
    caption = data.get("caption", "")
    media_size = data.get("media_size", [])
    media_duration = data.get("media_duration", [])
    media_urls = data.get("media_urls", [])
    is_video = data.get("is_video", [])
    timee_raw = data.get("time") or {}
    token = data.get("token")
    usernames = data.get("username")
    if not isinstance(usernames, list):
        usernames = [usernames] if usernames else []
    if not usernames:
        return jsonify({"error": "at least one username is required"}), 400
    if not media_urls or not is_video or not media_size or not media_duration:
        return jsonify({"success": False, "message": "media_urls, is_video, media_size, and media_duration are all required"}), 400
    if not (len(media_urls) == len(is_video) == len(media_size) == len(media_duration)):
        return jsonify({"success": False, "message": "media_urls, is_video, media_size, and media_duration must all be the same length"}), 400
    tokench = au.process(token)
    is_videoo = [str(p).strip().lower() == "true" for p in is_video]
    media_sizee = [_coerce_int(p) for p in media_size]
    if any(v is None for v in media_sizee):
        return jsonify({"success": False, "message": "one or more media_size values are not valid ints"}), 400
    media_durationn = [_coerce_int(p) for p in media_duration]
    if any(v is None for v in media_durationn):
        return jsonify({"success": False, "message": "one or more media_duration values are not valid ints"}), 400
    publish_now = str(publish).strip().lower() == "true"
    timee = parse_datetime(timee_raw)
    if timee is None:
        return jsonify({"error": "invalid or missing date/time"}), 400
    now = datetime.now(timezone.utc)
    lb = now + timedelta(seconds=180)
    up = now + timedelta(hours=23)
    if timee < lb or timee > up:
        return jsonify({"error": "invalid time for the posting"}), 400
    rows = dbimp.select_rows(token, TABLE_NAME, select="Username,Account_id",filters={"id": tokench["user_id"]})
    rows_by_username = {row["Username"]: row for row in rows}
    results = []
    for user in usernames:
        row = rows_by_username.get(user)
        if row is None:
            results.append({"username": user, "success": False, "message": "account not found"})
            continue
        account_id = row["Account_id"]
        access_token, err = get_authenticated_access_token(account_id)
        if err:
            results.append({"username": user, "account_id": account_id, "success": False, "message": err})
            continue
        try:
            id_post = uploadd.post_carousel(access_token=access_token,ig_user_id=account_id,is_video=is_videoo,media_size=media_sizee,media_duration=media_durationn,media_urls=media_urls,publish=publish_now,caption=caption,timmmm=timee, )
        except Exception as e:
            results.append({"username": user, "account_id": account_id, "success": False, "message": f"Unable to post carousel: {e}"})
            continue
        if id_post:
            uploadd.xcccc(user_id=tokench["user_id"], access_token=access_token, media_id=id_post, token=tokench["token"], typee="carousel1")
            results.append({"username": user, "account_id": account_id, "success": True, "media_id": id_post})
        else:
            results.append({"username": user, "account_id": account_id, "success": False, "message": "Unable to post carousel."})
    overall_success = any(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), (200 if overall_success else 500)

@app.route("/instagram/auto", methods=["POST"])
def get_thumbnail_auto():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    media_ids = body.get("media_id")
    if len(media_ids) == 1 and "," in media_ids[0]:
        media_ids = [m.strip() for m in media_ids[0].split(",") if m.strip()]
    access_token, err = get_authenticated_access_token(account_id)
    if err:
        return err
    if not media_ids:
        return jsonify({"success": False, "message": "media_id is required"}), 400
    results_by_id = {}
    errors_by_id = {}
    for mid in media_ids:
        try:
            result = uploadd.get_media_thumbnail(access_token=access_token, media_id=mid)
        except Exception as e:
            errors_by_id[mid] = f"Unable to fetch thumbnail: {e}"
            continue
        if result["success"]:
            results_by_id[mid] = result["data"]
        else:
            errors_by_id[mid] = result["error"]
    return jsonify({ "success": len(errors_by_id) == 0, "data": results_by_id,"errors": errors_by_id if errors_by_id else None }), 200 if not errors_by_id else 207

@app.route("/instagram/insight", methods=["POST"])
def insight():
    body = request.get_json(silent=True) or {}
    is_story = body.get("is_story")
    media_id = body.get("media_id")
    account_id = body.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    is_story = str(is_story).strip().lower() == "true" if is_story else False
    try:
        result = uploadd.get_media_insights(access_token=access_token, media_id=media_id , story = is_story)
    except Exception as e:
        return jsonify({"success": False, "message": f"Unable to fetch insight: {e}"}), 500
    if result["success"]:
        return jsonify({"success": True, "data": result["data"]}), 200
    else:
        return jsonify({"success": False, "message": result["error"]}), 500
    
@app.route("/instagram/comments/reply/batch/", methods=["POST"])
def reply_to_comments_batch():
    data = request.get_json(silent=True) or {}
    replies = data.get("replies")  # expects [{"comment_id": "...", "message": "..."}, ...]
    account_id = data.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    if not replies or not isinstance(replies, list):
        return jsonify({"success": False, "message": "expected a non-empty 'replies' list"}), 400
    results = []
    for item in replies:
        comment_id = item.get("comment_id")
        message = item.get("message")
        if not comment_id or not message:
            results.append({"comment_id": comment_id, "success": False, "error": "missing comment_id or message"})
            continue
        try:
            r = uploadd.reply_to_comment(access_token=access_token, comment_id=comment_id, message=message)
        except Exception as e:
            r = {"success": False, "data": None, "error": str(e)}
        results.append({"comment_id": comment_id, "success": r["success"], "data": r.get("data"), "error": r.get("error")})
        time.sleep(0.2)
    overall_success = all(r["success"] for r in results)
    return jsonify({"success": overall_success, "results": results}), 200

@app.route("/instagram/send-message/", methods=["POST"])
def send_instagram_message():
    data = request.get_json(silent=True) or {}
    recipient_id = data.get("recipient_id")
    account_id = data.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    message = data.get("message")
    if not recipient_id or not message:
        return jsonify({"error": "recipient_id and message are required"}), 400
    result = uploadd.send_message(recipient_id, message, access_token)
    if not result["success"]:
        return jsonify(result), 400
    return jsonify(result), 200

@app.route("/instagram/followers")
def get_followers_count_route():
    body = request.get_json(silent=True) or {}
    account_id = body.get("account_id")
    access_token, err = get_authenticated_access_token(account_id)
    if err: return err
    result = uploadd.get_follower_count(account_id, access_token)
    if not result["success"]:
        return jsonify({"error": result["error"]}), 400
    return jsonify(result["data"])

if __name__ == "__main__":
    app.run(port=5000, debug=True)













#  account id will be hideen and checks by the db 