from datetime import datetime, timezone
from supabase_auth.errors import AuthApiError
import os
import json
from typing import Any, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
import sqlite3
from contextlib import closing
import valkey
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
email = os.environ.get("email")
passw = os.environ.get("pass")
DB = "users.db"
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in your environment or .env file")
TABLE_NAME = "users"
_session_cache: dict[str, Any] = {}  
VALKEY_HOST = os.environ.get("VALKEY_HOST", "localhost")
VALKEY_PORT = int(os.environ.get("VALKEY_PORT", 6379))
VALKEY_DB = int(os.environ.get("VALKEY_DB", 0))
vk = valkey.Valkey(host=VALKEY_HOST, port=VALKEY_PORT, db=VALKEY_DB, decode_responses=True)

def _row_cache_key(table_name: str, filters: Optional[dict[str, Any]], select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None) -> str:
    key_data = { "filters": filters or {}, "select": select, "order_by": order_by, "ascending": ascending, "limit": limit, }
    return f"rows:{table_name}:{json.dumps(key_data, sort_keys=True, default=str)}"

def _is_session_valid(session) -> bool:
    if session is None:
        return False
    return session.expires_at is not None and session.expires_at > datetime.now(timezone.utc).timestamp() + 10

def get_authenticated_client(token: str) -> Client:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    cached = _session_cache.get(token)
    if _is_session_valid(cached):
        supabase.auth.set_session(cached.access_token, cached.refresh_token)
        return supabase
    row = get_user_by_token(token=token)
    if row is None:
        raise ValueError(f"Invalid or unknown token: {token!r}")
    email, password = row
    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
    except AuthApiError as e:
        if "invalid" in str(e).lower() or e.status == 400:
            res = supabase.auth.sign_up({"email": email, "password": password})
        else:
            raise
    _session_cache[token] = res.session
    return supabase

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(""" CREATE TABLE IF NOT EXISTS users ( id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,token TEXT UNIQUE) """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_token ON users(token)")

def insert_user(email, password, token=None):
    with closing(get_conn()) as conn:
        with conn:
            conn.execute("INSERT INTO users (email, password, token) VALUES (?, ?, ?)",(email, password, token),)

def get_user_by_token(token) -> Optional[tuple]:
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT email, password FROM users WHERE token = ?", (token,))
        return cur.fetchone()

def update_token_by_token(token, new_token):
    with closing(get_conn()) as conn:
        with conn:
            conn.execute("UPDATE users SET token = ? WHERE token = ?",(new_token, token),)

def update_token_by_mail(email, token):
    with closing(get_conn()) as conn:
        with conn:
            conn.execute("UPDATE users SET token = ? WHERE email = ?",(token, email),)

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
                query = query.in_(column, value)   # value must be a list
            elif op == "is":
                query = query.is_(column, value)   # e.g. None for IS NULL
            elif op == "contains":
                query = query.contains(column, value)  # for array/jsonb columns
            else:
                raise ValueError(f"Unsupported operator: {op}")
        else:
            query = query.eq(column, condition)
    return query

def insert_rows(token, table_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> list[dict]:
    supabase = get_authenticated_client(token)
    response = supabase.table(table_name).insert(data).execute()
    return response.data

def update_rows(token, table_name: str, updates: dict[str, Any], filters: dict[str, Any]) -> list[dict]:
    supabase = get_authenticated_client(token)
    query = supabase.table(table_name).update(updates)
    query = _apply_filters(query, filters)
    response = query.execute()
    cache_key = _row_cache_key(table_name, filters)
    vk.set(cache_key, json.dumps(response.data, default=str))
    return response.data

def delete_rows(token, table_name: str, filters: dict[str, Any]) -> list[dict]:
    supabase = get_authenticated_client(token)
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    return response.data

def select_rows(token, table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None, ) -> list[dict]:
    cache_key = _row_cache_key(table_name, filters, select, order_by, ascending, limit)
    cached = vk.get(cache_key)
    if cached is not None:
        return json.loads(cached)
    supabase = get_authenticated_client(token)
    query = supabase.table(table_name).select(select)
    if filters:
        query = _apply_filters(query, filters)
    if order_by:
        query = query.order(order_by, desc=not ascending)
    if limit:
        query = query.limit(limit)
    response = query.execute()
    vk.set(cache_key, json.dumps(response.data, default=str))
    return response.data

def select_rows_web(table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None,) -> list[dict]:
    cache_key = _row_cache_key(table_name, filters, select, order_by, ascending, limit)
    cached = vk.get(cache_key)
    if cached is not None:
        return json.loads(cached)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email , "password":passw})
    query = supabase.table(table_name).select(select)
    if filters:
        query = _apply_filters(query, filters)
    if order_by:
        query = query.order(order_by, desc=not ascending)
    if limit:
        query = query.limit(limit)
    response = query.execute()
    vk.set(cache_key, json.dumps(response.data, default=str))
    return response.data

def insert_rows_web(table_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> list[dict]:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email , "password":passw})
    response = supabase.table(table_name).insert(data).execute()
    return response.data

def update_rows_web(table_name: str, updates: dict[str, Any], filters: dict[str, Any]) -> list[dict]:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email , "password":passw})    
    query = supabase.table(table_name).update(updates)
    query = _apply_filters(query, filters)
    response = query.execute()
    cache_key = _row_cache_key(table_name, filters)
    vk.set(cache_key, json.dumps(response.data, default=str))
    return response.data

def delete_rows_web(table_name: str, filters: dict[str, Any]) -> list[dict]:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email , "password":passw})    
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    return response.data