from datetime import datetime, timezone , timedelta
import os
import json
from typing import Any, Optional
import hmac
import hashlib
from dotenv import load_dotenv
from supabase import create_client, Client
import sqlite3
from contextlib import closing
import valkey
load_dotenv()
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
vk = valkey.Valkey(host=VALKEY_HOST, port=VALKEY_PORT, db=VALKEY_DB, decode_responses=True)

def _row_cache_key(table_name: str, filters: Optional[dict[str, Any]], select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None) -> str:
    key_data = { "filters": filters or {}, "select": select, "order_by": order_by, "ascending": ascending, "limit": limit, }
    return f"rows:{table_name}:{json.dumps(key_data, sort_keys=True, default=str)}"

def retrieve(user_id):
    with closing(get_conn()) as conn:
        row = conn.execute( "SELECT Access_token, Refresh_token, Expire FROM access_tokens WHERE user_id = ?",(user_id,),).fetchone()
        if not row:
            return False
        access, refresh, expire = row
        if not access or not refresh or not expire:
            return False
        expire_dt = datetime.fromisoformat(expire)
        if expire_dt.tzinfo is None:              # safety: treat naive as UTC
            expire_dt = expire_dt.replace(tzinfo=timezone.utc)
        if expire_dt > datetime.now(timezone.utc) + timedelta(minutes=1):
            return access
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            session = supabase.auth.refresh_session(refresh).session
        except Exception as e:
            print(f"refresh failed for {user_id}: {e}")
            return False
        access = session.access_token
        refresh = session.refresh_token
        new_expire = datetime.fromtimestamp(session.expires_at, timezone.utc).isoformat()
        with conn: 
            conn.execute( "UPDATE access_tokens SET Access_token = ?, Refresh_token = ?, Expire = ? WHERE user_id = ?", (access, refresh, new_expire, user_id), )
        return access

def get_authenticated_client(user_id: str) -> Client:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    access_token = retrieve(user_id)
    if access_token:
        supabase.postgrest.auth(access_token)
        return supabase

def get_conn():
    return sqlite3.connect(DB)

def init_db():
    with closing(get_conn()) as conn:
        with conn:
            conn.execute(""" CREATE TABLE IF NOT EXISTS users ( id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,user_id TEXT UNIQUE) """)
            conn.execute(""" CREATE TABLE IF NOT EXISTS access_tokens ( user_id TEXT PRIMARY KEY ,Access_token TEXT UNIQUE NOT NULL,Refresh_token TEXT NOT NULL, Expire TEXT NOT NULL , ) """)

def insert_user(email, password, user_id =None):
    with closing(get_conn()) as conn:
        with conn:
            conn.execute("INSERT INTO users (email, password, user_id) VALUES (?, ?, ?)",(email, password, user_id),)

def get_user_by_token(user_id) :
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT email, password FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def add_new_user(email:str,password:str):
    if not email or not password :
        return False
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email, "password": password})
    if not res :
        return False
    passworddd = hmac.new(SECRET_KEY, password.encode("utf-8"), hashlib.sha256).hexdigest()
    user_id = res.user.id
    with closing(get_conn()) as conn:
        conn.execute("INSERT INTO users (email, password, user_id) VALUES (?, ?, ?)",(email, passworddd, user_id),)
        return True

def User_exist_check(email :str ,password:str,user_id : str):
    if not email or not password or not user_id :
        return False 
    passworddd = hmac.new(SECRET_KEY, password.encode("utf-8"), hashlib.sha256).hexdigest()
    with closing(get_conn()) as conn:
        cur = conn.execute("SELECT password , user_id FROM users WHERE email = ?", (email,))
        passwork , user_id1 =  cur.fetchone()
    if not passwork or not user_id1 :
        return False
    if passworddd == passwork and user_id == user_id1 :
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