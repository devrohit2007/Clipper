"""Pick clips from a *real* transcript. Gemini proposes candidates; this module validates
every timestamp against the transcript and video duration, snaps to word/sentence
boundaries, removes overlaps and keeps the top-scoring clips. Nothing is invented."""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from .utilities import ClipperError, parse_ts

log = logging.getLogger("clipper")

SENT_END = re.compile(r"[.!?…।]['\"”’)\]]*$")


class ClipSelectionError(ClipperError):
    pass


def flatten_words(transcript: dict) -> list[dict]:
    """[{w, s, e, sent_end}] over the whole transcript."""
    words = []
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            words.append({"w": w["word"], "s": float(w["start"]), "e": float(w["end"]),
                          "speaker": seg.get("speaker")})
    for i, w in enumerate(words):
        nxt = words[i + 1]["s"] if i + 1 < len(words) else None
        w["sent_end"] = bool(SENT_END.search(w["w"])) or (nxt is not None and nxt - w["e"] >= 0.8) \
            or nxt is None
    return words


def format_chunks(transcript: dict, chunk_sec: int = 600, overlap_sec: int = 60) -> list[tuple]:
    """Split the transcript into prompt-sized chunks -> [(start, end, text)]. Lines carry exact seconds."""
    segs = transcript["segments"]
    if not segs:
        return []
    total = max(s["end"] for s in segs)
    chunks, start = [], 0.0
    while start < total:
        end = start + chunk_sec
        lines = [f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}"
                 for s in segs if s["end"] > start and s["start"] < end]
        if lines:
            chunks.append((start, min(end, total), "\n".join(lines)))
        if end >= total:
            break
        start = end - overlap_sec
    return chunks


def build_prompt(chunk_text: str, info: dict, durations: tuple, want: int, focus: str = "") -> str:
    focus_line = f"\nThe creator especially wants: {focus.strip()[:300]}\n" if focus.strip() else ""
    lens = ", ".join(str(d) for d in durations)
    return f"""You are an expert short-form video editor. Choose the best moments for YouTube Shorts from this
timestamped transcript. Video title: {info.get('title', '')!r}.

Look for: interesting conversations, funny moments, surprising statements, emotional moments,
educational explanations, strong opinions, stories with punchlines, useful information, and a strong
opening hook in the first sentence.
{focus_line}
Rules:
- Each clip should be about one of these lengths in seconds: {lens}. Never longer than {max(durations)}.
- "start" and "end" are seconds and MUST be copied from the bracketed [start-end] timestamps in the transcript.
  Never invent a time that is not in the transcript.
- Start at the beginning of a sentence and end after a complete sentence; never cut a punchline or an
  explanation in half. The clip must make sense on its own.
- Clips must not overlap each other.
- "score" is 0-100: how likely the clip is to hold a viewer (hook strength, payoff, emotion, usefulness).
- Describe only what is actually said. No exaggeration, no claims not supported by the transcript.
- title: under 70 characters. hashtags: 5-8 relevant to the content (no claims of trending).
- Return up to {want} candidates, best first.

Reply with JSON only:
{{"clips": [{{"start": 0.0, "end": 0.0, "title": "", "reason": "", "score": 0, "hook": "",
"description": "", "hashtags": ["#tag"]}}]}}

TRANSCRIPT:
{chunk_text}"""


def snap_clip(words: list[dict], start: float, end: float, *, min_dur: float, max_dur: float,
              duration: float, tol: float = 2.0) -> Optional[tuple]:
    """Align (start,end) to real speech: start on a word near `start` (prefer sentence starts),
    end on a sentence end. Returns (start, end) or None if it does not line up with speech."""
    if not words or start < 0 or start >= end or start >= duration:
        return None
    cands = [i for i, w in enumerate(words) if abs(w["s"] - start) <= tol]
    if not cands:
        return None

    def cost(i: int) -> float:
        starts_sentence = i == 0 or words[i - 1]["sent_end"]
        return abs(words[i]["s"] - start) - (1.0 if starts_sentence else 0.0)

    si = min(cands, key=cost)
    t0 = words[si]["s"]
    limit = max_dur - 0.3  # room for the small lead/tail padding
    window = [j for j in range(si, len(words)) if words[j]["e"] - t0 <= limit]
    if not window:
        return None
    ends = [j for j in window if words[j]["sent_end"] and words[j]["e"] - t0 >= min_dur]
    if ends:
        j = min(ends, key=lambda j: abs(words[j]["e"] - end))
    else:
        long_enough = [j for j in window if words[j]["e"] - t0 >= min_dur]
        if not long_enough:
            return None
        j = long_enough[-1]
    s = max(0.0, t0 - 0.10)
    e = min(duration, words[j]["e"] + 0.20)
    return (round(s, 2), round(e, 2))


def norm_hashtags(tags, limit: int = 10) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split()
    out, seen = [], set()
    for t in tags or []:
        t = "#" + re.sub(r"[^\w]", "", str(t).lstrip("#"))
        if len(t) > 1 and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


def _overlap_ratio(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-9, min(a["end"] - a["start"], b["end"] - b["start"]))


def nearest_length(dur: float, durations: tuple) -> int:
    return min(durations, key=lambda d: abs(d - dur))


def validate_candidates(raw: list, words: list[dict], duration: float, durations: tuple,
                        max_overlap: float = 0.2) -> list[dict]:
    """Validate, snap, de-duplicate. Returns clips sorted by score desc (no limit applied)."""
    min_dur = max(8.0, 0.6 * min(durations))
    max_dur = float(max(durations))
    accepted: list[dict] = []
    ranked = sorted((r for r in raw if isinstance(r, dict)),
                    key=lambda r: -(float(r.get("score", 0)) if str(r.get("score", "")).replace(".", "", 1).isdigit() else 0))
    for r in ranked:
        try:
            s, e = parse_ts(r["start"]), parse_ts(r["end"])
        except (KeyError, ValueError):
            log.debug("rejected candidate with bad timestamps: %r", r.get("title"))
            continue
        if e > duration + 1.0 or s >= duration:
            log.debug("rejected candidate beyond video duration: %s-%s", s, e)
            continue
        snapped = snap_clip(words, s, min(e, duration), min_dur=min_dur, max_dur=max_dur, duration=duration)
        if not snapped:
            log.debug("rejected candidate not aligned with speech: %s-%s", s, e)
            continue
        try:
            score = max(0, min(100, int(round(float(r.get("score", 0))))))
        except (TypeError, ValueError):
            score = 0
        clip = {
            "start": snapped[0], "end": snapped[1], "duration": round(snapped[1] - snapped[0], 2),
            "title": str(r.get("title", "")).strip()[:100] or "Untitled clip",
            "reason": str(r.get("reason", "")).strip(),
            "score": score,
            "hook": str(r.get("hook", "")).strip(),
            "description": str(r.get("description", "")).strip(),
            "hashtags": norm_hashtags(r.get("hashtags")),
        }
        clip["length_class"] = nearest_length(clip["duration"], durations)
        if any(_overlap_ratio(clip, a) > max_overlap for a in accepted):
            log.debug("rejected overlapping candidate %.1f-%.1f", clip["start"], clip["end"])
            continue
        accepted.append(clip)
    accepted.sort(key=lambda c: (-c["score"], c["start"]))
    return accepted


def select_clips(transcript: dict, duration: float, info: dict, ask_json: Callable,
                 durations: tuple, count: int, max_overlap: float = 0.2, focus: str = "",
                 min_score: int = 0) -> list[dict]:
    words = flatten_words(transcript)
    if not words:
        raise ClipSelectionError("transcript has no words")
    raw: list = []
    for cs, ce, text in format_chunks(transcript):
        try:
            reply = ask_json(build_prompt(text, info, durations, max(5, count * 2), focus))
        except ClipperError as e:
            log.warning("Gemini failed for transcript chunk %.0f-%.0fs: %s", cs, ce, e)
            continue
        items = reply.get("clips") if isinstance(reply, dict) else reply
        if isinstance(items, list):
            raw += items
    clips = validate_candidates(raw, words, duration, durations, max_overlap)
    if min_score and clips:
        kept = [c for c in clips if c["score"] >= min_score]
        if not kept:
            log.warning("No clip reached the minimum score %d; keeping the best one", min_score)
        clips = kept or clips[:1]
    clips = clips[:count]
    if not clips:
        raise ClipSelectionError("no valid clips could be selected from this transcript")
    for i, c in enumerate(clips, 1):
        c["id"] = f"clip{i}"
    return clips
