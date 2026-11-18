"""Local web app: stdlib HTTP server + JSON API + background worker. No third-party web framework.

Security model: binds to 127.0.0.1 by default; Host header allow-list (DNS-rebinding guard); mutating requests
need a custom header (CSRF guard); optional password (required when listening on the network); secrets never
leave the server; every log line passes through the redaction filter.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from . import caption_lang, captions, content_generator, pipeline, settings, templates, video_editor, youtube_uploader
from .scheduling import Scheduler, from_iso, to_iso
from .store import DBJobState, Store
from .utilities import (CANCEL_EVENT, Cancelled, ClipperError, Config, ConfigError, RedactFilter, ffmpeg_filters,
                        file_ok, redact, run, safe_remove)

log = logging.getLogger("clipper")
VERSION = "2.0"
MAX_JSON = 2 * 1024 * 1024
UPLOAD_RULES = {"music": ({".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}, 40 * 1024 * 1024),
                "logo": ({".png", ".jpg", ".jpeg", ".webp"}, 5 * 1024 * 1024),
                "font": ({".ttf", ".otf"}, 15 * 1024 * 1024)}
SESSION_SECONDS = 30 * 24 * 3600
MAX_DRAIN = 64 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ============================================================================ helpers
def hash_password(password: str, salt: Optional[bytes] = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return {"salt": salt.hex(), "hash": h.hex()}


def check_password(password: str, rec: dict) -> bool:
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(rec["salt"]), 200_000)
    return hmac.compare_digest(h.hex(), rec["hash"])


def font_family(path: str) -> str:
    """Family name from a .ttf/.otf name table (no dependencies). Empty string if unreadable."""
    try:
        data = open(path, "rb").read(4_000_000)
        base = 0
        if data[:4] == b"ttcf":
            base = struct.unpack(">I", data[12:16])[0]
        num = struct.unpack(">H", data[base + 4:base + 6])[0]
        name_off = None
        for i in range(num):
            tag, _, off, _l = struct.unpack(">4sIII", data[base + 12 + i * 16: base + 28 + i * 16])
            if tag == b"name":
                name_off = off
                break
        if name_off is None:
            return ""
        count, str_off = struct.unpack(">HH", data[name_off + 2:name_off + 6])
        found = {}
        for i in range(count):
            pid, eid, _lang, nid, ln, off = struct.unpack(">HHHHHH", data[name_off + 6 + i * 12:name_off + 18 + i * 12])
            if nid not in (1, 16):
                continue
            raw = data[name_off + str_off + off: name_off + str_off + off + ln]
            txt = raw.decode("utf-16-be", "ignore") if pid in (0, 3) else raw.decode("latin-1", "ignore")
            if txt.strip():
                found.setdefault(nid, txt.strip())
        return found.get(16) or found.get(1, "")
    except Exception:
        return ""


def safe_name(name: str, exts: set) -> str:
    name = os.path.basename(urllib.parse.unquote(name or "")).strip()
    name = re.sub(r"[^\w.\- ]", "_", name)[:80]
    if not name or name.startswith(".") or Path(name).suffix.lower() not in exts:
        raise HttpError(400, f"File type not allowed. Use: {', '.join(sorted(exts))}")
    return name


def parse_publish_at(value) -> str:
    if value in (None, ""):
        return ""
    try:
        t = from_iso(str(value))
    except ValueError:
        raise HttpError(400, "Publish time must look like 2026-12-01T12:30:00Z") from None
    if t <= datetime.now(timezone.utc):
        raise HttpError(400, "Publish time must be in the future")
    return to_iso(t)


def compute_status(data: dict) -> str:
    clips = data.get("clips", {})
    plan = data.get("plan", [])
    sts = [clips.get(c["id"], {}).get("status", "planned") for c in plan]
    if plan and all(s in ("uploaded", "discarded") for s in sts):
        return "done"
    if any(s in ("rendered", "upload_failed") for s in sts):
        return "review"
    if any(s == "render_failed" for s in sts):
        return "failed"
    return "review" if plan else "failed"


class JobLogHandler(logging.Handler):
    def __init__(self, app: "App"):
        super().__init__(logging.INFO)
        self.app = app
        self.addFilter(RedactFilter())

    def emit(self, record):
        jid = self.app.current_job_id
        if jid is None:
            return
        try:
            self.app.store.add_log(jid, record.levelname, record.getMessage())
        except Exception:
            pass


# ============================================================================ app + worker
class App:
    def __init__(self, data_dir: str, ui_dir: str, host: str = "127.0.0.1", port: int = 8000):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.ui_dir = ui_dir
        self.host, self.port = host, port
        self.store = Store(os.path.join(self.data_dir, "clipper.db"))
        self.sessions: dict[str, float] = {}
        self.flows: dict[str, tuple] = {}
        self.fail_times: list[float] = []
        self.current_job_id: Optional[int] = None
        self.stopping = False
        self.wake = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.preview_lock = threading.Lock()
        CANCEL_EVENT.clear()
        self.log_handler = JobLogHandler(self)
        log.addHandler(self.log_handler)
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}", "localhost", "127.0.0.1"}
        for h in os.environ.get("CLIPPER_ALLOWED_HOSTS", "").split(","):
            if h.strip():
                self.allowed_hosts.add(h.strip())
        self.lan = host not in ("127.0.0.1", "localhost", "::1")
        self.seed_accounts()
        n = self.store.recover_tasks()
        if n:
            log.info("Resuming %d unfinished task(s) from the last run", n)

    # ---- accounts
    def seed_accounts(self) -> None:
        if not self.store.accounts():
            env_token = os.path.expanduser(os.environ.get("YOUTUBE_TOKEN_FILE", "token.json"))
            self.store.add_account("Default", os.path.abspath(env_token))

    def account_for(self, options: dict) -> Optional[dict]:
        aid = options.get("account")
        acct = self.store.account(int(aid)) if str(aid or "").isdigit() else None
        return acct or (self.store.accounts() or [None])[0]

    @staticmethod
    def account_view(a: dict) -> dict:
        authorized = False
        try:
            authorized = bool(json.load(open(a["token_file"], encoding="utf-8")).get("refresh_token"))
        except (OSError, ValueError):
            pass
        return {"id": a["id"], "name": a["name"], "authorized": authorized}

    # ---- config for a job
    def cfg_for(self, job: dict) -> Config:
        overrides = self.store.get_overrides()
        cfg = settings.build_config(overrides, job["options"])
        if "keep_raw" not in overrides and "keep_raw" not in job["options"]:
            cfg.keep_raw = True
        mode = job["options"].get("mode")
        if mode in ("auto", "review"):
            cfg.auto_upload = mode == "auto"
        acct = self.account_for(job["options"])
        if acct:
            cfg.youtube_token_file = acct["token_file"]
        return cfg

    def make_ctx(self, job: dict) -> pipeline.Ctx:
        cfg = self.cfg_for(job)
        jid = job["id"]
        acct = self.account_for(job["options"])
        aid = acct["id"] if acct else None
        last = self.store.last_scheduled(aid, to_iso(datetime.now(timezone.utc)))

        def progress(stage, pct, msg=""):
            self.store.update_job(jid, stage=stage, progress=round(float(pct), 1), message=msg)

        def check_cancel():
            if CANCEL_EVENT.is_set():
                raise Cancelled("cancelled")

        def on_info(info):
            self.store.update_job(jid, title=info.get("title", ""), video_id=info.get("id", ""))

        def on_uploaded(cid, when, vid):
            if when:
                self.store.add_scheduled(aid, when, jid, cid, vid)

        return pipeline.Ctx(url=job["url"], cfg=cfg, state=DBJobState(self.store, jid), tag=f"j{jid}_",
                            progress=progress, check_cancel=check_cancel, scheduler=Scheduler(cfg, last),
                            on_uploaded=on_uploaded, on_info=on_info, interactive_auth=False,
                            fresh=bool(job["options"].get("fresh")))

    # ---- worker
    def start_worker(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        CANCEL_EVENT.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=15)
        CANCEL_EVENT.clear()                  # the flag was only for stopping our own job; do not leave it set
        log.removeHandler(self.log_handler)

    def _loop(self) -> None:
        while not self.stopping:
            task = self.store.claim_task()
            if not task:
                self.wake.wait(2.0)
                self.wake.clear()
                continue
            self.run_task(task)

    def run_task(self, t: dict) -> None:
        jid, cid, kind = t["job_id"], t["clip_id"], t["kind"]
        job = self.store.job(jid)
        if not job:
            self.store.finish_task(t["id"], "cancelled")
            return
        CANCEL_EVENT.clear()
        self.current_job_id = jid
        self.store.update_job(jid, status="running", error="")
        status, err = "done", ""
        try:
            ctx = self.make_ctx(job)
            if kind == "pipeline":
                self.store.update_job(jid, options={k: v for k, v in job["options"].items() if k != "fresh"})
                res = pipeline.run_job(ctx)
                log.info("Job finished: rendered %(rendered)d, uploaded %(uploaded)d of %(total)d", res)
            elif kind in ("rerender", "recaption"):
                pipeline.rerender(ctx, cid, regenerate_captions=(kind == "recaption"))
            elif kind == "upload":
                pipeline.upload_existing(ctx, cid)
            else:
                raise ClipperError(f"unknown task {kind}")
        except Cancelled:
            status, err = "cancelled", ""
            log.info("Cancelled")
        except Exception as e:
            status, err = "failed", redact(e)[:500]
            if isinstance(e, ClipperError):
                log.error("%s", redact(e))
            else:
                log.error("Unexpected error: %s", redact(traceback.format_exc()[-1200:]))
        finally:
            self.current_job_id = None
        if self.stopping and status == "cancelled":
            return                                      # shutdown: leave the task 'running' so it resumes next start
        self.store.finish_task(t["id"], "done" if status == "done" else status, err)
        fresh = self.store.job(jid)
        if not fresh:
            return
        if status == "cancelled" and kind == "pipeline":
            self.store.update_job(jid, status="cancelled", message="Cancelled", progress=fresh["progress"])
            return
        if status == "failed" and kind == "pipeline":
            self.store.update_job(jid, status="failed", error=err, message="Failed")
            return
        new = compute_status(fresh["data"])
        self.store.update_job(jid, status=new, stage="", message="", progress=100 if new == "done" else fresh["progress"],
                              error=err if status == "failed" else "")

    def enqueue(self, kind: str, jid: int, cid: str = "") -> int:
        tid = self.store.add_task(kind, jid, cid)
        self.wake.set()
        return tid

    # ---- views
    def job_view(self, jid: int) -> dict:
        job = self.store.job(jid)
        if not job:
            raise HttpError(404, "Job not found")
        data = job["data"]
        clips = []
        try:
            cfg = self.cfg_for(job)
            base = {"template": cfg.template, "caption_mode": "off" if not cfg.caption_enabled else cfg.caption_mode,
                    "tweet_name": cfg.tweet_name, "tweet_handle": cfg.tweet_handle,
                    "tweet_avatar": next((os.path.basename(p) for p in (cfg.tweet_avatar, cfg.logo_file) if p and os.path.isfile(p)), "")}
        except ConfigError:
            base = {"template": "blur", "caption_mode": "words", "tweet_name": "", "tweet_handle": "", "tweet_avatar": ""}
        for c in data.get("plan", []):
            cs = data["clips"].get(c["id"], {})
            f = cs.get("file") or ""
            m = cs.get("meta") or {}
            title = m.get("title") or c.get("title") or ""
            clips.append({**c, "look": {**(cs.get("look") or {}), **({"tweet_avatar": os.path.basename(cs["look"]["tweet_avatar"])} if (cs.get("look") or {}).get("tweet_avatar") else {})}, "look_base": base,
                          "look_defaults": {"tweet_text": title, "caption_text": m.get("cover_text") or c.get("hook") or title}, "status": cs.get("status", "planned"), "error": cs.get("error", ""),
                          "meta": cs.get("meta"), "captions": cs.get("captions", []), "caption_notes": cs.get("caption_notes", []),
                          "publish_at": cs.get("publish_at"), "youtube_id": cs.get("youtube_id"), "mode": cs.get("mode"),
                          "video_url": f"/media/{jid}/{c['id']}.mp4?v={int(os.path.getmtime(f))}" if f and os.path.isfile(f) else None})
        return {"id": jid, "url": job["url"], "title": job["title"], "status": job["status"], "stage": job["stage"],
                "progress": job["progress"], "message": job["message"], "error": job["error"], "options": job["options"],
                "created": job["created"], "updated": job["updated"], "info": data.get("info"),
                "language": data.get("language"), "engine": data.get("transcript_engine"), "clips": clips,
                "busy": bool(self.store.pending_tasks(jid))}

    def remove_job_files(self, job: dict) -> None:
        data = job["data"]
        info = data.get("info")
        if not info:
            return
        cfg = self.cfg_for(job)
        ctx = pipeline.Ctx(url=job["url"], cfg=cfg, state=DBJobState(self.store, job["id"]), tag=f"j{job['id']}_")
        for c in data.get("plan", []):
            final, work = pipeline.clip_paths(ctx, info, c["id"])
            safe_remove(final, work)


# ============================================================================ request handling
class Req:
    def __init__(self, h: "Handler", method: str, parsed):
        self.h, self.method = h, method
        self.path = parsed.path
        self.q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        self._body = None
        self.body_read = False

    @property
    def app(self) -> App:
        return self.h.server.app

    def json(self) -> dict:
        if self._body is None:
            n = int(self.h.headers.get("Content-Length") or 0)
            if n > MAX_JSON:
                raise HttpError(413, "Request too large")
            self.body_read = True
            raw = self.h.rfile.read(n) if n else b""
            try:
                self._body = json.loads(raw.decode() or "{}")
            except ValueError:
                raise HttpError(400, "Body must be JSON") from None
            if not isinstance(self._body, dict):
                raise HttpError(400, "Body must be a JSON object")
        return self._body


ROUTES: list[tuple[str, re.Pattern, Callable, bool]] = []


def route(method: str, pattern: str, public: bool = False):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, public))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "Clipper/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # quiet; errors are logged by us
        pass

    # ---- plumbing
    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                         "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, obj, extra=None) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def authed(self) -> bool:
        app = self.server.app
        if not app.store.kv_get("auth"):
            return True
        m = re.search(r"clipper_session=([\w\-]+)", self.headers.get("Cookie", ""))
        exp = app.sessions.get(m.group(1)) if m else None
        return bool(exp and exp > time.time())

    def dispatch(self, method: str) -> None:
        app: App = self.server.app
        parsed = urllib.parse.urlparse(self.path)
        req = Req(self, method, parsed)
        try:
            host = (self.headers.get("Host") or "").lower()
            if not app.lan and host not in app.allowed_hosts:
                raise HttpError(403, "Unexpected Host header")
            if method in ("POST", "PUT", "DELETE") and self.headers.get("X-Clipper") != "1":
                raise HttpError(403, "Missing X-Clipper header")
            if method == "GET" and (parsed.path == "/" or parsed.path.startswith("/ui/")):
                return self.serve_static(parsed.path)
            for m, rx, fn, public in ROUTES:
                if m != method:
                    continue
                mo = rx.match(parsed.path)
                if not mo:
                    continue
                if not public and not self.authed():
                    raise HttpError(401, "Sign in required")
                out = fn(req, *mo.groups())
                if out is not None:                                   # handlers may write the response themselves
                    status, obj = out
                    self.send_json(status, obj)
                return
            raise HttpError(404, "Not found")
        except HttpError as e:
            self.send_json(e.status, {"error": e.message})
        except Cancelled:
            self.send_json(409, {"error": "Cancelled"})
        except ClipperError as e:
            self.send_json(400, {"error": redact(e)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            log.error("Server error on %s %s: %s", method, parsed.path, redact(traceback.format_exc()[-1500:]))
            try:
                self.send_json(500, {"error": "Something went wrong on the server. See the log for details."})
            except Exception:
                pass
        finally:
            self.drain(req)

    def drain(self, req: "Req") -> None:
        """Read and discard a request body the handler did not use, so the next keep-alive request stays in sync
        (and a browser sending a too-large file still receives our error message)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if req.body_read or n <= 0:
                return
            if n > MAX_DRAIN:
                self.close_connection = True
                return
            left = n
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except Exception:
            self.close_connection = True

    def do_GET(self): self.dispatch("GET")
    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_DELETE(self): self.dispatch("DELETE")

    def serve_static(self, path: str) -> None:
        name = "index.html" if path == "/" else path[len("/ui/"):]
        if name not in ("index.html", "app.js", "style.css", "icon.svg"):
            raise HttpError(404, "Not found")
        p = os.path.join(self.server.app.ui_dir, name)
        if not os.path.isfile(p):
            raise HttpError(404, "UI files not found")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        self._send(200, open(p, "rb").read(), ctype + ("; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else ""))

    def send_file_range(self, path: str, ctype: str) -> None:
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, app: App):
        self.app = app
        super().__init__(addr, Handler)


# ============================================================================ routes: auth / status
@route("GET", "/api/auth/status", public=True)
def r_auth_status(req):
    app = req.app
    return 200, {"password_set": bool(app.store.kv_get("auth")), "authed": req.h.authed()}


@route("POST", "/api/auth/login", public=True)
def r_login(req):
    app = req.app
    rec = app.store.kv_get("auth")
    if not rec:
        return 200, {"ok": True}
    now = time.time()
    app.fail_times = [t for t in app.fail_times if now - t < 60]
    if len(app.fail_times) >= 5:
        raise HttpError(429, "Too many wrong passwords. Wait a minute and try again.")
    if not check_password(str(req.json().get("password", "")), rec):
        app.fail_times.append(now)
        raise HttpError(401, "Wrong password")
    token = secrets.token_urlsafe(32)
    app.sessions[token] = now + SESSION_SECONDS
    req.h.send_json(200, {"ok": True}, {"Set-Cookie": f"clipper_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"})


@route("POST", "/api/auth/logout")
def r_logout(req):
    m = re.search(r"clipper_session=([\w\-]+)", req.h.headers.get("Cookie", ""))
    if m:
        req.app.sessions.pop(m.group(1), None)
    req.h.send_json(200, {"ok": True}, {"Set-Cookie": "clipper_session=; Path=/; Max-Age=0"})


@route("POST", "/api/auth/password")
def r_set_password(req):
    app, body = req.app, req.json()
    rec = app.store.kv_get("auth")
    if rec and not check_password(str(body.get("current", "")), rec):
        raise HttpError(403, "Current password is wrong")
    new = str(body.get("password", ""))
    if not new:
        app.store.kv_del("auth")
        app.sessions.clear()
        return 200, {"password_set": False}
    if len(new) < 6:
        raise HttpError(400, "Use at least 6 characters")
    app.store.kv_set("auth", hash_password(new))
    app.sessions.clear()
    token = secrets.token_urlsafe(32)
    app.sessions[token] = time.time() + SESSION_SECONDS
    req.h.send_json(200, {"password_set": True}, {"Set-Cookie": f"clipper_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_SECONDS}"})


@route("GET", "/api/status")
def r_status(req):
    app = req.app
    cfg = settings.build_config(app.store.get_overrides())
    free = shutil.disk_usage(app.data_dir).free // (1024 * 1024)
    return 200, {"version": VERSION, "queue": len(app.store.pending_tasks()), "gemini_key": bool(cfg.gemini_api_key),
                 "groq_key": bool(cfg.groq_api_key), "youtube_secrets": os.path.isfile(cfg.youtube_client_secrets),
                 "accounts": [app.account_view(a) for a in app.store.accounts()], "disk_free_mb": free,
                 "lan": app.lan}


# ============================================================================ routes: settings
@route("GET", "/api/schema")
def r_schema(req):
    return 200, {"schema": settings.schema_json(), "categories": list(content_generator.CATEGORY_IDS)}


@route("GET", "/api/settings")
def r_get_settings(req):
    ov = req.app.store.get_overrides()
    return 200, {"overrides": ov, "values": settings.effective_values(ov), "defaults": settings.effective_values({})}


@route("PUT", "/api/settings")
def r_put_settings(req):
    ov = settings.clean_overrides(req.json().get("overrides", {}))
    settings.build_config(ov)                      # validates the combination; raises ConfigError -> 400
    req.app.store.set_overrides(ov)
    return 200, {"overrides": ov, "values": settings.effective_values(ov)}


@route("POST", "/api/settings/reset")
def r_reset_settings(req):
    keys = req.json().get("keys")
    ov = {} if keys is None else {k: v for k, v in req.app.store.get_overrides().items() if k not in keys}
    req.app.store.set_overrides(ov)
    return 200, {"overrides": ov, "values": settings.effective_values(ov)}


@route("GET", "/api/presets")
def r_presets(req):
    return 200, {"presets": req.app.store.presets()}


@route("POST", "/api/presets")
def r_save_preset(req):
    name = str(req.json().get("name", "")).strip()[:40]
    if not name:
        raise HttpError(400, "Give the preset a name")
    req.app.store.save_preset(name, req.app.store.get_overrides())
    return 200, {"presets": req.app.store.presets()}


@route("POST", "/api/presets/apply")
def r_apply_preset(req):
    name = str(req.json().get("name", ""))
    p = req.app.store.presets().get(name)
    if p is None:
        raise HttpError(404, "Preset not found")
    ov = settings.clean_overrides(p)
    settings.build_config(ov)
    req.app.store.set_overrides(ov)
    return 200, {"overrides": ov, "values": settings.effective_values(ov)}


@route("DELETE", "/api/presets/([^/]+)")
def r_del_preset(req, name):
    if not req.app.store.delete_preset(urllib.parse.unquote(name)):
        raise HttpError(404, "Preset not found")
    return 200, {"presets": req.app.store.presets()}


# ============================================================================ routes: jobs
YT_URL = re.compile(r"^https?://([\w\-]+\.)?(youtube\.com|youtu\.be)/\S+$", re.I)


@route("GET", "/api/jobs")
def r_jobs(req):
    return 200, {"jobs": req.app.store.jobs()}


@route("POST", "/api/jobs")
def r_create_job(req):
    body = req.json()
    url = str(body.get("url", "")).strip()
    if not YT_URL.match(url):
        raise HttpError(400, "Paste a YouTube link (youtube.com or youtu.be)")
    opts = settings.clean_overrides(body.get("options", {}), allow_job=True)
    if opts.get("mode") not in (None, "auto", "review"):
        raise HttpError(400, "Mode must be auto or review")
    settings.build_config(req.app.store.get_overrides(), opts)          # fail now, not after queueing
    jid = req.app.store.create_job(url, opts)
    req.app.enqueue("pipeline", jid)
    return 201, {"id": jid}


@route("GET", r"/api/jobs/(\d+)")
def r_job(req, jid):
    return 200, req.app.job_view(int(jid))


@route("GET", r"/api/jobs/(\d+)/log")
def r_job_log(req, jid):
    since = int(req.q.get("since", 0) or 0)
    return 200, {"lines": req.app.store.logs(int(jid), since)}


@route("POST", r"/api/jobs/(\d+)/cancel")
def r_cancel(req, jid):
    app, jid = req.app, int(jid)
    running = app.current_job_id == jid
    app.store.cancel_queued(jid)
    if running:
        CANCEL_EVENT.set()
    else:
        job = app.store.job(jid)
        if job and job["status"] == "queued":
            app.store.update_job(jid, status="cancelled", message="Cancelled")
    return 200, {"ok": True}


@route("POST", r"/api/jobs/(\d+)/retry")
def r_retry(req, jid):
    app, jid = req.app, int(jid)
    if not app.store.job(jid):
        raise HttpError(404, "Job not found")
    if app.store.pending_tasks(jid):
        raise HttpError(409, "This job is already running")
    app.store.update_job(jid, status="queued", error="", message="", progress=0)
    app.enqueue("pipeline", jid)
    return 200, {"ok": True}


@route("DELETE", r"/api/jobs/(\d+)")
def r_delete_job(req, jid):
    app, jid = req.app, int(jid)
    job = app.store.job(jid)
    if not job:
        raise HttpError(404, "Job not found")
    if app.store.pending_tasks(jid):
        raise HttpError(409, "Cancel the job before deleting it")
    app.remove_job_files(job)
    app.store.delete_job(jid)
    return 200, {"ok": True}


def get_clip(req, jid: str, cid: str):
    app = req.app
    job = app.store.job(int(jid))
    if not job:
        raise HttpError(404, "Job not found")
    if not any(c["id"] == cid for c in job["data"].get("plan", [])):
        raise HttpError(404, "Clip not found")
    return job, DBJobState(app.store, int(jid))


@route("PUT", r"/api/jobs/(\d+)/clips/(clip\d+)")
def r_edit_clip(req, jid, cid):
    app, body = req.app, req.json()
    job, state = get_clip(req, jid, cid)
    cs = state.clip(cid)
    cfg = app.cfg_for(job)
    if "meta" in body and isinstance(body["meta"], dict) and cs.get("meta"):
        meta = cs["meta"]
        m = body["meta"]
        for k in ("title", "description", "category"):
            if k in m:
                meta[k] = str(m[k])
        if "hashtags" in m:
            meta["hashtags"] = m["hashtags"]
        info = state.data.get("info", {})
        content_generator.rebuild(meta, info, cfg.extra_tags, cfg.description_footer, cfg.credit_source)
        state.update_clip(cid, meta=meta)
    if "captions" in body and isinstance(body["captions"], dict) and cs.get("captions"):
        phrases = caption_lang.apply_edits(cs["captions"], body["captions"])
        state.update_clip(cid, captions=phrases)
    if "look" in body and isinstance(body["look"], dict):
        look = dict(cs.get("look") or {})
        for k, v in body["look"].items():
            v = str(v or "").strip()
            if k == "template" and v not in ("", *templates.TEMPLATES):
                raise HttpError(400, "Template must be blur, tweet or black")
            if k == "caption_mode" and v not in ("", *pipeline.CAPTION_MODES):
                raise HttpError(400, "Captions must be words, single or off")
            if k not in ("template", "caption_mode", "tweet_text", "caption_text", "tweet_name", "tweet_handle", "tweet_avatar"):
                raise HttpError(400, f"Unknown look setting {k}")
            if k in ("tweet_name", "tweet_handle") and len(v) > 40:
                raise HttpError(400, "Name and handle are 40 characters at most")
            if len(v) > 200:
                raise HttpError(400, "Text is too long (200 characters at most)")
            if k == "tweet_avatar" and v:
                v = os.path.join(asset_dir(req.app, "logo"), safe_name(v, UPLOAD_RULES["logo"][0]))
                if not os.path.isfile(v):
                    raise HttpError(400, "Pick one of your uploaded pictures")
            if k == "tweet_handle" and v and not v.startswith("@"):
                v = "@" + v
            if v:
                look[k] = v
            else:
                look.pop(k, None)
        state.update_clip(cid, look=look)
    if "publish_at" in body:
        state.update_clip(cid, publish_at=parse_publish_at(body["publish_at"]) or None)
    return 200, {"ok": True}


def queue_clip_task(req, jid, cid, kind, status=None):
    app = req.app
    job, state = get_clip(req, jid, cid)
    if any(t["clip_id"] == cid and t["job_id"] == int(jid) for t in app.store.pending_tasks(int(jid))):
        raise HttpError(409, "This clip is already being processed")
    cs = state.clip(cid)
    if kind == "upload" and not (cs.get("meta") and cs.get("file") and os.path.isfile(cs["file"])):
        raise HttpError(409, "Render the clip first")
    if status:
        state.update_clip(cid, status=status, error="")
    app.store.update_job(int(jid), status="running")
    app.enqueue(kind, int(jid), cid)
    return 202, {"queued": True}


@route("POST", r"/api/jobs/(\d+)/clips/(clip\d+)/rerender")
def r_rerender(req, jid, cid):
    kind = "recaption" if req.json().get("regenerate_captions") else "rerender"
    return queue_clip_task(req, jid, cid, kind)


@route("POST", r"/api/jobs/(\d+)/rerender-all")
def r_rerender_all(req, jid):
    """Render every clip of this video again with the current Style settings (clips already on YouTube are left alone)."""
    app = req.app
    job = app.store.job(int(jid))
    if not job:
        raise HttpError(404, "Job not found")
    if app.store.pending_tasks(int(jid)):
        raise HttpError(409, "Wait for the running task to finish")
    todo = [c["id"] for c in job["data"].get("plan", [])
            if (job["data"]["clips"].get(c["id"]) or {}).get("status") in ("rendered", "upload_failed", "render_failed")
            and (job["data"]["clips"][c["id"]] or {}).get("meta")]
    if not todo:
        raise HttpError(409, "No clips to render again")
    for cid in todo:
        queue_clip_task(req, jid, cid, "rerender")
    return 202, {"queued": len(todo)}


@route("POST", r"/api/jobs/(\d+)/clips/(clip\d+)/upload")
def r_upload_clip(req, jid, cid):
    body = req.json()
    if "publish_at" in body:
        _, state = get_clip(req, jid, cid)
        state.update_clip(cid, publish_at=parse_publish_at(body["publish_at"]) or None)
    return queue_clip_task(req, jid, cid, "upload", status="upload_queued")


@route("POST", r"/api/jobs/(\d+)/clips/(clip\d+)/discard")
def r_discard(req, jid, cid):
    app = req.app
    job, state = get_clip(req, jid, cid)
    if app.store.pending_tasks(int(jid)):
        raise HttpError(409, "Wait for the running task to finish")
    ctx = pipeline.Ctx(url=job["url"], cfg=app.cfg_for(job), state=state, tag=f"j{jid}_")
    pipeline.discard_clip(ctx, cid)
    app.store.update_job(int(jid), status=compute_status(state.data))
    return 200, {"ok": True}


@route("GET", r"/media/(\d+)/(clip\d+)\.mp4")
def r_media(req, jid, cid):
    app = req.app
    job = app.store.job(int(jid))
    f = ((job or {}).get("data", {}).get("clips", {}).get(cid, {}) or {}).get("file")
    if not f or not os.path.isfile(f):
        raise HttpError(404, "Clip file not found")
    clips_dir = os.path.realpath(os.path.join(app.cfg_for(job).output_dir, "clips"))
    if not os.path.realpath(f).startswith(clips_dir + os.sep):
        raise HttpError(403, "Not allowed")
    req.h.send_file_range(f, "video/mp4")


# ============================================================================ routes: accounts
@route("GET", "/api/accounts")
def r_accounts(req):
    return 200, {"accounts": [req.app.account_view(a) for a in req.app.store.accounts()]}


@route("POST", "/api/accounts")
def r_add_account(req):
    name = str(req.json().get("name", "")).strip()[:40]
    if not name:
        raise HttpError(400, "Give the account a name")
    app = req.app
    aid = app.store.add_account(name, "")
    tf = os.path.join(app.data_dir, "tokens", f"account_{aid}.json")
    with app.store.conn() as c:
        c.execute("UPDATE accounts SET token_file=? WHERE id=?", (tf, aid))
    return 201, {"id": aid}


@route("PUT", r"/api/accounts/(\d+)")
def r_rename_account(req, aid):
    name = str(req.json().get("name", "")).strip()[:40]
    if not name or not req.app.store.account(int(aid)):
        raise HttpError(400, "Give the account a name")
    req.app.store.rename_account(int(aid), name)
    return 200, {"ok": True}


@route("DELETE", r"/api/accounts/(\d+)")
def r_del_account(req, aid):
    app = req.app
    a = app.store.account(int(aid))
    if not a:
        raise HttpError(404, "Account not found")
    if len(app.store.accounts()) <= 1:
        raise HttpError(409, "Keep at least one account")
    if "/tokens/" in a["token_file"].replace("\\", "/"):
        safe_remove(a["token_file"])                    # only token files this app created
    app.store.delete_account(int(aid))
    return 200, {"ok": True}


@route("POST", r"/api/accounts/(\d+)/auth")
def r_account_auth(req, aid):
    app = req.app
    if not app.store.account(int(aid)):
        raise HttpError(404, "Account not found")
    cfg = settings.build_config(app.store.get_overrides())
    redirect = f"http://localhost:{app.port}/oauth/callback"
    flow, url = youtube_uploader.web_flow(cfg.youtube_client_secrets, redirect)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state", [""])[0]
    now = time.time()
    app.flows = {k: v for k, v in app.flows.items() if v[2] > now}
    app.flows[state] = (flow, int(aid), now + 900)
    return 200, {"url": url}


def _page(title: str, msg: str, ok: bool) -> bytes:
    color = "#0B6E6E" if ok else "#C0392B"
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><body style='font:16px system-ui;padding:32px;max-width:32rem;margin:auto'>"
            f"<h2 style='color:{color}'>{title}</h2><p>{msg}</p><p><a href='/#/accounts'>Back to the app</a></p>").encode()


@route("GET", "/oauth/callback", public=True)
def r_oauth_callback(req):
    app = req.app
    entry = app.flows.pop(req.q.get("state", ""), None)
    if not entry or entry[2] < time.time():
        req.h._send(400, _page("Sign-in expired", "Start the sign-in again from the Accounts screen.", False), "text/html; charset=utf-8")
        return None
    flow, aid, _ = entry
    if req.q.get("error") or not req.q.get("code"):
        req.h._send(400, _page("Sign-in cancelled", "Google did not approve access. Nothing was saved.", False), "text/html; charset=utf-8")
        return None
    acct = app.store.account(aid)
    try:
        youtube_uploader.web_flow_finish(flow, req.q["code"], acct["token_file"])
    except Exception as e:
        log.error("OAuth failed: %s", redact(e))
        req.h._send(400, _page("Sign-in failed", "Google rejected the sign-in. Check the app's test users and try again.", False), "text/html; charset=utf-8")
        return None
    req.h._send(200, _page("Signed in", f"Account <b>{acct['name']}</b> can now upload.", True), "text/html; charset=utf-8")
    return None


# ============================================================================ routes: assets, preview, diagnostics, maintenance
def asset_dir(app: App, kind: str) -> str:
    return os.path.join(app.data_dir, "assets", kind)


@route("GET", "/api/assets")
def r_assets(req):
    app = req.app
    cfg = settings.build_config(app.store.get_overrides())
    out = {"music": [], "logo": [], "font": []}
    music_dirs = [(cfg.music_dir, "folder"), (asset_dir(app, "music"), "uploaded")]
    for d, src in music_dirs:
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                if Path(n).suffix.lower() in UPLOAD_RULES["music"][0]:
                    p = os.path.join(d, n)
                    out["music"].append({"name": n, "path": p, "source": src, "size": os.path.getsize(p)})
    for kind in ("logo", "font"):
        d = asset_dir(app, kind)
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                p = os.path.join(d, n)
                if Path(n).suffix.lower() in UPLOAD_RULES[kind][0]:
                    e = {"name": n, "path": p, "source": "uploaded", "size": os.path.getsize(p)}
                    if kind == "font":
                        e["family"] = font_family(p)
                    out[kind].append(e)
    out["font_dir"] = asset_dir(app, "font")
    return 200, out


@route("POST", "/api/assets")
def r_upload_asset(req):
    app = req.app
    kind = req.q.get("kind", "")
    if kind not in UPLOAD_RULES:
        raise HttpError(400, "kind must be music, logo or font")
    exts, limit = UPLOAD_RULES[kind]
    name = safe_name(req.q.get("name", ""), exts)
    n = int(req.h.headers.get("Content-Length") or 0)
    if n <= 0:
        raise HttpError(400, "Empty file")
    if n > limit:
        raise HttpError(413, f"File too large (limit {limit // (1024 * 1024)} MB)")
    d = asset_dir(app, kind)
    os.makedirs(d, exist_ok=True)
    req.body_read = True
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            left = n
            while left > 0:
                chunk = req.h.rfile.read(min(256 * 1024, left))
                if not chunk:
                    raise HttpError(400, "Upload was cut off")
                f.write(chunk)
                left -= len(chunk)
        dest = os.path.join(d, name)
        os.replace(tmp, dest)
    finally:
        safe_remove(tmp)
    res = {"name": name, "path": dest}
    if kind == "font":
        res["family"] = font_family(dest)
    return 201, res


@route("DELETE", "/api/assets")
def r_delete_asset(req):
    kind = req.q.get("kind", "")
    if kind not in UPLOAD_RULES:
        raise HttpError(400, "kind must be music, logo or font")
    name = safe_name(req.q.get("name", ""), UPLOAD_RULES[kind][0])
    p = os.path.join(asset_dir(req.app, kind), name)
    if not os.path.isfile(p):
        raise HttpError(404, "File not found")
    os.remove(p)
    return 200, {"ok": True}


SAMPLES = {"en": "This is how your captions look", "hi": "आपके कैप्शन ऐसे दिखेंगे", "hinglish": "Aapke captions aise dikhenge"}
TWEET_SAMPLES = {"en": "Why most people quit too early and what to do about it",
                 "hi": "ज्यादातर लोग क्यों जल्दी हार मान लेते हैं और इसका हल क्या है",
                 "hinglish": "Zyadatar log jaldi haar kyun maan lete hain"}


def render_preview(cfg: Config, workdir: str) -> bytes:
    """One frame built with the same filter graph, layout and caption code as a real render, on a stand-in 16:9 video."""
    lang = cfg.caption_language if cfg.caption_language in SAMPLES else "en"
    sw, sh = 1280, 720
    template = cfg.template
    fit = template in templates.FIT_TEMPLATES
    layout = tweet_path = None
    if fit:
        layout = templates.plan_layout(template, sw, sh, TWEET_SAMPLES[lang])
        if template == "tweet":
            tweet_path = os.path.join(workdir, "tweet.ass")
            open(tweet_path, "w", encoding="utf-8").write(templates.tweet_ass(
                layout, cfg.tweet_name, cfg.tweet_handle, font=cfg.caption_font, font_hindi=cfg.caption_font_hindi))
    over = captions.overrides_from_cfg(cfg)
    for k, v in templates.caption_defaults(template).items():
        if not over.get(k):
            over[k] = v
    pos = cfg.caption_position
    if pos == "auto":
        pos = "bottom"
        if fit and layout.cap_top and not over.get("margin"):
            pos, over["margin"] = "top", layout.cap_top
    ass_path, t = None, 0.5
    if cfg.caption_enabled and cfg.caption_mode == "words":
        sample = SAMPLES[lang].split()
        words = [{"w": w, "s": i * 0.45, "e": i * 0.45 + 0.4, "speaker": None} for i, w in enumerate(sample)]
        ass = captions.generate_ass(words, cfg.caption_style, position=pos, font=cfg.caption_font,
                                    font_hindi=cfg.caption_font_hindi, over=over)
        t = min(len(sample) - 1, 2) * 0.45 + 0.2
    elif cfg.caption_enabled:
        ass = captions.generate_single_ass(SAMPLES[lang], 4, cfg.caption_style, position=pos, font=cfg.caption_font,
                                           font_hindi=cfg.caption_font_hindi, over=over)
    else:
        ass = ""
    if ass:
        ass_path = os.path.join(workdir, "p.ass")
        open(ass_path, "w", encoding="utf-8").write(ass)
    inputs = ["-f", "lavfi", "-i", f"color=c=0x38424d:s={sw}x{sh}:r=10:d=4,drawgrid=w=160:h=90:t=2:c=white@0.14"]
    logo_idx = avatar_idx = None
    avatar_default = False
    if template == "tweet":
        pic = next((p for p in (cfg.tweet_avatar, cfg.logo_file) if p and os.path.isfile(p)), "")
        if pic:
            inputs += ["-i", pic]
            avatar_idx = 1
        else:
            avatar_default = True
    elif cfg.logo_file and os.path.isfile(cfg.logo_file):
        inputs += ["-i", cfg.logo_file]
        logo_idx = 1
    vg = video_editor.build_video_graph(
        mode="crop" if template == "face" else "blur", src_w=sw, src_h=sh, out_dur=4.0, keys=None, keep=None, zoom=0.0,
        ass_name=os.path.basename(ass_path) if ass_path else None, fonts_dir=cfg.caption_fonts_dir, logo_idx=logo_idx,
        logo_size=cfg.logo_size, logo_pos=cfg.logo_position, logo_opacity=cfg.logo_opacity, template=template,
        layout=layout, tweet_ass_name=os.path.basename(tweet_path) if tweet_path else None, avatar_idx=avatar_idx,
        avatar_default=avatar_default)
    out = os.path.join(workdir, "p.png")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(vg + ["[vout]scale=360:640[o]"]), "-map", "[o]",
         "-ss", f"{t:.2f}", "-frames:v", "1", out], cwd=workdir, timeout=60)
    return open(out, "rb").read()


def _ffesc(p: str) -> str:
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@route("POST", "/api/preview")
def r_preview(req):
    app = req.app
    body = req.json()
    cfg = settings.build_config({**app.store.get_overrides(), **settings.clean_overrides(body.get("overrides", {}))})
    if not app.preview_lock.acquire(timeout=20):
        raise HttpError(429, "Preview is busy")
    try:
        with tempfile.TemporaryDirectory() as d:
            png = render_preview(cfg, d)
    finally:
        app.preview_lock.release()
    req.h._send(200, png, "image/png")
    return None


def check(name: str, fn) -> dict:
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, redact(f"{type(e).__name__}: {e}")[:300]
    return {"name": name, "ok": bool(ok), "detail": detail}


@route("POST", "/api/diagnostics")
def r_diagnostics(req):
    import requests
    app = req.app
    cfg = settings.build_config(app.store.get_overrides())
    rows = []

    def tool(t):
        p = shutil.which(t)
        return bool(p), p or "not found. Install it with pkg install."
    for t in ("ffmpeg", "ffprobe", "yt-dlp"):
        rows.append(check(t, lambda t=t: tool(t)))
    rows.append(check("yt-dlp version", lambda: (True, run(["yt-dlp", "--version"], timeout=20).stdout.strip())))
    rows.append(check("FFmpeg caption filter", lambda: ("ass" in ffmpeg_filters(), "libass available" if "ass" in ffmpeg_filters() else "this FFmpeg has no caption support")))

    def gemini():
        from .ai_analyzer import make_client
        c = make_client(cfg)
        if not c:
            return False, "GEMINI_API_KEY is not set in .env"
        c.generate_text("Reply with the single word: ok", temperature=0)
        return True, f"answered using {c.model}"
    rows.append(check("Gemini", gemini))

    def groq():
        if not cfg.groq_api_key:
            return False, "GROQ_API_KEY is not set (optional, used for transcription)"
        r = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {cfg.groq_api_key}"}, timeout=20)
        return r.status_code == 200, "key accepted" if r.status_code == 200 else f"HTTP {r.status_code}"
    rows.append(check("Groq", groq))

    def yt():
        if not os.path.isfile(cfg.youtube_client_secrets):
            return False, f"{cfg.youtube_client_secrets} not found"
        accts = [app.account_view(a) for a in app.store.accounts()]
        good = [a["name"] for a in accts if a["authorized"]]
        return bool(good), ("signed in: " + ", ".join(good)) if good else "no account is signed in yet"
    rows.append(check("YouTube", yt))
    rows.append(check("Music file", lambda: (os.path.isfile(cfg.music_file), cfg.music_file)))
    rows.append(check("Logo file", lambda: (os.path.isfile(cfg.logo_file), cfg.logo_file)))

    def fonts():
        dirs = [d for d in (cfg.caption_fonts_dir, asset_dir(app, "font"), "/system/fonts", "/usr/share/fonts") if d and os.path.isdir(d)]
        found = [n for d in dirs for n in os.listdir(d) if "devanagari" in n.lower()]
        return bool(found), (f"found {found[0]}" if found else "no Devanagari font found. Upload one under Style > Fonts for Hindi captions")
    rows.append(check("Hindi font", fonts))

    def disk():
        os.makedirs(cfg.output_dir, exist_ok=True)
        probe = os.path.join(cfg.output_dir, ".write_test")
        open(probe, "w").write("x")
        os.remove(probe)
        free = shutil.disk_usage(cfg.output_dir).free // (1024 * 1024)
        return free > 500, f"{free} MB free"
    rows.append(check("Storage", disk))
    return 200, {"checks": rows}


def dir_size(p: str) -> int:
    t = 0
    for r, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return t


@route("POST", "/api/maintenance/cleanup")
def r_cleanup(req):
    app = req.app
    cfg = settings.build_config(app.store.get_overrides())
    days = int(req.json().get("days") or cfg.cleanup_days)
    if app.store.pending_tasks():
        raise HttpError(409, "Wait until the queue is empty")
    cutoff = time.time() - days * 86400
    freed = 0
    used = set()
    for j in app.store.jobs(1000):
        if j["status"] in ("review", "queued", "running"):
            used.add(j["video_id"])
    if os.path.isdir(cfg.temp_dir):
        for name in os.listdir(cfg.temp_dir):
            p = os.path.join(cfg.temp_dir, name)
            if name in used or not os.path.isdir(p) or os.path.getmtime(p) > cutoff:
                continue
            freed += dir_size(p)
            shutil.rmtree(p, ignore_errors=True)
    clips = os.path.join(cfg.output_dir, "clips")
    if os.path.isdir(clips):
        for n in os.listdir(clips):
            if n.endswith(".part.mp4"):
                p = os.path.join(clips, n)
                freed += os.path.getsize(p)
                os.remove(p)
    return 200, {"freed_mb": round(freed / (1024 * 1024), 1)}


@route("GET", "/api/disk")
def r_disk(req):
    app = req.app
    cfg = settings.build_config(app.store.get_overrides())
    return 200, {"free_mb": shutil.disk_usage(app.data_dir).free // (1024 * 1024),
                 "clips_mb": round(dir_size(os.path.join(cfg.output_dir, "clips")) / 1048576, 1),
                 "temp_mb": round(dir_size(cfg.temp_dir) / 1048576, 1)}
