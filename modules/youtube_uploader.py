"""YouTube upload: OAuth (loopback, headless-friendly), automatic token refresh, resumable upload
with retry, private + scheduled publishing. Google libraries are imported lazily so the rest of
the app works without them."""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from .utilities import ClipperError, ConfigError, redact

log = logging.getLogger("clipper")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRYABLE_STATUS = {500, 502, 503, 504}
REDIRECT_URI = "http://localhost:8080/"


class UploadError(ClipperError):
    pass


def _google():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        raise ConfigError("Google libraries missing. Run: pip install google-api-python-client "
                          "google-auth google-auth-oauthlib") from e
    return Request, Credentials, InstalledAppFlow, build, MediaFileUpload


def read_client_secrets(path: str) -> dict:
    if not os.path.isfile(path):
        raise ConfigError(f"YOUTUBE_CLIENT_SECRETS file not found: {path}. Download an OAuth 'Desktop app' "
                          f"client JSON from Google Cloud Console and point YOUTUBE_CLIENT_SECRETS at it.")
    try:
        data = json.load(open(path, encoding="utf-8"))
    except ValueError as e:
        raise ConfigError(f"{path} is not valid JSON") from e
    cfg = data.get("installed") or data.get("web")
    if not cfg or not cfg.get("client_id") or not cfg.get("client_secret"):
        raise ConfigError(f"{path} has no installed/web client_id and client_secret")
    return cfg


def save_token(creds, path: str) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.replace(tmp, path)


def authorize(cfg):
    """Headless-friendly flow: print a URL, open it in any browser, then paste the address the
    browser ends up on (it will fail to load - that is expected)."""
    Request, Credentials, InstalledAppFlow, *_ = _google()
    read_client_secrets(cfg.youtube_client_secrets)
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")   # loopback http redirect
    flow = InstalledAppFlow.from_client_secrets_file(cfg.youtube_client_secrets, SCOPES)
    flow.redirect_uri = REDIRECT_URI
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("\nOpen this URL, approve access, then copy the FULL address of the page you land on")
    print("(it will say 'site can't be reached' - that's fine):\n")
    print(url + "\n")
    redirected = input("Paste the full address here: ").strip()
    flow.fetch_token(authorization_response=redirected)
    save_token(flow.credentials, cfg.youtube_token_file)
    log.info("YouTube authorisation saved to %s", cfg.youtube_token_file)
    return flow.credentials


def load_credentials(cfg, interactive: bool = True):
    Request, Credentials, *_ = _google()
    secrets = read_client_secrets(cfg.youtube_client_secrets)
    creds, legacy = None, False
    if os.path.isfile(cfg.youtube_token_file):
        try:
            info = json.load(open(cfg.youtube_token_file, encoding="utf-8"))
        except ValueError:
            info = {}
        if info.get("client_id"):
            creds = Credentials.from_authorized_user_info(info, SCOPES)
        elif info.get("refresh_token"):   # token.json written by the old script
            legacy = True
            creds = Credentials(token=info.get("access_token"), refresh_token=info["refresh_token"],
                                token_uri="https://oauth2.googleapis.com/token",
                                client_id=secrets["client_id"], client_secret=secrets["client_secret"],
                                scopes=SCOPES)
    if creds is not None:
        if creds.refresh_token and (legacy or not creds.valid):
            try:
                creds.refresh(Request())
                save_token(creds, cfg.youtube_token_file)
                log.info("YouTube token refreshed")
                return creds
            except Exception as e:
                log.warning("Token refresh failed (%s); re-authorisation needed", type(e).__name__)
        elif creds.valid:
            return creds
    if not interactive:
        raise UploadError("YouTube is not authorised. Run: python agent.py --auth")
    return authorize(cfg)


def build_service(creds):
    *_, build, _m = _google()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def schedule_times(n: int, start_delay_min: int, interval_hours: float,
                   now: Optional[datetime] = None) -> list[str]:
    base = (now or datetime.now(timezone.utc)) + timedelta(minutes=start_delay_min)
    return [(base + timedelta(hours=interval_hours * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def build_body(meta: dict, publish_at: Optional[str], privacy: str = "private",
               title_suffix: str = "#Shorts") -> dict:
    yt = meta["platforms"]["youtube"]
    title = yt["title"].strip()
    if title_suffix and title_suffix.lower() not in title.lower():
        title = title[:max(10, 98 - len(title_suffix))].rstrip() + " " + title_suffix
    status = {"privacyStatus": privacy if privacy in ("private", "public", "unlisted") else "private",
              "selfDeclaredMadeForKids": False}
    if publish_at and status["privacyStatus"] == "private":
        status["publishAt"] = publish_at
    tags, total = [], 0
    for t in yt["tags"]:
        if total + len(t) + 1 > 480:
            break
        tags.append(t)
        total += len(t) + 1
    return {"snippet": {"title": title, "description": yt["description"][:4900], "tags": tags,
                        "categoryId": yt.get("categoryId", "22")}, "status": status}


def _retryable(e: Exception) -> bool:
    status = getattr(getattr(e, "resp", None), "status", None) or getattr(e, "status_code", None)
    if status is not None:
        return int(status) in RETRYABLE_STATUS
    import http.client
    import socket
    return isinstance(e, (ConnectionError, TimeoutError, socket.timeout, http.client.HTTPException))


def upload_video(service, path: str, meta: dict, publish_at: Optional[str], *, retries: int = 5,
                 sleep: Callable = time.sleep, media_factory: Optional[Callable] = None,
                 privacy: str = "private", title_suffix: str = "#Shorts") -> str:
    """Resumable upload. Returns the YouTube video id; raises UploadError otherwise."""
    if media_factory is None:
        *_, MediaFileUpload = _google()
        media_factory = lambda p: MediaFileUpload(p, mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=build_body(meta, publish_at, privacy, title_suffix),
                                      media_body=media_factory(path))
    response, attempt = None, 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                log.info("upload %d%%", int(status.progress() * 100))
            attempt = 0
        except Exception as e:
            if not _retryable(e) or attempt >= retries:
                raise UploadError(redact(f"{type(e).__name__}: {e}")[:400]) from e
            attempt += 1
            delay = min(2 ** attempt, 60) + random.random()
            log.warning("temporary upload error (%s); retry %d/%d in %.0fs", type(e).__name__, attempt, retries, delay)
            sleep(delay)
    vid = response.get("id") if isinstance(response, dict) else None
    if not vid:
        raise UploadError("YouTube did not return a video id; treating the upload as failed")
    return vid


# ------------------------------------------------------------------ web sign-in (used by the local UI)
def web_flow(secrets_path: str, redirect_uri: str):
    """Returns (flow, authorization_url). The redirect goes to the local server, so no copy/paste is needed."""
    read_client_secrets(secrets_path)
    _google()
    from google_auth_oauthlib.flow import Flow
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")      # loopback http redirect
    flow = Flow.from_client_secrets_file(secrets_path, scopes=SCOPES, redirect_uri=redirect_uri)
    url, _state = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, url


def web_flow_finish(flow, code: str, token_file: str) -> None:
    flow.fetch_token(code=code)
    os.makedirs(os.path.dirname(os.path.abspath(token_file)), exist_ok=True)
    save_token(flow.credentials, token_file)
