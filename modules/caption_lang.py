"""Caption language handling.

A clip's captions are a list of *phrases* (one per transcript segment), each with clip-relative word timings:
  {"s", "e", "text", "words": [{"w","s","e"}], "approx": bool, "edited": bool}

Targets:  auto (as spoken) | hi (Devanagari) | en | hinglish (Hindi written in English letters)
- Same language, different script (Devanagari <-> Roman): converted WORD FOR WORD, timing stays exact.
- Translation (Hindi <-> English): translated PER PHRASE; words are spread across the phrase's real time span,
  so sync is accurate per phrase, not per word (marked approx=True).
- If Gemini is unavailable, captions stay as spoken and a note says so. Nothing is invented offline.
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from .transcription import distribute_words
from .utilities import ClipperError

log = logging.getLogger("clipper")
DEV = re.compile(r"[ऀ-ॿ]")
LATIN = re.compile(r"[A-Za-z]")
TARGETS = ("auto", "hi", "en", "hinglish")
BATCH_WORDS = 70
BATCH_PHRASES = 25


def script_of(words: list[dict]) -> str:
    text = " ".join(w["w"] for w in words)
    d, l = len(DEV.findall(text)), len(LATIN.findall(text))
    return "dev" if d > l else "latin"


def build_phrases(transcript: dict, clip: dict) -> list[dict]:
    """Real word timings from the transcript, grouped by transcript segment, relative to the clip start."""
    start, end = clip["start"], clip["end"]
    phrases = []
    for seg in transcript["segments"]:
        if seg["end"] <= start or seg["start"] >= end:
            continue
        words = []
        for w in seg.get("words", []):
            ws, we = float(w["start"]), float(w["end"])
            if we <= start or ws >= end:
                continue
            words.append({"w": w["word"], "s": round(max(0.0, ws - start), 3), "e": round(min(end, we) - start, 3)})
        if words:
            phrases.append({"s": words[0]["s"], "e": words[-1]["e"], "text": " ".join(x["w"] for x in words),
                            "words": words, "approx": False, "edited": False})
    return phrases


def flatten(phrases: list[dict]) -> list[dict]:
    return [{**w, "speaker": None} for p in phrases for w in p["words"]]


def set_phrase_text(phrase: dict, text: str, approx: bool = True) -> dict:
    """Replace a phrase's text, spreading the new words over the phrase's own time span."""
    words = [{"w": d["word"], "s": d["start"], "e": d["end"]} for d in distribute_words(text, phrase["s"], phrase["e"])]
    if words:
        phrase["words"], phrase["text"] = words, " ".join(w["w"] for w in words)
        phrase["approx"] = approx
    return phrase


def apply_edits(phrases: list[dict], edits: dict) -> list[dict]:
    """edits: {phrase index (int or str): new text}. An empty text removes the caption for that phrase."""
    for k, text in (edits or {}).items():
        i = int(k)
        if not 0 <= i < len(phrases):
            continue
        text = str(text).strip()
        if not text:
            phrases[i].update(words=[], text="", edited=True)
        else:
            set_phrase_text(phrases[i], text)
            phrases[i]["edited"] = True
    return phrases


# ---------------------------------------------------------------- Gemini helpers
_SCRIPT_PROMPT = """Rewrite each word below in {target}. Keep EXACTLY the same number of words, in the same order,
one output item per input item. Do not translate, add, merge or drop words. Keep punctuation attached to its word.
{rules}
Reply with JSON only: {{"words": ["..."]}}
Input ({n} words): {items}"""
_SCRIPT_TARGETS = {
    "dev": ("Hindi in Devanagari script", "Hindi words go in Devanagari. English words stay in English letters unless they are commonly written in Devanagari."),
    "roman": ("Hinglish (Hindi written in English letters, the way people type it on a phone)",
              "Use simple common spellings (e.g. 'kya', 'hai', 'nahi', 'bahut'). Numbers and English words stay as they are."),
}
_TRANSLATE_PROMPT = """Translate each phrase into {target}. The phrases are consecutive subtitles from one video.
Keep the meaning, tone and level of formality. Short natural subtitle wording. Do not add or remove information.
Each phrase must stay one phrase. Names stay as names.
Reply with JSON only: {{"phrases": [{{"i": 0, "text": "..."}}]}}
Input: {items}"""
_TRANSLATE_TARGETS = {
    "en": "natural English",
    "hi": "Hindi in Devanagari script (English brand names may stay in English letters)",
    "hinglish": "Hinglish: Hindi written in English letters, the way people type it on a phone",
}


def _json_items(values) -> str:
    import json
    return json.dumps(values, ensure_ascii=False)


def convert_script(phrases: list[dict], target_script: str, ask_json: Callable, notes: list) -> None:
    """Word-for-word script change; timing untouched. Batches that fail validation stay as spoken."""
    refs = [w for p in phrases for w in p["words"]]
    name, rules = _SCRIPT_TARGETS[target_script]
    for i in range(0, len(refs), BATCH_WORDS):
        batch = refs[i:i + BATCH_WORDS]
        prompt = _SCRIPT_PROMPT.format(target=name, rules=rules, n=len(batch), items=_json_items([w["w"] for w in batch]))
        out = None
        for attempt in range(2):
            try:
                reply = ask_json(prompt)
            except ClipperError as e:
                notes.append(f"Gemini unavailable for script conversion ({e}); kept as spoken")
                return
            items = reply.get("words") if isinstance(reply, dict) else None
            if isinstance(items, list) and len(items) == len(batch) and all(isinstance(x, str) and x.strip() for x in items):
                out = items
                break
        if out is None:
            notes.append("Part of the captions could not be converted and was kept as spoken")
            continue
        for w, new in zip(batch, out):
            w["w"] = new.strip()
    for p in phrases:
        p["text"] = " ".join(w["w"] for w in p["words"])


def translate_phrases(phrases: list[dict], target: str, ask_json: Callable, notes: list) -> None:
    for i in range(0, len(phrases), BATCH_PHRASES):
        batch = phrases[i:i + BATCH_PHRASES]
        items = [{"i": j, "text": p["text"]} for j, p in enumerate(batch)]
        try:
            reply = ask_json(_TRANSLATE_PROMPT.format(target=_TRANSLATE_TARGETS[target], items=_json_items(items)))
        except ClipperError as e:
            notes.append(f"Gemini unavailable for translation ({e}); kept as spoken")
            return
        got = {}
        for r in (reply.get("phrases") if isinstance(reply, dict) else None) or []:
            try:
                got[int(r["i"])] = str(r["text"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
        missing = 0
        for j, p in enumerate(batch):
            if got.get(j):
                set_phrase_text(p, got[j])
            else:
                missing += 1
        if missing:
            notes.append(f"{missing} phrase(s) could not be translated and were kept as spoken")


def make_plan(transcript: dict, clip: dict, target: str, ask_json: Optional[Callable]) -> tuple[list[dict], list[str]]:
    """Returns (phrases, notes). notes are shown to the user when something fell back."""
    phrases = build_phrases(transcript, clip)
    notes: list[str] = []
    if not phrases or target in ("auto", "", None):
        return phrases, notes
    if target not in TARGETS:
        return phrases, [f"Unknown caption language {target!r}; kept as spoken"]
    src_script = script_of(flatten(phrases))
    lang = str(transcript.get("language", "")).lower()
    hindi_speech = lang.startswith("hi") or lang.startswith("ur") or src_script == "dev"
    if target == "hi":
        if src_script == "dev":
            return phrases, notes
        action = ("script", "dev") if hindi_speech else ("translate", "hi")
    elif target == "hinglish":
        if src_script == "latin" and hindi_speech:
            return phrases, notes
        action = ("script", "roman") if src_script == "dev" else ("translate", "hinglish")
    else:  # en
        if not hindi_speech and src_script == "latin":
            return phrases, notes
        action = ("translate", "en")
    if ask_json is None:
        return phrases, ["Gemini key missing: captions kept as spoken"]
    kind, tgt = action
    if kind == "script":
        convert_script(phrases, tgt, ask_json, notes)
    else:
        translate_phrases(phrases, tgt, ask_json, notes)
    return phrases, notes
