"""Shared helpers: config, logging with secret redaction, timestamps,
subprocess wrapper, ffprobe, persistent job state, dependency checks."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import logging
import math
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("clipper")


class ClipperError(Exception):
    """Base class for expected, user-reportable errors."""


class ConfigError(ClipperError):
    pass


class ToolError(ClipperError):
    pass


class Cancelled(ClipperError):
    """The user cancelled the running job."""


CANCEL_EVENT = threading.Event()      # set by the web worker to stop the running ffmpeg/yt-dlp process


# --------------------------------------------------------------------------
# Secret redaction
# --------------------------------------------------------------------------
SECRET_ENV_NAMES = ("GEMINI_API_KEY", "GROQ_API_KEY", "YOUTUBE_CLIENT_SECRET")
_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gsk_[0-9A-Za-z]{16,}"),
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),
    re.compile(r"ya29\.[0-9A-Za-z_\-\.]+"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"1//[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(api[_-]?key|access_token|refresh_token|client_secret)(\"?\s*[:=]\s*\"?)[^\s\"&,}]+"),
]


def redact(text: Any) -> str:
    s = str(text)
    for name in SECRET_ENV_NAMES:
        v = os.environ.get(name)
        if v and len(v) >= 8:
            s = s.replace(v, "***")
    for p in _SECRET_PATTERNS[:-1]:
        s = p.sub("***", s)
    s = _SECRET_PATTERNS[-1].sub(lambda m: f"{m.group(1)}{m.group(2)}***", s)
    return s


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging(log_file: Optional[str] = None, verbose: bool = False) -> None:
    root = logging.getLogger("clipper")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    console.addFilter(RedactFilter())
    root.addHandler(console)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        fh.addFilter(RedactFilter())
        root.addHandler(fh)
    root.propagate = False


# --------------------------------------------------------------------------
# Config (environment variables, optional .env file)
# --------------------------------------------------------------------------
def load_env_file(path: str = ".env") -> bool:
    """Minimal .env parser. Existing environment variables win."""
    p = Path(path).expanduser()
    if not p.is_file():
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if v[:1] in ("'", '"') and v[0] in v[1:]:
            v = v[1:v.index(v[0], 1)]                 # quoted: take text up to the closing quote
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()   # drop trailing "  # comment"
        if k and k not in os.environ:
            os.environ[k] = v
    return True


@dataclass
class Config:
    # --- credentials (environment only, never hard-coded) ---
    gemini_api_key: str = ""
    groq_api_key: str = ""                 # optional: free Groq Whisper API for transcription
    gemini_model: str = "gemini-3.8-flash"          # tried first
    # Free-tier fallback chain, tried in order when a model is rate-limited, overloaded or retired.
    gemini_models: str = ("gemini-3.8-flash,gemini-3.6-flash,gemini-3.5-flash-lite,"
                          "gemini-3.1-flash-lite,gemini-2.5-flash,gemini-2.5-flash-lite")
    youtube_client_secrets: str = "client_secret.json"
    youtube_token_file: str = "token.json"
    # --- paths ---
    output_dir: str = "output"
    temp_dir: str = ""
    music_dir: str = "~/music"
    music_file: str = "~/music/track1.mp3"
    logo_file: str = "~/logo.png"
    # --- audio ---
    music_volume: float = 0.15
    duck_music: bool = True
    duck_strength: str = "normal"     # light | normal | strong
    normalize_audio: bool = True
    remove_silence: bool = False
    silence_db: float = -35.0
    silence_min_sec: float = 0.7
    silence_keep_sec: float = 0.15
    # --- look ---
    template: str = "blur"             # blur (default) | tweet (white, tweet card above the video) | black
    tweet_name: str = ""
    tweet_handle: str = ""
    tweet_avatar: str = ""             # empty = use the logo file
    max_height: int = 1080             # download quality ceiling: 360 | 480 | 720 | 1080
    # --- captions ---
    caption_enabled: bool = True
    caption_mode: str = "words"        # words (word by word) | single (one line for the whole video)
    caption_language: str = "auto"   # auto (as spoken) | hi (Devanagari) | en | hinglish (Hindi in Roman letters)
    caption_size: int = 0            # 0 = automatic for the style
    caption_color: str = ""          # #RRGGBB, empty = style default
    caption_highlight: str = ""
    caption_outline_color: str = ""
    caption_outline: int = 0         # outline width, 0 = style default
    caption_line_chars: int = 0      # characters per line, 0 = style default
    caption_position: str = "auto"   # auto | bottom | middle | top
    caption_margin: int = 0          # distance from bottom/top in px, 0 = default
    caption_uppercase: str = "auto"  # auto | yes | no
    caption_bold: str = "auto"       # auto | yes | no
    caption_style: str = "karaoke"
    caption_font: str = "Noto Sans"
    caption_font_hindi: str = "Noto Sans Devanagari"
    caption_fonts_dir: str = ""
    export_srt: bool = False
    # --- video ---
    fit_mode: str = "auto"          # auto | crop | blur
    face_tracking: bool = True
    zoom: float = 0.06              # 0 disables; fraction zoomed over the clip
    logo_size: int = 120
    logo_position: str = "top-right"   # top-right | top-left | bottom-right | bottom-left
    logo_opacity: float = 1.0
    crf: int = 21                      # video quality: lower = better/larger (18-30)
    x264_preset: str = "veryfast"
    intro_hook: bool = False
    intro_hook_sec: float = 2.5
    outro_enabled: bool = True
    outro_sec: float = 2.0
    outro_bg: str = "#000000"
    outro_lines: str = "Subscribe to Daily Creates|Like  Comment  Share|Hit the Bell"
    outro_font: str = ""
    transition_sec: float = 0.3     # 0 = hard cut + stream-copy concat (original behaviour)
    # --- clip selection ---
    clip_count: int = 3
    clip_durations: tuple = (15, 30, 45, 60)
    max_overlap: float = 0.2
    min_score: int = 0                # drop clips scoring below this (the best one is always kept)
    clip_finder: str = "video"        # video = Gemini watches the video, then only the chosen parts are transcribed | transcript = transcribe everything first
    live_minutes: int = 60            # live streams: use this many minutes before the moment you paste the link (0 = all available)
    clip_focus: str = ""              # free text: what kind of moments to look for
    # --- transcription ---
    transcribe_engine: str = "auto"  # auto (local -> groq -> gemini) | local | groq | gemini
    groq_models: str = "whisper-large-v3-turbo,whisper-large-v3"
    whisper_model: str = "base"
    whisper_device: str = "cpu"
    whisper_compute: str = "int8"
    whisper_language: str = ""       # empty = auto-detect
    whisper_prompt: str = ""
    # --- publishing ---
    auto_upload: bool = True
    publish_mode: str = "scheduled"   # scheduled (private, goes public at its time) | private | public
    schedule_mode: str = "interval"   # interval | slots
    publish_slots: str = "18:00,21:00"   # used when schedule_mode=slots (local time)
    tz_offset: str = "+05:30"
    title_suffix: str = "#Shorts"
    description_footer: str = ""
    credit_source: bool = True
    category_override: str = ""
    keep_raw: bool = False            # keep downloaded clip until upload (enables fast caption re-render)
    cleanup_days: int = 7
    review_mode: bool = False
    publish_start_delay_min: int = 20
    publish_interval_hours: float = 3.0
    upload_retries: int = 5
    extra_tags: str = "#Shorts"

    @classmethod
    def from_env(cls) -> "Config":
        kw: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            raw = os.environ.get(f.name.upper())
            if raw is None or raw.strip() == "":
                continue
            raw = raw.strip()
            default = f.default
            try:
                if isinstance(default, bool):
                    kw[f.name] = raw.lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    kw[f.name] = int(raw)
                elif isinstance(default, float):
                    kw[f.name] = float(raw)
                elif isinstance(default, tuple):
                    kw[f.name] = tuple(int(x) for x in raw.split(",") if x.strip())
                else:
                    kw[f.name] = raw
            except ValueError as e:
                raise ConfigError(f"Invalid value for {f.name.upper()}: {e}") from e
        cfg = cls(**kw)
        cfg.finalize()
        return cfg

    def finalize(self) -> None:
        for name in ("music_file", "music_dir", "logo_file", "youtube_client_secrets", "youtube_token_file",
                     "output_dir", "caption_fonts_dir", "outro_font", "tweet_avatar"):
            v = getattr(self, name)
            if v:
                setattr(self, name, os.path.expanduser(v))
        if not self.temp_dir:
            self.temp_dir = os.path.join(self.output_dir, "tmp")
        self.temp_dir = os.path.expanduser(self.temp_dir)
        self.validate()

    def validate(self) -> None:
        if self.fit_mode not in ("auto", "crop", "blur"):
            raise ConfigError("FIT_MODE must be auto, crop or blur")
        if self.transcribe_engine not in ("auto", "local", "groq", "gemini"):
            raise ConfigError("TRANSCRIBE_ENGINE must be auto, local, groq or gemini")
        if self.caption_style.lower() not in ("karaoke", "bold", "minimal", "neon", "podcast"):
            raise ConfigError("CAPTION_STYLE must be karaoke, bold, minimal, neon or podcast")
        self.caption_style = self.caption_style.lower()
        if not self.clip_durations or any(d <= 0 for d in self.clip_durations):
            raise ConfigError("CLIP_DURATIONS must be positive integers, e.g. 15,30,45,60")
        self.clip_durations = tuple(sorted(set(self.clip_durations)))
        if self.clip_finder not in ("video", "transcript"):
            raise ConfigError("clip_finder must be video or transcript")
        if self.live_minutes not in (0, 15, 30, 60, 120):
            raise ConfigError("live_minutes must be 0, 15, 30, 60 or 120")
        if self.clip_count < 1:
            raise ConfigError("CLIP_COUNT must be >= 1")
        if not 0 <= self.zoom <= 0.3:
            raise ConfigError("ZOOM must be between 0 and 0.3")
        if self.transition_sec < 0 or self.transition_sec > 1.5:
            raise ConfigError("TRANSITION_SEC must be between 0 and 1.5")
        if not 0 <= self.music_volume <= 2:
            raise ConfigError("MUSIC_VOLUME must be between 0 and 2")
        choices = {
            "caption_language": ("auto", "hi", "en", "hinglish"),
            "template": ("blur", "tweet", "black", "face"), "caption_mode": ("words", "single"),
            "caption_position": ("auto", "bottom", "middle", "top"),
            "caption_uppercase": ("auto", "yes", "no"), "caption_bold": ("auto", "yes", "no"),
            "logo_position": ("top-right", "top-left", "bottom-right", "bottom-left"),
            "duck_strength": ("light", "normal", "strong"),
            "publish_mode": ("scheduled", "private", "public"), "schedule_mode": ("interval", "slots"),
            "x264_preset": ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium"),
        }
        for k, allowed in choices.items():
            if getattr(self, k) not in allowed:
                raise ConfigError(f"{k.upper()} must be one of: {', '.join(allowed)}")
        for k in ("caption_color", "caption_highlight", "caption_outline_color", "outro_bg"):
            v = getattr(self, k)
            if v and not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
                raise ConfigError(f"{k.upper()} must look like #RRGGBB")
        if self.max_height not in (360, 480, 720, 1080):
            raise ConfigError("MAX_HEIGHT must be 360, 480, 720 or 1080")
        if not 14 <= self.crf <= 35:
            raise ConfigError("CRF must be between 14 and 35")
        if not 0 <= self.logo_opacity <= 1:
            raise ConfigError("LOGO_OPACITY must be between 0 and 1")
        if not 0.5 <= self.outro_sec <= 10:
            raise ConfigError("OUTRO_SEC must be between 0.5 and 10")
        if not re.fullmatch(r"[+-]\d{2}:\d{2}", self.tz_offset):
            raise ConfigError("TZ_OFFSET must look like +05:30")
        if self.schedule_mode == "slots" and not re.fullmatch(
                r"\s*\d{1,2}:\d{2}(\s*,\s*\d{1,2}:\d{2})*\s*", self.publish_slots):
            raise ConfigError("PUBLISH_SLOTS must look like 18:00,21:00")
        for k in ("caption_size", "caption_outline", "caption_line_chars", "caption_margin", "min_score"):
            if getattr(self, k) < 0:
                raise ConfigError(f"{k.upper()} cannot be negative")


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def parse_ts(value: Any) -> float:
    """'90', '1:30', '01:01:30.5', 90, 90.5 -> seconds. Raises ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"bad timestamp {value!r}")
    if isinstance(value, (int, float)):
        out = float(value)
    else:
        s = str(value).strip()
        if not s:
            raise ValueError("empty timestamp")
        parts = s.split(":")
        if len(parts) > 3:
            raise ValueError(f"bad timestamp {value!r}")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            raise ValueError(f"bad timestamp {value!r}") from None
        out = 0.0
        for n in nums:
            out = out * 60 + n
    if not math.isfinite(out) or out < 0:
        raise ValueError(f"bad timestamp {value!r}")
    return out


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# --------------------------------------------------------------------------
# Subprocess / ffprobe
# --------------------------------------------------------------------------
def run(cmd: list, *, cwd: Optional[str] = None, timeout: Optional[float] = None,
        check: bool = True) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    log.debug("run: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
    except FileNotFoundError:
        raise ToolError(f"'{cmd[0]}' not found. Install it and make sure it is on PATH.") from None
    started = time.monotonic()
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            if CANCEL_EVENT.is_set():
                proc.kill()
                proc.communicate()
                raise Cancelled("cancelled")
            if timeout and time.monotonic() - started > timeout:
                proc.kill()
                proc.communicate()
                raise ToolError(f"'{cmd[0]}' timed out after {timeout}s") from None
    p = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and p.returncode != 0:
        tail = redact((p.stderr or p.stdout or "").strip()[-900:])
        raise ToolError(f"'{cmd[0]}' failed (exit {p.returncode}): {tail}")
    return p


def probe(path: str) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)])
    data = json.loads(p.stdout or "{}")
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(data.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    fps = 30.0
    if v and v.get("avg_frame_rate") and "/" in v["avg_frame_rate"]:
        n, d = v["avg_frame_rate"].split("/")
        if float(d or 0):
            fps = float(n) / float(d)
    return {"duration": dur, "width": int((v or {}).get("width") or 0),
            "height": int((v or {}).get("height") or 0), "fps": fps,
            "has_video": v is not None, "has_audio": a is not None}


def file_ok(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def safe_remove(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif p and os.path.exists(p):
                os.remove(p)
        except OSError as e:
            log.warning("could not remove %s: %s", p, e)


# --------------------------------------------------------------------------
# Persistent job state
# --------------------------------------------------------------------------
class JobState:
    def __init__(self, path: Optional[str] = None):
        self.path = Path(path) if path else None
        self.data: dict = {"clips": {}}
        if self.path is not None and self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
                self.data.setdefault("clips", {})
            except (OSError, ValueError):
                log.warning("job file %s unreadable; starting fresh", self.path)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def set(self, **kw: Any) -> None:
        self.data.update(kw)
        self.save()

    def clip(self, cid: str) -> dict:
        return self.data["clips"].setdefault(cid, {})

    def update_clip(self, cid: str, **kw: Any) -> None:
        self.clip(cid).update(kw)
        self.save()


# --------------------------------------------------------------------------
# Dependency checks
# --------------------------------------------------------------------------
def ffmpeg_filters() -> set:
    try:
        p = run(["ffmpeg", "-hide_banner", "-filters"], check=False)
    except ToolError:
        return set()
    names = set()
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) <= 4:
            names.add(parts[1])
    return names


def have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_dependencies(cfg: Config) -> list[tuple[str, bool, bool, str]]:
    """Returns (name, ok, required, detail). Never includes secret values."""
    out: list[tuple[str, bool, bool, str]] = []
    add = lambda n, ok, req, d="": out.append((n, bool(ok), req, d))
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        add(tool, shutil.which(tool), True, shutil.which(tool) or "not found on PATH")
    filters = ffmpeg_filters()
    for flt, req in (("ass", True), ("sidechaincompress", False), ("loudnorm", False),
                     ("silencedetect", False), ("xfade", False), ("drawtext", False)):
        add(f"ffmpeg filter: {flt}", flt in filters, req,
            "" if flt in filters else "missing in this ffmpeg build")
    add("GEMINI_API_KEY", bool(cfg.gemini_api_key), True, "set" if cfg.gemini_api_key else "not set")
    add("GROQ_API_KEY (free cloud transcription)", bool(cfg.groq_api_key), False,
        "optional; get one free at console.groq.com")
    add("faster-whisper (local transcription)", have_module("faster_whisper"), False,
        "optional; Gemini audio fallback is used if missing")
    add("opencv (face tracking)", have_module("cv2"), False, "optional; falls back to blurred background")
    gl = all(have_module(m) for m in ("googleapiclient", "google.oauth2", "google_auth_oauthlib"))
    add("google api client libs (upload)", gl, True, "pip install -r requirements.txt")
    add("YouTube client secrets file", os.path.isfile(cfg.youtube_client_secrets), cfg.auto_upload,
        cfg.youtube_client_secrets)
    add("background music file", os.path.isfile(cfg.music_file), False, cfg.music_file)
    add("logo file", os.path.isfile(cfg.logo_file), False, cfg.logo_file)
    return out


def format_dependency_report(rows) -> str:
    lines = []
    for name, ok, req, detail in rows:
        mark = "OK  " if ok else ("MISS" if req else "skip")
        lines.append(f"[{mark}] {name}" + (f"  ({detail})" if detail and not ok else ""))
    return "\n".join(lines)
