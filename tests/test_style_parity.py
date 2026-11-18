"""What you save on the Style screen must show up in the rendered clip.

Renders one clip with real FFmpeg through pipeline.render_clip (the same code the app uses), then renders it again with
ONE setting changed. If the output (video bytes, caption file or tweet-card file) does not change, that setting is
dead: saved but ignored. Each setting must change the result."""
import hashlib
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import pipeline, settings, video_editor  # noqa: E402
from modules.utilities import JobState, ffmpeg_filters  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or "ass" not in ffmpeg_filters(),
                                reason="needs ffmpeg with the ass filter")

SEEN = {}                                   # workdir -> (caption ass text, tweet ass text)
_orig_render_main = video_editor.render_main


def _spy_render_main(raw, out, **kw):
    read = lambda p: open(p, encoding="utf-8").read() if p and os.path.isfile(p) else ""
    SEEN[kw["workdir"]] = (read(kw.get("ass_path")), read(kw.get("tweet_ass_path")))
    return _orig_render_main(raw, out, **kw)


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("parity")
    assets = {}
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=4",
       "-f", "lavfi", "-i", "aevalsrc='if(between(t,1.5,2.5),0.03,0.5)*sin(2*PI*300*t)':d=4:s=44100",
       "-pix_fmt", "yuv420p", "-shortest", str(tmp / "raw.mp4"))
    ff("-f", "lavfi", "-i", "sine=f=220:d=10", str(tmp / "music.wav"))
    for name, colour in (("logo", "0xff0000"), ("logo2", "0x00ff00"), ("avatar", "0xffaa00")):
        ff("-f", "lavfi", "-i", f"color=c={colour}:s=300x300", "-frames:v", "1", str(tmp / f"{name}.png"))
        assets[name] = str(tmp / f"{name}.png")
    assets["music"] = str(tmp / "music.wav")
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.45} for i, w in enumerate("these are the words you will see here".split())]
    transcript = {"language": "en", "word_level": True, "engine": "test", "duration": 4.0,
                  "segments": [{"start": 0.0, "end": 4.0, "text": " ".join(w["word"] for w in words), "words": words}]}
    clip = {"id": "clip1", "start": 0, "end": 4, "duration": 4, "title": "Test title", "hook": "Wait for it", "score": 90}
    pipeline.video_editor.render_main = _spy_render_main
    yield tmp, assets, transcript, clip
    pipeline.video_editor.render_main = _orig_render_main


BASE = {"x264_preset": "ultrafast", "crf": 32, "face_tracking": False, "intro_hook": True, "outro_sec": 1.0,
        "tweet_name": "Rohit", "tweet_handle": "@rohit"}


def signature(rig, name, extra, base=None):
    tmp, assets, transcript, clip = rig
    ov = {**BASE, "music_file": assets["music"], "logo_file": assets["logo"], **(base or {}), **extra}
    ov = {k: (assets[v[1:]] if isinstance(v, str) and v.startswith("@") and v[1:] in assets else v) for k, v in ov.items()}
    cfg = settings.build_config(ov)
    work = str(tmp / name)
    os.makedirs(work, exist_ok=True)
    shutil.copy(tmp / "raw.mp4", os.path.join(work, f"raw_{cfg.max_height}.mp4"))
    ctx = pipeline.Ctx(url="https://youtu.be/x", cfg=cfg, state=JobState(), tag="p_")
    final = str(tmp / f"{name}_final.mp4")
    pipeline.render_clip(ctx, {"id": "vid", "title": "Video"}, dict(clip), transcript, final, work)
    h = hashlib.sha256(open(final, "rb").read()).hexdigest()
    return (h, *SEEN[work])


def run_all(rig, base_name, base, variants):
    """Render the baseline twice (proves rendering is repeatable) and every variant; return those that did not change."""
    jobs = {f"{base_name}_a": {}, f"{base_name}_b": {}}
    jobs.update({f"{base_name}_{k}": v for k, v in variants.items()})
    with ThreadPoolExecutor(max_workers=2) as ex:
        sigs = dict(zip(jobs, ex.map(lambda kv: signature(rig, kv[0], kv[1], base), jobs.items())))
    assert sigs[f"{base_name}_a"] == sigs[f"{base_name}_b"], "rendering is not repeatable, the comparison would be meaningless"
    ref = sigs[f"{base_name}_a"]
    return [k for k in variants if sigs[f"{base_name}_{k}"] == ref]


def test_every_look_and_sound_setting_changes_the_rendered_clip(rig):
    variants = {
        "caption_style": {"caption_style": "neon"}, "caption_size": {"caption_size": 120}, "caption_line_chars": {"caption_line_chars": 6},
        "caption_color": {"caption_color": "#ff0000"}, "caption_highlight": {"caption_highlight": "#00ff00"},
        "caption_outline_color": {"caption_outline_color": "#0000ff"}, "caption_outline": {"caption_outline": 10},
        "caption_position": {"caption_position": "top"}, "caption_margin": {"caption_margin": 900},
        "caption_uppercase": {"caption_uppercase": "yes"}, "caption_bold": {"caption_bold": "no"},
        "caption_font": {"caption_font": "DejaVu Serif"}, "intro_hook_off": {"intro_hook": False},
        "intro_hook_sec": {"intro_hook_sec": 5}, "caption_mode": {"caption_mode": "single"},
        "caption_enabled": {"caption_enabled": False}, "template_black": {"template": "black"}, "template_tweet": {"template": "tweet"}, "template_face": {"template": "face"},
        "fit_mode": {"fit_mode": "crop"}, "zoom": {"zoom": 0.2}, "logo_size": {"logo_size": 300},
        "logo_position": {"logo_position": "bottom-left"}, "logo_opacity": {"logo_opacity": 0.3}, "logo_file": {"logo_file": "@logo2"},
        "outro_enabled": {"outro_enabled": False}, "outro_lines": {"outro_lines": "A|B|C"}, "outro_sec": {"outro_sec": 1.5},
        "outro_bg": {"outro_bg": "#ff0000"}, "transition_sec": {"transition_sec": 0.2}, "crf": {"crf": 20},
        "x264_preset": {"x264_preset": "medium"}, "music_volume": {"music_volume": 0.5}, "duck_music": {"duck_music": False},
        "duck_strength": {"duck_strength": "strong"}, "normalize_audio": {"normalize_audio": False}, "no_music": {"music_file": "/none.mp3"},
    }
    dead = run_all(rig, "gen", None, variants)
    assert dead == [], f"saved but ignored by the renderer: {dead}"


def test_every_tweet_card_setting_changes_the_rendered_clip(rig):
    variants = {
        "tweet_name": {"tweet_name": "Zed"}, "tweet_handle": {"tweet_handle": "@zed"}, "tweet_avatar": {"tweet_avatar": "@avatar"},
        "logo_is_the_picture": {"logo_file": "@logo2"}, "caption_color": {"caption_color": "#ff0000"},
    }
    dead = run_all(rig, "tw", {"template": "tweet"}, variants)
    assert dead == [], f"tweet card settings that do nothing: {dead}"


def test_every_silence_setting_changes_the_rendered_clip(rig):
    variants = {"silence_db": {"silence_db": -50}, "silence_min_sec": {"silence_min_sec": 3.0}, "silence_keep_sec": {"silence_keep_sec": 0.8},
                "remove_silence_off": {"remove_silence": False}}
    dead = run_all(rig, "sil", {"remove_silence": True, "silence_db": -20, "silence_keep_sec": 0.1, "silence_min_sec": 0.5}, variants)
    assert dead == [], f"silence settings that do nothing: {dead}"
