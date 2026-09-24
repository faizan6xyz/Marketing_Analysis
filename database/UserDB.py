from __future__ import annotations
from datetime import datetime, timezone , timedelta
import os
import json
import time
import logging
import secrets
import threading
from typing import Any, Optional, Callable
import hmac
import hashlib
from dotenv import load_dotenv
from supabase import create_client, Client
import sqlite3
import authnew as au
from contextlib import closing, contextmanager
import valkey
from valkey.exceptions import ValkeyError
load_dotenv()
log = logging.getLogger(__name__)
SECRET_KEY = os.environ["SECRET_KEY"].encode("utf-8")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
email = os.environ.get("email")
passw = os.environ.get("pass")
DB = "users.db"
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in your environment or .env file")
VALKEY_HOST = os.environ.get("VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", 6379))
VALKEY_DB = int(os.environ.get("VALKEY_DB", 0))
CACHE_TTL = int(os.environ.get("CACHE_TTL", 60))
vk = valkey.Valkey(host=VALKEY_HOST, port=VALKEY_PORT, db=VALKEY_DB, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
PK_COLUMNS: dict[str, str] = {}
_web_lock = threading.Lock()
_web_client: Optional[Client] = None
_web_expires_at: float = 0.0

class AuthError(Exception):
    pass

def _pk_col(table_name: str) -> str:
    return PK_COLUMNS.get(table_name, "id")

def _table_version(table_name: str) -> str:
    try:
        return vk.get(f"ver:{table_name}") or "0"
    except ValkeyError:
        return "0"

def _bump_table_version(table_name: str) -> None:
    try:
        vk.incr(f"ver:{table_name}")
    except ValkeyError as e:
        log.warning("cache invalidation failed for %s: %s", table_name, e)

def _row_cache_key(scope: str, table_name: str, version: str, filters: Optional[dict[str, Any]], select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None) -> str:
    key_data = { "filters": filters or {}, "select": select, "order_by": order_by, "ascending": ascending, "limit": limit, }
    digest = hashlib.sha256(json.dumps(key_data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
    return f"rows:{scope}:{table_name}:v{version}:{digest}"

def _single_row_key(scope: str, table_name: str, pk_value: Any) -> str:
    return f"row:{scope}:{table_name}:{pk_value}"

def _sync_row_cache(scope: str, table_name: str, rows: list[dict], deleted: bool = False) -> None:
    if not rows:
        return
    pk = _pk_col(table_name)
    try:
        pipe = vk.pipeline()
        for row in rows:
            if pk not in row:
                continue
            key = _single_row_key(scope, table_name, row[pk])
            if deleted:
                pipe.delete(key)
            else:
                pipe.set(key, json.dumps(row, default=str), ex=CACHE_TTL)
        pipe.execute()
    except ValkeyError as e:
        log.warning("row cache sync failed for %s: %s", table_name, e)

def _after_write(scope: str, table_name: str, rows: list[dict], deleted: bool = False) -> None:
    _sync_row_cache(scope, table_name, rows, deleted)
    _bump_table_version(table_name)

def _prehash(password: str) -> bytes:
    return hmac.new(SECRET_KEY, password.encode("utf-8"), hashlib.sha256).digest()

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(_prehash(password), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"

def _verify_password(password: str, stored: str) -> bool:
    if stored.startswith("scrypt$"):
        try:
            _, salt_hex, dk_hex = stored.split("$")
            dk = hashlib.scrypt(_prehash(password), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        except ValueError:
            return False
        return hmac.compare_digest(dk.hex(), dk_hex)
    legacy = hmac.new(SECRET_KEY, password.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(legacy, stored)

def _load_tokens(user_id):
    with closing(get_conn()) as conn:
        row = conn.execute( "SELECT Access_token, Refresh_token, Expire FROM access_tokens WHERE user_id = ?",(user_id,),).fetchone()
    if not row or not all(row):
        return None
    return row

def _is_fresh(expire: str) -> bool:
    expire_dt = datetime.fromisoformat(expire)
    if expire_dt.tzinfo is None:
        expire_dt = expire_dt.replace(tzinfo=timezone.utc)
    return expire_dt > datetime.now(timezone.utc) + timedelta(minutes=1)

@contextmanager
def _refresh_lock(user_id):
    lock = vk.lock(f"lock:refresh:{user_id}", timeout=15, blocking_timeout=10)
    acquired = False
    try:
        try:
            acquired = lock.acquire()
        except ValkeyError:
            acquired = False
        yield
    finally:
        if acquired:
            try:
                lock.release()
            except ValkeyError:
                pass

def retrieve(user_id):
    row = _load_tokens(user_id)
    if not row:
        return False
    access, refresh, expire = row
    if _is_fresh(expire):
        return access
    with _refresh_lock(user_id):
        row = _load_tokens(user_id)
        if not row:
            return False
        access, refresh, expire = row
        if _is_fresh(expire):
            return access
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            session = supabase.auth.refresh_session(refresh).session
            if session is None:
                raise RuntimeError("no session returned")
        except Exception as e:
            log.warning("refresh failed for %s: %s", user_id, e)
            return False
        access = session.access_token
        refresh = session.refresh_token
        expires_at = session.expires_at or int(time.time()) + int(session.expires_in or 3600)
        new_expire = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
        with closing(get_conn()) as conn, conn:
            conn.execute( "UPDATE access_tokens SET Access_token = ?, Refresh_token = ?, Expire = ? WHERE user_id = ?", (access, refresh, new_expire, user_id), )
        return access

def get_authenticated_client(user_id: str) -> Client:
    access_token = retrieve(user_id)
    if not access_token:
        raise AuthError(f"no valid Supabase session for user {user_id}")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase.postgrest.auth(access_token)
    return supabase

def _user_id_from_token(token) -> str:
    claims = au.process(token)
    user_id = claims.get("user_id") if claims else None
    if not user_id:
        raise AuthError("invalid token")
    return user_id

def _get_web_client() -> Client:
    global _web_client, _web_expires_at
    if not email or not passw:
        raise RuntimeError("Set the `email` and `pass` environment variables for *_web helpers")
    with _web_lock:
        if _web_client is None or time.time() > _web_expires_at - 60:
            client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
            res = client.auth.sign_in_with_password({"email": email , "password":passw})
            _web_client = client
            _web_expires_at = float(res.session.expires_at or time.time() + 3000)
        return _web_client

def get_conn():
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(""" CREATE TABLE IF NOT EXISTS users ( id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,user_id TEXT UNIQUE) """)
            conn.execute(""" CREATE TABLE IF NOT EXISTS access_tokens ( user_id TEXT PRIMARY KEY ,Access_token TEXT UNIQUE NOT NULL,Refresh_token TEXT NOT NULL, Expire TEXT NOT NULL ) """)

def add_new_user(email:str,password:str):
    if not email or not password :
        return False
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        log.warning("supabase sign-in failed for %s: %s", email, e)
        return False
    if not res or not res.user :
        return False
    passworddd = _hash_password(password)
    user_id = res.user.id
    try:
        with closing(get_conn()) as conn, conn:
            conn.execute("INSERT INTO users (email, password, user_id) VALUES (?, ?, ?)",(email, passworddd, user_id),)
    except sqlite3.IntegrityError:
        return False
    return True

def User_exist_check(email :str ,password:str,user_id : str):
    if not email or not password or not user_id :
        return False
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT password , user_id FROM users WHERE email = ?", (email,))
        row = cur.fetchone()
    if not row :
        return False
    passwork , user_id1 = row
    if not passwork or not user_id1 :
        return False
    if not (_verify_password(password, passwork) and hmac.compare_digest(user_id, user_id1)) :
        return False
    if not passwork.startswith("scrypt$") :
        try:
            with closing(get_conn()) as conn, conn:
                conn.execute("UPDATE users SET password = ? WHERE email = ?", (_hash_password(password), email),)
        except sqlite3.Error as e:
            log.warning("password hash upgrade failed: %s", e)
    return True

def _apply_filters(query, filters: dict[str, Any]):
    for column, condition in filters.items():
        if isinstance(condition, tuple):
            op, value = condition
            op = op.lower()
            if op == "eq":
                query = query.eq(column, value)
            elif op == "neq":
                query = query.neq(column, value)
            elif op == "gt":
                query = query.gt(column, value)
            elif op == "gte":
                query = query.gte(column, value)
            elif op == "lt":
                query = query.lt(column, value)
            elif op == "lte":
                query = query.lte(column, value)
            elif op == "like":
                query = query.like(column, value)
            elif op == "ilike":
                query = query.ilike(column, value)
            elif op == "in":
                query = query.in_(column, value)
            elif op == "is":
                query = query.is_(column, "null" if value is None else value)
            elif op == "contains":
                query = query.contains(column, value)
            else:
                raise ValueError(f"Unsupported operator: {op}")
        else:
            query = query.eq(column, condition)
    return query

def _require_filters(filters: Optional[dict[str, Any]], fn_name: str) -> None:
    if not filters:
        raise ValueError(f"{fn_name} requires non-empty filters")

def _select_cached(scope: str, get_client: Callable[[], Client], table_name: str, filters: Optional[dict[str, Any]], select: str, order_by: Optional[str], ascending: bool, limit: Optional[int]) -> list[dict]:
    version = _table_version(table_name)
    cache_key = _row_cache_key(scope, table_name, version, filters, select, order_by, ascending, limit)
    try:
        cached = vk.get(cache_key)
        if cached is not None:
            return json.loads(cached)
    except ValkeyError as e:
        log.warning("cache read failed: %s", e)
    query = get_client().table(table_name).select(select)
    if filters:
        query = _apply_filters(query, filters)
    if order_by:
        query = query.order(order_by, desc=not ascending)
    if limit is not None:
        query = query.limit(limit)
    response = query.execute()
    try:
        vk.set(cache_key, json.dumps(response.data, default=str), ex=CACHE_TTL)
    except ValkeyError as e:
        log.warning("cache write failed: %s", e)
    return response.data

def insert_rows(token, table_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> list[dict]:
    user_id = _user_id_from_token(token)
    supabase = get_authenticated_client(user_id)
    response = supabase.table(table_name).insert(data).execute()
    _after_write(f"user:{user_id}", table_name, response.data)
    return response.data

def update_rows(token, table_name: str, updates: dict[str, Any], filters: dict[str, Any]) -> list[dict]:
    _require_filters(filters, "update_rows")
    user_id = _user_id_from_token(token)
    supabase = get_authenticated_client(user_id)
    query = supabase.table(table_name).update(updates)
    query = _apply_filters(query, filters)
    response = query.execute()
    _after_write(f"user:{user_id}", table_name, response.data)
    return response.data

def delete_rows(token, table_name: str, filters: dict[str, Any]) -> list[dict]:
    _require_filters(filters, "delete_rows")
    user_id = _user_id_from_token(token)
    supabase = get_authenticated_client(user_id)
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    _after_write(f"user:{user_id}", table_name, response.data, deleted=True)
    return response.data

def select_rows(token, table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None, ) -> list[dict]:
    user_id = _user_id_from_token(token)
    return _select_cached(f"user:{user_id}", lambda: get_authenticated_client(user_id), table_name, filters, select, order_by, ascending, limit)

def select_rows_web(table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None,) -> list[dict]:
    return _select_cached("web", _get_web_client, table_name, filters, select, order_by, ascending, limit)

def insert_rows_web(table_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> list[dict]:
    supabase = _get_web_client()
    response = supabase.table(table_name).insert(data).execute()
    _after_write("web", table_name, response.data)
    return response.data

def update_rows_web(table_name: str, updates: dict[str, Any], filters: dict[str, Any]) -> list[dict]:
    _require_filters(filters, "update_rows_web")
    supabase = _get_web_client()
    query = supabase.table(table_name).update(updates)
    query = _apply_filters(query, filters)
    response = query.execute()
    _after_write("web", table_name, response.data)
    return response.data

def delete_rows_web(table_name: str, filters: dict[str, Any]) -> list[dict]:
    _require_filters(filters, "delete_rows_web")
    supabase = _get_web_client()
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    _after_write("web", table_name, response.data, deleted=True)
    return response.data