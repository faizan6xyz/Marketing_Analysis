import os
import time
import json
import uuid
import base64
import zlib
import hashlib
import logging
import threading
from decimal import Decimal
from urllib.parse import urlparse
from datetime import datetime, timezone
import authnew as au
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
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
PAYPAL_ENV = os.environ.get("PAYPAL_ENV", "sandbox")
PLAN_CATALOG = json.loads(_require("PAYPAL_PLANS"))
BASE_URL = "https://api-m.paypal.com" if PAYPAL_ENV == "live" else "https://api-m.sandbox.paypal.com"
PAYPAL_TABLE = "paypal"
VERIFY_TABLE = "paypal_verify"
CART_COLUMNS = ("id", "cart_id", "plan", "status", "paid", "paypal_order_id")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("paypal_app")
TRUSTED_CERT_HOSTS = ("api.paypal.com", "api.sandbox.paypal.com")
ACTIVE_ORDER_STATUSES = ("CREATED", "COMPLETED")
REUSABLE_ORDER_STATUSES = ("CREATED", "PAYER_ACTION_REQUIRED")
app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
frontend = os.environ.get("front_end")
if not frontend:
    raise RuntimeError("front_end env var is not set")
CORS(app, origins=[frontend] if frontend else [], supports_credentials=True, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "Authorization", "Request-ID"])
_session = requests.Session()

class PayPalError(Exception):
    pass

def _col(row, key):
    if row is None:
        return None
    if key in row:
        return row[key]
    return row.get(key.capitalize())

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

def _format_amount(amount):
    return f"{Decimal(str(amount)):.2f}"

def authenticate():
    body = request.get_json(silent=True) or {}
    token = body.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or None
    tokench = au.process(token=token)
    if not tokench["status"]:
        return body, None, (jsonify({"status": "failed", "reason": tokench["reason"]}), 401)
    return body, tokench, None

def user_is_paid(user_id):
    rows = dbimp.select_rows("users", select="paid", filters={"user_id": user_id})
    if not rows:
        return None
    return bool(_col(rows[0], "paid"))

def seed_demo_cart(token, user_id, cart_id, plan_key):
    rows = dbimp.select_rows(PAYPAL_TABLE, select="id", filters={"id": user_id})
    data = {"cart_id": cart_id, "plan": plan_key, "status": "CREATED", "paid": 0}
    if rows:
        dbimp.update_rows(PAYPAL_TABLE, data, {"id": user_id})
    else:
        data["id"] = user_id
        dbimp.insert_rows(PAYPAL_TABLE, data)

def get_cart_for_user(token, user_id):
    rows = dbimp.select_rows(PAYPAL_TABLE, select=",".join(CART_COLUMNS), filters={"id": user_id})
    if not rows:
        return None
    return {c: _col(rows[0], c) for c in CART_COLUMNS}

def get_order_for_user(token, user_id, paypal_order_id):
    row = get_cart_for_user(token, user_id)
    if not row or row["paypal_order_id"] != paypal_order_id:
        return None
    return row

def get_user_id_for_order(paypal_order_id):
    rows = dbimp.select_rows(PAYPAL_TABLE, select="id", filters={"paypal_order_id": paypal_order_id})
    return _col(rows[0], "id") if rows else None

def save_order(token, user_id, paypal_order_id, cart_id, plan_key) -> bool:
    resp = dbimp.update_rows(PAYPAL_TABLE, {"paypal_order_id": paypal_order_id, "cart_id": cart_id, "plan": plan_key, "status": "CREATED", "updated_at": _now()}, {"id": user_id})
    return _update_succeeded(resp)

def mark_order_paid(user_id, paypal_order_id) -> bool:
    rows = dbimp.select_rows(PAYPAL_TABLE, select="plan", filters={"id": user_id, "paypal_order_id": paypal_order_id})
    if not rows:
        return False
    plan_id = _col(rows[0], "plan")
    dbimp.update_rows("users", {"paid": True, "plan": plan_id}, {"user_id": user_id})
    resp = dbimp.update_rows(PAYPAL_TABLE, {"paid": 1, "status": "COMPLETED", "updated_at": _now()}, {"id": user_id, "paypal_order_id": paypal_order_id, "paid": 0})
    return _update_succeeded(resp)

def _hash_request(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def get_idempotent_response(token, user_id, key, request_hash):
    rows = dbimp.select_rows(VERIFY_TABLE, select="idempotency_key,request_hash,response", filters={"id": user_id})
    row = rows[0] if rows else None
    if not row or _col(row, "idempotency_key") != key:
        return None, False
    if _col(row, "request_hash") != request_hash:
        return None, True
    resp = _col(row, "response")
    return (json.loads(resp) if isinstance(resp, str) else resp), False

def _upsert_verify_row(user_id, data):
    rows = dbimp.select_rows(VERIFY_TABLE, select="id", filters={"id": user_id})
    if rows:
        dbimp.update_rows(VERIFY_TABLE, data, {"id": user_id})
    else:
        dbimp.insert_rows(VERIFY_TABLE, {**data, "id": user_id})

def save_idempotent_response(token, user_id, key, request_hash, response):
    _upsert_verify_row(user_id, {"idempotency_key": key, "request_hash": request_hash, "response": response})

def is_duplicate_webhook_event(event_id) -> bool:
    rows = dbimp.select_rows(VERIFY_TABLE, select="event_id", filters={"event_id": event_id})
    return bool(rows)

def record_webhook_event(event_id, user_id) -> bool:
    if not user_id:
        return False
    _upsert_verify_row(user_id, {"event_id": event_id, "created_at": _now()})
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
        resp = _session.post(f"{BASE_URL}/v1/oauth2/token", headers={"Accept": "application/json"}, data={"grant_type": "client_credentials"}, auth=(CLIENT_ID, SECRET), timeout=10)
        if resp.status_code != 200:
            raise PayPalError(f"Auth failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        _token_cache["access_token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"] - 60
        return _token_cache["access_token"]

def _headers(extra=None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {get_access_token()}"}
    if extra:
        headers.update(extra)
    return headers

def paypal_request(method, path, **kwargs):
    url = f"{BASE_URL}{path}"
    refreshed = False
    last_exc = None
    attempt = 0
    while attempt < 3:
        try:
            resp = _session.request(method, url, timeout=10, **kwargs)
        except requests.RequestException as e:
            last_exc = e
            attempt += 1
            if attempt == 3:
                raise PayPalError(f"Network error after retries: {e}") from e
            time.sleep(1.5 * attempt)
            continue
        if resp.status_code == 401 and not refreshed:
            refreshed = True
            with _token_lock:
                _token_cache["access_token"] = None
                _token_cache["expires_at"] = 0
            if kwargs.get("headers"):
                kwargs["headers"] = {**kwargs["headers"], "Authorization": f"Bearer {get_access_token()}"}
            continue
        attempt += 1
        if resp.status_code >= 500 and attempt < 3:
            time.sleep(1.5 * attempt)
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
    resp = _session.get(cert_url, timeout=10)
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
    payload = {"auth_algo": headers.get("Paypal-Auth-Algo"), "cert_url": headers.get("Paypal-Cert-Url"), "transmission_id": headers.get("Paypal-Transmission-Id"), "transmission_sig": headers.get("Paypal-Transmission-Sig"), "transmission_time": headers.get("Paypal-Transmission-Time"), "webhook_id": WEBHOOK_ID, "webhook_event": event}
    resp = paypal_request("POST", "/v1/notifications/verify-webhook-signature", headers=_headers(), json=payload)
    if resp.status_code != 200:
        raise PayPalError(f"Verification call failed: {resp.status_code}")
    return resp.json().get("verification_status") == "SUCCESS"

def verify_webhook_signature(headers, raw_body: bytes, event: dict) -> bool:
    try:
        return verify_webhook_signature_local(headers, raw_body)
    except Exception as e:
        logger.warning("Local webhook verification failed, falling back to API: %s", e)
        return verify_webhook_signature_remote(headers, event)

def get_approval_link(order):
    return next((l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")), None)

def find_reusable_order(cart, amount, currency):
    try:
        resp = paypal_request("GET", f"/v2/checkout/orders/{cart['paypal_order_id']}", headers=_headers())
    except PayPalError:
        return None
    if resp.status_code != 200:
        return None
    order = resp.json()
    if order.get("status") not in REUSABLE_ORDER_STATUSES:
        return None
    unit = (order.get("purchase_units") or [{}])[0]
    unit_amount = unit.get("amount", {})
    if unit.get("custom_id") != cart["cart_id"] or unit_amount.get("value") != amount or unit_amount.get("currency_code") != currency:
        return None
    link = get_approval_link(order)
    if not link:
        return None
    return {"order_id": order["id"], "approval_link": link}

@app.route("/api/demo/seed-cart", methods=["POST"])
def seed_cart():
    body, tokench, err = authenticate()
    if err:
        return err
    user_id = tokench["user_id"]
    cart_id = body.get("cart_id")
    plan_key = body.get("plan_id", "basic_monthly")
    if not cart_id or not user_id:
        return jsonify({"error": "cart_id and user_id required"}), 400
    if plan_key not in PLAN_CATALOG:
        return jsonify({"error": "invalid_plan"}), 400
    if user_is_paid(user_id):
        return jsonify({"error": "user paid Already"}), 400
    seed_demo_cart(tokench["token"], user_id, cart_id, plan_key)
    return jsonify({"seeded": True, "cart_id": cart_id})

@app.route("/api/payment/create", methods=["POST"])
def create_payment():
    body, tokench, err = authenticate()
    if err:
        return err
    token = tokench["token"]
    user_id = tokench["user_id"]
    cart_id = body.get("cart_id")
    paid = user_is_paid(user_id)
    if paid is None:
        return jsonify({"error": "user_not_exist"}), 400
    if paid:
        return jsonify({"error": "user paid Already"}), 400
    cart = get_cart_for_user(token, user_id)
    if not cart:
        return jsonify({"status": False}), 401
    if not cart_id:
        return jsonify({"error": "cart_id required"}), 400
    if cart["cart_id"] != cart_id:
        return jsonify({"error": "cart_not_found"}), 404
    plan_key = cart["plan"]
    plan = PLAN_CATALOG.get(plan_key)
    if not plan:
        return jsonify({"error": "invalid_plan"}), 400
    amount = _format_amount(plan["amount"])
    currency = plan["currency"]
    idempotency_key = request.headers.get("Idempotency-Key")
    request_hash = _hash_request({"cart_id": cart_id, "user_id": user_id})
    if idempotency_key:
        cached, mismatch = get_idempotent_response(token, user_id, idempotency_key, request_hash)
        if mismatch:
            return jsonify({"error": "idempotency_key_reused_with_different_request"}), 409
        if cached:
            return jsonify(cached)
    if cart["paypal_order_id"] and cart["status"] in ACTIVE_ORDER_STATUSES:
        reusable = find_reusable_order(cart, amount, currency)
        if reusable:
            return jsonify(reusable)
        return jsonify({"error": "order_already_exists", "order_id": cart["paypal_order_id"]}), 409
    payload = {"intent": "CAPTURE", "purchase_units": [{"amount": {"currency_code": currency, "value": amount}, "custom_id": cart_id}], "application_context": {"return_url": RETURN_URL, "cancel_url": CANCEL_URL, "user_action": "PAY_NOW"}}
    try:
        resp = paypal_request("POST", "/v2/checkout/orders", headers=_headers({"PayPal-Request-Id": idempotency_key or str(uuid.uuid4())}), json=payload)
    except PayPalError as e:
        logger.error("Order creation failed: %s", e)
        return jsonify({"error": "order_creation_failed"}), 502
    if resp.status_code not in (200, 201):
        logger.error("PayPal order creation error: %s", resp.text)
        return jsonify({"error": "order_creation_failed"}), 502
    order = resp.json()
    if not save_order(token, user_id, order["id"], cart_id, plan_key):
        logger.warning("Could not persist order %s for user %s, checking for a race", order["id"], user_id)
        latest = get_cart_for_user(token, user_id)
        if latest and latest["paypal_order_id"] and latest["status"] in ACTIVE_ORDER_STATUSES:
            return jsonify({"error": "order_already_exists", "order_id": latest["paypal_order_id"]}), 409
        return jsonify({"error": "order_persist_failed"}), 500
    result = {"order_id": order["id"], "approval_link": get_approval_link(order)}
    if idempotency_key:
        save_idempotent_response(token, user_id, idempotency_key, request_hash, result)
    logger.info("Order created: %s for user %s", order["id"], user_id)
    return jsonify(result)

@app.route("/api/payment/status/<order_id>", methods=["GET", "POST"])
def payment_status(order_id):
    body, tokench, err = authenticate()
    if err:
        return err
    order = get_order_for_user(tokench["token"], tokench["user_id"], order_id)
    if not order:
        return jsonify({"error": "order_not_found"}), 404
    try:
        resp = paypal_request("GET", f"/v2/checkout/orders/{order_id}", headers=_headers())
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
def capture_payment(order_id):
    body, tokench, err = authenticate()
    if err:
        return err
    user_id = tokench["user_id"]
    order = get_order_for_user(tokench["token"], user_id, order_id)
    if not order:
        return jsonify({"error": "order_not_found"}), 404
    if order["paid"]:
        return jsonify({"status": "Payment Done"})
    try:
        resp = paypal_request("POST", f"/v2/checkout/orders/{order_id}/capture", headers=_headers())
    except PayPalError as e:
        logger.error("Capture failed: %s", e)
        return jsonify({"error": "paypal_unavailable"}), 503
    if resp.status_code == 422 and "ALREADY_CAPTURED" in resp.text.upper():
        mark_order_paid(user_id, order_id)
        return jsonify({"status": "Payment Done"})
    if resp.status_code not in (200, 201):
        logger.error("Capture error: %s", resp.text)
        return jsonify({"error": "capture_failed"}), 502
    status = resp.json().get("status")
    if status == "COMPLETED":
        mark_order_paid(user_id, order_id)
        logger.info("Order captured: %s", order_id)
        return jsonify({"status": "Payment Done"})
    return jsonify({"status": status})

@app.route("/api/webhook", methods=["POST"])
def paypal_webhook():
    raw_body = request.get_data()
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
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
    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        resource = event.get("resource") or {}
        order_id = (resource.get("supplementary_data") or {}).get("related_ids", {}).get("order_id")
        user_id = get_user_id_for_order(order_id) if order_id else None
        if not (order_id and user_id):
            logger.warning("Webhook %s missing order_id or matching user, could not mark paid", event_id)
            return jsonify({"received": True}), 200
        mark_order_paid(user_id, order_id)
        record_webhook_event(event_id, user_id)
        logger.info("Order marked paid via webhook: %s", order_id)
    logger.info("Webhook processed: %s (%s)", event_id, event_type)
    return jsonify({"received": True}), 200

@app.errorhandler(Exception)
def handle_unexpected_error(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.name.lower().replace(" ", "_")}), e.code
    logger.exception("Unhandled error")
    return jsonify({"error": "internal_error"}), 500

if __name__ == "__main__":
    app.run(port=5000, debug=False)