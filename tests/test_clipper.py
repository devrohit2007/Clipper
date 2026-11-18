import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent
from modules import captions, clip_selector, content_generator, transcription, video_editor, youtube_uploader
from modules.ai_analyzer import GeminiClient, GeminiError, extract_json
from modules.utilities import Config, ConfigError, JobState, parse_ts, redact


# ---------------------------------------------------------------- fixtures
def make_transcript(n_sent=40, sent_len=3.0, words_per=6):
    segs, t = [], 0.0
    for i in range(n_sent):
        dt = sent_len / words_per
        words = [{"word": f"word{i}_{j}" + ("." if j == words_per - 1 else ""), "start": t + j * dt,
                  "end": t + (j + 1) * dt - 0.02} for j in range(words_per)]
        segs.append({"start": t, "end": t + sent_len, "text": " ".join(w["word"] for w in words), "words": words})
        t += sent_len
    return {"language": "en", "duration": t, "engine": "test", "word_level": True, "segments": segs}


# ---------------------------------------------------------------- timestamps
@pytest.mark.parametrize("v,exp", [("90", 90), ("1:30", 90), ("01:01:30.5", 3690.5), (12, 12.0), (7.5, 7.5)])
def test_parse_ts_ok(v, exp):
    assert parse_ts(v) == exp


@pytest.mark.parametrize("v", ["", "abc", "1:2:3:4", -5, "-1", float("nan"), float("inf"), None, True])
def test_parse_ts_bad(v):
    with pytest.raises(ValueError):
        parse_ts(v)


# ---------------------------------------------------------------- clip selection
def test_validation_rejects_invented_and_out_of_range():
    tr = make_transcript()
    words = clip_selector.flatten_words(tr)
    raw = [
        {"start": 3.0, "end": 36.0, "title": "ok", "score": 80},
        {"start": 9999, "end": 10030, "title": "beyond video", "score": 99},
        {"start": "abc", "end": "10", "title": "bad ts", "score": 99},
        {"start": 500.2, "end": 530, "title": "no speech there", "score": 90},
    ]
    out = clip_selector.validate_candidates(raw, words, tr["duration"], (15, 30, 45, 60))
    assert [c["title"] for c in out] == ["ok"]
    c = out[0]
    assert 0 <= c["start"] < c["end"] <= tr["duration"]
    assert c["duration"] <= 60


def test_clip_ends_on_sentence_boundary_and_starts_on_word():
    tr = make_transcript()
    words = clip_selector.flatten_words(tr)
    out = clip_selector.validate_candidates([{"start": 3.3, "end": 34.1, "score": 70, "title": "x"}], words,
                                            tr["duration"], (15, 30, 45, 60))
    c = out[0]
    last = [w for w in words if w["e"] <= c["end"]][-1]
    assert last["w"].endswith(".")          # never cut mid-sentence
    assert any(abs(w["s"] - (c["start"] + 0.10)) < 0.02 for w in words)


def test_overlap_dedup_keeps_higher_score():
    tr = make_transcript()
    words = clip_selector.flatten_words(tr)
    raw = [{"start": 3, "end": 33, "score": 60, "title": "low"},
           {"start": 6, "end": 36, "score": 90, "title": "high"},
           {"start": 60, "end": 90, "score": 50, "title": "far"}]
    out = clip_selector.validate_candidates(raw, words, tr["duration"], (30,))
    titles = [c["title"] for c in out]
    assert titles == ["high", "far"]


def test_select_clips_uses_chunks_and_caps_count():
    tr = make_transcript()
    calls = []

    def fake(prompt):
        calls.append(prompt)
        return {"clips": [{"start": 3, "end": 33, "score": 88, "title": "A", "hook": "h", "hashtags": ["x y", "#ok"]},
                          {"start": 60, "end": 90, "score": 70, "title": "B"}]}

    clips = clip_selector.select_clips(tr, tr["duration"], {"title": "T"}, fake, (15, 30, 45, 60), 1)
    assert len(clips) == 1 and clips[0]["id"] == "clip1" and clips[0]["score"] == 88
    assert clips[0]["hashtags"] == ["#xy", "#ok"]
    assert "word0_0" in calls[0]


def test_select_clips_raises_when_nothing_valid():
    tr = make_transcript()
    with pytest.raises(clip_selector.ClipSelectionError):
        clip_selector.select_clips(tr, tr["duration"], {}, lambda p: {"clips": [{"start": 9999, "end": 10040}]},
                                   (30,), 3)


def test_extract_json_handles_fences_and_prose():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": [1,2]} done') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


# ---------------------------------------------------------------- captions
def clip_words():
    return [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.35, "speaker": None}
            for i, w in enumerate("This is a really incredible story. It changed everything for me.".split())]


@pytest.mark.parametrize("style", captions.STYLE_NAMES)
def test_ass_all_styles(style):
    ass = captions.generate_ass(clip_words(), style)
    assert "[Events]" in ass and "Dialogue:" in ass
    for line in ass.splitlines():
        if line.startswith("Dialogue: 0"):
            assert line.count("\\N") <= 1          # maximum two lines


def test_karaoke_uses_real_kf_tags_with_matching_durations():
    ass = captions.generate_ass(clip_words(), "karaoke")
    ev = [l for l in ass.splitlines() if l.startswith("Dialogue: 0")]
    assert ev and "\\kf" in ev[0]
    ks = [int(x) for x in __import__("re").findall(r"\\kf(\d+)", ev[0])]
    assert all(k >= 1 for k in ks)
    assert abs(sum(ks) / 100 - (0.4 * len(ks) - 0.05)) < 0.5   # sweep spans the spoken words


def test_neon_is_one_event_per_word():
    words = clip_words()[:4]
    ass = captions.generate_ass(words, "neon")
    assert len([l for l in ass.splitlines() if l.startswith("Dialogue: 0")]) == 4


def test_hindi_gets_devanagari_style_and_hook():
    words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3, "speaker": None} for i, w in enumerate(["यह", "बहुत", "अच्छा", "है"])]
    ass = captions.generate_ass(words, "karaoke", hook_text="सुनिए यह")
    assert ",CapHi,,0,0,0," in ass and ",HookHi,,0,0,0," in ass


def test_podcast_speaker_label():
    words = clip_words()
    for w in words[:3]:
        w["speaker"] = "Host"
    assert "Host:" in captions.generate_ass(words, "podcast")


def test_ass_escapes_braces_and_times_monotonic():
    words = [{"w": "{bad}", "s": 0.0, "e": 0.3, "speaker": None}, {"w": "ok", "s": 0.4, "e": 0.7, "speaker": None}]
    ass = captions.generate_ass(words, "minimal")
    assert "{bad}" not in ass and "(bad)" in ass


def test_srt_format():
    srt = captions.generate_srt(clip_words())
    assert srt.startswith("1\n00:00:00,000 --> ") and "-->" in srt


def test_remap_words_after_silence_removal():
    keep = [(0.0, 1.0), (3.0, 4.0)]
    words = [{"w": "a", "s": 0.2, "e": 0.5}, {"w": "gone", "s": 1.5, "e": 2.0}, {"w": "b", "s": 3.2, "e": 3.5}]
    out = captions.remap_words(words, keep)
    assert [w["w"] for w in out] == ["a", "b"] and abs(out[1]["s"] - 1.2) < 1e-6


# ---------------------------------------------------------------- FFmpeg filter construction
def test_silence_helpers():
    keep = video_editor.keep_intervals([(2.0, 4.0), (8.0, None)], 10.0, 0.15)
    assert keep == [(0.0, 2.15), (3.85, 8.15), (9.85, 10.0)]
    assert video_editor.keep_intervals([(2.0, 2.4)], 10.0, 0.15) is None


def test_lerp_expr_and_keyframes():
    e = video_editor.lerp_expr([(0, 0), (1, 100), (2, 50)])
    assert e.count("if(") == 2 and "50.0" in e
    samples = [{"t": float(i), "cx": 0.2 + 0.05 * i, "cy": .4, "w": .1, "h": .1, "coverage": 1.0} for i in range(10)]
    keys = video_editor.build_keyframes(samples, 10.0, 1920, 608)
    assert keys and all(0 <= x <= 1920 - 608 for _, x in keys)
    assert video_editor.build_keyframes([{"t": 0, "cx": .5, "cy": .5, "w": .1, "h": .1, "coverage": .05}], 10, 1920, 608) is None


def test_graphs_contain_expected_filters():
    v = ";".join(video_editor.build_video_graph(mode="blur", src_w=1920, src_h=1080, out_dur=20, keys=None,
                                                 keep=[(0, 5)], zoom=.06, ass_name="captions.ass", fonts_dir="/f",
                                                 logo_idx=2, logo_size=120))
    for token in ("boxblur", "select=", "ass=filename='captions.ass'", "fontsdir", "overlay=W-w-20:120", "[vout]"):
        assert token in v
    a = ";".join(video_editor.build_audio_graph(keep=None, music_idx=1, silent_idx=None, music_volume=.15,
                                                duck=True, normalize=True))
    assert "sidechaincompress" in a and "loudnorm" in a and "[aout]" in a
    a2 = ";".join(video_editor.build_audio_graph(keep=None, music_idx=None, silent_idx=None, music_volume=.15,
                                                 duck=False, normalize=False))
    assert "[sp]anull[aout]" in a2


def test_ff_path_escaping():
    assert video_editor.ff_path("C:\\a:b'c") == "C\\:\\\\a\\:b\\'c" or "\\:" in video_editor.ff_path("a:b")


FF = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0


@pytest.fixture
def synthetic(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=6", "-c:v", "libx264", "-c:a", "aac",
                    "-pix_fmt", "yuv420p", str(src)], check=True, capture_output=True)
    music = tmp_path / "m.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=3", str(music)],
                   check=True, capture_output=True)
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(logo)],
                   check=True, capture_output=True)
    return tmp_path, src, music, logo


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
@pytest.mark.parametrize("mode,style,silence", [("blur", "karaoke", False), ("crop", "neon", False),
                                                 ("blur", "bold", True)])
def test_real_render_end_to_end(synthetic, mode, style, silence):
    tmp, src, music, logo = synthetic
    cfg = Config(music_file=str(music), logo_file=str(logo), output_dir=str(tmp / "out"), remove_silence=silence,
                 caption_style=style)
    cfg.finalize()
    ass = tmp / "captions.ass"
    ass.write_text(captions.generate_ass(clip_words(), style))
    keep = [(0.0, 2.0), (3.0, 6.0)] if silence else None
    keys = [(0.0, 100.0), (3.0, 400.0)] if mode == "crop" else None
    out = tmp / "main.mp4"
    video_editor.render_main(str(src), str(out), cfg=cfg, ass_path=str(ass), keep=keep, mode=mode, keys=keys,
                             workdir=str(tmp))
    info = video_editor.probe(str(out))
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert abs(info["duration"] - (5.0 if silence else 6.0)) < 0.3
    outro = tmp / "outro.mp4"
    video_editor.make_outro(str(outro), cfg)
    for tr in (0.3, 0):
        final = tmp / f"final_{tr}.mp4"
        video_editor.join_clips(str(out), str(outro), str(final), tr, str(tmp))
        fi = video_editor.probe(str(final))
        assert (fi["width"], fi["height"]) == (1080, 1920)
        assert fi["duration"] > info["duration"] + (1.5 if tr else 1.9) - 0.2


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_render_without_music_logo_or_audio(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3", "-pix_fmt", "yuv420p",
                    str(src)], check=True, capture_output=True)
    cfg = Config(music_file="/nonexistent.mp3", logo_file="/nonexistent.png", output_dir=str(tmp_path))
    cfg.finalize()
    out = tmp_path / "o.mp4"
    video_editor.render_main(str(src), str(out), cfg=cfg, ass_path=None, keep=None, mode="blur", keys=None,
                             workdir=str(tmp_path))
    assert video_editor.probe(str(out))["has_audio"]


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_silence_detection_real(tmp_path):
    f = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=400:d=2", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                    "-f", "lavfi", "-i", "sine=f=400:d=2", "-filter_complex",
                    "[1:a]atrim=0:2[s];[0:a][s][2:a]concat=n=3:v=0:a=1", str(f)], check=True, capture_output=True)
    sil = video_editor.detect_silences(str(f), -35, 0.7)
    assert len(sil) == 1 and 1.8 < sil[0][0] < 2.2


# ---------------------------------------------------------------- transcription
def test_transcription_survives_missing_faster_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)     # simulates ImportError
    cfg = Config(); cfg.finalize()

    class C:
        def generate_json(self, parts, temperature=0):
            return {"language": "hi", "segments": [{"start": 0, "end": 3, "text": "namaste dosto aaj kya hai"}]}

    monkeypatch.setattr(transcription, "run", lambda *a, **k: None)
    monkeypatch.setattr("pathlib.Path.read_bytes", lambda self: b"x")
    monkeypatch.setattr(transcription, "safe_remove", lambda *a: None)
    tr = transcription.transcribe("a.flac", cfg, C(), 5.0, ".")
    assert tr["engine"] == "gemini" and tr["language"] == "hi" and tr["segments"][0]["words"]


def test_transcription_error_is_clean_without_any_engine(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    cfg = Config(); cfg.finalize()
    with pytest.raises(transcription.TranscriptionError):
        transcription.transcribe("a.flac", cfg, None, 5.0, ".")


def test_gemini_segment_cleaning_and_word_estimates():
    segs = transcription.clean_gemini_segments(
        [{"start": 0, "end": 4, "text": "hello big world"}, {"start": 3, "end": 6, "text": "overlap fixed"},
         {"start": 99, "end": 100, "text": "beyond chunk"}, {"start": "x", "end": 1, "text": "bad"}], 10, 100)
    assert len(segs) == 2 and segs[0]["start"] == 100 and segs[1]["start"] >= segs[0]["end"]
    w = segs[0]["words"]
    assert [x["word"] for x in w] == ["hello", "big", "world"] and w[-1]["end"] == segs[0]["end"]


# ---------------------------------------------------------------- Gemini client + secrets
class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._p, self.text = status, payload, text

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, resps):
        self.resps, self.calls = list(resps), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers))
        return self.resps.pop(0)


def ok(text):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def test_gemini_key_in_header_not_url_and_retries():
    s = FakeSession([FakeResp(503), ok('{"a": 1}')])
    c = GeminiClient("SECRETKEY123", "m", session=s, sleep=lambda x: None)
    assert c.generate_json("hi") == {"a": 1}
    assert all("SECRETKEY123" not in u for u, _ in s.calls) and s.calls[0][1]["x-goog-api-key"] == "SECRETKEY123"


def test_gemini_hard_error_and_redaction():
    s = FakeSession([FakeResp(400, text="API key not valid. key=AIzaSyA1234567890123456789012345")])
    c = GeminiClient("k", "m", fallbacks=["m2"], session=s, sleep=lambda x: None)
    with pytest.raises(GeminiError) as e:
        c.generate_text("x")
    assert "AIzaSy" not in redact(e.value) and "AIzaSy" not in str(e.value)
    assert len(s.calls) == 1                      # bad key: no point trying other models


def models_used(s):
    return [u.split("/models/")[1].split(":")[0] for u, _ in s.calls]


def test_fallback_on_rate_limit_then_remembers_cooldown():
    now = [0.0]
    s = FakeSession([FakeResp(429), ok("A"), ok("B")])
    c = GeminiClient("k", "m1", fallbacks="m2,m3", session=s, sleep=lambda x: None, clock=lambda: now[0])
    assert c.generate_text("x") == "A" and c.model == "m2"
    assert c.generate_text("x") == "B"
    assert models_used(s) == ["m1", "m2", "m2"]        # m1 rests, not retried immediately
    now[0] = 500                                       # cooldown over: back to the best model
    s.resps.append(ok("C"))
    assert c.generate_text("x") == "C" and models_used(s)[-1] == "m1"


def test_retired_model_404_skipped_for_rest_of_run():
    s = FakeSession([FakeResp(404), ok("A"), ok("B")])
    c = GeminiClient("k", "old", fallbacks=["new"], session=s, sleep=lambda x: None)
    c.generate_text("x"); c.generate_text("x")
    assert models_used(s) == ["old", "new", "new"]


def test_fallback_on_400_empty_answer_and_network_error():
    import requests as rq

    class Boom(FakeSession):
        def post(self, url, headers=None, json=None, timeout=None):
            if not self.calls:
                self.calls.append((url, headers))
                raise rq.ConnectionError("x")
            return super().post(url, headers, json, timeout)

    s = Boom([FakeResp(400, text="audio not supported"), FakeResp(200, {"candidates": []}), ok("final")])
    c = GeminiClient("k", "a", fallbacks="b,c,d", session=s, sleep=lambda x: None)
    assert c.generate_text("x") == "final" and c.model == "d"


def test_all_models_fail_raises_summary_without_secrets():
    s = FakeSession([FakeResp(503)] * 6)
    c = GeminiClient("SECRET-KEY-VALUE", "a", fallbacks="b", session=s, sleep=lambda x: None, rounds=3)
    with pytest.raises(GeminiError) as e:
        c.generate_text("x")
    assert "a:" in str(e.value) and "b:" in str(e.value) and "SECRET-KEY-VALUE" not in str(e.value)


def test_model_chain_dedupes_and_orders():
    from modules.ai_analyzer import model_chain
    assert model_chain("b", "a, b ,models/c,,a") == ["b", "a", "c"]
    cfg = Config(); cfg.finalize()
    assert model_chain(cfg.gemini_model, cfg.gemini_models)[0] == cfg.gemini_model
    assert len(model_chain(cfg.gemini_model, cfg.gemini_models)) == 6


def test_redact_known_secret_formats(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "my-super-secret-key")
    txt = "k=my-super-secret-key GOCSPX-abcDEF123_-x ya29.a0Af_bc-d access_token=abc123 AQ.Ab8RN6LCwty1AomzHNWqE3kFAV"
    r = redact(txt)
    for s in ("my-super-secret-key", "GOCSPX-abc", "ya29.a0", "abc123", "AQ.Ab8RN6"):
        assert s not in r


def test_no_hardcoded_secrets_in_source():
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pat = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|GOCSPX-[0-9A-Za-z_\-]{8,}|\.apps\.googleusercontent\.com|AQ\.Ab8")
    for d, _, files in os.walk(root):
        for f in files:
            if f.endswith((".py", ".md", ".example", ".txt")) and "test_clipper" not in f:
                assert not pat.search(open(os.path.join(d, f), encoding="utf-8").read()), f


# ---------------------------------------------------------------- config + OAuth
def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CLIP_DURATIONS", "30,15")
    monkeypatch.setenv("DUCK_MUSIC", "false")
    monkeypatch.setenv("CAPTION_STYLE", "NEON")
    monkeypatch.setenv("GEMINI_API_KEY", "abc")
    cfg = Config.from_env()
    assert cfg.clip_durations == (15, 30) and cfg.duck_music is False and cfg.caption_style == "neon"
    assert cfg.gemini_api_key == "abc"


@pytest.mark.parametrize("k,v", [("CAPTION_STYLE", "comic"), ("ZOOM", "5"), ("CLIP_COUNT", "0"), ("FIT_MODE", "x"),
                                  ("MUSIC_VOLUME", "abc")])
def test_config_rejects_bad_values(monkeypatch, k, v):
    monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError):
        Config.from_env()


def test_client_secrets_validation(tmp_path):
    with pytest.raises(ConfigError):
        youtube_uploader.read_client_secrets(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    with pytest.raises(ConfigError):
        youtube_uploader.read_client_secrets(str(bad))
    empty = tmp_path / "e.json"; empty.write_text('{"installed": {}}')
    with pytest.raises(ConfigError):
        youtube_uploader.read_client_secrets(str(empty))
    good = tmp_path / "g.json"; good.write_text('{"installed": {"client_id": "i", "client_secret": "s"}}')
    assert youtube_uploader.read_client_secrets(str(good))["client_id"] == "i"


def test_load_credentials_non_interactive_without_token(tmp_path):
    pytest.importorskip("googleapiclient")
    g = tmp_path / "g.json"; g.write_text('{"installed": {"client_id": "i", "client_secret": "s"}}')
    cfg = Config(youtube_client_secrets=str(g), youtube_token_file=str(tmp_path / "t.json")); cfg.finalize()
    with pytest.raises(youtube_uploader.UploadError):
        youtube_uploader.load_credentials(cfg, interactive=False)


def test_missing_google_libs_gives_config_error(monkeypatch):
    for m in ("google.auth.transport.requests", "google.oauth2.credentials", "google_auth_oauthlib.flow",
              "googleapiclient.discovery", "googleapiclient.http"):
        monkeypatch.setitem(sys.modules, m, None)
    with pytest.raises(ConfigError):
        youtube_uploader._google()


# ---------------------------------------------------------------- scheduling + upload
def test_schedule_times():
    from datetime import datetime, timezone
    t = youtube_uploader.schedule_times(3, 20, 3, now=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert t == ["2026-01-01T00:20:00Z", "2026-01-01T03:20:00Z", "2026-01-01T06:20:00Z"]


def meta():
    return content_generator.generate_metadata(
        {"id": "clip1", "title": "A title", "reason": "r", "hook": "h", "hashtags": ["#a"], "description": "d"},
        "some speech", {"title": "Source", "url": "https://y/x"}, None)


def test_body_is_private_scheduled_and_clean():
    b = youtube_uploader.build_body(meta(), "2026-01-01T00:20:00Z")
    assert b["status"] == {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
                           "publishAt": "2026-01-01T00:20:00Z"}
    assert b["snippet"]["title"].endswith("#Shorts") and "#Shorts" in b["snippet"]["description"]
    assert "#" not in "".join(b["snippet"]["tags"])


class FakeRequest:
    def __init__(self, script):
        self.script = list(script)

    def next_chunk(self):
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeHttpErr(Exception):
    def __init__(self, status):
        self.resp = type("R", (), {"status": status})()


class FakeService:
    def __init__(self, script):
        self.req = FakeRequest(script)

    def videos(self):
        return self

    def insert(self, **kw):
        return self.req


def test_upload_retries_temporary_then_succeeds():
    svc = FakeService([FakeHttpErr(503), ConnectionError("x"), (None, {"id": "VID1"})])
    vid = youtube_uploader.upload_video(svc, "f.mp4", meta(), None, sleep=lambda s: None, media_factory=lambda p: None)
    assert vid == "VID1"


def test_upload_permanent_error_raises():
    svc = FakeService([FakeHttpErr(403)])
    with pytest.raises(youtube_uploader.UploadError):
        youtube_uploader.upload_video(svc, "f.mp4", meta(), None, sleep=lambda s: None, media_factory=lambda p: None)


def test_upload_gives_up_after_retries_and_missing_id():
    svc = FakeService([FakeHttpErr(500)] * 4)
    with pytest.raises(youtube_uploader.UploadError):
        youtube_uploader.upload_video(svc, "f.mp4", meta(), None, retries=2, sleep=lambda s: None,
                                      media_factory=lambda p: None)
    svc = FakeService([(None, {})])
    with pytest.raises(youtube_uploader.UploadError):
        youtube_uploader.upload_video(svc, "f.mp4", meta(), None, sleep=lambda s: None, media_factory=lambda p: None)


def test_failed_upload_keeps_file_and_success_deletes(tmp_path):
    f = tmp_path / "clip.mp4"; f.write_bytes(b"data")
    job = JobState(str(tmp_path / "job.json"))

    def boom():
        raise RuntimeError("HTTP 500 api_key=SECRET1234567890")

    assert agent.upload_and_cleanup(str(f), boom, job, "clip1") is False
    assert f.exists() and job.data["clips"]["clip1"]["status"] == "upload_failed"
    assert "SECRET1234567890" not in json.dumps(job.data)
    assert agent.upload_and_cleanup(str(f), lambda: "", job, "clip1") is False and f.exists()
    assert agent.upload_and_cleanup(str(f), lambda: "VID", job, "clip1") is True
    assert not f.exists() and job.data["clips"]["clip1"]["youtube_id"] == "VID"


def test_job_state_persists(tmp_path):
    p = str(tmp_path / "j.json")
    j = JobState(p); j.update_clip("c1", status="rendered")
    assert JobState(p).data["clips"]["c1"]["status"] == "rendered"
    open(p, "w").write("{corrupt")
    assert JobState(p).data == {"clips": {}}


# ---------------------------------------------------------------- metadata + CLI
def test_metadata_with_gemini_and_without():
    clip = {"id": "clip1", "title": "T", "reason": "r", "hook": "h", "hashtags": ["#a"], "description": "d"}
    m = content_generator.generate_metadata(clip, "speech", {"title": "S", "url": "u"}, lambda p: {
        "titles": ["One", "Two", "Three", "Four", "Five", "Six"], "description": "desc", "hashtags": ["x", "#y"],
        "hooks": ["h1"], "cover_text": "cover", "category": "Education"})
    assert len(m["titles"]) == 5 and m["platforms"]["youtube"]["categoryId"] == "27"
    assert content_generator.generate_metadata(clip, "speech", {}, None)["title"] == "T"

    def fail(p):
        raise GeminiError("down")

    assert content_generator.generate_metadata(clip, "speech", {}, fail)["title"] == "T"


def test_check_report_never_prints_key(monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "TOPSECRETVALUE123")
    cfg = Config.from_env()
    from modules.utilities import check_dependencies, format_dependency_report
    rep = format_dependency_report(check_dependencies(cfg))
    assert "TOPSECRETVALUE123" not in rep and "GEMINI_API_KEY" in rep


def test_cli_check_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert agent.main(["--check", "--env-file", str(tmp_path / "none")]) == 0
    assert "ffmpeg" in capsys.readouterr().out


def test_env_file_inline_comments(tmp_path, monkeypatch):
    from modules.utilities import load_env_file
    for k in ("ZOOM", "EXTRA_TAGS", "OUTRO_LINES", "QUOTED", "EMPTY_K"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text('ZOOM=0.06                      # 0 = off\nEXTRA_TAGS=#Shorts\n'
                 'OUTRO_LINES="Hi # there|Bye"  # note\nQUOTED=\'a b\'\n# full comment\n')
    assert load_env_file(str(f))
    assert os.environ["ZOOM"] == "0.06" and os.environ["EXTRA_TAGS"] == "#Shorts"
    assert os.environ["OUTRO_LINES"] == "Hi # there|Bye" and os.environ["QUOTED"] == "a b"
    monkeypatch.setenv("ZOOM", "0.06")
    assert Config.from_env().zoom == 0.06
    for k in ("ZOOM", "EXTRA_TAGS", "OUTRO_LINES", "QUOTED", "EMPTY_K"):
        os.environ.pop(k, None)


# ---------------------------------------------------------------- Groq transcription
GROQ_JSON = {"language": "hindi", "segments": [
    {"start": 0.0, "end": 2.0, "text": " namaste dosto aaj"}, {"start": 2.0, "end": 4.0, "text": " kuch khaas hai."}],
    "words": [{"word": "namaste", "start": 0.0, "end": 0.6}, {"word": "dosto", "start": 0.7, "end": 1.2},
              {"word": "aaj", "start": 1.3, "end": 1.9}, {"word": "kuch", "start": 2.1, "end": 2.5},
              {"word": "khaas", "start": 2.6, "end": 3.2}, {"word": "hai.", "start": 3.3, "end": 3.9}]}


class GroqSession:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def post(self, url, headers=None, data=None, files=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data})
        r = self.script.pop(0)
        return r


class GResp:
    def __init__(self, status, payload=None, text="", headers=None):
        self.status_code, self._p, self.text, self.headers = status, payload, text, headers or {}

    def json(self):
        return self._p


def groq_cfg(**kw):
    cfg = Config(groq_api_key="gsk_TESTKEY1234567890abcdef", **kw)
    cfg.finalize()
    return cfg


def test_groq_assembles_real_word_timings_with_offset():
    segs = transcription.assemble_groq_segments(GROQ_JSON, 10.0, 100.0)
    assert [s["start"] for s in segs] == [100.0, 102.0]
    w = segs[1]["words"]
    assert [x["word"] for x in w] == ["kuch", "khaas", "hai."] and w[0]["start"] == 102.1 and w[2]["end"] == 103.9
    assert transcription._lang_code("Hindi") == "hi" and transcription._lang_code("english") == "en"


def test_groq_segments_missing_builds_from_words_and_ignores_junk():
    data = {"words": [{"word": "Hello", "start": 0, "end": .4}, {"word": "world.", "start": .5, "end": 1.0},
                      {"word": "", "start": 1, "end": 2}, {"start": 1}, {"word": "late", "start": 99, "end": 100}]}
    segs = transcription.assemble_groq_segments(data, 10, 0)
    assert len(segs) == 1 and segs[0]["text"] == "Hello world." and len(segs[0]["words"]) == 2


@pytest.mark.skipif(not FF, reason="ffmpeg missing")
def test_groq_multi_chunk_flow_retry_and_model_fallback(tmp_path):
    audio = tmp_path / "a.flac"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=f=300:d=7", "-ac", "1", str(audio)],
                   check=True, capture_output=True)
    sess = GroqSession([GResp(429, headers={"retry-after": "1"}), GResp(400, text="bad"),   # chunk0: retry, then model 2 fails...
                        GResp(200, GROQ_JSON), GResp(200, GROQ_JSON)])
    sleeps = []
    cfg = groq_cfg()
    tr = transcription.transcribe_groq(str(audio), cfg, 7.0, str(tmp_path), session=sess, sleep=sleeps.append,
                                       chunk_sec=4)
    models = [c["data"]["model"] for c in sess.calls]
    assert models == ["whisper-large-v3-turbo", "whisper-large-v3-turbo", "whisper-large-v3", "whisper-large-v3-turbo"]
    assert sleeps and tr["engine"] == "groq" and tr["word_level"] and tr["language"] == "hi"
    assert tr["segments"][-1]["start"] >= 4.0                          # second chunk offset applied
    assert sess.calls[0]["headers"]["Authorization"].startswith("Bearer ")
    assert "word" in sess.calls[0]["data"]["timestamp_granularities[]"]
    assert not list(tmp_path.glob("groq_*"))                            # chunk files cleaned up


def test_groq_bad_key_and_total_failure(tmp_path):
    cfg = groq_cfg()
    with pytest.raises(transcription.TranscriptionError) as e:
        transcription._groq_request(__file__, ["m"], cfg, GroqSession([GResp(401, text="invalid key")]), lambda s: None)
    assert "gsk_" not in str(e.value) and "API key" in str(e.value)
    with pytest.raises(transcription.TranscriptionError):
        transcription._groq_request(__file__, ["m1", "m2"], cfg,
                                    GroqSession([GResp(503)] * 3 + [GResp(503)] * 3), lambda s: None)


def test_orchestrator_order_groq_before_gemini(monkeypatch):
    seen = []
    monkeypatch.setattr(transcription, "transcribe_local",
                        lambda *a: (_ for _ in ()).throw(transcription.TranscriptionUnavailable("no fw")))
    monkeypatch.setattr(transcription, "transcribe_groq", lambda *a, **k: seen.append("groq") or {"engine": "groq"})
    monkeypatch.setattr(transcription, "transcribe_gemini", lambda *a, **k: seen.append("gemini") or {"engine": "g"})
    cfg = groq_cfg()
    assert transcription.transcribe("a", cfg, object(), 5, ".")["engine"] == "groq" and seen == ["groq"]

    def boom(*a, **k):
        raise transcription.TranscriptionError("down")

    monkeypatch.setattr(transcription, "transcribe_groq", boom)
    assert transcription.transcribe("a", cfg, object(), 5, ".")["engine"] == "g"      # groq failed -> gemini
    cfg2 = groq_cfg(transcribe_engine="groq")                                          # forbid gemini
    before = list(seen)
    with pytest.raises(transcription.TranscriptionError):
        transcription.transcribe("a", cfg2, object(), 5, ".")
    assert seen == before == ["groq", "gemini"]   # engine=groq: gemini transcription not called again


def test_groq_key_redacted_everywhere(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_ABCDEFGHIJKLMNOPQRSTUV")
    assert "gsk_ABCDEF" not in redact("fail key gsk_ABCDEFGHIJKLMNOPQRSTUV Bearer x")
    assert "gsk_" not in redact("gsk_zzzzzzzzzzzzzzzzzzzzzz")
    assert Config(transcribe_engine="groq").validate() is None
    with pytest.raises(ConfigError):
        Config(transcribe_engine="whisperx").validate()


def test_find_cascade_looks_in_termux_folder_when_cv2_has_no_data(tmp_path, monkeypatch):
    from modules import video_editor
    d = tmp_path / "share" / "opencv5" / "haarcascades"
    d.mkdir(parents=True)
    (d / "haarcascade_frontalface_default.xml").write_text("<x/>")
    monkeypatch.setenv("PREFIX", str(tmp_path))
    monkeypatch.delenv("CLIPPER_CASCADE_DIR", raising=False)
    assert video_editor.find_cascade(object()) == str(d / "haarcascade_frontalface_default.xml")


def test_unstarted_or_just_ended_streams_are_refused_but_running_ones_become_their_own_video(monkeypatch):
    import json
    from types import SimpleNamespace
    from modules import video_editor
    from modules.utilities import ClipperError
    def fake(status, **extra):
        monkeypatch.setattr(video_editor, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"id": "x", "live_status": status, **extra})))
    for st, word in (("is_upcoming", "not started"), ("post_live", "processing")):
        fake(st)
        with pytest.raises(ClipperError, match=word):
            video_editor.get_info("https://youtu.be/x")
    fake("is_live", is_live=True)
    info = video_editor.get_info("https://youtu.be/x")
    assert info["is_live"] and info["id"].startswith("x_live") and info["duration"] == 0
    fake("was_live", duration=60)
    assert video_editor.get_info("https://youtu.be/x")["id"] == "x"
