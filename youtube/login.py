import io
import os
import google_auth_oauthlib.flow
import google.auth.transport.requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from googleapiclient.errors import HttpError
SCOPES = [ "https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/drive.readonly",]
CLIENT_SECRETS_FILE = "client_secret.json"
TOKEN_FILE = "token.json"

def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
        else:
            flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file( CLIENT_SECRETS_FILE, SCOPES )
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


class DriveStreamAsFile(io.RawIOBase):
    def __init__(self, drive_service, file_id):
        self._request = drive_service.files().get_media(fileId=file_id)
        self._buffer = io.BytesIO()
        self._downloader = MediaIoBaseDownload(self._buffer, self._request, chunksize=10 * 1024 * 1024)
        self._done = False

    def readable(self):
        return True

    def readinto(self, b):
        while self._buffer.tell() >= len(self._buffer.getvalue()) - len(b) and not self._done:
            _, self._done = self._downloader.next_chunk()
        data = self._buffer.getvalue()
        n = min(len(b), len(data))
        if n == 0:
            return 0
        b[:n] = data[:n]
        remaining = data[n:]
        self._buffer = io.BytesIO(remaining)
        self._buffer.seek(0, io.SEEK_END)
        return n

def get_drive_file_size(drive_service, file_id: str) -> int:
    meta = drive_service.files().get(fileId=file_id, fields="size,mimeType,name").execute()
    return int(meta["size"]), meta.get("mimeType", "video/*"), meta.get("name")

def upload_from_drive_to_youtube( file_id: str, title: str, description: str = "", tags=None, category_id: str = "22",privacy_status: str = "private",):
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)
    size, mimetype, name = get_drive_file_size(drive, file_id)
    stream = DriveStreamAsFile(drive, file_id)
    media = MediaIoBaseUpload(stream, mimetype=mimetype, chunksize=10 * 1024 * 1024, resumable=True)
    body = { "snippet": { "title": title, "description": description, "tags": tags or [], "categoryId": category_id, }, "status": {"privacyStatus": privacy_status}, }
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploading... {int(status.progress() * 100)}%")
    return response

if __name__ == "__main__":
    # The Drive file ID is the long id in the file's share link:
    # https://drive.google.com/file/d/FILE_ID_HERE/view
    DRIVE_FILE_ID = "PASTE_DRIVE_FILE_ID_HERE"
    try:
        result = upload_from_drive_to_youtube( DRIVE_FILE_ID, title="My Drive video", description="Uploaded straight from Drive, no local download", tags=["demo"], privacy_status="private", )
    except HttpError as e:
        print(f"Failed: {e}")