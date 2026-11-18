"""Video templates: blurred background (default), white tweet card, plain black.

Pure layout maths plus the ASS text for the tweet card. FFmpeg does the compositing (video_editor.build_video_graph).
The tweet text is placed line by line at exact positions, so the card height is known before rendering and the
card always ends right above the video's top-left corner, also for Hindi."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .captions import DEVANAGARI, _esc, _lines, ass_color

W, H = 1080, 1920
TEMPLATES = ("blur", "tweet", "black", "face")
FIT_TEMPLATES = ("tweet", "black")
AVATAR = 112
NAME_SIZE, HANDLE_SIZE, TEXT_SIZE, LINE_H = 44, 38, 52, 68
GAP_HEADER, GAP_VIDEO = 20, 18
MAX_LINES = 4
CAPTION_ZONE = 220                      # room kept under the video for captions
INK, GREY, BLUE = "#0F1419", "#536471", "#1D9BF0"
BIG = "9:00:00.00"


@dataclass
class Layout:
    template: str
    vw: int
    vh: int
    vx: int
    vy: int
    card_x: int = 0
    card_y: int = 0
    card_h: int = 0
    lines: list = field(default_factory=list)
    cap_top: int = 0                     # y where captions start, just under the video (0 = no room: use the default spot)


def _even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()


_NARROW = set("iljtfrI.,:;'!|()[]\" ")
_WIDE = set("mwMW@%")


def char_em(ch: str) -> float:
    """Rough advance width in em for a typical proportional font. Slightly generous so wrapped text never overflows."""
    if unicodedata.category(ch) == "Mn":
        return 0.0                       # combining marks (Devanagari vowel signs, virama) add no width
    if DEVANAGARI.match(ch):
        return 0.52
    if ch in _NARROW:
        return 0.28
    if ch in _WIDE:
        return 0.85
    if ch.isupper():
        return 0.66
    if ch.isdigit():
        return 0.58
    return 0.50


def text_px(s: str, size: int = TEXT_SIZE) -> float:
    return sum(char_em(c) for c in s) * size


def tweet_lines(text: str, width_px: int) -> list[str]:
    """Wrap by estimated pixel width into at most MAX_LINES lines; the last line gets an ellipsis if text was cut."""
    text = clean_text(text)
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    toks: list[str] = []
    for w in text.split(" "):                      # a single huge word (a link, say) is cut into pieces that fit
        while text_px(w) > width_px and len(w) > 1:
            k = len(w)
            while k > 1 and text_px(w[:k]) > width_px:
                k -= 1
            toks.append(w[:k])
            w = w[k:]
        toks.append(w)
    for w in toks:
        trial = f"{cur} {w}" if cur else w
        if cur and text_px(trial) > width_px:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    lines.append(cur)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        last = lines[-1]
        while last and text_px(last + "…") > width_px:
            last = last[:-1]
        lines[-1] = last.rstrip(" .,;:") + "…"
    return lines


def _cap_top(video_bottom: int) -> int:
    """Captions start 30 px under the video when at least CAPTION_ZONE + 100 px of screen is left there."""
    return video_bottom + 30 if H - video_bottom >= CAPTION_ZONE + 100 else 0


def plan_layout(template: str, src_w: int, src_h: int, tweet_text: str = "") -> Layout:
    src_w, src_h = max(2, src_w), max(2, src_h)
    if template == "tweet":
        card_x0 = 48
        lines = tweet_lines(tweet_text, W - 2 * card_x0)
        card_h = AVATAR + (GAP_HEADER + len(lines) * LINE_H if lines else 0) + GAP_VIDEO
        vh_max = H - 2 * 60 - card_h - CAPTION_ZONE
        f = min(W / src_w, vh_max / src_h)
        vw, vh = _even(src_w * f), _even(src_h * f)
        top = max(60, (H - (card_h + vh + CAPTION_ZONE)) // 2)
        vx = (W - vw) // 2
        return Layout("tweet", vw, vh, vx, top + card_h, card_x=max(card_x0, vx), card_y=top, card_h=card_h, lines=lines,
                      cap_top=_cap_top(top + card_h + vh))
    f = min(W / src_w, H / src_h)
    vw, vh = _even(src_w * f), _even(src_h * f)
    vx, vy = (W - vw) // 2, (H - vh) // 2
    return Layout("black", vw, vh, vx, vy, cap_top=_cap_top(vy + vh))


def _style(name: str, font: str, size: int, color: str, bold: int) -> str:
    return (f"Style: {name},{font},{size},{ass_color(color)},{ass_color(color)},{ass_color('#FFFFFF')},"
            f"{ass_color('#000000', 0xFF)},{bold},0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")


def tweet_ass(layout: Layout, name: str, handle: str, *, font: str = "Noto Sans", font_hindi: str = "Noto Sans Devanagari") -> str:
    """Name, handle and the wrapped tweet text. The picture is overlaid by FFmpeg."""
    name, handle = clean_text(name) or "Your Name", clean_text(handle)
    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,"
        "MarginV,Encoding",
        _style("Name", font, NAME_SIZE, INK, 1), _style("NameHi", font_hindi, NAME_SIZE, INK, 1),
        _style("Handle", font, HANDLE_SIZE, GREY, 0), _style("HandleHi", font_hindi, HANDLE_SIZE, GREY, 0),
        _style("Text", font, TEXT_SIZE, INK, 0), _style("TextHi", font_hindi, TEXT_SIZE, INK, 0),
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    tx = layout.card_x + AVATAR + 22
    ev = []

    def put(style_base: str, text: str, x: int, y: int) -> None:
        st = style_base + ("Hi" if DEVANAGARI.search(text) else "")
        ev.append(f"Dialogue: 1,0:00:00.00,{BIG},{st},,0,0,0,,{{\\an7\\pos({x},{y})}}{_esc(text)}")
    put("Name", name, tx, layout.card_y + 6)
    if handle:
        put("Handle", handle, tx, layout.card_y + 6 + NAME_SIZE + 12)
    for i, ln in enumerate(layout.lines):
        put("Text", ln, layout.card_x, layout.card_y + AVATAR + GAP_HEADER + i * LINE_H)
    return "\n".join(header + ev) + "\n"


def avatar_filters(src_idx: Optional[int], x: int, y: int, base: str, out: str) -> list[str]:
    """Round profile picture overlaid at (x, y). With no picture a plain blue disc is drawn."""
    r = AVATAR // 2
    mask = f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(lt(hypot(X-{r},Y-{r}),{r}),255,0)'"
    if src_idx is None:
        first = f"color=c={BLUE.replace('#', '0x')}:s={AVATAR}x{AVATAR}:r=30,format=gbrap,{mask}[av]"
    else:
        first = (f"[{src_idx}:v]scale={AVATAR}:{AVATAR}:force_original_aspect_ratio=increase,crop={AVATAR}:{AVATAR},"
                 f"format=gbrap,{mask}[av]")
    return [first, f"[{base}][av]overlay={x}:{y}:format=auto[{out}]"]


def caption_defaults(template: str) -> dict:
    """On the white template the caption colours that suit a dark background would vanish, so use readable ones
    unless the user chose their own."""
    if template == "tweet":
        return {"color": INK, "highlight": BLUE, "outline_color": "#FFFFFF"}
    return {}
