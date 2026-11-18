"""Templates (blurred / white tweet card / black), single-line captions, download quality. Real FFmpeg where noted."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import captions, settings, templates, video_editor  # noqa: E402
from modules.utilities import Config, ConfigError, ffmpeg_filters, probe  # noqa: E402

W, H = 1080, 1920
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or "ass" not in ffmpeg_filters(),
                                  reason="needs ffmpeg with the ass filter")


# ---------------------------------------------------------------- layout and wrapping
@pytest.mark.parametrize("text", [
    "Why most people quit too early and what to do about it",
    "WHY MOST PEOPLE QUIT TOO EARLY AND WHAT TO DO ABOUT IT NOW",
    "ज्यादातर लोग क्यों जल्दी हार मान लेते हैं और इसका हल क्या है",
    "mmm WWW @@@ " * 12,
    "Supercalifragilisticexpialidocious" * 4,
])
def test_tweet_lines_never_exceed_card_width_or_four_lines(text):
    lines = templates.tweet_lines(text, W - 96)
    assert 1 <= len(lines) <= templates.MAX_LINES
    assert all(templates.text_px(l) <= W - 96 for l in lines)


def test_tweet_lines_truncates_with_ellipsis_and_handles_empty():
    lines = templates.tweet_lines("word " * 200, W - 96)
    assert len(lines) == 4 and lines[-1].endswith("…")
    assert templates.tweet_lines("   \n ", W - 96) == []


def test_tweet_layout_sits_directly_above_video_top_left():
    lay = templates.plan_layout("tweet", 1280, 720, "Why most people quit too early and what to do about it")
    assert (lay.vw, lay.vh, lay.vx) == (1080, 606, 0)
    assert lay.card_y + lay.card_h == lay.vy                        # the card ends exactly where the video starts
    assert lay.card_x == 48 and lay.card_y >= 60                    # left aligned, never off the top
    assert lay.vy + lay.vh <= H and lay.cap_top == lay.vy + lay.vh + 30
    more = templates.plan_layout("tweet", 1280, 720, "word " * 100)
    assert more.card_h > lay.card_h and more.vy + more.vh + templates.CAPTION_ZONE <= H


def test_tweet_layout_with_narrow_and_tall_sources():
    tall = templates.plan_layout("tweet", 1080, 1920, "A short title")             # a vertical video must still fit
    assert tall.vy + tall.vh <= H and tall.vh <= H - 2 * 60 - tall.card_h - templates.CAPTION_ZONE
    assert tall.card_x == max(48, tall.vx) and tall.vx > 0                          # card aligns with the video's left edge
    assert tall.vw % 2 == 0 and tall.vh % 2 == 0


def test_black_layout_centres_and_full_screen_source_keeps_default_caption_spot():
    wide = templates.plan_layout("black", 1280, 720)
    assert (wide.vx, wide.vy) == (0, (H - wide.vh) // 2) and wide.cap_top == wide.vy + wide.vh + 30
    vertical = templates.plan_layout("black", 1080, 1920)
    assert (vertical.vw, vertical.vh, vertical.vy) == (1080, 1920, 0) and vertical.cap_top == 0


def test_tweet_ass_positions_lines_and_switches_to_hindi_font():
    lay = templates.plan_layout("tweet", 1280, 720, "Hello there. आप कैसे हैं")
    ass = templates.tweet_ass(lay, "Rohit", "@rohit", font="Latin Font", font_hindi="Hindi Font")
    assert f"\\pos({lay.card_x + templates.AVATAR + 22}," in ass and "Rohit" in ass and "@rohit" in ass
    assert "Style: TextHi,Hindi Font" in ass and "Style: Text,Latin Font" in ass
    ev = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert len(ev) == 2 + len(lay.lines)
    ys = [int(l.split("\\pos(")[1].split(",")[1].split(")")[0]) for l in ev if ",Text" in l]
    assert ys == sorted(ys) and ys[0] == lay.card_y + templates.AVATAR + templates.GAP_HEADER
    assert "Your Name" in templates.tweet_ass(lay, "", "")                       # empty name gets a visible placeholder
    assert "{" not in ass.split("[Events]")[1].replace("{\\an7", "").replace("}", "")  # text cannot inject ASS override tags


def test_tweet_text_cannot_inject_ass_tags():
    lay = templates.plan_layout("tweet", 1280, 720, "evil {\\fs200} text")
    body = "\n".join(l for l in templates.tweet_ass(lay, "a{\\b1}b", "@x").splitlines() if l.startswith("Dialogue"))
    assert "\\fs200" not in body and "\\b1" not in body


def test_caption_defaults_only_for_white_template():
    assert templates.caption_defaults("tweet")["color"] == templates.INK
    assert templates.caption_defaults("blur") == {} and templates.caption_defaults("black") == {}


# ---------------------------------------------------------------- single caption ASS
def test_single_caption_is_one_event_for_whole_clip_and_styled():
    a = captions.generate_single_ass("Why most people quit early", 12.0, "bold", font="F1", font_hindi="F2")
    ev = [l for l in a.splitlines() if l.startswith("Dialogue")]
    assert len(ev) == 1 and ",Cap," in ev[0] and ev[0].split(",")[1] == "0:00:00.00" and ev[0].split(",")[2] == "0:00:13.00"
    assert "WHY MOST" in ev[0] and "\\N" in ev[0]                                  # bold style = capitals, wrapped
    hi = captions.generate_single_ass("आप कैसे हैं", 5.0, "minimal")
    assert ",CapHi," in hi
    assert [l for l in captions.generate_single_ass("", 5).splitlines() if l.startswith("Dialogue")] == []
    long = captions.generate_single_ass("word " * 200, 5.0, "karaoke")
    assert long.count("\\N") <= 3 and "…" in long
    over = captions.generate_single_ass("x", 5, over={"size": 99, "color": "#112233", "margin": 777}, position="top")
    assert ",99," in over and "&H00332211&" in over and ",777,1" in over


def test_single_caption_cannot_inject_ass_tags():
    a = captions.generate_single_ass("hi {\\fs300} there", 5)
    assert "\\fs300" not in a


# ---------------------------------------------------------------- settings
def test_new_settings_validate_and_defaults_are_unchanged_look():
    cfg = settings.build_config()
    assert (cfg.template, cfg.caption_mode, cfg.max_height, cfg.caption_enabled) == ("blur", "words", 1080, True)
    assert settings.coerce("max_height", "720") == 720 and settings.coerce("max_height", 480) == 480
    for k, bad in (("max_height", "2160"), ("template", "neon"), ("caption_mode", "both")):
        with pytest.raises(ConfigError):
            settings.coerce(k, bad)
    c = settings.build_config({"template": "tweet", "max_height": 360}, {"caption_mode": "single", "caption_enabled": False})
    assert (c.template, c.max_height, c.caption_mode, c.caption_enabled) == ("tweet", 360, "single", False)
    cfg2 = Config()
    cfg2.max_height = 999
    with pytest.raises(ConfigError):
        cfg2.validate()


def test_download_format_caps_height_but_always_has_a_fallback():
    f = video_editor.format_for(720)
    assert f.startswith("bv*[height<=720]+ba") and f.endswith("/bv*+ba/b")
    assert "1080" in video_editor.format_for(1080)
    calls = []
    orig = video_editor.run
    video_editor.run = lambda cmd, **k: calls.append(cmd) or open(cmd[cmd.index("-o") + 1], "wb").write(b"x" * 2000)
    try:
        video_editor.download_segment("https://youtu.be/x", 1, 5, "/tmp/_q_test.mp4", 480)
    finally:
        video_editor.run = orig
        if os.path.exists("/tmp/_q_test.mp4"):
            os.remove("/tmp/_q_test.mp4")
    assert calls[0][calls[0].index("-f") + 1] == video_editor.format_for(480)


# ---------------------------------------------------------------- real renders, checked by looking at pixels
def make_source(path, w, h, dur=3):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=25:d={dur}",
                    "-f", "lavfi", "-i", f"sine=f=300:d={dur}", "-pix_fmt", "yuv420p", "-shortest", path], check=True)


def frame(path, t=1.2):
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    assert len(p.stdout) == W * H * 3
    return p.stdout


def px(buf, x, y):
    i = (y * W + x) * 3
    return tuple(buf[i:i + 3])


def is_black(p):
    return max(p) < 8                      # YUV round trip: black can come back as (1, 1, 1)


def count_dark(buf, y0, y1):
    return sum(1 for y in range(y0, y1, 2) for x in range(0, W, 2) if max(px(buf, x, y)) < 90)


def render(tmp_path, name, template, src=(1280, 720), tweet_text="Why most people quit too early", cap="words",
           logo=None, **cfg_kw):
    raw = str(tmp_path / f"{name}_raw.mp4")
    make_source(raw, *src)
    cfg = Config()
    cfg.music_file, cfg.logo_file, cfg.x264_preset, cfg.crf = "/none.mp3", logo or "/none.png", "ultrafast", 32
    cfg.tweet_name, cfg.tweet_handle = "Rohit", "@rohit"
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    vi = probe(raw)
    layout = tass = None
    over = captions.overrides_from_cfg(cfg)
    for k, v in templates.caption_defaults(template).items():
        if not over.get(k):
            over[k] = v
    pos = "bottom"
    if template in templates.FIT_TEMPLATES:
        layout = templates.plan_layout(template, vi["width"], vi["height"], tweet_text)
        if layout.cap_top and not over.get("margin"):
            pos, over["margin"] = "top", layout.cap_top
        if template == "tweet":
            tass = str(tmp_path / f"{name}_tweet.ass")
            open(tass, "w", encoding="utf-8").write(templates.tweet_ass(layout, cfg.tweet_name, cfg.tweet_handle))
    if cap == "single":
        ass = captions.generate_single_ass("Quit too early?", 3, "karaoke", position=pos, over=over)
    else:
        words = [{"w": w, "s": i * 0.5, "e": i * 0.5 + 0.45, "speaker": None} for i, w in enumerate("these are the words".split())]
        ass = captions.generate_ass(words, "karaoke", position=pos, over=over)
    ap = str(tmp_path / f"{name}.ass")
    open(ap, "w", encoding="utf-8").write(ass)
    out = str(tmp_path / f"{name}.mp4")
    video_editor.render_main(raw, out, cfg=cfg, ass_path=ap, keep=None, mode="fit" if layout else "blur", keys=None,
                             workdir=str(tmp_path), template=template, layout=layout, tweet_ass_path=tass)
    return out, layout, cfg


@needs_ffmpeg
def test_render_white_tweet_template(tmp_path):
    out, lay, _ = render(tmp_path, "tw", "tweet")
    info = probe(out)
    assert (info["width"], info["height"]) == (W, H) and info["has_audio"]
    f = frame(out)
    assert px(f, 0, 0) == (255, 255, 255) and px(f, W - 1, H - 1) == (255, 255, 255)         # white page
    cx, cy = lay.card_x + 56, lay.card_y + 56
    r, g, b = px(f, cx, cy)
    assert abs(r - 0x1D) < 20 and abs(g - 0x9B) < 20 and abs(b - 0xF0) < 20                  # default avatar disc is blue
    assert px(f, lay.card_x + 1, lay.card_y + 1) == (255, 255, 255)                          # ...and it is round, not square
    assert count_dark(f, lay.card_y + 112, lay.card_y + lay.card_h - 10) > 150               # tweet text drawn above the video
    assert px(f, W // 2, lay.vy + lay.vh // 2) != (255, 255, 255)                            # the video itself is there
    assert count_dark(f, lay.vy + lay.vh + 20, lay.vy + lay.vh + 300) > 100                   # captions under the video, dark on white
    assert count_dark(f, 0, lay.card_y - 5) == 0                                              # nothing above the card


@needs_ffmpeg
def test_tweet_template_uses_logo_as_round_picture_without_corner_watermark(tmp_path):
    logo = str(tmp_path / "logo.png")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0xff0000:s=300x300", "-frames:v", "1", logo], check=True)
    out, lay, _ = render(tmp_path, "tw_logo", "tweet", logo=logo)
    f = frame(out)
    r, g, b = px(f, lay.card_x + 56, lay.card_y + 56)
    assert r > 220 and g < 40 and b < 40                                                      # the logo, not the blue default
    assert px(f, lay.card_x + 1, lay.card_y + 1) == (255, 255, 255)
    assert px(f, W - 80, 140) == (255, 255, 255)                                              # no watermark top right


@needs_ffmpeg
def test_render_black_template_with_logo_watermark_and_single_caption(tmp_path):
    logo = str(tmp_path / "logo.png")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x00ff00:s=300x300", "-frames:v", "1", logo], check=True)
    out, lay, _ = render(tmp_path, "bk", "black", cap="single", logo=logo)
    f = frame(out)
    assert is_black(px(f, 0, 0)) and is_black(px(f, 5, H - 5)) and is_black(px(f, 500, 40))
    assert not is_black(px(f, W // 2, lay.vy + lay.vh // 2))
    assert px(f, W - 20 - 60, 120 + 60)[1] > 200                                              # watermark kept on black
    bright = sum(1 for y in range(lay.vy + lay.vh + 10, lay.vy + lay.vh + 300, 2) for x in range(0, W, 2) if min(px(f, x, y)) > 200)
    assert bright > 100                                                                       # white single caption under the video


@needs_ffmpeg
def test_single_caption_stays_the_whole_clip_and_words_change(tmp_path):
    out, lay, _ = render(tmp_path, "one", "black", cap="single")
    a, b = frame(out, 0.3), frame(out, 2.6)
    area = lambda f: sum(1 for y in range(lay.vy + lay.vh + 10, lay.vy + lay.vh + 300, 3) for x in range(0, W, 3) if min(px(f, x, y)) > 200)
    assert area(a) > 50 and area(b) > 50                                                      # present at start and near the end


@needs_ffmpeg
def test_vertical_source_in_tweet_and_black_templates_fits_without_cropping(tmp_path):
    for tpl in ("tweet", "black"):
        out, lay, _ = render(tmp_path, f"v_{tpl}", tpl, src=(360, 640))
        info = probe(out)
        assert (info["width"], info["height"]) == (W, H)
        assert lay.vy >= 0 and lay.vy + lay.vh <= H and lay.vw <= W
        assert abs(lay.vw / lay.vh - 360 / 640) < 0.01                                        # aspect ratio preserved


@needs_ffmpeg
def test_blur_template_is_unchanged_and_fills_screen(tmp_path):
    raw = str(tmp_path / "raw.mp4")
    make_source(raw, 1280, 720)
    cfg = Config()
    cfg.music_file, cfg.logo_file, cfg.x264_preset, cfg.crf = "/none.mp3", "/none.png", "ultrafast", 32
    out = str(tmp_path / "blur.mp4")
    video_editor.render_main(raw, out, cfg=cfg, ass_path=None, keep=None, mode="blur", keys=None, workdir=str(tmp_path))
    f = frame(out)
    assert px(f, 5, 5) != (255, 255, 255) and not is_black(px(f, 5, 5)) and not is_black(px(f, W - 5, H - 5))   # blurred picture, no bars


def test_face_template_is_full_screen_not_blurred():
    from modules import video_editor as ve
    g = ve.build_video_graph(mode="crop", src_w=1280, src_h=720, out_dur=1.0, keys=None, keep=None, zoom=0.0, ass_name=None,
                             fonts_dir="", logo_idx=None, logo_size=100, template="face")
    text = ";".join(g)
    assert "crop=" in text and "boxblur" not in text
