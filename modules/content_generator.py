"""Per-clip metadata: 5 titles, SEO description, hashtags, hooks, cover text, category.
Grounded in the clip's actual transcript. No real-time trend claims."""
from __future__ import annotations

import logging
import re
from typing import Callable, Optional

from .clip_selector import norm_hashtags
from .utilities import ClipperError

log = logging.getLogger("clipper")

CATEGORY_IDS = {
    "Film & Animation": "1", "Autos & Vehicles": "2", "Music": "10", "Pets & Animals": "15", "Sports": "17",
    "Travel & Events": "19", "Gaming": "20", "People & Blogs": "22", "Comedy": "23", "Entertainment": "24",
    "News & Politics": "25", "Howto & Style": "26", "Education": "27", "Science & Technology": "28",
    "Nonprofits & Activism": "29",
}
DEFAULT_CATEGORY = "People & Blogs"


def _prompt(clip: dict, excerpt: str, info: dict) -> str:
    return f"""Write metadata for a YouTube Short cut from a longer video.
Source video title: {info.get('title', '')!r}
Clip transcript (what is actually said):
\"\"\"{excerpt[:3500]}\"\"\"

Rules: describe only what is in the transcript. No clickbait, no exaggeration, no false promises, no
claims about trending topics. Write in the language of the transcript (Hinglish stays Hinglish).
Titles: 5 different angles, each under 70 characters, no ALL CAPS, no emoji spam.

Reply with JSON only:
{{"titles": ["", "", "", "", ""],
"description": "2-3 sentence SEO-friendly summary of the clip",
"hashtags": ["#tag", "... 5-8 tags drawn from the actual topic"],
"hooks": ["3 alternative opening-hook lines, taken or adapted from the speech"],
"cover_text": "max 6 words for the thumbnail",
"category": "one of: {', '.join(CATEGORY_IDS)}"}}"""


def _clean_titles(titles, fallback: str) -> list[str]:
    out, seen = [], set()
    for t in titles if isinstance(titles, list) else []:
        t = re.sub(r"\s+", " ", str(t)).strip().strip('"')[:90]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    if fallback and fallback.lower() not in seen:
        out.append(fallback[:90])
    return out[:5] or [fallback or "Short clip"]


def fallback_metadata(clip: dict, info: dict) -> dict:
    """Used when Gemini is unavailable: built only from fields the clip selector already produced."""
    return {"titles": [clip["title"]], "description": clip.get("description") or clip.get("reason", ""),
            "hashtags": clip.get("hashtags", []), "hooks": [clip["hook"]] if clip.get("hook") else [],
            "cover_text": clip["title"][:40], "category": DEFAULT_CATEGORY}


def rebuild(meta: dict, info: dict, extra_tags: str = "#Shorts", footer: str = "", credit: bool = True) -> dict:
    """(Re)compute platform-ready fields from the editable ones: title, description, hashtags, category."""
    tags = norm_hashtags(meta.get("hashtags"), 8)
    extras = [t for t in extra_tags.split() if t]
    all_tags = tags + [t for t in extras if t.lower() not in {x.lower() for x in tags}]
    category = meta.get("category") if meta.get("category") in CATEGORY_IDS else DEFAULT_CATEGORY
    title = re.sub(r"\s+", " ", str(meta.get("title") or "")).strip()[:95] or "Short clip"
    credit_line = f"Clip from: {info.get('title', '')} {info.get('url', '')}".strip() if credit else ""
    desc = str(meta.get("description") or "").strip()
    yt_desc = "\n\n".join(p for p in (desc, str(footer).strip(), credit_line, " ".join(all_tags)) if p)
    meta.update(title=title, hashtags=tags, category=category, description=desc, platforms={
        "youtube": {"title": title, "description": yt_desc, "tags": [t.lstrip("#") for t in all_tags],
                    "categoryId": CATEGORY_IDS[category]},
        "instagram": {"caption": f"{title}\n\n{desc}\n\n{' '.join(tags)}"},
        "tiktok": {"caption": f"{title} {' '.join(tags[:5])}"},
    })
    return meta


def generate_metadata(clip: dict, excerpt: str, info: dict, ask_json: Optional[Callable],
                      extra_tags: str = "#Shorts", footer: str = "", credit: bool = True,
                      category_override: str = "") -> dict:
    data = None
    if ask_json is not None and excerpt.strip():
        try:
            data = ask_json(_prompt(clip, excerpt, info))
            if not isinstance(data, dict):
                data = None
        except ClipperError as e:
            log.warning("Metadata generation failed for %s (%s); using selector fields", clip["id"], e)
    base = fallback_metadata(clip, info)
    data = data or {}
    titles = _clean_titles(data.get("titles"), clip["title"])
    category = data.get("category") if data.get("category") in CATEGORY_IDS else DEFAULT_CATEGORY
    if category_override in CATEGORY_IDS:
        category = category_override
    meta = {
        "titles": titles, "title": titles[0],
        "description": str(data.get("description") or base["description"]).strip(),
        "hashtags": data.get("hashtags") or base["hashtags"],
        "hooks": [str(h).strip() for h in (data.get("hooks") or base["hooks"]) if str(h).strip()][:3],
        "cover_text": str(data.get("cover_text") or base["cover_text"]).strip()[:60], "category": category,
    }
    return rebuild(meta, info, extra_tags, footer, credit)
