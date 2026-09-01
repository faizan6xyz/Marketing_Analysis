import re
import time
import Instagram.schedule_video as sccc
import logging
import requests
import database.UserDB as dbimp
import authnew as au
from datetime import datetime, timezone,timedelta
from urllib.parse import urlparse
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ig_post")
GRAPH_VERSION = "v25.0"
BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
MAX_REEL_SECONDS = 15 * 60      # 15 min
MAX_STORY_SECONDS = 60          # 60 sec
MAX_VIDEO_SECONDS = 60 * 60     # 60 min
MIN_VIDEO_SECONDS = 3           # IG rejects clips shorter than this
MAX_CAPTION_CHARS = 2170
MAX_HASHTAGS = 5
MAX_PHOTO_BYTES = 8 * 1024 * 1024        # 8 MB
MAX_VIDEO_BYTES = 1024 * 1024 * 1024     # 1 GB
MIN_ASPECT_RATIO = 4 / 5    # tallest allowed (portrait)
MAX_ASPECT_RATIO = 1.91     # widest allowed (landscape)
REQUEST_TIMEOUT = 30                 # seconds, for every HTTP call
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2               # seconds; doubles each retry
RETRYABLE_IG_ERROR_CODES = {4, 17, 32}   # IG rate-limit / throttling codes
ALLOWED_URL_SCHEMES = {"https"}
TABLE_NAME = "Instagram"

def _redact(text: str, access_token: str = None) -> str:
    if access_token:
        text = text.replace(access_token, "[REDACTED]")
    return text

def _validate_media_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(f"Refusing to fetch '{url}': only {ALLOWED_URL_SCHEMES} URLs are allowed.")
    if not parsed.netloc:
        raise ValueError(f"'{url}' is not a valid absolute URL.")

def refresh_token(token, user_id, access_token):
    resp = requests.get("https://graph.instagram.com/refresh_access_token",params={"grant_type": "ig_refresh_access_token", "access_token": access_token},).json()
    new_token = resp.get("access_token")
    seconds = resp.get("expires_in")
    if new_token and seconds:
        new_expire = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        dbimp.update_rows(token,TABLE_NAME,{"Access_token": new_token, "Token_expire": new_expire.isoformat()},filters={"id": user_id},)
        return new_token
    return access_token

def _request_with_retry(method: str, url: str, access_token: str = None, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            logger.warning(_redact(f"Network error on attempt {attempt}/{MAX_RETRIES}: {e}", access_token))
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue
        if resp.status_code == 429:
            logger.warning(f"Rate limited (HTTP 429) on attempt {attempt}/{MAX_RETRIES}")
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue
        try:
            body = resp.json()
        except ValueError:
            return resp
        err_code = body.get("error", {}).get("code")
        if err_code in RETRYABLE_IG_ERROR_CODES and attempt < MAX_RETRIES:
            logger.warning(f"IG error code {err_code} (throttled), retrying {attempt}/{MAX_RETRIES}")
            time.sleep(RETRY_BACKOFF_BASE ** attempt)
            continue
        return resp
    raise RuntimeError(_redact(f"Request to '{url}' failed after {MAX_RETRIES} attempts: {last_exc}", access_token))


def _post(endpoint: str, params: dict) -> dict:
    token = params.get("access_token")
    resp = _request_with_retry("POST", f"{BASE_URL}/{endpoint}", data=params, access_token=token)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(_redact(f"Instagram API error: {data['error']}", token))
    return data

def _get(endpoint: str, params: dict) -> dict:
    token = params.get("access_token")
    resp = _request_with_retry("GET", f"{BASE_URL}/{endpoint}", params=params, access_token=token)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(_redact(f"Instagram API error: {data['error']}", token))
    return data

def check_ig_username(target_username, ig_user_id, access_token) -> bool:
    params = {"fields": f"business_discovery.username({target_username})" "{username,id,followers_count,media_count,biography}","access_token": access_token,}
    resp = _request_with_retry("GET", f"{BASE_URL}/{ig_user_id}", params=params, access_token=access_token)
    try:
        payload = resp.json()
    except ValueError:
        return False
    if resp.status_code == 200 and "business_discovery" in payload:
        return True   # username exists (as a Business/Creator account)
    return False       # not found, or exists but isn't a business/creator account

def _check_caption(caption: str) -> str:
    if len(caption) > MAX_CAPTION_CHARS:
        caption = caption[:MAX_CAPTION_CHARS]
    hashtags = list(re.finditer(r"(?<!\w)#\w+", caption))
    if len(hashtags) > MAX_HASHTAGS:
        cutoff = hashtags[MAX_HASHTAGS].start()
        caption = caption[:cutoff].rstrip()
    match = re.search(r"(?<!\w)#\w+$", caption)
    if match and len(caption) == MAX_CAPTION_CHARS:
        caption = caption[:match.start()].rstrip()
    return caption

# Container is the object that holds the media and other info before publishing
def wait_for_container(access_token: str, container_id: str, timeout: int = 300, interval: int = 5) -> None:
    elapsed = 0
    while elapsed < timeout:
        status = _get(container_id, {"fields": "status_code", "access_token": access_token})
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"Container {container_id} failed to process")
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Container {container_id} did not finish within {timeout}s")


def post_photo(timmmm,access_token: str, ig_user_id: str, image_url: str, caption: str = "", media_size: int = None, publish: bool = True, ) -> str:
    _validate_media_url(image_url)
    caption = _check_caption(caption)
    if media_size is not None and media_size > MAX_PHOTO_BYTES:
        raise ValueError(f"Photo exceeds max size of {MAX_PHOTO_BYTES} bytes")
    params = {"image_url": image_url, "caption": caption, "access_token": access_token, }
    container = _post(f"{ig_user_id}/media", params)
    creation_id = container["id"]
    if not publish:
        sccc.insert_time(ig_user_id,creation_id,timmmm,access_token)
        return creation_id
    return publish_container(access_token, ig_user_id, creation_id)

def post_video(timmmm,access_token: str, ig_user_id: str, height: int, width: int, video_url: str, media_size: int, caption: str = "", as_reel: bool = True, cover_url: str = None,publish: bool = True, media_duration: int = 0, ) -> str:
    _validate_media_url(video_url)
    if cover_url is not None:
        if not as_reel:
            raise ValueError("cover_url (custom thumbnail) is only supported for Reels")
        _validate_media_url(cover_url)
    caption = _check_caption(caption)
    if media_size > MAX_VIDEO_BYTES:
        raise ValueError(f"Video exceeds max size of {MAX_VIDEO_BYTES} bytes")
    if media_duration < MIN_VIDEO_SECONDS:
        raise ValueError(f"Video is shorter than the minimum of {MIN_VIDEO_SECONDS}s")
    if as_reel:
        if media_duration > MAX_REEL_SECONDS:
            raise ValueError(f"Reel exceeds max duration of {MAX_REEL_SECONDS}s")
    else:
        if media_duration > MAX_VIDEO_SECONDS:
            raise ValueError(f"Video exceeds max duration of {MAX_VIDEO_SECONDS}s")
    ratio = width / height
    if not (MIN_ASPECT_RATIO - 0.01 <= ratio <= MAX_ASPECT_RATIO + 0.01):
        raise ValueError(f"Aspect ratio {ratio:.2f} is outside the allowed range")
    params = {"video_url": video_url,"caption": caption,"media_type": "REELS" if as_reel else "VIDEO", "access_token": access_token, }
    if cover_url is not None:
        params["cover_url"] = cover_url  # custom thumbnail image; takes precedence over thumb_offset
    container = _post(f"{ig_user_id}/media", params)
    creation_id = container["id"]
    wait_for_container(access_token, creation_id)
    if not publish:
        sccc.insert_time(ig_user_id,creation_id,timmmm,access_token)
        return creation_id
    return publish_container(access_token, ig_user_id, creation_id)
 
def post_carousel(timmmm, access_token: str, ig_user_id: str,   media_size: list[int], media_duration: list[int], media_urls: list[str], is_video: list[bool], caption: str = "", publish: bool = True, ) -> str:
    if len(media_urls) != len(is_video):
        raise ValueError("media_urls and is_video must be the same length")
    if not (2 <= len(media_urls) <= 5):
        raise ValueError("Carousels need 2-10 items")
    caption = _check_caption(caption)
    for url, vid, siz, dura in zip(media_urls, is_video, media_size, media_duration):
        _validate_media_url(url)
        if vid:
            if siz > MAX_VIDEO_BYTES:
                raise ValueError(f"Video exceeds max size of {MAX_VIDEO_BYTES} bytes")
            if dura > MAX_VIDEO_SECONDS:
                raise ValueError(f"Video exceeds max duration of {MAX_VIDEO_SECONDS}s")
        else:
            if siz > MAX_PHOTO_BYTES:
                raise ValueError(f"Photo exceeds max size of {MAX_PHOTO_BYTES} bytes")
    child_ids = []
    for url, vid in zip(media_urls, is_video):
        params = {"is_carousel_item": "true", "access_token": access_token}
        if vid:
            params["media_type"] = "VIDEO"
            params["video_url"] = url
        else:
            params["image_url"] = url
        child = _post(f"{ig_user_id}/media", params)
        child_id = child["id"]
        if vid:
            wait_for_container(access_token, child_id)
        child_ids.append(child_id)
    params = { "media_type": "CAROUSEL", "children": ",".join(child_ids), "caption": caption, "access_token": access_token,}
    container = _post(f"{ig_user_id}/media", params)
    creation_id = container["id"]
    if not publish:
        sccc.insert_time(ig_user_id,creation_id,timmmm,access_token)
        return creation_id
    return publish_container(access_token, ig_user_id, creation_id)

def post_story( timmmm,access_token: str, ig_user_id: str, media_size: int, media_url: str, is_video: bool = False, publish: bool = True, media_duration: int = 0, ) -> str:
    _validate_media_url(media_url)
    if is_video:
        if media_size > MAX_VIDEO_BYTES:
            raise ValueError(f"Video exceeds max size of {MAX_VIDEO_BYTES} bytes")
        if media_duration > MAX_STORY_SECONDS:
            raise ValueError(f"Story exceeds max duration of {MAX_STORY_SECONDS}s")
    else:
        if media_size > MAX_PHOTO_BYTES:
            raise ValueError(f"Photo exceeds max size of {MAX_PHOTO_BYTES} bytes")
    params = {"media_type": "STORIES", "access_token": access_token}
    if is_video:
        params["video_url"] = media_url
    else:
        params["image_url"] = media_url
    container = _post(f"{ig_user_id}/media", params)
    creation_id = container["id"]
    if is_video:
        wait_for_container(access_token, creation_id)
    if not publish:
        sccc.insert_time(ig_user_id,creation_id,timmmm,access_token)
        return creation_id
    return publish_container(access_token, ig_user_id, creation_id)

def get_media_insights(media_id, access_token, story ):
    metrics = ("views","reach", "replies","shares","likes","navigation","profile_activity") if story else ("views","reach","likes","comments","saved","shares","total_interactions","profile_activity","follows","caption","timestamp")    
    if not access_token:
        return {"success": False, "data": None, "error": f"missing access_token for {media_id}"}
    url = f"{BASE_URL}/{media_id}/insights"
    params = {"metric": ",".join(metrics), "access_token": access_token}
    try:
        response = requests.get(url, params=params, timeout=5)
        payload = response.json()
    except requests.RequestException as e:
        return {"success": False, "data": None, "error": f"request failed for {media_id}: {e}"}
    except ValueError as e:
        return {"success": False, "data": None, "error": f"response was not valid JSON for {media_id}: {e}"}
    if "error" in payload:
        return {"success": False, "data": None, "error": f"API error for {media_id}: {payload['error']}"}
    result = {}
    try:
        for item in payload.get("data", []):
            name = item.get("name")
            values = item.get("values", [])
            if name and values:
                result[name] = values[0].get("value")
    except (KeyError, IndexError, TypeError) as e:
        return {"success": False, "data": None, "error": f"unexpected response shape for {media_id}: {e}"}
    return {"success": True, "data": result, "error": None}

def get_media_thumbnail(media_id, access_token):
    if not access_token:
        return {"success": False, "data": None, "error": f"missing access_token for {media_id}"}
    url = f"https://graph.instagram.com/{media_id}"
    params = {"fields": "id,media_type,media_url,thumbnail_url,permalink,children{id,media_type,media_url,thumbnail_url}", "access_token": access_token,}
    try:
        response = requests.get(url, params=params, timeout=5)
        payload = response.json()
    except requests.RequestException as e:
        return {"success": False, "data": None, "error": f"request failed for {media_id}: {e}"}
    except ValueError as e:
        return {"success": False, "data": None, "error": f"response was not valid JSON for {media_id}: {e}"}
    if "error" in payload:
        return {"success": False, "data": None, "error": f"API error for {media_id}: {payload['error']}"}
    media_type = payload.get("media_type")
    if media_type == "VIDEO":
        thumbnail_url = payload.get("thumbnail_url")
        if not thumbnail_url:
            return {"success": False, "data": None, "error": f"no thumbnail_url returned for {media_id}"}
        return {"success": True, "data": {"media_id": media_id, "thumbnail_url": thumbnail_url, "media_type": media_type}, "error": None}
    if media_type == "IMAGE":
        media_url = payload.get("media_url")
        if not media_url:
            return {"success": False, "data": None, "error": f"no media_url returned for {media_id}"}
        return {"success": True, "data": {"media_id": media_id, "thumbnail_url": media_url, "media_type": media_type}, "error": None}
    if media_type == "CAROUSEL_ALBUM":
        children = payload.get("children", {}).get("data", [])
        if not children:
            return {"success": False, "data": None, "error": f"carousel {media_id} has no children"}
        first = children[0]
        child_type = first.get("media_type")
        thumbnail_url = first.get("thumbnail_url") if child_type == "VIDEO" else first.get("media_url")
        if not thumbnail_url:
            return {"success": False, "data": None, "error": f"no thumbnail found for first item in carousel {media_id}"}
        return {"success": True, "data": {"media_id": media_id, "thumbnail_url": thumbnail_url, "media_type": media_type}, "error": None}
    return {"success": False, "data": None, "error": f"unsupported media_type '{media_type}' for {media_id}"}

def reply_to_comment(comment_id, message, access_token):
    if not access_token:
        return {"success": False, "data": None, "error": f"missing access_token for {comment_id}"}
    if not message:
        return {"success": False, "data": None, "error": f"missing message for {comment_id}"}
    url = f"{BASE_URL}/{comment_id}/replies"
    params = {"message": message, "access_token": access_token}
    try:
        response = requests.post(url, params=params, timeout=5)
        payload = response.json()
    except requests.RequestException as e:
        return {"success": False, "data": None, "error": f"request failed for {comment_id}: {e}"}
    except ValueError as e:
        return {"success": False, "data": None, "error": f"response was not valid JSON for {comment_id}: {e}"}
    if "error" in payload:
        return {"success": False, "data": None, "error": f"API error for {comment_id}: {payload['error']}"}
    reply_id = payload.get("id")
    if not reply_id:
        return {"success": False, "data": None, "error": f"unexpected response shape for {comment_id}: {payload}"}
    return {"success": True, "data": {"id": reply_id}, "error": None}

def send_message(recipient_id, message, access_token):
    if not access_token:
        return {"success": False, "data": None, "error": f"missing access_token for {recipient_id}"}
    if not message:
        return {"success": False, "data": None, "error": f"missing message for {recipient_id}"}
    url = f"{BASE_URL}/me/messages"
    params = {"access_token": access_token}
    body = {"recipient": {"id": recipient_id},"message": {"text": message}}
    try:
        response = requests.post(url, params=params, json=body, timeout=5)
        payload = response.json()
    except requests.RequestException as e:
        return {"success": False, "data": None, "error": f"request failed for {recipient_id}: {e}"}
    except ValueError as e:
        return {"success": False, "data": None, "error": f"response was not valid JSON for {recipient_id}: {e}"}
    if "error" in payload:
        return {"success": False, "data": None, "error": f"API error for {recipient_id}: {payload['error']}"}
    message_id = payload.get("message_id")
    recipient = payload.get("recipient_id")
    if not message_id:
        return {"success": False, "data": None, "error": f"unexpected response shape for {recipient_id}: {payload}"}
    return {"success": True, "data": {"message_id": message_id, "recipient_id": recipient}, "error": None}

def get_follower_count(account_id, access_token):
    if not access_token:
        return {"success": False, "data": None, "error": f"missing access_token for {account_id}"}
    url = f"{BASE_URL}/{account_id}"
    params = {"fields": "followers_count", "access_token": access_token}
    try:
        response = requests.get(url, params=params, timeout=10)
        payload = response.json()
    except requests.RequestException as e:
        return {"success": False, "data": None, "error": f"request failed for {account_id}: {e}"}
    except ValueError as e:
        return {"success": False, "data": None, "error": f"response was not valid JSON for {account_id}: {e}"}
    if "error" in payload:
        return {"success": False, "data": None, "error": f"API error for {account_id}: {payload['error']}"}
    followers_count = payload.get("followers_count")
    if followers_count is None:
        return {"success": False, "data": None, "error": f"unexpected response shape for {account_id}: {payload}"}
    return {"success": True, "data": {"followers_count": followers_count}, "error": None}

def story_schedule(token,hour,media_id,access_token):
    tokench = au.process(token=token)
    access_token = refresh_token(tokench["token"],tokench["user_id"],access_token)
    one_hour_before = (datetime.now(timezone.utc)).isoformat()
    meta_resp = requests.get(f"https://graph.instagram.com/{media_id}", params={ "fields": "id,media_type,media_product_type,thumbnail_url,timestamp,permalink", "access_token": access_token,},timeout=10,).json()
    insights_resp = requests.get(f"https://graph.instagram.com/{media_id}/insights",params={"metric": "views,reach,replies,shares,follows","access_token": access_token,},timeout=10,).json()
    nav_resp = requests.get(f"https://graph.instagram.com/{media_id}/insights",params={"metric": "navigation","breakdown": "story_navigation_action_type","access_token": access_token,},timeout=10,).json()
    profile_resp = requests.get(f"https://graph.instagram.com/{media_id}/insights",params={"metric": "profile_activity","breakdown": "action_type","access_token": access_token,},timeout=10,).json()
    flat_metrics = {item["name"]: item["values"][0]["value"] for item in insights_resp.get("data", [])}
    return f"{media_id},{flat_metrics.get("views")},,{flat_metrics.get("reach")},{flat_metrics.get("replies")},{flat_metrics.get("shares")},{nav_resp.get("data", [{}])[0].get("total_value", {}).get("breakdowns", [])},{flat_metrics.get("follows")},{profile_resp.get("data", [{}])[0].get("total_value", {}).get("breakdowns", [])},{hour},{meta_resp.get("thumbnail_url")},{one_hour_before}"

def publish_container(token,access_token: str, ig_user_id: str, creation_id: str) -> str:
    tokench = au.process(token=token)
    access_token = refresh_token(tokench["token"],tokench["user_id"],access_token)
    published = _post(f"{ig_user_id}/media_publish", {"creation_id": creation_id, "access_token": access_token})
    return published["id"]

def get_media_analytics(token,media_id,access_token):
    tokench = au.process(token=token)
    access_token = refresh_token(tokench["token"],tokench["user_id"],access_token)
    one_hour_before = (datetime.now(timezone.utc)).isoformat()
    meta_resp = requests.get(f"https://graph.instagram.com/{media_id}",params={"fields": "id,media_type,media_product_type,thumbnail_url,timestamp,permalink", "access_token": access_token,},timeout=10,).json()
    media_type = meta_resp.get("media_type")
    flat_metrics_list = ["views", "saved", "shares", "total_interactions", "follows"]
    if media_type == "VIDEO" or meta_resp.get("media_product_type") == "REELS":
        flat_metrics_list += ["likes", "comments"]
    insights_resp = requests.get(f"https://graph.instagram.com/{media_id}/insights",params={"metric": ",".join(flat_metrics_list),"access_token": access_token,},timeout=10,).json()
    flat_metrics = {item["name"]: item["values"][0]["value"] for item in insights_resp.get("data", [])}
    profile_resp = requests.get(f"https://graph.instagram.com/{media_id}/insights", params={"metric": "profile_activity", "breakdown": "action_type", "access_token": access_token,},timeout=10,).json()
    return f"{media_id},{flat_metrics.get("views")},{flat_metrics.get("likes")},{flat_metrics.get("comments")},{flat_metrics.get("saved")},{flat_metrics.get("shares")},{flat_metrics.get("total_interactions")},{profile_resp.get("data", [{}])[0].get("total_value", {}).get("breakdowns", [])},{one_hour_before},{flat_metrics.get("follows")},{meta_resp.get("thumbnail_url")}"

def scccc(user_id,access_token,media_id,token,typee):
    for i in range(22):
        timesss = (datetime.now(timezone.utc) + timedelta(hours=i+1)).isoformat()
        sccc.insert__story(user_id, timesss, access_token,media_id,i,token,typee)

def xcccc(user_id,access_token,media_id,token,typee):
    for i in range(7): 
        timesss = (datetime.now(timezone.utc) + timedelta(days=(i))).isoformat()
        sccc.insert__story1(user_id, timesss, access_token,media_id,token,typee)
