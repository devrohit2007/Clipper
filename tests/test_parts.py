"""Time ranges, your own video files, and "equal parts" (no AI). Real FFmpeg; only yt-dlp, Gemini and the speech
service are stand-ins. The main thing checked everywhere: only the parts that are needed are downloaded/transcribed."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import pipeline, templates, video_editor, video_scout, webapp  # noqa: E402
from test_e2e import FakeGemini, rig, settle, status, transcript  # noqa: E402,F401


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def own_video(tmp_path_factory):
    p = tmp_path_factory.mktemp("own") / "My Holiday_clip.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=60", "-f", "lavfi", "-i", "sine=f=300:d=60", "-pix_fmt", "yuv420p",
       "-shortest", str(p))
    return p


def relative_transcribe(seen):
    """Stand-in speech service: one word per second, times relative to the audio it was given."""
    def fn(audio, cfg, client, duration, jobdir):
        seen.append(round(duration))
        ws = [{"word": f"r{i}" + ("." if i % 5 == 4 else ""), "start": i + 0.05, "end": i + 0.9} for i in range(int(duration))]
        segs = [{"start": ws[k]["start"], "end": ws[min(k + 4, len(ws) - 1)]["end"], "text": " ".join(w["word"] for w in ws[k:k + 5]),
                 "words": ws[k:k + 5]} for k in range(0, len(ws), 5)]
        return {"engine": "groq", "language": "en", "duration": duration, "segments": segs}
    return fn


def log_of(calls):
    p = calls["ytdlp_log"]
    return open(p).read() if os.path.exists(p) else ""


class Pick(FakeGemini):
    """Selects one clip at the given absolute times (the transcript route reads times off the transcript)."""
    def __init__(self, start, end):
        self.start, self.end = start, end

    def generate_json(self, prompt, **kw):
        if '"titles"' in str(prompt):
            return super().generate_json(prompt, **kw)
        f = lambda s: f"{int(s // 60):02d}:{int(s % 60):02d}"
        return {"clips": [{"start": f(self.start), "end": f(self.end), "title": "Picked", "score": 90, "reason": "r", "hook": "h",
                           "description": "d", "hashtags": ["#x"]}]}


def make_job(c, url, **opts):
    opts = {"mode": "review", **opts}
    return c.j("POST", "/api/jobs", {"url": url, "options": opts})


# ------------------------------------------------------------------ pure logic
def test_split_parts_makes_equal_parts_and_handles_the_leftover():
    assert pipeline.split_parts(0, 180, 60) == [(0, 60), (60, 120), (120, 180)]
    assert pipeline.split_parts(0, 190, 60) == [(0, 60), (60, 120), (120, 190)]          # small leftover joins the last part
    assert pipeline.split_parts(0, 150, 60) == [(0, 60), (60, 120), (120, 150)]          # big leftover is its own part
    assert pipeline.split_parts(10, 50, 60) == [(10, 50)]                                # shorter than one part
    assert pipeline.split_parts(0, 125, 180) == [(0, 125)]
    parts = pipeline.split_parts(30, 400, 45)
    assert parts[0][0] == 30 and parts[-1][1] == 400
    assert all(a[1] == b[0] for a, b in zip(parts, parts[1:]))                           # no gaps, no overlaps
    assert all(e - s <= 180 for s, e in parts)
    assert pipeline.split_parts(0, 360, 180) == [(0, 180), (180, 360)]


def test_chunking_scout_and_windows_stay_inside_the_range():
    assert video_scout.chunk_ranges(3600, lo=1200, size=900) == [(1200, 2100), (2040, 2940), (2880, 3600)]
    assert video_scout.chunk_ranges(300, lo=100) == [(100, 300)]
    raw = [{"start": 10, "end": 40, "score": 90}, {"start": 100, "end": 130, "score": 80}, {"start": 90, "end": 105, "score": 50}]
    short = video_scout.shortlist(raw, 200, 3, lo=95)
    assert [r["start"] for r in short] == [100]                                          # first one ends before the range
    assert video_scout.windows([{"start": 96, "end": 120}], 200, lo=95)[0][0] == 95      # padding never leaves the range


def test_gemini_is_only_shown_the_range_and_times_become_absolute():
    seen = []

    def ask(parts, **kw):
        seen.append(parts[0])
        return {"clips": [{"start": "00:05", "end": "00:25", "score": 90}]}
    info = {"title": "t", "duration": 3600.0}
    raw = video_scout.scout("https://youtu.be/x", info, ask, (30,), 2, lo=600, hi=900)
    assert seen[0]["videoMetadata"] == {"startOffset": "600s", "endOffset": "900s"}
    assert raw[0]["start"] == 605 and raw[0]["end"] == 625


def test_part_label_and_badge_text():
    cfg = webapp.settings.build_config({"part_word": "Bhaag", "part_total": True})
    assert pipeline.part_label(cfg, {"part": 2, "parts": 5}) == "Bhaag 2/5"
    assert pipeline.part_label(cfg, {"id": "clip1"}) == ""
    ass = templates.badge_ass("Part 3", "#1D9BF0")
    assert "Part 3" in ass and "&HF09B1D&" in ass and "\\p1" in ass


def test_config_rejects_bad_ranges_and_part_lengths():
    from modules.utilities import ConfigError
    for bad in ({"range_start": 50, "range_end": 20}, {"part_seconds": 5}, {"part_seconds": 500}):
        with pytest.raises(ConfigError):
            webapp.settings.build_config({}, bad) if "range_start" in bad else webapp.settings.build_config(bad)
    assert webapp.settings.build_config({}, {"range_start": "1:00", "range_end": "2:30"}).range_end == 150


# ------------------------------------------------------------------ range on a link
def test_time_range_with_gemini_downloads_only_inside_the_range(rig, monkeypatch):
    app, c, calls, tmp = rig
    seen = {"watch": [], "audio": []}

    class Scout(FakeGemini):
        def generate_json(self, prompt, **kw):
            if isinstance(prompt, list):
                seen["watch"].append(prompt[0].get("videoMetadata"))
                return {"clips": [{"start": "00:05", "end": "00:25", "title": "In range", "score": 90, "reason": "r", "hook": "h",
                                   "description": "d", "hashtags": ["#x"]}]}
            return super().generate_json(prompt, **kw)
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Scout())
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen["audio"]))
    r = make_job(c, "https://youtu.be/fakevid1", clip_count=1, range_start="0:20", range_end="0:50")[1]
    v = settle(c, r["id"], "review")
    assert seen["watch"] == [{"startOffset": "20s", "endOffset": "50s"}]
    assert seen["audio"] == [30]                                                          # 30 s of the 60 s video, not all of it
    plan = app.store.job(r["id"])["data"]["plan"]
    assert 20 <= plan[0]["start"] and plan[0]["end"] <= 50 and 24 <= plan[0]["start"] <= 27
    log = log_of(calls)
    assert "--download-sections *20.00-50.00 -f bestaudio/best" in log
    assert "*0.00" not in log
    assert v["range"] == [20.0, 50.0]


def test_time_range_in_transcript_mode_transcribes_only_the_range(rig, monkeypatch):
    app, c, calls, tmp = rig
    seen = []
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen))
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Pick(35, 55))
    r = make_job(c, "https://youtu.be/fakevid1", clip_count=1, clip_finder="transcript", range_start=30)[1]
    v = settle(c, r["id"], "review")
    assert seen == [30]
    plan = app.store.job(r["id"])["data"]["plan"]
    assert all(p["start"] >= 30 for p in plan)
    log = log_of(calls)
    assert "*30.00-60.00" in log and "--download-sections" in log
    assert not any(l for l in log.splitlines() if "bestaudio/best" in l and "--download-sections" not in l)   # no whole-video audio


def test_range_after_the_end_fails_clearly_and_bad_range_is_rejected(rig):
    app, c, calls, tmp = rig
    assert make_job(c, "https://youtu.be/fakevid1", range_start="2:00", range_end="1:00")[0] == 400
    assert make_job(c, "https://youtu.be/fakevid1", range_start="abc")[0] == 400
    r = make_job(c, "https://youtu.be/fakevid1", range_start="5:00")[1]
    wait = __import__("test_server").wait_for
    wait(lambda: status(c, r["id"])["status"] == "failed" and not status(c, r["id"])["busy"], timeout=60)
    assert "past the end" in status(c, r["id"])["error"]


# ------------------------------------------------------------------ your own video
def upload(c, path, name=None):
    from urllib.parse import quote
    data = Path(path).read_bytes()
    return c.j("POST", f"/api/videos?name={quote(name or Path(path).name)}", raw=data)


def test_upload_list_use_and_delete_own_video(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    seen = []
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen))
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Pick(15, 35))
    s, up = upload(c, own_video)
    assert s == 201 and up["duration"] > 59 and up["name"].endswith(".mp4")
    vids = c.j("GET", "/api/videos")[1]["videos"]
    assert [v["name"] for v in vids] == [up["name"]]
    r = make_job(c, "", file=up["name"], clip_count=1, range_start=10, range_end=40)[1] if False else \
        c.j("POST", "/api/jobs", {"file": up["name"], "options": {"mode": "review", "clip_count": 1, "range_start": 10, "range_end": 40}})[1]
    v = settle(c, r["id"], "review")
    assert seen == [30]                                                                   # only the chosen 30 s was transcribed
    assert not os.path.exists(calls["ytdlp_log"]) or log_of(calls) == ""                  # yt-dlp is never used for your own file
    plan = app.store.job(r["id"])["data"]["plan"]
    assert all(10 <= p["start"] and p["end"] <= 40 for p in plan)
    final = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
    info = video_editor.probe(final)
    assert info["width"] == 1080 and info["height"] == 1920
    assert os.path.isfile(own_video)                                                      # the original is never touched
    assert c.j("DELETE", "/api/videos?name=" + __import__("urllib.parse").parse.quote(up["name"]))[0] == 200
    assert c.j("GET", "/api/videos")[1]["videos"] == []


def test_own_video_by_typed_path_whole_file(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    seen = []
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen))
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Pick(5, 35))
    # the stand-in only handles 60 s of speech: that is the whole file here
    s, r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {"mode": "review", "clip_count": 1}})
    assert s == 201
    v = settle(c, r["id"], "review")
    assert seen == [60] and v["title"] == "My Holiday clip"
    assert v["clips"][0]["status"] == "rendered"


def test_own_video_upload_and_path_validation(rig, own_video, monkeypatch, tmp_path):
    app, c, calls, tmp = rig
    assert c.j("POST", "/api/videos?name=notes.txt", raw=b"hello")[0] == 400                       # wrong type
    s, body = c.j("POST", "/api/videos?name=fake.mp4", raw=b"this is not a video" * 100)
    assert s == 400 and not any(p.name.endswith((".part", "fake.mp4")) for p in Path(app.data_dir, "videos").glob("*"))
    assert c.j("POST", "/api/videos?name=empty.mp4", raw=b"")[0] == 400
    monkeypatch.setattr(webapp, "VIDEO_LIMIT", 1000)
    assert c.j("POST", "/api/videos?name=big.mp4", raw=b"x" * 2000)[0] == 413
    monkeypatch.undo()
    secret = tmp_path / "secret.txt"
    secret.write_text("password")
    assert c.j("POST", "/api/jobs", {"path": str(secret), "options": {}})[0] == 400                # not a video file
    assert c.j("POST", "/api/jobs", {"path": str(tmp_path / "missing.mp4"), "options": {}})[0] == 404
    fake = tmp_path / "lie.mp4"
    fake.write_bytes(b"not a video")
    assert c.j("POST", "/api/jobs", {"path": str(fake), "options": {}})[0] == 400                  # right name, not a video
    assert c.j("POST", "/api/jobs", {"file": "../../etc/passwd", "options": {}})[0] == 400
    assert c.j("DELETE", "/api/videos?name=nothing.mp4")[0] == 404
    # a client cannot smuggle in a file path through the options
    s, r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"local_file": str(secret)}})
    assert s == 400 or "local_file" not in app.store.job(r["id"])["options"]


def test_same_name_upload_never_overwrites(rig, own_video):
    app, c, calls, tmp = rig
    a = upload(c, own_video)[1]["name"]
    b = upload(c, own_video)[1]["name"]
    assert a != b and len(c.j("GET", "/api/videos")[1]["videos"]) == 2


def test_deleted_own_video_gives_a_clear_error(tmp_path):
    p = tmp_path / "gone.mp4"
    p.write_bytes(b"x" * 100)
    info = {"id": "local_x", "local_file": str(p)}
    p.unlink()
    with pytest.raises(webapp.ClipperError, match="deleted or moved"):
        pipeline.source_file(info)


# ------------------------------------------------------------------ equal parts, no AI
def test_equal_parts_from_a_link_use_no_gemini_and_download_only_each_part(rig, monkeypatch):
    app, c, calls, tmp = rig
    asked = []

    class NoAI:
        def generate_json(self, *a, **k):
            asked.append(a)
            raise AssertionError("Gemini must not be used for equal parts")
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: NoAI())
    seen = []
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen))
    r = make_job(c, "https://youtu.be/fakevid1", make_mode="parts", part_seconds=20, part_word="Part")[1]
    v = settle(c, r["id"], "review")
    assert asked == []
    assert [cl["id"] for cl in v["clips"]] == ["clip1", "clip2", "clip3"]
    assert all(cl["status"] == "rendered" for cl in v["clips"])
    assert seen == [20, 20, 20]                                                           # captions: one transcription per part
    assert "Part 2" in v["clips"][1]["title"] or "Part 2" in v["clips"][1]["meta"]["title"]
    log = log_of(calls)
    for a, b in ((0, 20), (20, 40), (40, 60)):
        assert f"*{a}.00-{b}.00" in log
    assert not any(l for l in log.splitlines() if "bestaudio/best" in l and "--download-sections" not in l)
    for cl in v["clips"]:
        info = video_editor.probe(app.store.job(r["id"])["data"]["clips"][cl["id"]]["file"])
        assert info["height"] == 1920 and 19 < info["duration"] < 28                      # ~20 s + the outro


def test_equal_parts_without_captions_never_transcribes(rig, monkeypatch):
    app, c, calls, tmp = rig
    seen = []
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe(seen))
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: None)                 # not even a Gemini key
    r = make_job(c, "https://youtu.be/fakevid1", make_mode="parts", part_seconds=30, caption_enabled=False)[1]
    v = settle(c, r["id"], "review")
    assert len(v["clips"]) == 2 and seen == []


def test_equal_parts_inside_a_time_range(rig, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe([]))
    r = make_job(c, "https://youtu.be/fakevid1", make_mode="parts", part_seconds=20, range_start=10, range_end=50, caption_enabled=False)[1]
    v = settle(c, r["id"], "review")
    plan = app.store.job(r["id"])["data"]["plan"]
    assert [(p["start"], p["end"]) for p in plan] == [(10.0, 30.0), (30.0, 50.0)]
    assert "*10.00-30.00" in log_of(calls) and "*30.00-50.00" in log_of(calls)
    assert len(v["clips"]) == 2


def test_equal_parts_of_own_file_work_offline(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: None)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")                                         # no yt-dlp stand-in at all
    r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {"mode": "review", "make_mode": "parts", "part_seconds": 30,
                                                                    "caption_enabled": False, "template": "black"}})[1]
    v = settle(c, r["id"], "review")
    assert [cl["status"] for cl in v["clips"]] == ["rendered", "rendered"]


def test_too_many_parts_is_refused_with_advice(tmp_path):
    from modules.utilities import Config, JobState
    cfg = webapp.settings.build_config({}, {"make_mode": "parts", "part_seconds": 10})
    ctx = pipeline.Ctx(url="u", cfg=cfg, state=JobState(str(tmp_path / "j.json")))
    with pytest.raises(webapp.ClipperError, match="limit"):
        pipeline.plan_parts(ctx, {"title": "t", "duration": 3000.0}, 0.0, 3000.0)
    assert len(pipeline.plan_parts(ctx, {"title": "t", "duration": 600.0}, 0.0, 600.0)) == 60


def pixel(path, x, y, t=1.0):
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", path, "-vf", f"crop=2:2:{x}:{y}", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return tuple(out[:3])


def test_part_badge_is_drawn_top_left_on_blur_and_black_but_not_on_tweet(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: None)
    colours = {}
    for tpl in ("blur", "black", "tweet", "face"):
        r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {
            "mode": "review", "make_mode": "parts", "part_seconds": 30, "caption_enabled": False, "template": tpl,
            "part_color": "#FF0000", "outro_enabled": False, "intro_hook": False, "tweet_name": "Rohit"}})[1]
        v = settle(c, r["id"], "review")
        path = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
        colours[tpl] = pixel(path, 56, 100)
    for tpl in ("blur", "black", "face"):
        red = colours[tpl]
        assert red[0] > 200 and red[1] < 60 and red[2] < 60, (tpl, red)                   # the red badge box is there
    assert not (colours["tweet"][0] > 200 and colours["tweet"][1] < 60)                    # the tweet card has no badge box


def test_badge_can_be_turned_off_and_follows_word_and_total(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: None)
    seen = {}
    orig = templates.badge_ass

    def spy(text, *a, **k):
        seen.setdefault("texts", []).append(text)
        return orig(text, *a, **k)
    monkeypatch.setattr(templates, "badge_ass", spy)
    r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {
        "mode": "review", "make_mode": "parts", "part_seconds": 30, "caption_enabled": False, "part_word": "Episode", "part_total": True,
        "outro_enabled": False}})[1]
    settle(c, r["id"], "review")
    assert seen["texts"] == ["Episode 1/2", "Episode 2/2"]
    seen.clear()
    r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {
        "mode": "review", "make_mode": "parts", "part_seconds": 30, "caption_enabled": False, "part_badge": False,
        "outro_enabled": False}})[1]
    settle(c, r["id"], "review")
    assert "texts" not in seen


def test_tweet_template_carries_the_part_number_in_the_card_text(rig, own_video, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: None)
    seen = {}
    orig = templates.tweet_ass

    def spy(layout, name, handle, **k):
        seen["lines"] = " ".join(layout.lines)
        return orig(layout, name, handle, **k)
    monkeypatch.setattr(templates, "tweet_ass", spy)
    r = c.j("POST", "/api/jobs", {"path": str(own_video), "options": {
        "mode": "review", "make_mode": "parts", "part_seconds": 30, "caption_enabled": False, "template": "tweet",
        "tweet_name": "Rohit", "outro_enabled": False}})[1]
    settle(c, r["id"], "review")
    assert "Part" in seen["lines"]


# ------------------------------------------------------------------ isolation, speed
def test_two_jobs_on_one_video_keep_their_own_partial_transcripts(rig, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", relative_transcribe([]))
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Pick(5, 25))
    a = make_job(c, "https://youtu.be/fakevid1", clip_count=1, clip_finder="transcript", range_end=30)[1]
    settle(c, a["id"], "review")
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Pick(35, 55))
    b = make_job(c, "https://youtu.be/fakevid1", clip_count=1, clip_finder="transcript", range_start=30)[1]
    settle(c, b["id"], "review")
    files = sorted(p.name for p in Path(tmp, "tmp").rglob("*transcript_partial.json"))
    assert files == [f"j{a['id']}_transcript_partial.json", f"j{b['id']}_transcript_partial.json"]
    # re-rendering the first job's clip after the second job ran still finds ITS words
    assert c.j("POST", f"/api/jobs/{a['id']}/clips/clip1/rerender", {})[0] == 202
    wait = __import__("test_server").wait_for
    wait(lambda: not status(c, a["id"])["busy"], timeout=90)
    v = status(c, a["id"])
    assert v["clips"][0]["status"] == "rendered", v["clips"][0]


def test_cutting_from_a_big_file_shrinks_it_first(tmp_path):
    src = tmp_path / "big.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=25:d=3", "-f", "lavfi", "-i", "sine=f=300:d=3", "-pix_fmt", "yuv420p",
       "-shortest", str(src))
    raw = tmp_path / "raw.mp4"
    video_editor.cut_local(str(src), 0.5, 2.5, str(raw), 360)
    p = video_editor.probe(str(raw))
    assert p["height"] == 360 and 1.8 < p["duration"] < 2.3
    video_editor.cut_local(str(src), 0.5, 2.5, str(raw), 0)
    assert video_editor.probe(str(raw))["height"] == 1080


def test_the_closing_card_is_made_once_not_for_every_clip(tmp_path, monkeypatch):
    from modules.utilities import Config
    cfg = Config()
    cfg.outro_lines = "Thanks|for watching|Subscribe"
    cfg.outro_sec = 1.0
    made = []
    monkeypatch.setattr(video_editor, "_make_outro", lambda out, cfg: (made.append(out), Path(out).write_bytes(b"x" * 2000))[1] and out)
    for n in ("a", "b", "c"):
        work = tmp_path / "job" / n
        work.mkdir(parents=True)
        video_editor.make_outro(str(work / "outro.mp4"), cfg)
        assert (work / "outro.mp4").stat().st_size == 2000
    assert len(made) == 1
    cfg.outro_lines = "Different|text|here"
    video_editor.make_outro(str(tmp_path / "job" / "a" / "outro.mp4"), cfg)
    assert len(made) == 2


def test_preview_shows_the_badge_when_asked(rig):
    app, c, calls, tmp = rig
    s, r, png = c.call("POST", "/api/preview", {"overrides": {"template": "blur", "part_color": "#FF0000"}, "parts": True})
    assert s == 200 and png[:4] == b"\x89PNG"
    s2, r2, png2 = c.call("POST", "/api/preview", {"overrides": {"template": "blur", "part_color": "#FF0000"}})
    assert png != png2


def test_a_MAKE_environment_variable_from_the_build_tool_is_harmless(monkeypatch):
    monkeypatch.setenv("MAKE", "make")
    monkeypatch.setenv("MAKE_MODE", "")
    assert webapp.settings.build_config({}).make_mode == "ai"


def test_long_videos_are_watched_in_pieces_at_the_same_time_and_in_order():
    import threading
    import time
    lock, now, peak = threading.Lock(), [0], [0]

    def ask(parts, **kw):
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        time.sleep(0.3)
        with lock:
            now[0] -= 1
        off = int(parts[0]["videoMetadata"]["startOffset"][:-1])
        return {"clips": [{"start": "00:10", "end": "00:40", "score": 50 + off // 900}]}
    info = {"title": "t", "duration": 3600.0}
    t0 = time.time()
    raw = video_scout.scout("https://youtu.be/x", info, ask, (30,), 3)
    assert time.time() - t0 < 0.3 * 4 * 0.8 and peak[0] >= 2          # 5 pieces would take 1.5 s one after another
    assert [r["start"] for r in raw] == sorted(r["start"] for r in raw)  # pieces keep their order
