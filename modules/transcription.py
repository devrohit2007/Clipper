"""Audio extraction + transcription.

Local: faster-whisper (optional import, CPU/int8, word timestamps, language auto-detect).
Fallback: Gemini audio transcription (segment-level timestamps; words are *estimated*).
Transcript format (JSON-serialisable):
  {"language", "duration", "engine", "word_level": bool,
   "segments": [{"start", "end", "text", "words": [{"word", "start", "end"}], "speaker"?}]}
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from .utilities import ClipperError, redact, run, safe_remove

log = logging.getLogger("clipper")


class TranscriptionError(ClipperError):
    pass


class TranscriptionUnavailable(ClipperError):
    pass


def extract_audio(src: str, dst: str, rate: int = 16000) -> str:
    """Video/audio -> mono 16 kHz FLAC using FFmpeg."""
    run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", str(rate), "-c:a", "flac", dst])
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        raise TranscriptionError("audio extraction produced no file")
    return dst


def distribute_words(text: str, start: float, end: float) -> list[dict]:
    """Estimate word timings inside a segment, proportional to word length."""
    tokens = text.split()
    if not tokens or end <= start:
        return []
    weights = [max(1, len(re.sub(r"\W", "", t)) or 1) for t in tokens]
    total = float(sum(weights))
    out, t = [], start
    for tok, w in zip(tokens, weights):
        d = (end - start) * w / total
        out.append({"word": tok, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    out[-1]["end"] = round(end, 3)
    return out


def _segment(start: float, end: float, text: str, words: Optional[list] = None, **extra) -> dict:
    seg = {"start": round(start, 3), "end": round(end, 3), "text": text.strip(),
           "words": words if words else distribute_words(text, start, end)}
    seg.update({k: v for k, v in extra.items() if v})
    return seg


# ------------------------------------------------------------------ local
def transcribe_local(audio: str, cfg) -> dict:
    try:
        from faster_whisper import WhisperModel  # optional dependency
    except Exception as e:  # ImportError, or a broken native lib
        raise TranscriptionUnavailable(f"faster-whisper not usable: {type(e).__name__}") from e
    log.info("Transcribing locally with faster-whisper (%s, %s)...", cfg.whisper_model, cfg.whisper_compute)
    model = WhisperModel(cfg.whisper_model, device=cfg.whisper_device, compute_type=cfg.whisper_compute)
    segs, info = model.transcribe(
        audio, word_timestamps=True, vad_filter=True, condition_on_previous_text=False,
        language=cfg.whisper_language or None, initial_prompt=cfg.whisper_prompt or None)
    segments, has_words = [], False
    for s in segs:
        words = [{"word": w.word.strip(), "start": float(w.start), "end": float(w.end)}
                 for w in (s.words or []) if w.word.strip()]
        has_words = has_words or bool(words)
        segments.append(_segment(float(s.start), float(s.end), s.text, words))
    if not segments:
        raise TranscriptionError("no speech detected")
    return {"language": info.language, "duration": float(info.duration), "engine": "faster-whisper",
            "word_level": has_words, "segments": segments}


# ------------------------------------------------------------------ groq (free cloud Whisper)
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MAX_BYTES = 24 * 1024 * 1024        # free tier limit is 25 MB per file
LANG_CODES = {"hindi": "hi", "english": "en", "urdu": "ur", "bengali": "bn", "tamil": "ta", "telugu": "te",
              "marathi": "mr", "gujarati": "gu", "punjabi": "pa", "kannada": "kn", "malayalam": "ml"}


def _lang_code(name) -> str:
    n = str(name or "").strip().lower()
    return LANG_CODES.get(n, n[:5] or "unknown")


def assemble_groq_segments(data: dict, chunk_len: float, offset: float) -> list[dict]:
    """verbose_json -> our segments, with REAL word timings attached to the segment they fall in."""
    words = []
    for w in data.get("words") or []:
        try:
            ws, we, txt = float(w["start"]), float(w["end"]), str(w["word"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not txt or ws < 0 or ws >= chunk_len:
            continue
        words.append({"word": txt, "start": ws, "end": min(max(we, ws + 0.02), chunk_len)})
    words.sort(key=lambda w: w["start"])
    raw = []
    for sg in data.get("segments") or []:
        try:
            raw.append((float(sg["start"]), float(sg["end"]), str(sg["text"]).strip()))
        except (KeyError, TypeError, ValueError):
            continue
    if not raw and words:     # no segments returned: group words into sentence-sized segments
        group = []
        for w in words:
            group.append(w)
            if re.search(r"[.!?।]$", w["word"]) or len(group) >= 12:
                raw.append((group[0]["start"], group[-1]["end"], " ".join(x["word"] for x in group)))
                group = []
        if group:
            raw.append((group[0]["start"], group[-1]["end"], " ".join(x["word"] for x in group)))
    out, wi = [], 0
    for i, (s, e, text) in enumerate(raw):
        if not text or s >= chunk_len:
            continue
        e = min(max(e, s + 0.05), chunk_len)
        mine = []
        while wi < len(words) and (i == len(raw) - 1 or words[wi]["start"] < e - 1e-3):
            mine.append({"word": words[wi]["word"], "start": round(words[wi]["start"] + offset, 3),
                         "end": round(words[wi]["end"] + offset, 3)})
            wi += 1
        out.append(_segment(s + offset, e + offset, text, mine))
    return out


def _groq_request(path: str, models: list, cfg, session, sleep) -> dict:
    """POST one chunk. Retries temporary errors, then falls back to the next Groq model."""
    headers = {"Authorization": f"Bearer {cfg.groq_api_key}"}
    last = "no model tried"
    for model in models:
        for attempt in range(3):
            data = {"model": model, "response_format": "verbose_json", "temperature": "0",
                    "timestamp_granularities[]": ["word", "segment"]}
            if cfg.whisper_language:
                data["language"] = cfg.whisper_language
            if cfg.whisper_prompt:
                data["prompt"] = cfg.whisper_prompt
            try:
                with open(path, "rb") as f:
                    r = session.post(GROQ_URL, headers=headers, data=data, timeout=300,
                                     files={"file": (os.path.basename(path), f)})
            except Exception as e:
                last = f"{model}: network error ({type(e).__name__})"
                sleep(5 * (attempt + 1))
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last = f"{model}: response was not JSON"
                    continue
            if r.status_code in (401, 403) and "model" not in (r.text or "").lower():
                raise TranscriptionError("Groq rejected the API key (check GROQ_API_KEY)")
            last = f"{model}: HTTP {r.status_code} {redact((r.text or '')[:120])}"
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers.get("retry-after", 0)) or 5 * (attempt + 1)
                except (TypeError, ValueError, AttributeError):
                    wait = 5 * (attempt + 1)
                log.warning("Groq %s busy (HTTP %s); waiting %.0fs", model, r.status_code, min(wait, 30))
                sleep(min(wait, 30))
                continue
            break           # 400/404 etc.: this model will not work for this file, try the next
        log.warning("Groq model %s failed (%s)", model, last)
    raise TranscriptionError(f"Groq transcription failed: {last}")


def transcribe_groq(audio: str, cfg, duration: float, workdir: str, *, session=None,
                    sleep=time.sleep, chunk_sec: int = 480) -> dict:
    if not cfg.groq_api_key:
        raise TranscriptionUnavailable("GROQ_API_KEY not set")
    import requests
    session = session or requests.Session()
    models = [m.strip() for m in cfg.groq_models.split(",") if m.strip()] or ["whisper-large-v3-turbo"]
    log.info("Transcribing with Groq Whisper (%s)...", models[0])
    segments, langs, off, idx = [], [], 0.0, 0
    while off < duration:
        length = min(chunk_sec, duration - off)
        chunk = os.path.join(workdir, f"groq_{idx:03d}.flac")
        run(["ffmpeg", "-y", "-ss", f"{off:.2f}", "-t", f"{length:.2f}", "-i", audio,
             "-ac", "1", "-ar", "16000", "-c:a", "flac", chunk])
        if os.path.getsize(chunk) > GROQ_MAX_BYTES:       # keep under the 25 MB upload limit
            safe_remove(chunk)
            chunk = os.path.join(workdir, f"groq_{idx:03d}.mp3")
            run(["ffmpeg", "-y", "-ss", f"{off:.2f}", "-t", f"{length:.2f}", "-i", audio,
                 "-ac", "1", "-ar", "16000", "-b:a", "48k", chunk])
        try:
            data = _groq_request(chunk, models, cfg, session, sleep)
        finally:
            safe_remove(chunk)
        segments += assemble_groq_segments(data, length, off)
        if data.get("language"):
            langs.append(_lang_code(data["language"]))
        off += length
        idx += 1
    if not segments:
        raise TranscriptionError("Groq returned no speech")
    has_words = any(s["words"] for s in segments)
    lang = max(set(langs), key=langs.count) if langs else "unknown"
    return {"language": lang, "duration": duration, "engine": "groq", "word_level": has_words,
            "segments": segments}


# ------------------------------------------------------------------ gemini
_GEMINI_PROMPT = """Transcribe this audio exactly as spoken. The language may be Hindi, English, or Hinglish
(Hindi and English mixed). Write Hindi words in Devanagari and English words in Latin script.
Use natural punctuation. Do not translate. Do not invent speech; skip music and silence.
Return JSON only:
{{"language": "<ISO 639-1 code of the main language>",
  "segments": [{{"start": <seconds from the start of this audio, number>, "end": <seconds>, "text": "<speech>"}}]}}
Segments should be one sentence or phrase (2-10 seconds). The audio is {length:.0f} seconds long;
no timestamp may exceed that."""


def clean_gemini_segments(raw_segments, chunk_len: float, offset: float) -> list[dict]:
    out, last_end = [], 0.0
    for r in raw_segments or []:
        try:
            s, e, text = float(r["start"]), float(r["end"]), str(r["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if not text or s < 0 or s >= chunk_len:
            continue
        e = min(e, chunk_len)
        s = max(s, last_end)
        if e - s < 0.2:
            continue
        out.append(_segment(s + offset, e + offset, text))
        last_end = e
    return out


def transcribe_gemini(audio: str, cfg, client, duration: float, workdir: str,
                      chunk_sec: int = 600) -> dict:
    log.info("Transcribing with Gemini audio fallback (word timings will be estimated)...")
    segments, langs, off = [], [], 0.0
    idx = 0
    while off < duration:
        length = min(chunk_sec, duration - off)
        chunk = os.path.join(workdir, f"chunk_{idx:03d}.mp3")
        run(["ffmpeg", "-y", "-ss", f"{off:.2f}", "-t", f"{length:.2f}", "-i", audio,
             "-ac", "1", "-ar", "16000", "-b:a", "32k", chunk])
        data = base64.b64encode(Path(chunk).read_bytes()).decode()
        safe_remove(chunk)
        parts = [{"text": _GEMINI_PROMPT.format(length=length)},
                 {"inlineData": {"mimeType": "audio/mp3", "data": data}}]
        try:
            result = client.generate_json(parts, temperature=0.0)
        except ClipperError as e:
            raise TranscriptionError(f"Gemini transcription failed on chunk {idx}: {e}") from e
        if isinstance(result, dict):
            segments += clean_gemini_segments(result.get("segments"), length, off)
            if result.get("language"):
                langs.append(str(result["language"]).lower()[:5])
        off += length
        idx += 1
    if not segments:
        raise TranscriptionError("Gemini returned no usable transcript")
    lang = max(set(langs), key=langs.count) if langs else "unknown"
    return {"language": lang, "duration": duration, "engine": "gemini", "word_level": False,
            "segments": segments}


# ------------------------------------------------------------------ orchestrator
def transcribe(audio: str, cfg, client, duration: float, workdir: str) -> dict:
    """auto = local faster-whisper -> Groq -> Gemini (last resort). Pick one with TRANSCRIBE_ENGINE."""
    order = {"auto": ["local", "groq", "gemini"], "local": ["local"], "groq": ["groq"],
             "gemini": ["gemini"]}[cfg.transcribe_engine]
    problems = []
    for eng in order:
        try:
            if eng == "local":
                return transcribe_local(audio, cfg)
            if eng == "groq":
                return transcribe_groq(audio, cfg, duration, workdir)
            if client is None:
                problems.append("GEMINI_API_KEY not set")
                continue
            return transcribe_gemini(audio, cfg, client, duration, workdir)
        except TranscriptionUnavailable as e:
            problems.append(str(e))
            log.info("%s: skipped (%s)", eng, e)
        except ClipperError as e:
            problems.append(f"{eng}: {e}")
            log.warning("%s transcription failed: %s", eng, redact(e))
        except Exception as e:
            problems.append(f"{eng}: {type(e).__name__}: {redact(e)}")
            log.warning("%s transcription failed (%s)", eng, type(e).__name__)
    raise TranscriptionError("Could not transcribe: " + "; ".join(problems) +
                             ". Set GROQ_API_KEY (free) or install faster-whisper.")
