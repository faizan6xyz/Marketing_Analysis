import os
import re
import time
import json
import uuid
import base64
import zlib
import logging
import threading
import authnew as au
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography import x509
from urllib.parse import urlparse
from datetime import datetime, timezone
from supabase import create_client, Client
load_dotenv()
import database.UserDB as dbimp

def _now():
    return datetime.now(timezone.utc).isoformat()

def _require(key):
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

CLIENT_ID = _require("PAYPAL_CLIENT_ID")
SECRET = _require("PAYPAL_SECRET")
WEBHOOK_ID = _require("PAYPAL_WEBHOOK_ID")
FLASK_SECRET_KEY = _require("FLASK_SECRET_KEY")
RETURN_URL = os.environ.get("RETURN_URL", "https://example.com/success")
CANCEL_URL = os.environ.get("CANCEL_URL", "https://example.com/cancel")
RATE_LIMIT_STORAGE_URI = _require("RATE_LIMIT_STORAGE_URI")
PAYPAL_ENV = os.environ.get("PAYPAL_ENV", "sandbox")
BASE_URL = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"
SUPABASE_URL = _require("SUPABASE_URL")
SUPABASE_KEY = _require("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
PAYPAL_TABLE = "Paypal"
VERIFY_TABLE = "Paypal_verify"
PLAN_CATALOG = json.loads(_require("PAYPAL_PLANS"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("paypal_app")
TRUSTED_CERT_HOSTS = ("api.paypal.com", "api.sandbox.paypal.com")
ACTIVE_ORDER_STATUSES = ("CREATED", "COMPLETED")

class PayPalError(Exception):
    pass

def get_all_users(limit: int = 100) -> list[dict]:
    response = supabase.table("users").select("*").limit(limit).execute()
    return response.data

def _update_succeeded(resp) -> bool:
    if resp is None:
        return False
    data = getattr(resp, "data", None)
    if data is not None:
        return len(data) > 0
    rowcount = getattr(resp, "rowcount", None)
    if rowcount is not None:
        return rowcount > 0
    return bool(resp)

def seed_demo_cart(token,user_id, cart_id, plan_key):
    rows = dbimp.select_rows(token,PAYPAL_TABLE, select="id", filters={"id": user_id})
    existing = rows[0] if rows else None

    data = {"Cart_id": cart_id, "Plan": plan_key, "Status": "CREATED", "Paid": 0}
    if existing:
        dbimp.update_rows(token,PAYPAL_TABLE, data, {"id": user_id})
    else:
        data["id"] = user_id
        dbimp.insert_rows(token,PAYPAL_TABLE, data)

def get_cart_for_user(token,user_id):
    rows = dbimp.select_rows(token, PAYPAL_TABLE, select="id,Cart_id,Plan,Status,Paid,Paypal_order_id", filters={"id": user_id}, )
    row = rows[0] if rows else None

    return dict(row) if row else None

def get_active_order_for_user(token,user_id):
    row = get_cart_for_user(token,user_id)
    if row and row.get("Paypal_order_id") and row.get("Status") in ACTIVE_ORDER_STATUSES:
        return row
    return None

def get_order_for_user(token,user_id, paypal_order_id):
    row = get_cart_for_user(token,user_id)
    if not row or row.get("Paypal_order_id") != paypal_order_id:
        return None
    return row

def get_user_id_for_order(paypal_order_id):
    rows = dbimp.select_rows_web(PAYPAL_TABLE, select="id", filters={"Paypal_order_id": paypal_order_id})
    row = rows[0] if rows else None

    return row["id"] if row else None

def save_order(token,user_id, paypal_order_id, cart_id, plan_key) -> bool:
    resp = dbimp.update_rows(token, PAYPAL_TABLE, {"Paypal_order_id": paypal_order_id,"Cart_id": cart_id,"Plan": plan_key,"Status": "CREATED","Updated_at": _now(),},{"id": user_id},)
    return _update_succeeded(resp)

def mark_order_paid(user_id, paypal_order_id) -> bool:
    resp = dbimp.update_rows_web(PAYPAL_TABLE, {"Paid": 1, "Status": "COMPLETED", "Updated_at": _now()}, {"id": user_id, "Paypal_order_id": paypal_order_id, "Paid": 0},)
    return _update_succeeded(resp)

def _hash_request(payload):
    import hashlib
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def get_idempotent_response(token,user_id, key, request_hash):
    rows = dbimp.select_rows( token,VERIFY_TABLE, select="Idempotency_key,Request_hash,Response", filters={"id": user_id})
    row = rows[0] if rows else None

    if not row or row["Idempotency_key"] != key:
        return None, False
    if row["Request_hash"] != request_hash:
        return None, True
    resp = row.get("Response")
    return (json.loads(resp) if isinstance(resp, str) else resp), False

def save_idempotent_response(token,user_id, key, request_hash, response):
    rows = dbimp.select_rows(token,VERIFY_TABLE, select="id", filters={"id": user_id})
    existing = rows[0] if rows else None
    data = {"Idempotency_key": key, "Request_hash": request_hash, "Response": response}
    if existing:
        dbimp.update_rows(token,VERIFY_TABLE, data, {"id": user_id})
    else:
        data["id"] = user_id
        dbimp.insert_rows(token,VERIFY_TABLE, data)

def is_duplicate_webhook_event(event_id) -> bool:
    rows = dbimp.select_rows_web(VERIFY_TABLE, select="Event_id", filters={"Event_id": event_id})
    row = rows[0] if rows else None

    return row is not None

def record_webhook_event(event_id, user_id) -> bool:
    if not user_id:
        return False
    rows = dbimp.select_rows_web(VERIFY_TABLE, select="id", filters={"id": user_id})
    existing = rows[0] if rows else None
    data = {"Event_id": event_id, "Created_at": _now()}
    if existing:
        dbimp.update_rows_web(VERIFY_TABLE, data, {"id": user_id})
    else:
        data["id"] = user_id
        dbimp.insert_rows_web(VERIFY_TABLE, data)
    return True

_token_lock = threading.Lock()
_token_cache = {"access_token": None, "expires_at": 0}
_cert_cache = {}
_cert_lock = threading.Lock()
_CERT_TTL = 60 * 60

def get_access_token():
    with _token_lock:
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["access_token"]
        resp = requests.post( f"{BASE_URL}/v1/oauth2/token", headers={"Accept": "application/json"}, data={"grant_type": "client_credentials"},auth=(CLIENT_ID, SECRET), timeout=10, )
        if resp.status_code != 200:
            raise PayPalError(f"Auth failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"] - 60
        return _token_cache["access_token"]

def paypal_request(method, path, retry_on_auth_fail=True, **kwargs):
    url = f"{BASE_URL}{path}"
    last_exc = None
    for attempt in range(3):
        try:
            resp = requests.request(method, url, timeout=10, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            if attempt == 2:
                raise PayPalError(f"Network error after retries: {e}") from e
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 401 and retry_on_auth_fail:
            with _token_lock:
                _token_cache["access_token"] = None
                _token_cache["expires_at"] = 0
            if "headers" in kwargs and "Authorization" in kwargs["headers"]:
                kwargs["headers"]["Authorization"] = f"Bearer {get_access_token()}"
            return paypal_request(method, path, retry_on_auth_fail=False, **kwargs)
        if resp.status_code >= 500 and attempt < 2:
            time.sleep(1.5 * (attempt + 1))
            continue
        return resp
    raise PayPalError(f"Request failed after retries: {last_exc}")

def _get_cert(cert_url):
    with _cert_lock:
        cached = _cert_cache.get(cert_url)
        if cached and time.time() < cached["expires_at"]:
            return cached["cert"]
    parsed = urlparse(cert_url)
    if parsed.scheme != "https" or parsed.hostname not in TRUSTED_CERT_HOSTS:
        raise PayPalError(f"Untrusted cert_url host: {parsed.hostname}")
    resp = requests.get(cert_url, timeout=10)
    if resp.status_code != 200:
        raise PayPalError(f"Could not fetch webhook cert: {resp.status_code}")
    cert = x509.load_pem_x509_certificate(resp.content)
    with _cert_lock:
        _cert_cache[cert_url] = {"cert": cert, "expires_at": time.time() + _CERT_TTL}
    return cert

def verify_webhook_signature_local(headers, raw_body: bytes) -> bool:
    transmission_id = headers.get("Paypal-Transmission-Id")
    transmission_time = headers.get("Paypal-Transmission-Time")
    cert_url = headers.get("Paypal-Cert-Url")
    signature_b64 = headers.get("Paypal-Transmission-Sig")
    if not all([transmission_id, transmission_time, cert_url, signature_b64]):
        return False
    crc = zlib.crc32(raw_body) & 0xFFFFFFFF
    message = f"{transmission_id}|{transmission_time}|{WEBHOOK_ID}|{crc}".encode()
    cert = _get_cert(cert_url)
    signature = base64.b64decode(signature_b64)
    try:
        cert.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False

def verify_webhook_signature_remote(headers, event: dict) -> bool: 
    payload = { "auth_algo": headers.get("Paypal-Auth-Algo"), "cert_url": headers.get("Paypal-Cert-Url"), "transmission_id": headers.get("Paypal-Transmission-Id"),"transmission_sig": headers.get("Paypal-Transmission-Sig"),"transmission_time": headers.get("Paypal-Transmission-Time"),"webhook_id": WEBHOOK_ID,"webhook_event": event,}
    resp = paypal_request("POST", "/v1/notifications/verify-webhook-signature", headers={"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"}, json=payload, )
    if resp.status_code != 200:
        raise PayPalError(f"Verification call failed: {resp.status_code}")
    return resp.json().get("verification_status") == "SUCCESS"

def verify_webhook_signature(headers, raw_body: bytes, event: dict) -> bool:
    try:
        return verify_webhook_signature_local(headers, raw_body)
    except Exception as e:
        logger.warning("Local webhook verification failed, falling back to API: %s", e)
        return verify_webhook_signature_remote(headers, event)

def logincheck(token ,user_id):
    if not user_id:
        return {"logged_in": False}
    try:
        rows = dbimp.select_rows(token,PAYPAL_TABLE, select="id", filters={"id": user_id})
        found = rows[0] if rows else None
    except Exception:
        return {"logged_in": False}
    return {"logged_in": bool(found)}

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
limiter = Limiter(get_remote_address, app=app, storage_uri=RATE_LIMIT_STORAGE_URI, default_limits=[])

@app.route("/api/demo/seed-cart", methods=["POST"])
@limiter.limit("10 per minute")
def seed_cart():
    body = request.get_json(silent=True) or {}
    cart_id = body.get("cart_id")
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    plan_key = body.get("plan_id", "basic_monthly") # default is basic monthly
    if not cart_id or not user_id:
        return jsonify({"error": "cart_id and user_id required"}), 400
    if plan_key not in PLAN_CATALOG:
        return jsonify({"error": "invalid_plan"}), 400
    seed_demo_cart(tokench["token"],user_id, cart_id, plan_key)
    return jsonify({"seeded": True, "cart_id": cart_id})

@app.route("/api/payment/create", methods=["POST"])
@limiter.limit("10 per minute")
def create_payment():
    body = request.get_json(silent=True) or {}
    cart_id = body.get("cart_id")
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    check = logincheck(tokench['token'],user_id)
    if not check["logged_in"] :
        return jsonify({"status": False}), 401
    if not cart_id:
        return jsonify({"error": "cart_id required"}), 400
    cart = get_cart_for_user(token['token'],user_id)
    if not cart or cart.get("Cart_id") != cart_id:
        return jsonify({"error": "cart_not_found"}), 404
    plan_key = cart.get("Plan")
    plan = PLAN_CATALOG.get(plan_key)
    if not plan:
        return jsonify({"error": "invalid_plan"}), 400
    amount = plan["amount"]
    currency = plan["currency"]
    idempotency_key = request.headers.get("Idempotency-Key")
    request_hash = _hash_request({"cart_id": cart_id, "user_id": user_id})
    if idempotency_key:
        cached, mismatch = get_idempotent_response(tokench["token"],user_id, idempotency_key, request_hash)
        if mismatch:
            return jsonify({"error": "idempotency_key_reused_with_different_request"}), 409
        if cached:
            return jsonify(cached)
    existing_order = get_active_order_for_user(tokench["token"],user_id)
    if existing_order:
        return jsonify({"error": "order_already_exists", "order_id": existing_order["Paypal_order_id"]}), 409
    payload = {"intent": "CAPTURE",
                    "purchase_units": [{"amount": {"currency_code": currency, "value": amount},"custom_id": cart_id, }],
                    "application_context": { "return_url": RETURN_URL, "cancel_url": CANCEL_URL,"user_action": "PAY_NOW",},}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"}
    headers["PayPal-Request-Id"] = idempotency_key or str(uuid.uuid4())
    try:
        resp = paypal_request("POST", "/v2/checkout/orders", headers=headers, json=payload)
    except PayPalError as e:
        logger.error("Order creation failed: %s", e)
        return jsonify({"error": "order_creation_failed"}), 502
    if resp.status_code not in (200, 201):
        logger.error("PayPal order creation error: %s", resp.text)
        return jsonify({"error": "order_creation_failed"}), 502
    order = resp.json()
    approval_link = next((l["href"] for l in order["links"] if l["rel"] == "approve"), None)
    if not save_order(tokench["token"],user_id, order["id"], cart_id, plan_key):
        logger.warning("Could not persist order %s for user %s, checking for a race", order["id"], user_id)
        existing_order = get_active_order_for_user(tokench['token'],user_id)
        if existing_order:
            return jsonify({"error": "order_already_exists", "order_id": existing_order["Paypal_order_id"]}), 409
        return jsonify({"error": "order_persist_failed"}), 500
    result = {"order_id": order["id"], "approval_link": approval_link}
    if idempotency_key:
        save_idempotent_response(tokench['token'],user_id, idempotency_key, request_hash, result)
    logger.info("Order created: %s for user %s", order["id"], user_id)
    return jsonify(result)

@app.route("/api/payment/status/<order_id>", methods=["GET"])
@limiter.limit("15 per minute")
def payment_status(order_id):
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    check = logincheck(tokench['token'],user_id)
    if not check["logged_in"] :
        return jsonify({"status": False}), 401
    order = get_order_for_user(user_id , order_id)
    if not order:
        return jsonify({"error": "order_not_found"}), 404
    try:
        resp = paypal_request( "GET",f"/v2/checkout/orders/{order_id}",headers={"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"},)
    except PayPalError as e:
        logger.error("Status check failed: %s", e)
        return jsonify({"error": "paypal_unavailable"}), 503
    if resp.status_code == 404:
        return jsonify({"error": "order_not_found"}), 404
    if resp.status_code != 200:
        logger.error("Status check error: %s", resp.text)
        return jsonify({"error": "status_check_failed"}), 502
    status = resp.json().get("status")
    return jsonify({"status": "Payment Done" if status == "COMPLETED" else status})

@app.route("/api/payment/capture/<order_id>", methods=["POST"])
@limiter.limit("10 per minute")
def capture_payment(order_id):
    body = request.get_json(silent=True) or {}
    token = body.get("token")
    tokench = au.process(token=token)
    if not tokench["status"] :
        return jsonify({"status": "failed" , "reason": tokench["reason"]})
    user_id = tokench['user_id']
    check = logincheck(tokench['token'],user_id)
    if not check["logged_in"] :
        return jsonify({"status": False}), 401
    order = get_order_for_user(user_id, order_id)
    if not order:
        return jsonify({"error": "order_not_found"}), 404
    if order.get("Paid"):
        return jsonify({"status": "Payment Done"})
    try:
        status_resp = paypal_request( "GET", f"/v2/checkout/orders/{order_id}", headers={"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"}, )
        if status_resp.status_code == 200 and status_resp.json().get("status") == "COMPLETED":
            mark_order_paid(user_id, order_id)
            return jsonify({"status": "Payment Done"})
        resp = paypal_request("POST",f"/v2/checkout/orders/{order_id}/capture",headers={"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"},)
    except PayPalError as e:
        logger.error("Capture failed: %s", e)
        return jsonify({"error": "paypal_unavailable"}), 503
    if resp.status_code == 422 and "ALREADY_CAPTURED" in resp.text.upper():
        mark_order_paid(user_id, order_id)
        return jsonify({"status": "Payment Done"})
    if resp.status_code not in (200, 201):
        logger.error("Capture error: %s", resp.text)
        return jsonify({"error": "capture_failed"}), 502
    data = resp.json()
    if data.get("status") == "COMPLETED":
        mark_order_paid(user_id, order_id)
        logger.info("Order captured: %s", order_id)
        return jsonify({"status": "Payment Done"})
    return jsonify({"status": data.get("status")})

@app.route("/api/paypal/webhook", methods=["POST"])
def paypal_webhook():
    raw_body = request.get_data()
    event = request.get_json(silent=True)
    if event is None:
        return jsonify({"error": "invalid_json"}), 400
    event_id = event.get("id")
    if not event_id:
        return jsonify({"error": "missing_event_id"}), 400
    if is_duplicate_webhook_event(event_id):
        return jsonify({"received": True, "duplicate": True}), 200
    try:
        verified = verify_webhook_signature(request.headers, raw_body, event)
    except PayPalError as e:
        logger.error("Webhook verification unavailable: %s", e)
        return jsonify({"error": "verification_unavailable"}), 503
    if not verified:
        logger.warning("Webhook signature verification failed for event %s", event_id)
        return jsonify({"error": "invalid_signature"}), 400
    event_type = event.get("event_type")
    order_id = None
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        resource = event.get("resource", {})
        order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
    user_id = get_user_id_for_order(order_id) if order_id else None
    if not record_webhook_event(event_id, user_id):
        logger.warning("Could not record webhook event %s (no matching user for order %s)", event_id, order_id)
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        if order_id and user_id:
            mark_order_paid(user_id, order_id)
            logger.info("Order marked paid via webhook: %s", order_id)
        else:
            logger.warning("Webhook %s missing order_id or matching user, could not mark paid", event_id)
    logger.info("Webhook processed: %s (%s)", event_id, event_type)
    return jsonify({"received": True}), 200

@app.errorhandler(Exception)
def handle_unexpected_error(e):
    logger.exception("Unhandled error")
    return jsonify({"error": "internal_error"}), 500

if __name__ == "__main__":
    # Dev server only. In production run: gunicorn -w 4 -b 0.0.0.0:5000 paypal_payments:app
    app.run(port=5000, debug=False)