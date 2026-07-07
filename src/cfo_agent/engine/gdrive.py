"""Google Drive storage for the cloud deployment.

When Penny runs on Railway there's no synced Drive folder on disk, so receipts
must be written to the Brain via the Drive API using a service account (the same
domain-wide-delegation service account PrioPals uses). Locally these env vars are
unset and callers fall back to filesystem writes — so this module is inert in
local/test runs.

Configure via env:
  GOOGLE_SERVICE_ACCOUNT_JSON   the service-account key, as JSON text OR a path
  DRIVE_RECEIPTS_FOLDER_ID      Drive id of the CFO Agent "receipts" folder
The Brain is a Shared Drive, so every call sets supportsAllDrives=True.
"""
from __future__ import annotations

import json
import os

_SCOPES = ["https://www.googleapis.com/auth/drive"]


def configured() -> bool:
    return bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
               and os.environ.get("DRIVE_RECEIPTS_FOLDER_ID"))


def _service():
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw))
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    # PrioPals-style domain-wide delegation: act AS a real user who already has
    # Drive access (so no folder needs to be shared with the service account).
    subject = os.environ.get("GOOGLE_IMPERSONATE_SUBJECT")
    if subject:
        creds = creds.with_subject(subject)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _subfolder(svc, parent: str, name: str) -> str:
    """Find or create a child folder; return its id."""
    q = (f"'{parent}' in parents and name = '{name}' and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    res = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                           includeItemsFromAllDrives=True).execute()
    hits = res.get("files", [])
    if hits:
        return hits[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent]}
    return svc.files().create(body=meta, fields="id",
                              supportsAllDrives=True).execute()["id"]


def upload(local_path, name: str, month: str) -> str:
    """Upload a file into <receipts>/<month>/ and return its shareable link."""
    from googleapiclient.http import MediaFileUpload
    svc = _service()
    folder = _subfolder(svc, os.environ["DRIVE_RECEIPTS_FOLDER_ID"], month)
    media = MediaFileUpload(str(local_path), resumable=False)
    f = svc.files().create(body={"name": name, "parents": [folder]},
                           media_body=media, fields="id, webViewLink",
                           supportsAllDrives=True).execute()
    return f.get("webViewLink") or f"drive:{f['id']}"
