import os
from typing import Any
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values, RealDictCursor
import authnew as au
from dotenv import load_dotenv
load_dotenv()
conn = psycopg2.connect( host=os.environ.get("DB_HOST", "localhost"), port=int(os.environ.get("DB_PORT", 5432)),database=os.environ.get("DB_NAME", "myapp_db"),user=os.environ.get("DB_USER", "myapp"),password=os.environ.get("DB_PASSWORD"), )

# postgresql write the EMP table as emp and "EMP" as EMP  

ALLOWED_TABLES = {"Drive", "Gmail", "Instagram", "Paypal", "Paypal_verify", "Pinterst", "Razorpay", "Razorpay_verify", "Threads", "Whatsapp", "X", "Youtube", "users"}

def insert_rows(token, table_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> list[dict]:
    tokench = au.process(token=token)
    if not tokench["status"]:  
        raise PermissionError("Invalid token")
    if table_name not in ALLOWED_TABLES:
        raise ValueError(f"Table not allowed: {table_name}")
    rows = [data] if isinstance(data, dict) else data
    if not rows:
        return []
    cols = list(rows[0].keys())
    if any(set(r.keys()) != set(cols) for r in rows):
        raise ValueError("All rows must have the same keys")
    query = sql.SQL("INSERT INTO {} ({}) VALUES %s ").format(sql.Identifier(table_name), sql.SQL(", ").join(map(sql.Identifier, cols)),    )
    values = [tuple(r[c] for c in cols) for r in rows]
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            execute_values(cur, query, values)
            result = cur.fetchall()
        conn.commit()
        return [dict(r) for r in result]
    except Exception:
        conn.rollback()
        raise