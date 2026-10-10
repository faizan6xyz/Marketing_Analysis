import os
from contextlib import contextmanager
from typing import Any
from typing import Any, Optional
from dotenv import load_dotenv
import hashlib
import hmac
import psycopg2
from psycopg2 import sql
import uuid
from psycopg2.extras import execute_values, RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import authnew as au
import secrets
load_dotenv()
SECRET_KEY = os.environ["SECRET_KEY"].encode("utf-8")
pool = ThreadedConnectionPool( 1, 10, host=os.environ.get("DB_HOST", "localhost"),port=int(os.environ.get("DB_PORT", 5432)), dbname=os.environ.get("DB_NAME", "myapp_db"), user=os.environ.get("DB_USER", "myapp"), password=os.environ.get("DB_PASSWORD"),) # 1 minimum connection and the 10 as maximum connection
ALLOWED_TABLES = { "drive", "gmail", "instagram", "paypal", "paypal_verify", "pinterest", "razorpay", "razorpay_verify", "threads", "whatsapp", "x", "youtube", "users",}
_SIMPLE_OPS = { "eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=","like": "LIKE", "ilike": "ILIKE", "contains": "@>",}

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

@contextmanager
def _cursor():
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)

def add_new_user(email: str, password: str) -> bool:
    if not email or not password:
        return False
    user_id = str(uuid.uuid4()) 
    try:
        with _cursor() as cur:
            cur.execute( "INSERT INTO userdetails (email, password, user_id) VALUES (%s, %s, %s)",(email, _hash_password(password), user_id), )
    except psycopg2.errors.UniqueViolation:
        return False
    except psycopg2.Error as e:
        return False
    return user_id

def user_exist_check(email: str, password: str):
    if not email or not password:
        return False
    try:
        with _cursor() as cur:
            cur.execute("SELECT password, user_id FROM userdetails WHERE email = %s", (email,),)
            row = cur.fetchone()
    except psycopg2.Error as e:
        return False
    if not row:
        return False
    stored_hash, user_id = row
    if not stored_hash or not user_id:
        return False
    if not _verify_password(password, stored_hash):
        return False
    if not stored_hash.startswith("scrypt$"):
        try:
            with _cursor() as cur:
                cur.execute( "UPDATE userdetails SET password = %s WHERE email = %s",(_hash_password(password), email),)
        except psycopg2.Error as e:
            return e
    return str(user_id)

def _check(table_name: str, token = None) -> str:
    if token :
        if not au.process(token=token)["status"]:
            raise PermissionError("Invalid token")
    table_name = table_name.lower()
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Table not allowed: {table_name}")
    return table_name

def _apply_filters(filters: dict[str, Any]) -> tuple[sql.Composable, list]:
    if not filters:
        raise ValueError("filters required (refusing to touch every row)")
    parts, params = [], []
    for column, condition in filters.items():
        op, value = condition if isinstance(condition, tuple) else ("eq", condition)
        op = op.lower()
        col = sql.Identifier(column)
        if op in ("eq", "is") and value is None:
            parts.append(sql.SQL("{} IS NULL").format(col))
        elif op == "neq" and value is None:
            parts.append(sql.SQL("{} IS NOT NULL").format(col))
        elif op == "is":
            parts.append(sql.SQL("{} IS %s").format(col))      # True / False
            params.append(value)
        elif op == "in":
            parts.append(sql.SQL("{} = ANY(%s)").format(col))
            params.append(list(value))
        elif op in _SIMPLE_OPS:
            parts.append(sql.SQL("{} " + _SIMPLE_OPS[op] + " %s").format(col))
            params.append(value)
        else:
            raise ValueError(f"Unsupported operator: {op}")
    return sql.SQL(" AND ").join(parts), params

def insert_rows(token, table_name: str, data: dict | list[dict]) -> list[dict]:
    table_name = _check(table_name,token)
    rows = [data] if isinstance(data, dict) else data
    if not rows:
        return []
    cols = list(rows[0].keys())
    if any(set(r.keys()) != set(cols) for r in rows):
        raise ValueError("All rows must have the same keys")
    query = sql.SQL("INSERT INTO {} ({}) VALUES %s RETURNING *").format( sql.Identifier(table_name),sql.SQL(", ").join(map(sql.Identifier, cols)), )
    values = [tuple(r[c] for c in cols) for r in rows]
    with _cursor() as cur:
        execute_values(cur, query, values, fetch=True)
        return [dict(r) for r in cur.fetchall()]

def update_rows(token, table_name: str, updates: dict, filters: dict) -> list[dict]:
    table_name = _check(table_name,token)
    if not updates:
        return []
    where, where_params = _apply_filters(filters)
    set_clause = sql.SQL(", ").join( sql.SQL("{} = %s").format(sql.Identifier(c)) for c in updates )
    query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format( sql.Identifier(table_name), set_clause, where )
    with _cursor() as cur:
        cur.execute(query, [*updates.values(), *where_params])
        return [dict(r) for r in cur.fetchall()]

def delete_rows(token, table_name: str, filters: dict[str, Any]) -> list[dict]:
    table_name = _check(table_name,token)
    where, where_params = _apply_filters(filters)  
    query = sql.SQL("DELETE FROM {} WHERE {} RETURNING *").format( sql.Identifier(table_name), where )
    with _cursor() as cur:
        cur.execute(query, where_params)
        return [dict(r) for r in cur.fetchall()]

def select_rows( token, table_name: str, filters: Optional[dict[str, Any]] = None,select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None,) -> list[dict]: 
    table_name = _check(table_name,token)
    if select.strip() == "*":
        select_clause = sql.SQL("*")
    else:
        cols = [c.strip() for c in select.split(",") if c.strip()]
        if not cols:
            raise ValueError("select must be '*' or a comma-separated column list")
        select_clause = sql.SQL(", ").join(map(sql.Identifier, cols))
    query = sql.SQL("SELECT {} FROM {}").format( select_clause, sql.Identifier(table_name) )
    params: list = []
    if filters:
        where, params = _apply_filters(filters)
        query += sql.SQL(" WHERE ") + where
    if order_by:
        direction = sql.SQL("ASC" if ascending else "DESC")
        query += sql.SQL(" ORDER BY {} {}").format(sql.Identifier(order_by), direction)
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        query += sql.SQL(" LIMIT %s")
        params.append(limit)
    with _cursor() as cur:
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]
    
def insert_rows_web(table_name: str, data: dict | list[dict]) -> list[dict]:
    table_name = _check(table_name)
    rows = [data] if isinstance(data, dict) else data
    if not rows:
        return []
    cols = list(rows[0].keys())
    if any(set(r.keys()) != set(cols) for r in rows):
        raise ValueError("All rows must have the same keys")
    query = sql.SQL("INSERT INTO {} ({}) VALUES %s RETURNING *").format( sql.Identifier(table_name),sql.SQL(", ").join(map(sql.Identifier, cols)), )
    values = [tuple(r[c] for c in cols) for r in rows]
    with _cursor() as cur:
        execute_values(cur, query, values, fetch=True)
        return [dict(r) for r in cur.fetchall()]

def update_rows_web(table_name: str, updates: dict, filters: dict) -> list[dict]:
    table_name = _check(table_name)
    if not updates:
        return []
    where, where_params = _apply_filters(filters)
    set_clause = sql.SQL(", ").join( sql.SQL("{} = %s").format(sql.Identifier(c)) for c in updates )
    query = sql.SQL("UPDATE {} SET {} WHERE {} RETURNING *").format( sql.Identifier(table_name), set_clause, where )
    with _cursor() as cur:
        cur.execute(query, [*updates.values(), *where_params])
        return [dict(r) for r in cur.fetchall()]

def delete_rows_web(table_name: str, filters: dict[str, Any]) -> list[dict]: # its the dead code bease its not used 
    table_name = _check(table_name)
    where, where_params = _apply_filters(filters)  
    query = sql.SQL("DELETE FROM {} WHERE {} RETURNING *").format( sql.Identifier(table_name), where )
    with _cursor() as cur:
        cur.execute(query, where_params)
        return [dict(r) for r in cur.fetchall()]

def select_rows_web(table_name: str, filters: Optional[dict[str, Any]] = None,select: str = "*", order_by: Optional[str] = None, ascending: bool = True, limit: Optional[int] = None,) -> list[dict]: 
    table_name = _check(table_name)
    if select.strip() == "*":
        select_clause = sql.SQL("*")
    else:
        cols = [c.strip() for c in select.split(",") if c.strip()]
        if not cols:
            raise ValueError("select must be '*' or a comma-separated column list")
        select_clause = sql.SQL(", ").join(map(sql.Identifier, cols))
    query = sql.SQL("SELECT {} FROM {}").format( select_clause, sql.Identifier(table_name) )
    params: list = []
    if filters:
        where, params = _apply_filters(filters)
        query += sql.SQL(" WHERE ") + where
    if order_by:
        direction = sql.SQL("ASC" if ascending else "DESC")
        query += sql.SQL(" ORDER BY {} {}").format(sql.Identifier(order_by), direction)
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        query += sql.SQL(" LIMIT %s")
        params.append(limit)
    with _cursor() as cur:
        cur.execute(query, params)
        return [dict(r) for r in cur.fetchall()]

if __name__ == "__main__":
    print("hi")
