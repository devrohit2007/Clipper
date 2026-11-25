"""Find the best moments by letting Gemini watch the video first (via its YouTube link), so only those parts
need to be downloaded and transcribed. Gemini's times are approximate (a few seconds), so each chosen moment gets
a padded window; the window is transcribed with real word timing and the clip is then snapped to sentence
boundaries by clip_selector (same checks as the transcript-first route). Nothing here is trusted blindly:
a candidate that does not line up with real speech is dropped."""
from __future__ import annotations

import logging
import math
from typing import Callable, Optional

from .clip_selector import _overlap_ratio
from .utilities import ClipperError, fmt_ts, parse_ts

log = logging.getLogger("clipper")

CHUNK_SEC = 900            # Gemini watches long videos in 15 minute pieces (keeps each request small)
OVERLAP_SEC = 60
PAD_BEFORE = 10.0          # extra audio transcribed around each moment, so the clip can snap to full sentences
PAD_AFTER = 15.0
MERGE_GAP = 20.0           # windows closer than this become one download
SPARE = 2                  # shortlist this many more than asked for, in case some do not line up with speech


class ScoutError(ClipperError):
    pass


def chunk_ranges(duration: float, size: int = CHUNK_SEC, overlap: int = OVERLAP_SEC, lo: float = 0.0) -> list[tuple[float, float]]:
    """Pieces for Gemini to watch, covering [lo, duration]. One piece when it is short enough."""
    if duration - lo <= size * 1.25:
        return [(lo, duration)]
    out, start = [], lo
    while start < duration:
        end = min(duration, start + size)
        out.append((start, end))
        if end >= duration:
            break
        start = end - overlap
    return out


def build_prompt(info: dict, durations: tuple, want: int, focus: str = "") -> str:
    focus_line = f"\nThe creator especially wants: {focus.strip()[:300]}\n" if focus.strip() else ""
    lens = ", ".join(str(d) for d in durations)
    return f"""You are an expert short-form video editor. Watch and listen to this video and choose the best moments
for YouTube Shorts. Video title: {info.get('title', '')!r}.

Look for: interesting conversations, funny moments, surprising statements, emotional moments, educational
explanations, strong opinions, stories with punchlines, useful information, and a strong opening hook.
{focus_line}
Rules:
- Each clip should be about one of these lengths in seconds: {lens}. Never longer than {max(durations)}.
- "start" and "end" are positions in THIS video as MM:SS (or H:MM:SS), read from the player timeline.
- Start where a sentence begins and end after a complete sentence. The clip must make sense on its own.
- Clips must not overlap each other.
- "score" is 0-100: how likely the clip is to hold a viewer (hook, payoff, emotion, usefulness).
- Describe only what really happens. No exaggeration.
- title: under 70 characters. hashtags: 5-8 relevant to the content (no claims of trending).
- Return up to {want} clips, best first.

Reply with JSON only:
{{"clips": [{{"start": "00:00", "end": "00:00", "title": "", "reason": "", "score": 0, "hook": "",
"description": "", "hashtags": ["#tag"]}}]}}"""


def video_parts(url: str, prompt: str, start: float, end: float, whole: bool) -> list[dict]:
    part: dict = {"fileData": {"fileUri": url}}
    if not whole:
        part["videoMetadata"] = {"startOffset": f"{int(start)}s", "endOffset": f"{int(math.ceil(end))}s"}
    return [part, {"text": prompt}]


def _to_absolute(items: list, start: float, end: float, whole: bool) -> list[dict]:
    """Gemini reports times inside the piece it was shown. If a time is larger than the piece is long it must
    already be absolute, so it is left alone."""
    parsed = []
    for r in items:
        if not isinstance(r, dict):
            continue
        try:
            s, e = parse_ts(r["start"]), parse_ts(r["end"])
        except (KeyError, ValueError):
            continue
        if e <= s:
            continue
        parsed.append((r, s, e))
    if not parsed:
        return []
    length = end - start
    absolute = whole or (start > 0 and max(e for _, _, e in parsed) > length + 5)
    out = []
    for r, s, e in parsed:
        off = 0.0 if absolute else start
        out.append({**r, "start": s + off, "end": e + off})
    return out


def scout(url: str, info: dict, ask_json: Callable, durations: tuple, count: int, focus: str = "",
          progress: Optional[Callable] = None, lo: float = 0.0, hi: float = 0.0) -> list[dict]:
    """Ask Gemini (watching the video) for candidate moments; returns candidates with absolute seconds.
    lo/hi limit it to one part of the video (hi=0: until the end); Gemini is then only shown that part.
    Long videos are watched in pieces, a few at a time."""
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise ScoutError("video length unknown")
    hi = min(hi, duration) if hi else duration
    if lo >= hi - 5:
        raise ScoutError("the chosen part of the video is too short")
    ranges = chunk_ranges(hi, lo=lo)
    whole = len(ranges) == 1 and lo <= 0 and hi >= duration - 1
    want = max(4, count * 2) if len(ranges) == 1 else max(3, math.ceil(count * 2 / len(ranges)) + 1)
    prompt = build_prompt(info, durations, want, focus)
    raw: list[dict] = []
    failures = 0
    done = 0

    def watch(piece):
        s, e = piece
        try:
            return piece, ask_json(video_parts(url, prompt, s, e, whole=whole), media_low=True), None
        except ClipperError as err:
            return piece, None, err

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(3, len(ranges))) as pool:
        for (s, e), reply, err in pool.map(watch, ranges):
            if progress:
                progress(done, len(ranges))
            done += 1
            if err is not None:
                failures += 1
                log.warning("Gemini could not watch %s-%s: %s", fmt_ts(s), fmt_ts(e), err)
                continue
            items = reply.get("clips") if isinstance(reply, dict) else reply
            found = _to_absolute(items if isinstance(items, list) else [], s, e, whole)
            log.info("Gemini found %d possible moments in %s-%s", len(found), fmt_ts(s), fmt_ts(e))
            raw += found
    if not raw:
        raise ScoutError("Gemini could not watch this video" if failures == len(ranges) else "Gemini found no moments")
    return raw


def shortlist(raw: list[dict], duration: float, count: int, max_overlap: float = 0.2, lo: float = 0.0) -> list[dict]:
    """Best-scoring non-overlapping candidates that lie inside the video (and inside [lo, duration])."""
    def score(r):
        try:
            return float(r.get("score", 0))
        except (TypeError, ValueError):
            return 0.0
    out: list[dict] = []
    for r in sorted(raw, key=lambda r: -score(r)):
        if r["start"] >= duration or r["end"] <= lo:
            continue
        r = {**r, "start": max(r["start"], lo), "end": min(r["end"], duration)}
        if any(_overlap_ratio(r, a) > max_overlap for a in out):
            continue
        out.append(r)
        if len(out) >= count + SPARE:
            break
    return out


def windows(cands: list[dict], duration: float, lo: float = 0.0) -> list[tuple[float, float]]:
    """Audio ranges to download: each candidate plus padding, nearby ones merged (never outside [lo, duration])."""
    spans = sorted((max(lo, c["start"] - PAD_BEFORE), min(duration, c["end"] + PAD_AFTER)) for c in cands)
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s - merged[-1][1] <= MERGE_GAP:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def shift_transcript(tr: dict, offset: float) -> dict:
    """Move a transcript of a downloaded piece onto the video's own timeline."""
    segs = []
    for seg in tr["segments"]:
        s = {**seg, "start": float(seg["start"]) + offset, "end": float(seg["end"]) + offset}
        if seg.get("words"):
            s["words"] = [{**w, "start": float(w["start"]) + offset, "end": float(w["end"]) + offset} for w in seg["words"]]
        segs.append(s)
    return {**tr, "segments": segs}


def merge_transcripts(pieces: list[dict], duration: float, spans: list[tuple[float, float]]) -> dict:
    segs = sorted((s for p in pieces for s in p["segments"]), key=lambda s: s["start"])
    langs = [p.get("language") for p in pieces if p.get("language")]
    return {"language": max(set(langs), key=langs.count) if langs else "unknown",
            "engine": pieces[0].get("engine", ""), "word_level": all(p.get("word_level") for p in pieces),
            "duration": duration, "segments": segs, "partial": True,
            "windows": [[round(s, 2), round(e, 2)] for s, e in spans]}
