"""Gemini REST client (requests only) with automatic model fallback.

The API key travels in a header, never in the URL. If a model fails (rate limit, overload,
retired/unknown model, empty answer, unsupported input) the next model in the chain is used.
Models that are rate-limited/overloaded rest for a cooldown and are retried later; retired
models (404) are skipped for the rest of the run.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional, Sequence, Union

import requests

from .utilities import ClipperError, redact

log = logging.getLogger("clipper")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
COOLDOWN_RATE = 90          # seconds a 429'd model rests
COOLDOWN_DAILY = 3600       # 429 that mentions a per-day quota
COOLDOWN_SERVER = 60        # 5xx / network error


class GeminiError(ClipperError):
    pass


class _Switch(Exception):
    """This model failed for this request; try the next one."""

    def __init__(self, reason: str, cooldown: float = 0, dead: bool = False):
        super().__init__(reason)
        self.reason, self.cooldown, self.dead = reason, cooldown, dead


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating ```json fences and stray prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except ValueError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = t.find(open_c), t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except ValueError:
                continue
    raise ValueError("no valid JSON found in model reply")


def model_chain(primary: str, fallbacks: Union[str, Sequence[str]] = ()) -> list[str]:
    if isinstance(fallbacks, str):
        fallbacks = fallbacks.split(",")
    out: list[str] = []
    for m in [primary, *fallbacks]:
        m = (m or "").strip()
        if m.startswith("models/"):
            m = m[7:]
        if m and m not in out:
            out.append(m)
    return out


class GeminiClient:
    def __init__(self, api_key: str, model: str, *, fallbacks: Union[str, Sequence[str]] = (),
                 session: Optional[requests.Session] = None, timeout: int = 180, rounds: int = 3,
                 sleep=time.sleep, clock=time.monotonic):
        if not api_key:
            raise GeminiError("GEMINI_API_KEY is not set")
        self._key = api_key
        self.models = model_chain(model, fallbacks)
        if not self.models:
            raise GeminiError("No Gemini model configured (GEMINI_MODEL / GEMINI_MODELS)")
        self.model = self.models[0]          # model used by the last successful call
        self.session = session or requests.Session()
        self.timeout = timeout
        self.rounds = max(1, rounds)
        self._sleep, self._clock = sleep, clock
        self._dead: set[str] = set()
        self._cool: dict[str, float] = {}

    # ------------------------------------------------------------------ one call to one model
    @staticmethod
    def _text_of(data: dict) -> str:
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise _Switch(f"no answer ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            raise _Switch(f"empty text (finishReason={cands[0].get('finishReason')})")
        return text

    def _call(self, model: str, body: dict) -> str:
        url = f"{API_ROOT}/{model}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": self._key}
        try:
            r = self.session.post(url, headers=headers, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise _Switch(f"network error ({type(e).__name__})", COOLDOWN_SERVER) from None
        code, low = r.status_code, (r.text or "").lower()
        if code == 200:
            try:
                return self._text_of(r.json())
            except ValueError:
                raise _Switch("response was not JSON", 30) from None
        if code == 429:
            raise _Switch("rate limit / quota (HTTP 429)", COOLDOWN_DAILY if "perday" in low.replace(" ", "")
                          else COOLDOWN_RATE)
        if code in (500, 502, 503, 504):
            raise _Switch(f"server busy (HTTP {code})", COOLDOWN_SERVER)
        if code == 404:
            raise _Switch("model not found or retired (HTTP 404)", dead=True)
        key_problem = code == 401 or "api key not valid" in low or "api_key_invalid" in low or "leaked" in low
        if key_problem:
            raise GeminiError(f"Gemini rejected the API key (HTTP {code}). Check GEMINI_API_KEY.")
        if code == 403:
            raise _Switch("no access to this model (HTTP 403)", dead=True)
        raise _Switch(f"HTTP {code}: {redact((r.text or '')[:160])}")

    # ------------------------------------------------------------------ chain
    def _request(self, body: dict) -> str:
        errors: dict[str, str] = {}
        for rnd in range(self.rounds):
            live = [m for m in self.models if m not in self._dead]
            if not live:
                break
            now = self._clock()
            ready = [m for m in live if self._cool.get(m, 0) <= now] or live
            for m in ready:
                try:
                    text = self._call(m, body)
                except _Switch as s:
                    errors[m] = s.reason
                    if s.dead:
                        self._dead.add(m)
                    elif s.cooldown:
                        self._cool[m] = self._clock() + s.cooldown
                    log.warning("Gemini model %s failed: %s", m, s.reason)
                    continue
                if m != self.model:
                    log.info("Gemini now using model %s", m)
                self.model = m
                self._cool.pop(m, None)
                return text
            if rnd < self.rounds - 1 and any(x not in self._dead for x in self.models):
                delay = 10 * (rnd + 1)
                log.warning("All available Gemini models failed; waiting %ss before retrying", delay)
                self._sleep(delay)
        summary = "; ".join(f"{m}: {e}" for m, e in errors.items()) or "no usable model"
        raise GeminiError(f"All Gemini models failed ({summary})")

    def generate_text(self, prompt: Union[str, list], *, json_mode: bool = False,
                      temperature: float = 0.3) -> str:
        parts = [{"text": prompt}] if isinstance(prompt, str) else prompt
        body: dict = {"contents": [{"parts": parts}], "generationConfig": {"temperature": temperature}}
        if json_mode:
            body["generationConfig"]["responseMimeType"] = "application/json"
        return self._request(body)

    def generate_json(self, prompt: Union[str, list], *, temperature: float = 0.3) -> Any:
        text = self.generate_text(prompt, json_mode=True, temperature=temperature)
        try:
            return extract_json(text)
        except ValueError:
            log.warning("Gemini reply was not valid JSON; asking once more")
            again = self.generate_text(prompt, json_mode=True, temperature=0.0)
            try:
                return extract_json(again)
            except ValueError as e:
                raise GeminiError("Gemini did not return valid JSON") from e


def make_client(cfg) -> Optional[GeminiClient]:
    if not cfg.gemini_api_key:
        return None
    return GeminiClient(cfg.gemini_api_key, cfg.gemini_model, fallbacks=cfg.gemini_models)
