"""Word-timed captions rendered as ASS (libass): real karaoke sweeps (\\kf) and per-word
pop animation, not static subtitles. Also SRT export. Pure Python, no FFmpeg needed here."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Optional

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
SENT_END = re.compile(r"[.!?…।]['\"”’)\]]*$")
STYLE_NAMES = ("karaoke", "bold", "minimal", "neon", "podcast")


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}&".upper()


@dataclass(frozen=True)
class StyleSpec:
    size: int
    line_chars: int
    base: str          # text colour before/outside highlight
    highlight: str
    outline_color: str
    outline: int
    shadow: int
    bold: int
    uppercase: bool
    mode: str          # sweep | emphasis | static | pop


SPECS = {
    "karaoke": StyleSpec(80, 20, "#FFFFFF", "#FFE600", "#000000", 6, 2, 1, False, "sweep"),
    "bold":    StyleSpec(104, 14, "#FFFFFF", "#FFD400", "#000000", 8, 3, 1, True, "emphasis"),
    "minimal": StyleSpec(62, 28, "#FFFFFF", "#FFFFFF", "#000000", 3, 0, 0, False, "static"),
    "neon":    StyleSpec(84, 18, "#BFF8FF", "#FF2BD6", "#2A0A5E", 5, 0, 1, False, "pop"),
    "podcast": StyleSpec(66, 30, "#FFFFFF", "#3DDC84", "#000000", 4, 1, 1, False, "sweep"),
}

def style_spec(style: str, over: Optional[dict] = None) -> StyleSpec:
    """Built-in style with the user's overrides applied (empty/0/None = keep the style default)."""
    spec = SPECS.get(style, SPECS["karaoke"])
    o = over or {}
    ch = {}
    if o.get("size"): ch["size"] = int(o["size"])
    if o.get("line_chars"): ch["line_chars"] = int(o["line_chars"])
    if o.get("color"): ch["base"] = o["color"]
    if o.get("highlight"): ch["highlight"] = o["highlight"]
    if o.get("outline_color"): ch["outline_color"] = o["outline_color"]
    if o.get("outline"): ch["outline"] = int(o["outline"])
    if o.get("uppercase") in (True, False): ch["uppercase"] = bool(o["uppercase"])
    if o.get("bold") in (True, False): ch["bold"] = 1 if o["bold"] else 0
    return dataclasses.replace(spec, **ch) if ch else spec


def overrides_from_cfg(cfg) -> dict:
    tri = {"yes": True, "no": False}
    return {"size": cfg.caption_size, "color": cfg.caption_color, "highlight": cfg.caption_highlight,
            "outline_color": cfg.caption_outline_color, "outline": cfg.caption_outline,
            "line_chars": cfg.caption_line_chars, "margin": cfg.caption_margin,
            "uppercase": tri.get(cfg.caption_uppercase), "bold": tri.get(cfg.caption_bold)}


SPEAKER_COLORS = ["#FFD400", "#00E5FF", "#FF8A80", "#B9F6CA"]


# ------------------------------------------------------------------ words
def remap_time(t: float, keep: list[tuple]) -> Optional[float]:
    """Map a clip-relative time through kept intervals (silence removal). None if t is removed."""
    acc = 0.0
    for a, b in keep:
        if a <= t <= b:
            return acc + (t - a)
        acc += b - a
    return None


def remap_words(words: list[dict], keep: Optional[list[tuple]]) -> list[dict]:
    if not keep:
        return words
    out = []
    for w in words:
        s = remap_time(w["s"], keep)
        e = remap_time(w["e"], keep)
        if s is None and e is None:
            continue
        if s is None:   # word starts inside a removed gap: start at next kept interval
            s = next(remap_time(a, keep) for a, b in keep if a >= w["s"])
        if e is None:
            e = s + 0.05
        out.append({**w, "s": s, "e": max(e, s + 0.02)})
    return out


def words_for_clip(transcript: dict, start: float, end: float) -> list[dict]:
    """Clip-relative [{w, s, e, speaker}] for words inside [start, end]."""
    out = []
    for seg in transcript["segments"]:
        for w in seg.get("words", []):
            ws, we = float(w["start"]), float(w["end"])
            if we <= start or ws >= end:
                continue
            out.append({"w": w["word"], "s": max(0.0, ws - start), "e": min(end, we) - start,
                        "speaker": seg.get("speaker")})
    return out


# ------------------------------------------------------------------ layout
def _lines(tokens: list[str], line_chars: int) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    width = 0
    for i, t in enumerate(tokens):
        add = len(t) + (1 if lines[-1] else 0)
        if lines[-1] and width + add > line_chars:
            lines.append([])
            width = 0
            add = len(t)
        lines[-1].append(i)
        width += add
    return lines


def paginate(words: list[dict], line_chars: int, max_lines: int = 2, max_gap: float = 0.8) -> list[list[dict]]:
    """Group words into pages of at most `max_lines` lines."""
    pages: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            gap = w["s"] - cur[-1]["e"]
            too_long = len(_lines([x["w"] for x in cur + [w]], line_chars)) > max_lines
            sent_break = bool(SENT_END.search(cur[-1]["w"])) and len(cur) >= 3
            speaker_change = w.get("speaker") != cur[-1].get("speaker")
            if gap > max_gap or too_long or sent_break or speaker_change:
                pages.append(cur)
                cur = []
        cur.append(w)
    if cur:
        pages.append(cur)
    return pages


# ------------------------------------------------------------------ ASS
def _t(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _emphasis(word: str) -> bool:
    core = re.sub(r"\W", "", word)
    return len(core) >= 7 or any(c.isdigit() for c in core) or (len(core) > 2 and core.isupper())


def _style_line(name: str, font: str, spec: StyleSpec, alignment: int, margin_v: int) -> str:
    # ASS: PrimaryColour = colour AFTER a karaoke sweep, SecondaryColour = BEFORE.
    primary = spec.highlight if spec.mode == "sweep" else spec.base
    secondary = spec.base
    return (f"Style: {name},{font},{spec.size},{ass_color(primary)},{ass_color(secondary)},"
            f"{ass_color(spec.outline_color)},{ass_color('#000000', 0x80)},{spec.bold},0,0,0,100,100,0,0,1,"
            f"{spec.outline},{spec.shadow},{alignment},70,70,{margin_v},1")


def generate_ass(words: list[dict], style: str = "karaoke", *, position: str = "bottom",
                 font: str = "Noto Sans", font_hindi: str = "Noto Sans Devanagari",
                 hook_text: str = "", hook_sec: float = 2.5, width: int = 1080, height: int = 1920,
                 over: Optional[dict] = None) -> str:
    style = style if style in SPECS else "karaoke"
    spec = style_spec(style, over)
    alignment, margin_v = {"top": (8, 430), "middle": (5, 0)}.get(position, (2, 400))
    if (over or {}).get("margin"):
        margin_v = int(over["margin"])
    hook_size = 74
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
        "MarginV,Encoding",
        _style_line("Cap", font, spec, alignment, margin_v),
        _style_line("CapHi", font_hindi, spec, alignment, margin_v),
        f"Style: Hook,{font},{hook_size},{ass_color('#FFFFFF')},{ass_color('#FFFFFF')},{ass_color('#000000')},"
        f"{ass_color('#000000', 0x80)},1,0,0,0,100,100,0,0,1,6,2,8,80,80,260,1",
        f"Style: HookHi,{font_hindi},{hook_size},{ass_color('#FFFFFF')},{ass_color('#FFFFFF')},"
        f"{ass_color('#000000')},{ass_color('#000000', 0x80)},1,0,0,0,100,100,0,0,1,6,2,8,80,80,260,1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    events: list[str] = []
    if hook_text.strip():
        hs = "HookHi" if DEVANAGARI.search(hook_text) else "Hook"
        events.append(f"Dialogue: 1,{_t(0)},{_t(hook_sec)},{hs},,0,0,0,,{{\\fad(150,250)}}"
                      f"{_esc(hook_text.strip()[:90])}")

    pages = paginate(words, spec.line_chars, 2)
    speakers: list = []
    for pi, page in enumerate(pages):
        nxt_start = pages[pi + 1][0]["s"] if pi + 1 < len(pages) else None
        p_start = max(0.0, page[0]["s"] - 0.08)
        p_end = page[-1]["e"] + 0.20
        if nxt_start is not None:
            p_end = min(p_end, nxt_start - 0.01)
        p_end = max(p_end, page[-1]["e"])
        texts = [(_esc(w["w"]).upper() if spec.uppercase else _esc(w["w"])) for w in page]
        layout = _lines(texts, spec.line_chars)
        has_hi = any(DEVANAGARI.search(t) for t in texts)
        sname = "CapHi" if has_hi else "Cap"
        label = ""
        spk = page[0].get("speaker")
        if style == "podcast" and spk:
            if spk not in speakers:
                speakers.append(spk)
            col = ass_color(SPEAKER_COLORS[speakers.index(spk) % len(SPEAKER_COLORS)])
            label = f"{{\\1c{col}\\b1}}{_esc(str(spk))}:{{\\r}}\\N"

        if spec.mode == "pop":   # one event per word; active word pops and glows
            for wi, w in enumerate(page):
                ws = page[wi]["s"]
                we = page[wi + 1]["s"] if wi + 1 < len(page) else p_end
                if wi == 0:
                    ws = p_start
                body = _render_static(texts, layout, active=wi, spec=spec)
                events.append(f"Dialogue: 0,{_t(ws)},{_t(max(we, ws + 0.05))},{sname},,0,0,0,,{label}{body}")
            continue

        if spec.mode == "sweep":
            body = _render_sweep(page, texts, layout, p_start)
        elif spec.mode == "emphasis":
            body = _render_emphasis(page, texts, layout, spec)
        else:
            body = _render_static(texts, layout, active=None, spec=spec)
        events.append(f"Dialogue: 0,{_t(p_start)},{_t(p_end)},{sname},,0,0,0,,{label}{body}")
    return "\n".join(header + events) + "\n"


def generate_single_ass(text: str, duration: float, style: str = "karaoke", *, position: str = "bottom",
                        font: str = "Noto Sans", font_hindi: str = "Noto Sans Devanagari", width: int = 1080,
                        height: int = 1920, over: Optional[dict] = None, max_lines: int = 4) -> str:
    """One fixed caption shown for the whole clip (no word timing), in the chosen style's font, size and colours."""
    style = style if style in SPECS else "karaoke"
    spec = dataclasses.replace(style_spec(style, over), mode="static")
    alignment, margin_v = {"top": (8, 430), "middle": (5, 0)}.get(position, (2, 400))
    if (over or {}).get("margin"):
        margin_v = int(over["margin"])
    text = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    toks = text.split(" ") if text else []
    cpl = max(spec.line_chars, 16)
    lines = [" ".join(toks[i] for i in ln) for ln in _lines(toks, cpl)] if toks else []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;:") + "…"
    body = "\\N".join(_esc(l.upper() if spec.uppercase else l) for l in lines)
    sname = "CapHi" if DEVANAGARI.search(text) else "Cap"
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
        "MarginV,Encoding",
        _style_line("Cap", font, spec, alignment, margin_v), _style_line("CapHi", font_hindi, spec, alignment, margin_v),
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    ev = [f"Dialogue: 0,{_t(0)},{_t(max(0.5, duration) + 1)},{sname},,0,0,0,,{{\\fad(200,0)}}{body}"] if body else []
    return "\n".join(header + ev) + "\n"


def _join(layout, parts):
    """Join per-word strings with spaces inside a line and \\N between lines."""
    lines = []
    for line in layout:
        lines.append(" ".join(parts[i] for i in line))
    return "\\N".join(lines)


def _render_sweep(page, texts, layout, p_start):
    """{\\kf<cs>} per word: libass sweeps the highlight across each word while it is spoken."""
    parts = []
    first = int(round((page[0]["s"] - p_start) * 100))
    for i, w in enumerate(page):
        nxt = page[i + 1]["s"] if i + 1 < len(page) else w["e"]
        k = max(1, int(round(nxt * 100)) - int(round(w["s"] * 100)))
        parts.append(f"{{\\kf{k}}}{texts[i]}")
    body = _join(layout, parts)
    return (f"{{\\k{first}}}" if first > 0 else "") + body


def _render_emphasis(page, texts, layout, spec):
    parts = []
    hi, base = ass_color(spec.highlight), ass_color(spec.base)
    for i, w in enumerate(page):
        parts.append(f"{{\\1c{hi}}}{texts[i]}{{\\1c{base}}}" if _emphasis(w["w"]) else texts[i])
    return _join(layout, parts)


def _render_static(texts, layout, active, spec):
    parts = []
    hi, base = ass_color(spec.highlight), ass_color(spec.base)
    for i, t in enumerate(texts):
        if active is not None and i == active:
            parts.append(f"{{\\1c{hi}\\3c{hi}\\blur3\\fscx100\\fscy100\\t(0,90,\\fscx125\\fscy125)"
                         f"\\t(90,180,\\fscx112\\fscy112)}}{t}{{\\r}}")
        elif active is not None:
            parts.append(f"{{\\1c{base}}}{t}")
        else:
            parts.append(t)
    return _join(layout, parts)


# ------------------------------------------------------------------ SRT
def _srt_t(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    return f"{ms // 3600000:02d}:{(ms // 60000) % 60:02d}:{(ms // 1000) % 60:02d},{ms % 1000:03d}"


def generate_srt(words: list[dict], line_chars: int = 28) -> str:
    out = []
    for i, page in enumerate(paginate(words, line_chars, 2), 1):
        texts = [w["w"] for w in page]
        text = "\n".join(" ".join(texts[j] for j in line) for line in _lines(texts, line_chars))
        out.append(f"{i}\n{_srt_t(page[0]['s'])} --> {_srt_t(page[-1]['e'])}\n{text}\n")
    return "\n".join(out)
