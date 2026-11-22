"""Settings schema (drives the UI form), coercion/validation, and Config building.

Secrets (GEMINI_API_KEY, GROQ_API_KEY, YOUTUBE_*) are never stored here: they stay in .env.
Precedence: built-in defaults < .env < settings saved in the UI < per-job options."""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from .utilities import Config, ConfigError


def F(key, label, group, type_="text", help_="", **kw):
    d = {"key": key, "label": label, "group": group, "type": type_, "help": help_}
    d.update(kw)
    return d


YES_NO_AUTO = ["auto", "yes", "no"]
SCHEMA = [
    # ---- Captions
    F("caption_enabled", "Burn captions into video", "Captions", "bool", help_="Turn off for no captions at all."),
    F("caption_mode", "Caption type", "Captions", "select", choices=[
        ["words", "Word by word (follows the speech)"], ["single", "One line for the whole video"]],
      help_="One line shows a fixed text (the cover text by default) for the whole clip. You can edit it on each clip."),
    F("caption_language", "Caption language", "Captions", "select", choices=[
        ["auto", "As spoken"], ["hi", "Hindi (Devanagari)"], ["en", "English"], ["hinglish", "Hinglish (Hindi in English letters)"]],
      help_="Hindi and English as spoken keep exact word timing. Translation is timed per phrase."),
    F("caption_style", "Style", "Captions", "select", choices=[
        ["karaoke", "Karaoke"], ["bold", "Bold creator"], ["minimal", "Minimal"], ["neon", "Neon"], ["podcast", "Podcast"]]),
    F("caption_font", "English font", "Captions", "text", help_="Font family name installed on the phone or in the fonts folder."),
    F("caption_font_hindi", "Hindi font", "Captions", "text"),
    F("caption_fonts_dir", "Fonts folder", "Captions", "text", help_="Optional folder with .ttf files, e.g. /system/fonts", advanced=True),
    F("caption_size", "Text size", "Captions", "int", min=0, max=200, help_="0 = automatic for the style."),
    F("caption_line_chars", "Characters per line", "Captions", "int", min=0, max=60, help_="0 = automatic. Captions use at most two lines."),
    F("caption_color", "Text colour", "Captions", "color", help_="Empty = style default."),
    F("caption_highlight", "Highlight colour", "Captions", "color"),
    F("caption_outline_color", "Outline colour", "Captions", "color"),
    F("caption_outline", "Outline width", "Captions", "int", min=0, max=20, help_="0 = style default."),
    F("caption_position", "Position", "Captions", "select", choices=[
        ["auto", "Automatic"], ["bottom", "Bottom"], ["middle", "Middle"], ["top", "Top"]]),
    F("caption_margin", "Distance from edge (px)", "Captions", "int", min=0, max=900, help_="0 = default."),
    F("caption_uppercase", "Capital letters", "Captions", "select", choices=[["auto", "Style default"], ["yes", "Yes"], ["no", "No"]]),
    F("caption_bold", "Bold", "Captions", "select", choices=[["auto", "Style default"], ["yes", "Yes"], ["no", "No"]]),
    F("export_srt", "Also save .srt subtitle file", "Captions", "bool"),
    F("intro_hook", "Show opening hook text", "Captions", "bool"),
    F("intro_hook_sec", "Hook text seconds", "Captions", "float", min=1, max=6, step=0.5, advanced=True),
    # ---- Video
    F("template", "Template", "Video", "select", choices=[
        ["blur", "Blurred background (default)"], ["tweet", "White with tweet card"], ["black", "Plain black"], ["face", "Full screen, follows the face"]],
      help_="Blurred: the video fills the screen. White: a tweet card with the clip title sits above the video. Black: the video on black."),
    F("tweet_name", "Tweet card name", "Video", "text", help_="Shown on the white template, e.g. Rohit"),
    F("tweet_handle", "Tweet card handle", "Video", "text", help_="e.g. @rohit"),
    F("tweet_avatar", "Tweet card picture", "Video", "logo", help_="Empty = use the logo."),
    F("max_height", "Download quality", "Video", "select", as_int=True, choices=[
        ["360", "360p (smallest)"], ["480", "480p"], ["720", "720p"], ["1080", "1080p (best)"]],
      help_="Highest quality fetched from YouTube. Lower is faster and uses less data and storage."),
    F("fit_mode", "Framing (blurred template)", "Video", "select", choices=[
        ["auto", "Automatic"], ["crop", "Crop to fill (follows faces)"], ["blur", "Blurred background"]]),
    F("face_tracking", "Follow faces when cropping", "Video", "bool", help_="Needs OpenCV; falls back to blurred background."),
    F("zoom", "Slow zoom-in", "Video", "float", min=0, max=0.3, step=0.01, help_="0 = off."),
    F("logo_file", "Logo", "Video", "logo"),
    F("logo_size", "Logo size (px)", "Video", "int", min=20, max=600),
    F("logo_position", "Logo position", "Video", "select", choices=[
        ["top-right", "Top right"], ["top-left", "Top left"], ["bottom-right", "Bottom right"], ["bottom-left", "Bottom left"]]),
    F("logo_opacity", "Logo opacity", "Video", "float", min=0, max=1, step=0.05),
    F("outro_enabled", "Add outro screen", "Video", "bool"),
    F("outro_lines", "Outro lines (separate with |)", "Video", "text"),
    F("outro_sec", "Outro seconds", "Video", "float", min=0.5, max=10, step=0.5),
    F("outro_bg", "Outro background", "Video", "color"),
    F("transition_sec", "Crossfade into outro (s)", "Video", "float", min=0, max=1.5, step=0.1, help_="0 = hard cut."),
    F("crf", "Video quality (lower = better)", "Video", "int", min=14, max=35, advanced=True),
    F("x264_preset", "Encoding speed", "Video", "select", choices=[
        ["ultrafast", "Ultrafast"], ["superfast", "Superfast"], ["veryfast", "Very fast"], ["faster", "Faster"],
        ["fast", "Fast"], ["medium", "Medium (slow, smaller)"]], advanced=True),
    # ---- Audio
    F("music_file", "Background music", "Audio", "music"),
    F("music_volume", "Music volume", "Audio", "float", min=0, max=2, step=0.05),
    F("duck_music", "Lower music while someone speaks", "Audio", "bool"),
    F("duck_strength", "Ducking strength", "Audio", "select", choices=[["light", "Light"], ["normal", "Normal"], ["strong", "Strong"]]),
    F("normalize_audio", "Even out loudness", "Audio", "bool"),
    F("remove_silence", "Cut long silences", "Audio", "bool"),
    F("silence_db", "Silence threshold (dB)", "Audio", "float", min=-60, max=-15, step=1, advanced=True),
    F("silence_min_sec", "Shortest silence to cut (s)", "Audio", "float", min=0.2, max=5, step=0.1, advanced=True),
    F("silence_keep_sec", "Pause kept at each cut (s)", "Audio", "float", min=0, max=1, step=0.05, advanced=True),
    # ---- Clips
    F("clip_count", "Clips per video", "Clips", "int", min=1, max=10),
    F("clip_durations", "Allowed lengths (seconds)", "Clips", "csv_int", help_="Comma separated, e.g. 15,30,45,60"),
    F("min_score", "Minimum score (0-100)", "Clips", "int", min=0, max=100),
    F("live_minutes", "Live streams: use the last", "Clips", "select", as_int=True, choices=[
        ["15", "15 minutes"], ["30", "30 minutes"], ["60", "1 hour"], ["120", "2 hours"], ["0", "Everything available (up to 4 hours)"]],
      help_="If the link is a stream that is still running, it is saved up to that moment and clips are cut from the last part."),
    F("clip_finder", "How clips are found", "Clips", "select", choices=[
        ["video", "Gemini watches the video first, then only those parts are transcribed (faster)"],
        ["transcript", "Transcribe the whole video first (slower, most exact)"]],
      help_="The fast way downloads and transcribes just the chosen moments. If Gemini cannot watch the video (private video, no access), the whole video is transcribed instead."),
    F("clip_focus", "What to look for", "Clips", "textarea", help_="Optional, e.g. 'funny moments and strong opinions'."),
    F("max_overlap", "Allowed overlap between clips", "Clips", "float", min=0, max=0.5, step=0.05, advanced=True),
    # ---- AI
    F("transcribe_engine", "Transcription", "AI", "select", choices=[
        ["auto", "Automatic (local, then Groq, then Gemini)"], ["local", "Local faster-whisper only"],
        ["groq", "Groq only (Gemini never transcribes)"], ["gemini", "Gemini only"]]),
    F("whisper_language", "Spoken language", "AI", "select", choices=[
        ["", "Detect automatically"], ["hi", "Hindi"], ["en", "English"]], help_="Set this if auto-detect gets Hinglish wrong."),
    F("whisper_prompt", "Transcription hint", "AI", "text", help_="Names or terms that appear in the video.", advanced=True),
    F("whisper_model", "Local model size", "AI", "select", choices=[
        ["tiny", "tiny"], ["base", "base"], ["small", "small"]], advanced=True),
    F("gemini_model", "Gemini model (first choice)", "AI", "text"),
    F("gemini_models", "Gemini fallback order", "AI", "textarea", help_="Comma separated; the next one is used when a model fails."),
    F("groq_models", "Groq model order", "AI", "text", advanced=True),
    # ---- Publishing
    F("auto_upload", "Upload automatically", "Publishing", "bool", help_="Off = render only and review before uploading."),
    F("publish_mode", "Visibility", "Publishing", "select", choices=[
        ["scheduled", "Private, then public at the scheduled time"], ["private", "Private only"], ["public", "Public immediately"]]),
    F("schedule_mode", "Schedule by", "Publishing", "select", choices=[["interval", "Fixed gap"], ["slots", "Daily times"]]),
    F("publish_start_delay_min", "First clip after (minutes)", "Publishing", "int", min=5, max=10080),
    F("publish_interval_hours", "Gap between clips (hours)", "Publishing", "float", min=0.25, max=168, step=0.25),
    F("publish_slots", "Daily times", "Publishing", "text", help_="Local time, e.g. 18:00,21:00"),
    F("tz_offset", "Time zone offset", "Publishing", "text", help_="e.g. +05:30 for India"),
    F("title_suffix", "Title ending", "Publishing", "text", help_="Added to every title, e.g. #Shorts. Leave empty for none."),
    F("extra_tags", "Hashtags added to every video", "Publishing", "text"),
    F("description_footer", "Description footer", "Publishing", "textarea"),
    F("credit_source", "Credit the source video in the description", "Publishing", "bool"),
    F("category_override", "Always use this category", "Publishing", "text", advanced=True,
      help_="Empty = chosen by AI. Example: Education"),
    F("upload_retries", "Upload retries", "Publishing", "int", min=0, max=10, advanced=True),
    # ---- Storage
    F("output_dir", "Output folder", "Storage", "text"),
    F("keep_raw", "Keep downloaded clip until upload", "Storage", "bool", help_="Lets you re-render captions quickly."),
    F("cleanup_days", "Delete saved transcripts older than (days)", "Storage", "int", min=1, max=365),
]
# Screen layout: the Video group is split so each Style section is short. Order here is the order on screen.
_REGROUP = {"Look": ("template", "tweet_name", "tweet_handle", "tweet_avatar", "fit_mode", "face_tracking", "zoom"),
            "Logo and end screen": ("logo_file", "logo_size", "logo_position", "logo_opacity", "outro_enabled", "outro_lines",
                                    "outro_sec", "outro_bg", "transition_sec"),
            "Video quality": ("max_height", "crf", "x264_preset")}
_GROUP_ORDER = ["Look", "Captions", "Logo and end screen", "Video quality", "Audio", "Clips", "AI", "Publishing", "Storage"]
for _g, _keys in _REGROUP.items():
    for _f in SCHEMA:
        if _f["key"] in _keys:
            _f["group"] = _g
SCHEMA.sort(key=lambda f: _GROUP_ORDER.index(f["group"]))      # stable: fields keep their order inside a group
BY_KEY = {f["key"]: f for f in SCHEMA}
CONFIG_FIELDS = {f.name: f for f in dataclasses.fields(Config)}
MISSING_FROM_CONFIG = [k for k in BY_KEY if k not in CONFIG_FIELDS]
assert not MISSING_FROM_CONFIG, MISSING_FROM_CONFIG
# Job-level options that are not Config settings.
JOB_ONLY = {"mode", "account", "fresh"}


def coerce(key: str, value: Any) -> Any:
    f = BY_KEY.get(key)
    if f is None:
        raise ConfigError(f"unknown setting {key!r}")
    t = f["type"]
    try:
        if t == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if t == "int":
            v = int(float(value))
        elif t == "float":
            v = float(value)
        elif t == "csv_int":
            parts = value if isinstance(value, (list, tuple)) else str(value).split(",")
            v = tuple(int(float(x)) for x in parts if str(x).strip())
            if not v:
                raise ValueError("empty")
            return v
        else:
            v = "" if value is None else str(value).strip()
            if t == "select":
                if v not in [c[0] for c in f["choices"]]:
                    raise ConfigError(f"{f['label']}: pick one of the listed options")
                if f.get("as_int"):
                    return int(v)
            return v
    except (TypeError, ValueError):
        raise ConfigError(f"{f['label']}: invalid value {value!r}") from None
    if "min" in f and v < f["min"] or "max" in f and v > f["max"]:
        raise ConfigError(f"{f['label']} must be between {f.get('min')} and {f.get('max')}")
    return v


def clean_overrides(d: dict, allow_job: bool = False) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if allow_job and k in JOB_ONLY:
            out[k] = v
            continue
        out[k] = coerce(k, v)
    return out


def build_config(overrides: dict | None = None, job_options: dict | None = None) -> Config:
    """defaults < .env < saved overrides < job options. Raises ConfigError on invalid combinations."""
    cfg = Config.from_env()
    merged = {**clean_overrides(overrides or {}), **clean_overrides({k: v for k, v in (job_options or {}).items()
                                                                  if k not in JOB_ONLY and k in BY_KEY})}
    if "output_dir" in merged and "temp_dir" not in merged and not os.environ.get("TEMP_DIR"):
        cfg.temp_dir = ""
    for k, v in merged.items():
        setattr(cfg, k, v)
    cfg.finalize()
    return cfg


def effective_values(overrides: dict) -> dict:
    cfg = build_config(overrides)
    vals = {}
    for k in BY_KEY:
        v = getattr(cfg, k)
        vals[k] = list(v) if isinstance(v, tuple) else v
    return vals


def schema_json() -> list[dict]:
    return SCHEMA
