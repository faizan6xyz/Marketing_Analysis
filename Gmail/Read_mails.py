import database.UserDB as dbimp
from datetime import datetime
import os , re , time , base64 
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_KEY in your environment or .env file")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
TABLE_NAME = "Gmail"
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail_client")
Clientid = os.environ.get("client_id")
Clientsec = os.environ.get("client_secrect")
GMAIL_REDIRECT_URI = os.environ.get("GMAIL_REDIRECT_URI")
GMAIL_SCOPES = os.environ.get("GMAIL_SCOPES")
if not GMAIL_SCOPES or not GMAIL_SCOPES.strip():
    raise EnvironmentError("GMAIL_SCOPES environment variable is not set or empty.")
cleaned = [scope.strip() for scope in GMAIL_SCOPES.split(",") if scope.strip()]
SCOPES = [s.strip(' []"') for s in cleaned]
if not SCOPES:
    raise EnvironmentError("GMAIL_SCOPES did not contain any valid scopes.")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_SEND_BYTES = 25 * 1024 * 1024
MAX_RESULTS_CAP = 500
MAX_QUERY_LENGTH = 2048
EMAIL_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEADER_INJECTION_RE = re.compile(r"[\r\n]")
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503}
ALLOWED_ATTACHMENT_EXTENSIONS = { '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.csv', '.ppt', '.pptx', '.txt', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.mp3', }

def build_flow():
    return Flow.from_client_config({"web": { "client_id": Clientid, "client_secret": Clientsec, "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [GMAIL_REDIRECT_URI], }}, scopes=SCOPES, redirect_uri=GMAIL_REDIRECT_URI)

def save_tokens(token, user_id, creds, email_addr=None):
    payload = {"Access_token": creds.token, "Refresh_token": creds.refresh_token, "Token_expire": creds.expiry.isoformat(), "Timestamp": datetime.now(timezone.utc).isoformat()}
    if email_addr:
        payload["Email"] = email_addr
    rows = dbimp.select_rows(token,TABLE_NAME, select="id", filters={"id": user_id})
    if rows:
        dbimp.update_rows(token,TABLE_NAME, payload, filters={"id": user_id})
    else:
        dbimp.insert_rows(token,TABLE_NAME, {"id": user_id, **payload})

def get_service(token,user_id):
    rows = dbimp.select_rows(token,TABLE_NAME, select="Access_token,Refresh_token,Token_expire", filters={"id": user_id})
    row = rows[0] if rows else None
    if not row or not row.get("Access_token"):
        return None
    creds = Credentials(token=row["Access_token"], refresh_token=row["Refresh_token"], token_uri="https://oauth2.googleapis.com/token", client_id=Clientid, client_secret=Clientsec, scopes=SCOPES)
    if row.get("Token_expire"):
        creds.expiry = datetime.fromisoformat(row["Token_expire"])
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                resp = requests.get(
                    "https://gmail.googleapis.com/gmail/v1/users/me/profile",headers={"Authorization": f"Bearer {creds.token}"},).json()
                email = resp.get("emailAddress")
            except Exception:
                email = None
            save_tokens(token ,user_id, creds, email_addr=email)  # save refreshed token regardless
        else:
            return None
    if set(SCOPES) - set(getattr(creds, 'scopes', None) or SCOPES):
        logger.warning("Stored credentials may not cover all requested scopes.")
    return build('gmail', 'v1', credentials=creds)

def _validate_email_address(address: str, label: str = "recipient") -> None:
    if not address or not isinstance(address, str):
        raise ValueError(f"Invalid {label} email address: {address!r}")
    address = address.strip()
    if not EMAIL_ADDR_RE.match(address):
        raise ValueError(f"Invalid {label} email address: {address!r}")
    if HEADER_INJECTION_RE.search(address):
        raise ValueError(f"{label} contains invalid characters (possible header injection).")

def _validate_header_value(value: str, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    if HEADER_INJECTION_RE.search(value):
        raise ValueError(f"{label} contains newline characters, which is not allowed.")

def _validate_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query exceeds maximum length of {MAX_QUERY_LENGTH} characters.")
    return query

def _validate_max_results(max_results: int) -> int:
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise ValueError("max_results must be an integer.")
    if max_results <= 0:
        raise ValueError("max_results must be positive.")
    if max_results > MAX_RESULTS_CAP:
        raise ValueError(f"max_results cannot exceed {MAX_RESULTS_CAP}.")
    return max_results

def _validate_attachment(path: str, base_dir: str = None) -> str:   
    resolved = os.path.realpath(path)
    if base_dir is not None:
        base_resolved = os.path.realpath(base_dir)
        if os.path.commonpath([resolved, base_resolved]) != base_resolved:
            raise ValueError(f"Attachment path '{path}' resolves outside the allowed directory.")
    if not os.path.isfile(resolved):
        raise FileNotFoundError(f"Attachment not found: {path}")
    if os.path.islink(path):
        raise ValueError(f"Attachment '{path}' is a symlink, which is not allowed.")
    size = os.path.getsize(resolved)
    if size == 0:
        raise ValueError(f"Attachment '{path}' is empty.")
    if size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment '{path}' is {size / (1024*1024):.1f} MB, which exceeds "
            f"Gmail's {MAX_ATTACHMENT_BYTES / (1024*1024):.0f} MB per-message limit.")
    return resolved

def _safe_output_path(out_dir: str, filename: str) -> str:
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name in ('.', '..'):
        raise ValueError(f"Unsafe or empty attachment filename: {filename!r}")
    out_dir_resolved = os.path.realpath(out_dir)
    candidate = os.path.realpath(os.path.join(out_dir_resolved, safe_name))
    if os.path.commonpath([candidate, out_dir_resolved]) != out_dir_resolved:
        raise ValueError(f"Attachment filename '{filename}' resolves outside the output directory.")
    return candidate

def _with_retry(func, *args, **kwargs):
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs).execute()
        except HttpError as e:
            status = getattr(e, "status_code", None) or getattr(e.resp, "status", None)
            if status in RETRYABLE_HTTP_STATUSES and attempt < MAX_RETRIES:
                logger.warning(f"Gmail API error {status} on attempt {attempt}/{MAX_RETRIES}, retrying")
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
                last_exc = e
                continue
            raise
    raise last_exc

def _extract_body_parts(payload, plain_parts=None, html_parts=None):
    if plain_parts is None:
        plain_parts, html_parts = [], []
    mime_type = payload.get('mimeType', '')
    if 'parts' in payload:
        for part in payload['parts']:
            _extract_body_parts(part, plain_parts, html_parts)
    else:
        data = payload.get('body', {}).get('data', '')
        if not data:
            return plain_parts, html_parts
        try:
            decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
        except Exception as e:
            logger.warning(f"Failed to decode body part: {e}")
            return plain_parts, html_parts
        if mime_type == 'text/plain':
            plain_parts.append(decoded)
        elif mime_type == 'text/html':
            html_parts.append(decoded)
    return plain_parts, html_parts

def get_email_body(payload) -> str:
    if not payload or not isinstance(payload, dict):
        return "No body content found."
    plain_parts, html_parts = _extract_body_parts(payload)
    if plain_parts:
        return "\n".join(plain_parts)
    if html_parts:
        return "\n".join(html_parts)
    return "No body content found."

def list_messages(service, query='is:unread', max_results=10, all_pages=False, verbose=True) -> list[dict]:
    query = _validate_query(query)
    max_results = _validate_max_results(max_results)
    parsed = []
    page_token = None
    max_pages = 20 if all_pages else 1
    for _ in range(max_pages):
        list_kwargs = {"userId": "me", "q": query, "maxResults": max_results}
        if page_token:
            list_kwargs["pageToken"] = page_token
        results = _with_retry(service.users().messages().list, **list_kwargs)
        messages = results.get('messages', [])
        for msg in messages:
            full = _with_retry(service.users().messages().get, userId='me', id=msg['id'], format='full')
            headers = full.get('payload', {}).get('headers', [])
            subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
            sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
            body = get_email_body(full.get('payload'))
            entry = {"id": msg['id'], "from": sender, "subject": subject, "body": body}
            parsed.append(entry)
            if verbose:
                print(f"From: {sender} | Subject: {subject} | Body: {body}")
        page_token = results.get('nextPageToken')
        if not page_token or not all_pages:
            break
    return parsed

def send_message(service, to, subject, body_text, name=""):
    if name:
        subject = subject.replace("{name}", name)
        body_text = body_text.replace("{name}", name)
    _validate_email_address(to)
    _validate_header_value(subject, "subject")
    _validate_header_value(body_text, "body_text")
    message = MIMEText(body_text or "")
    message['to'] = to
    message['subject'] = subject or ""
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    return _with_retry(service.users().messages().send, userId='me', body={'raw': raw})

def send_message_with_attachments(service, to, subject, body_text, drive_links, name="", link_labels=None):
    if name:
        subject = subject.replace("{name}", name)
        body_text = body_text.replace("{name}", name)
    _validate_email_address(to)
    _validate_header_value(subject, "subject")
    _validate_header_value(body_text, "body_text")
    if not drive_links:
        raise ValueError("drive_links must contain at least one link.")
    if not isinstance(drive_links, list):
        drive_links = [drive_links]
    validated_links = []
    for link in drive_links:
        if not isinstance(link, str) or not link.startswith("https://drive.google.com/"):
            raise ValueError(f"Invalid Drive link: {link!r}")
        validated_links.append(link)
    if link_labels and len(link_labels) != len(validated_links):
        raise ValueError("link_labels must match drive_links in length.")
    links_section = "\n".join( f"- {link_labels[i]}: {link}" if link_labels else f"- {link}" for i, link in enumerate(validated_links) )
    full_body = f"{body_text}\n\nAttachments:\n{links_section}"
    message = MIMEMultipart()
    message['to'] = to
    message['subject'] = subject or ""
    message.attach(MIMEText(full_body))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    if len(raw) > MAX_ATTACHMENT_BYTES * 2:
        raise ValueError("Encoded message size exceeds safe transmission limits.")
    return _with_retry(service.users().messages().send, userId='me', body={'raw': raw})

def mark_as_read(service, message_id: str) -> dict:
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string.")
    return _with_retry(service.users().messages().modify, userId='me', id=message_id,body={'removeLabelIds': ['UNREAD']})

def mark_as_unread(service, message_id: str) -> dict:
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string.")
    return _with_retry(service.users().messages().modify, userId='me', id=message_id,body={'addLabelIds': ['UNREAD']})

def download_attachments(service, message_id: str, out_dir: str = "attachments") -> list[str]:
    if not message_id or not isinstance(message_id, str):
        raise ValueError("message_id must be a non-empty string.")
    os.makedirs(out_dir, exist_ok=True)
    full = _with_retry(service.users().messages().get, userId='me', id=message_id, format='full')
    saved = []
    skipped = []
    def walk(payload):
        if not payload or not isinstance(payload, dict):
            return
        if 'parts' in payload:
            for part in payload['parts']:
                walk(part)
            return
        filename = payload.get('filename')
        body = payload.get('body', {})
        attachment_id = body.get('attachmentId')
        if not filename or not attachment_id:
            return
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ALLOWED_ATTACHMENT_EXTENSIONS :
            logger.warning(f"Skipping '{filename}': potentially dangerous file type ({ext}).")
            skipped.append(filename)
            return
        estimated_size = body.get('size')
        if isinstance(estimated_size, int) and estimated_size > MAX_ATTACHMENT_BYTES:
            logger.warning(f"Skipping '{filename}': reported size exceeds safety limit.")
            skipped.append(filename)
            return
        out_path = _safe_output_path(out_dir, filename)
        att = _with_retry(service.users().messages().attachments().get,userId='me', messageId=message_id, id=attachment_id)
        data = base64.urlsafe_b64decode(att['data'] + '=' * (-len(att['data']) % 4))
        if len(data) > MAX_ATTACHMENT_BYTES:
            logger.warning(f"Skipping '{filename}': decoded size exceeds safety limit.")
            skipped.append(filename)
            return
        if len(data) == 0:
            logger.warning(f"Skipping '{filename}': attachment is empty.")
            skipped.append(filename)
            return
        with open(out_path, 'wb') as f:
            f.write(data)
        saved.append(out_path)
    walk(full.get('payload'))
    if skipped:
        logger.info(f"Skipped {len(skipped)} attachment(s): {skipped}")
    return saved

def create_filter(service, criteria, action):
    body = {'criteria': criteria, 'action': action}
    return service.users().settings().filters().create(userId='me', body=body).execute()

def list_filters(service):
    return service.users().settings().filters().list(userId='me').execute().get('filter', [])

def delete_filter(service, filter_id):
    return service.users().settings().filters().delete(userId='me', id=filter_id).execute()

def list_labels(service):
    return service.users().labels().list(userId='me').execute().get('labels', [])

def create_label(service, name, list_visibility='labelShow', label_visibility='labelShow'):
    body = {'name': name,'labelListVisibility': list_visibility,'messageListVisibility': label_visibility,}
    return service.users().labels().create(userId='me', body=body).execute()