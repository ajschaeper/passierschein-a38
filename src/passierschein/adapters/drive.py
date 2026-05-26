"""
Google Drive adapter — file listing and download by file ID.

Uses the same service account credentials as the Sheets adapter.
The service account must have at least Viewer access on DRIVE_FOLDER_ID.
"""
from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import Any

from google.oauth2.service_account import Credentials

from ..config import config

log = logging.getLogger(__name__)

_SCOPES_RO = ["https://www.googleapis.com/auth/drive.readonly"]
_SCOPES_RW = ["https://www.googleapis.com/auth/drive"]

_MIME_TO_SUFFIX = {
    "application/pdf": ".pdf",
    "image/png":       ".png",
    "image/jpeg":      ".jpg",
    "image/webp":      ".webp",
}


def _service(write: bool = False):
    from googleapiclient.discovery import build
    scopes = _SCOPES_RW if write else _SCOPES_RO
    creds = Credentials.from_service_account_file(
        str(config.GOOGLE_CREDENTIALS_FILE), scopes=scopes
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def is_drive_id(s: str) -> bool:
    """Return True if s looks like a Drive file/folder ID (not a local path)."""
    if not s or len(s) < 20 or len(s) > 50:
        return False
    if "/" in s or "\\" in s or "." in s:
        return False
    return all(c.isalnum() or c in "-_" for c in s)


def list_files(folder_id: str | None = None) -> list[dict[str, Any]]:
    """
    Return files from the Inbox folder (default) or a given folder, newest first.
    Pass folder_id=config.DRIVE_FOLDER_ID to browse the full archive tree.
    Each entry: {id, name, mimeType, createdTime, folder_path}
    """
    root = folder_id or config.DRIVE_INBOX_ID or config.DRIVE_FOLDER_ID
    if not root:
        log.warning("DRIVE_FOLDER_ID not configured — cannot list Drive files")
        return []
    svc = _service()

    def _list(fid: str, path: str) -> list[dict[str, Any]]:
        result = svc.files().list(
            q=f"'{fid}' in parents and trashed = false",
            fields="files(id, name, mimeType, createdTime)",
            orderBy="createdTime desc",
            pageSize=200,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        ).execute()
        items = result.get("files", [])
        files, folders = [], []
        for item in items:
            if item["mimeType"] == "application/vnd.google-apps.folder":
                folders.append(item)
            else:
                item["folder_path"] = path
                files.append(item)
        for sub in folders:
            sub_path = f"{path}/{sub['name']}" if path else sub["name"]
            files.extend(_list(sub["id"], sub_path))
        return files

    files = _list(root, "")
    files.sort(key=lambda f: f.get("createdTime") or "", reverse=True)
    log.info("Listed %d files in Drive folder tree %s", len(files), root)
    return files


def download_to_temp(file_id: str, name: str | None = None) -> Path:
    """
    Download a Drive file to a temporary local file.
    Returns the Path to the temp file — caller must delete it when done.
    """
    from googleapiclient.http import MediaIoBaseDownload

    svc = _service()
    meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
    display_name = name or meta.get("name", "")
    suffix = Path(display_name).suffix or _MIME_TO_SUFFIX.get(meta.get("mimeType", ""), ".pdf")

    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(buf.getvalue())
    tmp.close()
    log.info("Downloaded Drive:%s → %s (%d bytes)", file_id, tmp.name, buf.tell())
    return Path(tmp.name)


def _find_subfolder(name: str, parent_id: str) -> str | None:
    """Return the Drive folder ID of a direct child folder with the given name."""
    result = _service().files().list(
        q=(
            f"'{parent_id}' in parents"
            f" and name = '{name}'"
            f" and mimeType = 'application/vnd.google-apps.folder'"
            f" and trashed = false"
        ),
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        pageSize=5,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


# Maps entity type strings to Drive subfolder names
_SUBFOLDER: dict[str, str] = {
    "invoice":            "Rechnungen",
    "settlement_beihilfe": "Beihilfebescheide",
    "settlement_pkv":     "PKV-Abrechnungen",
}


def move_to_archive(file_id: str, year: int, entity_type: str) -> bool:
    """
    Move a Drive file from the Inbox to Medikosten/<year>/<subfolder>.

    entity_type: 'invoice' | 'settlement_beihilfe' | 'settlement_pkv'
    Returns True on success, False if folder not found or IDs not configured.
    """
    inbox = config.DRIVE_INBOX_ID
    root  = config.DRIVE_FOLDER_ID
    if not file_id or not inbox or not root:
        log.warning("move_to_archive: missing Drive IDs in config")
        return False

    subfolder_name = _SUBFOLDER.get(entity_type)
    if not subfolder_name:
        log.warning("move_to_archive: unknown entity_type %r", entity_type)
        return False

    year_id = _find_subfolder(str(year), root)
    if not year_id:
        log.warning("move_to_archive: year folder %d not found", year)
        return False

    dest_id = _find_subfolder(subfolder_name, year_id)
    if not dest_id:
        log.warning("move_to_archive: subfolder %s/%s not found", year, subfolder_name)
        return False

    _service(write=True).files().update(
        fileId=file_id,
        addParents=dest_id,
        removeParents=inbox,
        fields="id,parents",
        supportsAllDrives=True,
    ).execute()
    log.info("Moved Drive:%s → %d/%s", file_id, year, subfolder_name)
    return True
