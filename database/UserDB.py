from datetime import datetime, timezone
from gotrue.errors import AuthApiError
import os
from typing import Any, Optional
from dotenv import load_dotenv
from supabase import create_client, Client
import sqlite3
from contextlib import closing
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
email = os.environ.get("email")
passw = os.environ.get("pass")
DB = "users.db"
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in your environment or .env file")
TABLE_NAME = "users"
_session_cache: dict[str, Any] = {}  # its a plain Python dictionary that avoids re-authenticating with Supabase on every single database call.

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
    return response.data

def delete_rows(token, table_name: str, filters: dict[str, Any]) -> list[dict]:
    supabase = get_authenticated_client(token)
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    return response.data

def select_rows( token, table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None, ) -> list[dict]:
    supabase = get_authenticated_client(token)
    query = supabase.table(table_name).select(select)
    if filters:
        query = _apply_filters(query, filters)
    if order_by:
        query = query.order(order_by, desc=not ascending)
    if limit:
        query = query.limit(limit)
    response = query.execute()
    return response.data

def select_rows_web( table_name: str, filters: Optional[dict[str, Any]] = None, select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None,) -> list[dict]:
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
    return response.data

def delete_rows_web(table_name: str, filters: dict[str, Any]) -> list[dict]:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    res = supabase.auth.sign_in_with_password({"email": email , "password":passw})    
    query = supabase.table(table_name).delete()
    query = _apply_filters(query, filters)
    response = query.execute()
    return response.data

if __name__ == "__main__":
    init_db()
    token = "NDUxZDhiNTgtNDU3NS00YjdiLTkxNTgtY2IzOWRjM2FlZDFl.MjAyNi0wOC0yNiAxMTozMDo1Ny42ODYwNzMrMDA6MDA=.N2E0YzU2NzkwN2Y0ZjMwZWMzZDhmMTNhZmY5MzFkOTVhMjIzOWM1MmI5OGFkYTljYjVmMzFjZTM1YzUxZGI1ZA=="
    insert_user(email, passw, token)

"""F
# --- INSERT ---    
# Add a single user
insert_rows("users", {"name": "Ayesha", "email": "ayesha@example.com", "role": "admin"})
# Bulk insert multiple rows at once (e.g. importing contacts)
insert_rows("contacts", [
    {"name": "Ravi", "phone": "9990001111"},
    {"name": "Meena", "phone": "9990002222"},
    {"name": "Karan", "phone": "9990003333"},
])
# Insert a new order tied to a user
insert_rows("orders", {"user_id": 12, "product": "Laptop", "amount": 54999, "status": "pending"})
# --- UPDATE ---
# Promote a user
update_rows("users", {"role": "senior"}, {"id": 5})
# Mark an order as shipped
update_rows("orders", {"status": "shipped"}, {"id": 101})
# Update all pending orders for a specific user (multiple filter columns)
update_rows("orders", {"status": "cancelled"}, {"user_id": 12, "status": "pending"})
# Deactivate a user by email instead of id
update_rows("users", {"is_active": False}, {"email": "ayesha@example.com"})
# --- DELETE ---
# Delete a single row by id
delete_rows("users", {"id": 5})
# Delete all orders belonging to a user
delete_rows("orders", {"user_id": 12})
# Delete rows matching multiple conditions (e.g. clean up old failed jobs)
delete_rows("jobs", {"status": "failed", "retry_count": 3})
# --- SELECT (with conditions) ---
# Get all admins
select_rows("users", filters={"role": "admin"})
# Get a user's orders, most recent first
select_rows("orders", filters={"user_id": 12}, order_by="created_at", ascending=False)
# Get top 5 highest-value orders overall
select_rows("orders", order_by="amount", ascending=False, limit=5)
# Fetch only specific columns instead of "*"
select_rows("users", filters={"role": "engineer"}, select="id,name,email")
# Combine multiple filters (AND logic) — active engineers only
select_rows("users", filters={"role": "engineer", "is_active": True})
# Get everything, no filters, capped at 50 rows
select_rows("logs", limit=50)
    
    
    
# --- gte / lte together (range queries) ---
# Orders between ₹10,000 and ₹50,000 — call select twice with a combined filter dict won't AND a range on one column,
# so chain manually or use Supabase's raw filter builder:
supabase.table("orders").select("*").gte("amount", 10000).lte("amount", 50000).execute()
# --- Date ranges ---
# Orders placed in the last 7 days
from datetime import datetime, timedelta, timezone
week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
select_rows("orders", filters={"created_at": ("gte", week_ago)})
# Orders before a specific cutoff date
select_rows("orders", filters={"created_at": ("lt", "2026-01-01T00:00:00Z")})
# --- like vs ilike ---
# Emails ending in a specific domain (case-sensitive)
select_rows("users", filters={"email": ("like", "%@company.com")})
# Product names containing "phone" (case-insensitive)
select_rows("orders", filters={"product": ("ilike", "%phone%")})
# --- in / not-in ---
# Users with specific roles
select_rows("users", filters={"role": ("in", ["admin", "senior", "manager"])})
# Orders excluding cancelled/refunded (combine neq calls manually if you need "not in")
supabase.table("orders").select("*").not_.in_("status", ["cancelled", "refunded"]).execute()
# --- is (NULL / boolean checks) ---
# Users who haven't verified their email
select_rows("users", filters={"email_verified_at": ("is", None)})
# Active users only (boolean column)
select_rows("users", filters={"is_active": ("is", True)})
# --- contains (array/jsonb columns) ---
# Products tagged as "vip" or "premium" (tags is a Postgres array column)
select_rows("orders", filters={"tags": ("contains", ["vip"])})
# --- Combining multiple operators in one call ---
# Active engineers earning above a threshold, sorted by salary
select_rows( "users", filters={"role": "engineer", "is_active": True, "salary": ("gt", 80000)}, order_by="salary", ascending=False)
# Pending orders over ₹5,000 for a specific user
select_rows("orders", filters={"user_id": 12, "status": "pending", "amount": ("gt", 5000)})
# --- Update with conditional filters ---
# Flag all high-value pending orders for review
update_rows( "orders", {"flagged_for_review": True}, {"status": "pending", "amount": ("gt", 100000)})
# --- Delete with conditional filters ---
# Clean up inactive users who never logged in
delete_rows("users", {"is_active": False, "last_login": ("is", None)})"""