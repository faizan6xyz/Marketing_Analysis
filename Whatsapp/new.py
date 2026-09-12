import os
import re
import csv
import time
import hmac
import hashlib
import logging
import threading
from datetime import datetime, timezone, timedelta
from collections import defaultdict, deque
import requests
from flask import request, jsonify
import Drive.dep as  dpp
import database.UserDB as dbimp
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
WA_APP_ID = os.getenv("WA_APP_ID")
WA_REDIRECT_URI = os.getenv("WA_REDIRECT_URI")
GRAPH_VERSION = "v20.0"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE_NAME = "WhatsApp"
SCOPE = "whatsapp_business_management,whatsapp_business_messaging,business_management"
SEND_API_KEY = os.environ.get("WHATSAPP_SEND_API_KEY")
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET")
GRAPH_URL = "https://graph.facebook.com/v20.0"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5
MAX_TEXT_LENGTH = 1800
MAX_FILE_SIZE_BYTES = {    "image": 5 * 1024 * 1024,    "audio": 5 * 1024 * 1024,    "video": 20 * 1024 * 1024,    "document": 30 * 1024 * 1024,}
VALID_MEDIA_TYPES = set(MAX_FILE_SIZE_BYTES.keys())
MAX_BUTTONS = 3
MAX_BUTTON_TITLE_LEN = 20
MAX_BUTTON_ID_LEN = 256
MAX_LIST_SECTIONS = 10
MAX_LIST_ROWS_TOTAL = 10
MAX_LIST_ROW_TITLE_LEN = 24
MAX_LIST_ROW_DESC_LEN = 72
MAX_LIST_BUTTON_TEXT_LEN = 20
CSV_FILE = r"Analytics/Report/whatsapp_messages.csv"
EXCEL_FILE = r"Analytics/Report/whatsapp_messages.xlsx"
COLUMNS_NAME = ["Timestamp", "Sender Number", "Message"]
MAX_LOG_MESSAGE_LENGTH = 4000  # guards against pathological/huge payloads bloating the log
_request_log = defaultdict(deque)
file_lock = threading.Lock()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("whatsapp_integration")
if not all([WA_APP_ID, VERIFY_TOKEN, APP_SECRET, WA_REDIRECT_URI, SEND_API_KEY]):
    log.warning("Missing one or more required WhatsApp environment variables.")

class InvalidPhoneNumberError(Exception):
    pass

class MessageTooLongError(Exception):
    pass

class FileTooLargeError(Exception):
    pass

def is_valid_signature(req) -> bool:
    if not APP_SECRET:
        log.error("APP_SECRET not configured — refusing to process webhook.")
        return False
    signature_header = req.headers.get("X-Hub-Signature-256", "")
    if not signature_header.startswith("sha256="):
        log.warning("Missing or malformed X-Hub-Signature-256 header.")
        return False
    received_sig = signature_header.split("sha256=", 1)[1]
    expected_sig = hmac.new(APP_SECRET.encode("utf-8"), req.get_data(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(received_sig, expected_sig)

def validate_phone_number(number: str) -> str:
    if not number or not re.fullmatch(r"\+?[1-9]\d{7,14}", str(number)):
        raise InvalidPhoneNumberError(f"'{number}' is not a valid phone number.")
    return re.sub(r"[^\d+]", "", str(number))

def sanitize_field(value: str) -> str:
    if value is None:
        return ""
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))
    return value.replace("\x00", "")

def sanitize_log_field(value: str) -> str:
    if value is None:
        return ""
    value = str(value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    value = value.replace("\x00", "")
    if value[:1] in ("=", "+", "-", "@"):
        value = "'" + value
    return value[:MAX_LOG_MESSAGE_LENGTH]

def sanitize_sender(value: str) -> str:
    if not value:
        return "unknown"
    value = re.sub(r"[^0-9+]", "", str(value))
    return value[:20] or "unknown"

def validate_text_body(body: str) -> str:
    if body is None:
        body = ""
    if len(body) > MAX_TEXT_LENGTH:
        raise MessageTooLongError(f"Message is {len(body)} chars, exceeds WhatsApp's {MAX_TEXT_LENGTH}-char limit.")
    return body

def check_remote_file_size(url: str, msg_type: str):
    max_bytes = MAX_FILE_SIZE_BYTES.get(msg_type)
    if not max_bytes:
        return
    try:
        resp = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        content_length = resp.headers.get("Content-Length")
        if content_length is not None and int(content_length) > max_bytes:
            raise FileTooLargeError(f"File at {url} is {content_length} bytes, exceeds {max_bytes} byte limit for '{msg_type}'.")
    except requests.RequestException as e:
        log.warning(f"Could not verify remote file size for {url}: {e}")

def request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            if resp.status_code >= 500:
                raise requests.HTTPError(f"Server error {resp.status_code}")
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_exc = e
            wait = RETRY_BACKOFF_BASE ** attempt
            log.warning(f"Send attempt {attempt}/{MAX_RETRIES} failed: {e}. Retrying in {wait:.1f}s.")
            time.sleep(wait)
    raise last_exc

def require_api_key():
    if not SEND_API_KEY:
        return jsonify({"error": "Server not configured (missing API key)"}), 500
    provided = request.headers.get("X-API-Key", "")
    if provided != SEND_API_KEY:
        log.warning("Rejected /send-test call: missing/invalid X-API-Key.")
        return jsonify({"error": "Unauthorized"}), 401
    return None

def send_whatsapp_message(PHONE_NUMBER_ID, ACCESS_TOKEN, recipient_number: str, message_body: str) -> dict:
    recipient_number = validate_phone_number(recipient_number)
    message_body = validate_text_body(message_body)
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient_number, "type": "text", "text": {"body": message_body},    }
    url = f"{GRAPH_URL}/{PHONE_NUMBER_ID}/messages"
    resp = request_with_retry("POST", url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},json=payload, )
    return resp.json()

def send_whatsapp_media(PHONE_NUMBER_ID, ACCESS_TOKEN, recipient_number: str, msg_type: str, link: str, caption: str = None, filename: str = None) -> dict:
    recipient_number = validate_phone_number(recipient_number)
    if msg_type not in VALID_MEDIA_TYPES:
        raise ValueError(f"msg_type must be one of {VALID_MEDIA_TYPES}, got '{msg_type}'")
    if not link:
        raise ValueError("A 'link' URL is required to send media.")
    check_remote_file_size(link, msg_type)
    media_obj = {"link": link}
    if caption and msg_type in ("image", "video", "document"):
        media_obj["caption"] = validate_text_body(caption)
    if filename and msg_type == "document":
        media_obj["filename"] = filename
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": recipient_number, "type": msg_type, msg_type: media_obj, }
    url = f"{GRAPH_URL}/{PHONE_NUMBER_ID}/messages"
    resp = request_with_retry( "POST", url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}, json=payload,)
    return resp.json()

def get_user_for_phone_number_id(incoming_id: str):
    if not incoming_id:
        return None
    rows = dbimp.select_rows_web(TABLE_NAME, select="id,Access_token", filters={"Account_id": incoming_id})
    return rows[0] if rows else None

def check_user_id(token,user_id):
    exist = dbimp.select_rows(token,TABLE_NAME, select="id", filters={"id": user_id})
    return bool(exist)

def refresh_token(token,Account_id, access_token):
    resp = requests.get( f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": WA_APP_ID,"client_secret": APP_SECRET,"fb_exchange_token": access_token,},).json()
    new_token = resp.get("access_token")
    seconds = resp.get("expires_in")
    if new_token and seconds:
        new_expire = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        dbimp.update_rows(token,TABLE_NAME,{"Access_token": new_token, "Token_expire": new_expire.isoformat()},filters={"Account_id": Account_id},)
        return new_token
    return access_token

def refresh_token_web(Account_id, access_token):
    resp = requests.get( f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": WA_APP_ID,"client_secret": APP_SECRET,"fb_exchange_token": access_token,},).json()
    new_token = resp.get("access_token")
    seconds = resp.get("expires_in")
    if new_token and seconds:
        new_expire = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        dbimp.update_rows_web(TABLE_NAME,{"Access_token": new_token, "Token_expire": new_expire.isoformat()},filters={"Account_id": Account_id},)
        return new_token
    return access_token


