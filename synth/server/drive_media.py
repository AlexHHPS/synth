"""Read selected Drive files and folders using the current Google session."""
import json
from pathlib import Path
import re
import shutil
import ssl
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

from synth.worker.media import MAX_VIDEO_BYTES, MAX_ATTACHMENT_BYTES
from . import desktop_auth

MIME_EXTENSIONS = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
    "video/x-matroska": ".mkv", "video/x-m4v": ".m4v",
    "application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png",
    "text/plain": ".txt", "text/markdown": ".md", "text/csv": ".csv",
}
EXPORT = {"application/vnd.google-apps." + suffix for suffix in ("document", "spreadsheet", "presentation")}
DRIVE_API = "https://www.googleapis.com/drive/v3/files/"
ID = re.compile(r"[a-zA-Z0-9_-]{5,200}")
FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DRIVES_API = "https://www.googleapis.com/drive/v3/drives"


def parse_link(link):
    if not isinstance(link, str) or len(link) > 2000:
        raise ValueError("drive_link_invalid")
    link = link.strip()
    url = urllib.parse.urlsplit(link)
    if url.scheme != "https" or url.netloc not in {"drive.google.com", "docs.google.com"}:
        raise ValueError("drive_link_invalid")
    query = urllib.parse.parse_qs(url.query)
    match = re.fullmatch(r"/(?:file|document|spreadsheets|presentation)/(?:u/\d+/)?d/([a-zA-Z0-9_-]+)(?:/[^/]*)?/?", url.path)
    identifier = match[1] if match else query.get("id", [""])[0] if url.path in {"/open", "/uc"} else ""
    key = query.get("resourcekey", [""])[0]
    if not ID.fullmatch(identifier) or (key and not ID.fullmatch(key)):
        raise ValueError("drive_link_invalid")
    return identifier, key


def drive_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), desktop_auth.NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def browse(body):
    """Return one page of visible folders and compatible files."""
    allowed = {"location", "video", "folder_id", "drive_id", "resource_key", "cursor"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ValueError("drive_browser_invalid")
    location, video = body.get("location"), body.get("video")
    folder, drive, key, cursor = (body.get(name, "") for name in ("folder_id", "drive_id", "resource_key", "cursor"))
    if location not in ("my", "shared", "drives", "folder") or not isinstance(video, bool):
        raise ValueError("drive_browser_invalid")
    for value in (folder, drive, key):
        if not isinstance(value, str) or (value and not ID.fullmatch(value)):
            raise ValueError("drive_browser_invalid")
    if not isinstance(cursor, str) or len(cursor) > 3000:
        raise ValueError("drive_browser_invalid")
    if (location == "folder") != bool(folder) or (location != "folder" and (drive or key)):
        raise ValueError("drive_browser_invalid")

    media = {mime for mime in MIME_EXTENSIONS if mime.startswith("video/") == video}
    if not video:
        media.update(EXPORT)
    args = {"pageSize": 50}
    headers = {}
    if location == "drives":
        endpoint = DRIVES_API
        args["fields"] = "nextPageToken,drives(id,name)"
    else:
        endpoint = DRIVE_API.rstrip("/")
        within = "sharedWithMe" if location == "shared" else f"'{folder or 'root'}' in parents"
        kinds = " or ".join(f"mimeType = '{mime}'" for mime in sorted(media | {FOLDER_MIME, SHORTCUT_MIME}))
        args.update(q=f"trashed = false and {within} and ({kinds})", spaces="drive",
                    orderBy="folder,name_natural", supportsAllDrives="true", includeItemsFromAllDrives="true",
                    fields=("nextPageToken,incompleteSearch,files(id,name,mimeType,size,resourceKey,driveId,"
                            "capabilities(canDownload),shortcutDetails(targetId,targetMimeType,targetResourceKey))"))
        if drive:
            args.update(corpora="drive", driveId=drive)
        if key:
            headers["X-Goog-Drive-Resource-Keys"] = f"{folder}/{key}"
    if cursor:
        args["pageToken"] = cursor

    session = desktop_auth.drive_access()
    headers["Authorization"] = "Bearer " + session["token"]
    request = urllib.request.Request(endpoint + "?" + urllib.parse.urlencode(args), headers=headers)
    try:
        with drive_opener().open(request, timeout=20) as response:
            result = json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            raise ValueError("drive_access_denied") from None
        raise ValueError("drive_browse_failed") from None
    except (OSError, ValueError):
        raise ValueError("drive_browse_failed") from None
    if result.get("incompleteSearch"):
        raise ValueError("drive_browse_failed")
    if desktop_auth.drive_access()["principal_id"] != session["principal_id"]:
        raise ValueError("auth_identity_changed")

    entries = []
    for item in result.get("drives" if location == "drives" else "files", []):
        identifier, mime = item["id"], item.get("mimeType", FOLDER_MIME)
        resource = item.get("resourceKey") or ""
        team = identifier if location == "drives" else item.get("driveId") or drive
        shortcut = mime == SHORTCUT_MIME
        if shortcut:
            target = item.get("shortcutDetails") or {}
            identifier, mime = target.get("targetId", ""), target.get("targetMimeType")
            resource = target.get("targetResourceKey") or ""
            # A shortcut can point outside its own shared drive.
            team = ""
        if not ID.fullmatch(identifier) or any(value and not ID.fullmatch(value) for value in (resource, team)):
            continue
        if mime != FOLDER_MIME and mime not in media:
            continue
        size = int(item["size"]) if str(item.get("size", "")).isdigit() else None
        too_big = size is not None and size > (MAX_VIDEO_BYTES if video else MAX_ATTACHMENT_BYTES)
        selectable = shortcut or item.get("capabilities", {}).get("canDownload", False)
        entries.append({"id": identifier, "name": item["name"], "folder": mime == FOLDER_MIME,
                        "resource_key": resource, "drive_id": team, "size": size,
                        "disabled": mime != FOLDER_MIME and (too_big or not selectable)})
    return {"items": entries, "next_cursor": result.get("nextPageToken")}


def folder_videos(link):
    if not isinstance(link, str) or len(link) > 2000:
        raise ValueError("drive_folder_invalid")
    url = urllib.parse.urlsplit(link.strip())
    match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([a-zA-Z0-9_-]{5,200})/?", url.path)
    if url.scheme != "https" or url.netloc != "drive.google.com" or not match:
        raise ValueError("drive_folder_invalid")
    resource_key = urllib.parse.parse_qs(url.query).get("resourcekey", [""])[0]
    if resource_key and not ID.fullmatch(resource_key):
        raise ValueError("drive_folder_invalid")

    session = desktop_auth.drive_access()
    opener = drive_opener()
    headers = {"Authorization": "Bearer " + session["token"]}
    folder_mime = "application/vnd.google-apps.folder"

    def get(url, folder_id, key):
        request_headers = headers if not key else {**headers, "X-Goog-Drive-Resource-Keys": f"{folder_id}/{key}"}
        request = urllib.request.Request(url, headers=request_headers)
        with opener.open(request, timeout=20) as response:
            return json.loads(response.read(1024 * 1024))

    def collect(folder_id, key, parent):
        if folder_id in visited:
            return
        visited.add(folder_id)
        args = {"q": f"'{folder_id}' in parents and trashed = false",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "pageSize": 100,
                "fields": "nextPageToken,incompleteSearch,files(id,name,mimeType,size,resourceKey,capabilities(canDownload))"}
        while True:
            listing = get(DRIVE_API.rstrip("/") + "?" + urllib.parse.urlencode(args), folder_id, key)
            if listing.get("incompleteSearch"):
                raise ValueError("drive_folder_incomplete")
            for item in listing["files"]:
                identifier = item["id"]
                child_key = item.get("resourceKey") or ""
                if not ID.fullmatch(identifier) or (child_key and not ID.fullmatch(child_key)):
                    raise ValueError("drive_folder_invalid_response")
                if item["mimeType"] == folder_mime:
                    pending.append((identifier, child_key, [*parent, item["name"]]))
                elif item["mimeType"].startswith("video/"):
                    files.append({**item, "parents": parent})
            token = listing.get("nextPageToken")
            if not token:
                return
            args["pageToken"] = token

    root = match[1]
    pending, files, visited = [(root, resource_key, [])], [], set()
    try:
        metadata = get(DRIVE_API + root + "?fields=name,mimeType&supportsAllDrives=true", root, resource_key)
        if metadata.get("mimeType") != folder_mime:
            raise ValueError("drive_folder_invalid")
        while pending:
            collect(*pending.pop(0))
        if desktop_auth.drive_access()["principal_id"] != session["principal_id"]:
            raise ValueError("auth_identity_changed")
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            raise ValueError("drive_access_denied") from None
        raise ValueError("drive_folder_list_failed") from None
    return {"name": metadata["name"], "videos": sorted(files, key=lambda v: (v["parents"], v["name"].casefold()))}


def download(link, video, root):
    if not isinstance(video, bool):
        raise ValueError("drive_link_invalid")
    identifier, key = parse_link(link)
    session = desktop_auth.drive_access()
    headers = {"Authorization": "Bearer " + session["token"]}
    if key:
        headers["X-Goog-Drive-Resource-Keys"] = identifier + "/" + key
    opener = drive_opener()
    service = DRIVE_API + identifier

    def fetch(url):
        return opener.open(urllib.request.Request(url, headers=headers), timeout=20)

    try:
        fields = urllib.parse.urlencode({"supportsAllDrives": "true", "fields": "name,mimeType,size,capabilities(canDownload)"})
        with fetch(service + "?" + fields) as response:
            metadata = json.loads(response.read(64 * 1024))
        if not metadata.get("capabilities", {}).get("canDownload"):
            raise ValueError("drive_download_denied")
        mime = metadata.get("mimeType", "")
        export = mime in EXPORT
        extension = ".pdf" if export else MIME_EXTENSIONS.get(mime)
        if not extension or mime.startswith("video/") != video:
            raise ValueError("drive_type_unsupported")
        limit = MAX_VIDEO_BYTES if video else MAX_ATTACHMENT_BYTES
        if int(metadata.get("size", 0)) > limit:
            raise ValueError("drive_file_too_large")
        name = re.sub(r"[^\w .()-]", "_", str(metadata.get("name") or "")).strip(" .")[:100] or "Google Drive"
        if not name.lower().endswith(extension):
            name += extension
        if shutil.disk_usage(root).free < int(metadata.get("size", 0)) + 50 * 1024**2:
            raise ValueError("drive_disk_space")
        resource = service + ("/export?mimeType=application%2Fpdf" if export else "?alt=media&supportsAllDrives=true")
        deadline = time.monotonic() + 270
        file_id = str(uuid4())
        with TemporaryDirectory(dir=root, prefix="media-copy-") as directory, fetch(resource) as response:
            folder = Path(directory)
            size = 0
            with (folder / name).open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("drive_file_too_large")
                    if time.monotonic() > deadline:
                        raise ValueError("drive_download_timeout")
                    output.write(chunk)
            if not size or (not export and metadata.get("size") and size != int(metadata["size"])):
                raise ValueError("drive_download_incomplete")
            if desktop_auth.drive_access()["principal_id"] != session["principal_id"]:
                raise ValueError("auth_identity_changed")
            (folder / "capture-owner.json").write_text(json.dumps({
                "version": 1, "capture_id": file_id, "identity": session["principal_id"]}))
            folder.rename(root / ("media-draft-" + file_id))
        return {"id": file_id, "name": name, "video": video}
    except urllib.error.HTTPError as error:
        if error.code in {401, 403, 404}:
            raise ValueError("drive_access_denied") from None
        raise ValueError("drive_download_failed") from None
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError) and str(error).startswith(("drive_", "auth_")):
            raise
        raise ValueError("drive_download_failed") from None
