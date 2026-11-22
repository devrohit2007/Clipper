"""End-to-end: web API -> worker -> real pipeline code -> real FFmpeg render.
Only the network edges are faked: a stub `yt-dlp` executable, a stub transcript, a stub Gemini, a stub YouTube upload."""
import json
import os
import shutil
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import video_editor, webapp  # noqa: E402
from modules.utilities import ffmpeg_filters  # noqa: E402
from test_server import UI, Client, free_port, wait_for  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or "ass" not in ffmpeg_filters(),
                                reason="needs ffmpeg with the ass (caption) filter")

FAKE_YTDLP = '''#!{py}
import json, os, re, subprocess, sys
a = sys.argv[1:]
open(os.environ.get("YTDLP_LOG", os.devnull), "a").write(" ".join(a) + "\\n")
def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)
if "livevid" in a[-1]:
    if "--dump-single-json" in a:
        print(json.dumps({{"id": "livevid1", "title": "Live show", "is_live": True, "live_status": "is_live", "uploader": "Tester", "webpage_url": a[-1]}}))
    elif "-g" in a:
        print(os.environ["LIVE_M3U8"])
    else:
        sys.exit("live streams must never be downloaded with yt-dlp directly")
    sys.exit(0)
if "--dump-single-json" in a:
    print(json.dumps({{"id": "fakevid1", "title": "Fake talk", "duration": 60, "uploader": "Tester", "webpage_url": a[-1]}}))
    sys.exit(0)
out = a[a.index("-o") + 1]
if "--download-sections" in a:
    m = re.match(r"\\*([\\d.]+)-([\\d.]+)", a[a.index("--download-sections") + 1])
    dur = float(m.group(2)) - float(m.group(1))
    if "bestaudio/best" in a:
        ff("-f", "lavfi", "-i", f"sine=f=300:d={{dur}}", out.replace("%(ext)s", "m4a"))
        sys.exit(0)
    ff("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={{dur}}", "-f", "lavfi", "-i", f"sine=f=300:d={{dur}}",
       "-pix_fmt", "yuv420p", "-shortest", out)
else:
    ff("-f", "lavfi", "-i", "sine=f=300:d=60", out.replace("%(ext)s", "m4a"))
'''


def transcript():
    words, segs = [], []
    for s in range(12):                                   # twelve 5-second sentences, 60 s total
        ws = []
        for i in range(5):
            t = s * 5 + i
            txt = f"word{t}" + ("." if i == 4 else "")
            ws.append({"word": txt, "start": float(t) + 0.05, "end": float(t) + 0.9})
        segs.append({"start": ws[0]["start"], "end": ws[-1]["end"], "text": " ".join(w["word"] for w in ws), "words": ws})
    return {"engine": "groq", "language": "en", "duration": 60.0, "segments": segs}


class FakeGemini:
    def generate_json(self, prompt, **kw):
        if '"titles"' in str(prompt):
            return {"titles": ["A real title", "Second title", "Third", "Fourth", "Fifth"], "description": "Short desc.",
                    "hashtags": ["#one", "#two"], "hooks": ["hook"], "cover_text": "cover", "category": "Education"}
        return {"clips": [{"start": "00:05", "end": "00:35", "title": "Best part", "score": 91, "reason": "good", "hook": "word5",
                           "description": "d", "hashtags": ["#x"]}]}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    import logging
    logging.getLogger("clipper").setLevel(logging.INFO)
    for k in list(os.environ):
        if k.startswith(("GEMINI_", "GROQ_", "YOUTUBE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.setenv("MUSIC_FILE", str(tmp_path / "none.mp3"))
    monkeypatch.setenv("LOGO_FILE", str(tmp_path / "none.png"))
    monkeypatch.setenv("FACE_TRACKING", "0")
    monkeypatch.setenv("X264_PRESET", "ultrafast")        # speed only; output is still a real 1080x1920 render
    monkeypatch.setenv("CRF", "32")
    monkeypatch.setenv("YTDLP_LOG", str(tmp_path / "ytdlp.log"))
    monkeypatch.chdir(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ytd = bindir / "yt-dlp"
    ytd.write_text(FAKE_YTDLP.format(py=sys.executable))
    ytd.chmod(ytd.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    calls = {"transcribe": 0, "uploads": [], "ytdlp_log": str(tmp_path / "ytdlp.log")}

    def fake_transcribe(audio, cfg, client, duration, jobdir):
        calls["transcribe"] += 1
        assert os.path.isfile(audio) and os.path.getsize(audio) > 1000          # real audio was extracted by ffmpeg
        return transcript()
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", fake_transcribe)
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: FakeGemini())
    monkeypatch.setattr(webapp.pipeline, "get_service", lambda ctx: object())

    def fake_upload(service, path, meta, publish_at, retries=3, privacy="private", title_suffix=""):
        assert os.path.getsize(path) > 10000
        calls["uploads"].append({"path": path, "title": meta["title"], "publish_at": publish_at, "privacy": privacy})
        return f"YT{len(calls['uploads'])}"
    monkeypatch.setattr(webapp.pipeline.youtube_uploader, "upload_video", fake_upload)

    port = free_port()
    app = webapp.App(str(tmp_path / "data"), UI, "127.0.0.1", port)
    httpd = webapp.Server(("127.0.0.1", port), app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.start_worker()
    yield app, Client(port), calls, tmp_path
    httpd.shutdown()
    httpd.server_close()
    app.stop()


def status(c, jid):
    return c.j("GET", f"/api/jobs/{jid}")[1]


def settle(c, jid, want):
    wait_for(lambda: status(c, jid)["status"] in (want, "failed") and not status(c, jid)["busy"], timeout=120)
    v = status(c, jid)
    assert v["status"] == want, (v["status"], v["error"], c.j("GET", f"/api/jobs/{jid}/log")[1]["lines"][-8:])
    return v


def test_auto_mode_renders_uploads_schedules_and_deletes(rig):
    app, c, calls, tmp = rig
    s, r = c.j("POST", "/api/jobs", {"url": "https://www.youtube.com/watch?v=fakevid1",
                                      "options": {"mode": "auto", "clip_count": 1, "caption_style": "neon"}})
    assert s == 201
    v = settle(c, r["id"], "done")
    clip = v["clips"][0]
    assert v["title"] == "Fake talk" and v["language"] == "en" and v["engine"] == "groq"
    assert clip["status"] == "uploaded" and clip["youtube_id"] == "YT1" and clip["meta"]["title"] == "A real title"
    up = calls["uploads"][0]
    assert up["privacy"] == "private" and up["title"] == "A real title"
    assert from_iso(up["publish_at"]) > datetime.now(timezone.utc)
    assert not os.path.exists(up["path"])                                         # deleted after confirmed upload
    assert clip["video_url"] is None
    assert app.store.last_scheduled(1, "2000-01-01T00:00:00Z") == up["publish_at"]
    logs = " ".join(l["msg"] for l in c.j("GET", f"/api/jobs/{r['id']}/log")[1]["lines"])
    assert "Video: Fake talk" in logs


def from_iso(s):
    from modules.scheduling import from_iso as f
    return f(s)


def test_review_mode_edit_rerender_then_upload(rig):
    app, c, calls, tmp = rig
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1,
                                                                               "caption_language": "auto"}})[1]
    v = settle(c, r["id"], "review")
    clip = v["clips"][0]
    assert clip["status"] == "rendered" and not calls["uploads"]
    # the rendered file is a real 9:16 clip about 30 s long (plus the outro) and is streamable
    s, resp, body = c.call("GET", clip["video_url"])
    assert s == 200 and body[4:8] == b"ftyp"
    path = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
    info = video_editor.probe(path)
    assert 29 < info["duration"] < 36 and info["width"] == 1080 and info["height"] == 1920
    assert [p["text"] for p in clip["captions"]][0].startswith("word5")           # captions come from the real transcript
    # edit a caption and the title, re-render, and see the file change
    before = os.path.getsize(path)
    c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"captions": {"0": "completely different words here now"}, "meta": {"title": "Edited title"}})
    assert c.j("POST", f"/api/jobs/{r['id']}/clips/clip1/rerender", {})[0] == 202
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=120)
    v = settle(c, r["id"], "review")
    assert v["clips"][0]["status"] == "rendered" and v["clips"][0]["captions"][0]["text"].startswith("completely")
    assert os.path.getsize(path) != before
    assert calls["transcribe"] == 1
    when = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert c.j("POST", f"/api/jobs/{r['id']}/clips/clip1/upload", {"publish_at": when})[0] == 202
    v = settle(c, r["id"], "done")
    assert calls["uploads"][0]["title"] == "Edited title" and calls["uploads"][0]["publish_at"] == when
    assert not os.path.exists(path)


def test_failed_upload_keeps_the_file_and_can_be_retried(rig, monkeypatch):
    app, c, calls, tmp = rig
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1}})[1]
    settle(c, r["id"], "review")
    path = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
    good = webapp.pipeline.youtube_uploader.upload_video

    def broken(*a, **k):
        raise RuntimeError("quota exceeded")
    monkeypatch.setattr(webapp.pipeline.youtube_uploader, "upload_video", broken)
    c.j("POST", f"/api/jobs/{r['id']}/clips/clip1/upload", {})
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=60)
    v = status(c, r["id"])
    assert v["clips"][0]["status"] == "upload_failed" and "quota" in v["clips"][0]["error"] and os.path.isfile(path)
    assert v["status"] == "review"
    monkeypatch.setattr(webapp.pipeline.youtube_uploader, "upload_video", good)
    c.j("POST", f"/api/jobs/{r['id']}/clips/clip1/upload", {})
    settle(c, r["id"], "done")
    assert not os.path.exists(path) and len(calls["uploads"]) == 1


def test_second_job_for_same_video_reuses_saved_transcript(rig):
    app, c, calls, tmp = rig
    for _ in range(2):
        r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1, "clip_finder": "transcript"}})[1]
        settle(c, r["id"], "review")
    assert calls["transcribe"] == 1


def frame_px(path, x, y, t=1.0):
    import subprocess as sp
    raw = sp.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                 capture_output=True, check=True).stdout
    i = (y * 1080 + x) * 3
    return tuple(raw[i:i + 3])


def test_template_caption_mode_and_quality_flow_through_the_whole_app(rig):
    app, c, calls, tmp = rig
    c.j("PUT", "/api/settings", {"overrides": {"tweet_name": "Rohit", "tweet_handle": "@rohit"}})
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {
        "mode": "review", "clip_count": 1, "template": "tweet", "caption_mode": "single", "max_height": 720}})[1]
    v = settle(c, r["id"], "review")
    clip = v["clips"][0]
    path = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
    # quality ceiling reached yt-dlp
    section_calls = [l for l in open(calls["ytdlp_log"]).read().splitlines() if "--download-sections" in l and "bestaudio" not in l]
    assert section_calls and all("height<=720" in l for l in section_calls)
    # white page with the tweet card; defaults for the texts come from the generated metadata
    assert frame_px(path, 2, 2) == (255, 255, 255)
    assert clip["look_base"] == {"template": "tweet", "caption_mode": "single", "tweet_name": "Rohit", "tweet_handle": "@rohit", "tweet_avatar": ""}
    assert clip["look_defaults"] == {"tweet_text": "A real title", "caption_text": "cover"}
    info = video_editor.probe(path)
    assert (info["width"], info["height"]) == (1080, 1920)
    logs = " ".join(l["msg"] for l in c.j("GET", f"/api/jobs/{r['id']}/log")[1]["lines"])
    assert "template: tweet" in logs and "captions: single" in logs and "up to 720p" in logs
    # change this clip only: black template, no captions, then re-render
    assert c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"look": {"template": "black", "caption_mode": "off", "tweet_text": "Mine"}})[0] == 200
    assert c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"look": {"template": "neon"}})[0] == 400
    assert c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"look": {"colour": "red"}})[0] == 400
    assert c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"look": {"tweet_text": "x" * 201}})[0] == 400
    assert c.j("GET", f"/api/jobs/{r['id']}")[1]["clips"][0]["look"] == {"template": "black", "caption_mode": "off", "tweet_text": "Mine"}
    assert c.j("POST", f"/api/jobs/{r['id']}/clips/clip1/rerender", {})[0] == 202
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=120)
    v = settle(c, r["id"], "review")
    assert max(frame_px(path, 2, 2)) < 8                                            # now black
    assert v["clips"][0]["look"]["template"] == "black"
    # clearing the choices goes back to the global look
    c.j("PUT", f"/api/jobs/{r['id']}/clips/clip1", {"look": {"template": "", "caption_mode": "", "tweet_text": ""}})
    assert c.j("GET", f"/api/jobs/{r['id']}")[1]["clips"][0]["look"] == {}


def test_captions_off_renders_without_caption_work(rig):
    app, c, calls, tmp = rig
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {
        "mode": "review", "clip_count": 1, "caption_enabled": False}})[1]
    settle(c, r["id"], "review")
    logs = " ".join(l["msg"] for l in c.j("GET", f"/api/jobs/{r['id']}/log")[1]["lines"])
    assert "captions: off" in logs and "template: blur" in logs


def top_strip(path, rows=760, t=1.0):
    import subprocess as sp
    return sp.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-vf", f"crop=1080:{rows}:0:0",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout


def test_each_clip_can_have_its_own_tweet_name_handle_and_picture(rig):
    import subprocess as sp
    app, c, calls, tmp = rig
    c.j("PUT", "/api/settings", {"overrides": {"tweet_name": "Rohit", "tweet_handle": "@rohit"}})
    png = tmp / "green.png"
    sp.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x00ff00:s=200x200", "-frames:v", "1", str(png)], check=True)
    assert c.j("POST", "/api/assets?kind=logo&name=green.png", raw=png.read_bytes())[0] == 201
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1, "template": "tweet"}})[1]
    settle(c, r["id"], "review")
    path = app.store.job(r["id"])["data"]["clips"]["clip1"]["file"]
    first = top_strip(path)
    url = f"/api/jobs/{r['id']}/clips/clip1"
    # bad input
    assert c.j("PUT", url, {"look": {"tweet_name": "x" * 41}})[0] == 400
    assert c.j("PUT", url, {"look": {"tweet_avatar": "missing.png"}})[0] == 400
    assert c.j("PUT", url, {"look": {"tweet_avatar": "../../etc/passwd"}})[0] == 400
    # good input: handle gets its @, picture is shown by file name only (never the server path)
    assert c.j("PUT", url, {"look": {"tweet_name": "Priya", "tweet_handle": "priya", "tweet_avatar": "green.png"}})[0] == 200
    look = c.j("GET", f"/api/jobs/{r['id']}")[1]["clips"][0]["look"]
    assert look == {"tweet_name": "Priya", "tweet_handle": "@priya", "tweet_avatar": "green.png"}
    assert c.j("POST", url + "/rerender", {})[0] == 202
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=120)
    settle(c, r["id"], "review")
    second = top_strip(path)
    assert second != first
    px = [tuple(second[i:i + 3]) for i in range(0, len(second), 3)]
    assert sum(1 for p in px if p[1] > 200 and p[0] < 60 and p[2] < 60) > 3000        # green circle is the picture now
    # clearing goes back to the saved name and picture
    c.j("PUT", url, {"look": {"tweet_name": "", "tweet_handle": "", "tweet_avatar": ""}})
    assert c.j("GET", f"/api/jobs/{r['id']}")[1]["clips"][0]["look"] == {}


def test_rerender_all_applies_current_style_to_every_clip(rig):
    app, c, calls, tmp = rig
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 2}})[1]
    v = settle(c, r["id"], "review")
    n = len([x for x in v["clips"] if x["status"] == "rendered"])
    assert n >= 1
    before = {x["id"]: open(app.store.job(r["id"])["data"]["clips"][x["id"]]["file"], "rb").read() for x in v["clips"]}
    assert c.j("PUT", "/api/settings", {"overrides": {"caption_color": "#ff0000"}})[0] == 200
    s, body = c.j("POST", f"/api/jobs/{r['id']}/rerender-all", {})
    assert s == 202 and body["queued"] == n
    assert c.j("POST", f"/api/jobs/{r['id']}/rerender-all", {})[0] == 409            # already busy
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=240)
    v = settle(c, r["id"], "review")
    after = {x["id"]: open(app.store.job(r["id"])["data"]["clips"][x["id"]]["file"], "rb").read() for x in v["clips"]}
    assert all(after[k] != before[k] for k in before)
    assert c.j("POST", "/api/jobs/99999/rerender-all", {})[0] == 404


def make_live_playlist(tmp, seconds=60):
    """A 'still running' HLS stream: real 2 s segments and a playlist without #EXT-X-ENDLIST."""
    d = tmp / "live"
    d.mkdir(exist_ok=True)
    subprocess_run = __import__("subprocess").run
    subprocess_run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=300:d={seconds}", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-g", "50",
                    "-c:a", "aac", "-shortest", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0",
                    "-hls_segment_filename", str(d / "seg%03d.ts"), str(d / "stream.m3u8")], check=True)
    text = (d / "stream.m3u8").read_text().replace("#EXT-X-ENDLIST\n", "")
    (d / "stream.m3u8").write_text(text)
    return str(d / "stream.m3u8")


def test_record_live_takes_only_the_newest_part(tmp_path, monkeypatch):
    from types import SimpleNamespace
    pl = make_live_playlist(tmp_path)
    monkeypatch.setattr(video_editor, "run", (lambda orig: lambda cmd, **k: SimpleNamespace(stdout=pl + "\n") if cmd[0] == "yt-dlp" else orig(cmd, **k))(video_editor.run))
    dst = str(tmp_path / "rec.mp4")
    secs = video_editor.record_live("https://x/livevid1", dst, minutes=0.5, max_height=720)       # 30 s of a 60 s stream
    assert 28 <= secs <= 34
    secs = video_editor.record_live("https://x/livevid1", dst, minutes=60, max_height=720)        # more than exists: everything
    assert 56 <= secs <= 64


def test_live_stream_is_saved_up_to_now_and_clips_are_cut_from_it(rig, monkeypatch):
    app, c, calls, tmp = rig
    monkeypatch.setenv("LIVE_M3U8", make_live_playlist(tmp))
    s, r = c.j("POST", "/api/jobs", {"url": "https://www.youtube.com/watch?v=livevid1", "options": {"mode": "review", "clip_count": 1}})
    assert s == 201
    v = settle(c, r["id"], "review")
    assert "_live" in app.store.job(r["id"])["video_id"] and v["title"] == "Live show"
    assert 55 <= app.store.job(r["id"])["data"]["info"]["duration"] <= 65
    clip = v["clips"][0]
    assert clip["status"] == "rendered" and os.path.getsize(app.store.job(r["id"])["data"]["clips"][clip["id"]]["file"]) > 10000
    assert os.path.isfile(app.store.job(r["id"])["data"]["info"]["live_file"])                     # recording is kept for re-renders
    log = open(calls["ytdlp_log"]).read()
    assert "--download-sections" not in log and "bestaudio" not in log                              # never pulled from the live URL again
    # re-render works from the saved recording, still without touching the stream
    assert c.j("POST", f"/api/jobs/{r['id']}/clips/{clip['id']}/rerender", {})[0] == 202
    wait_for(lambda: not status(c, r["id"])["busy"], timeout=120)
    settle(c, r["id"], "review")
    assert "--download-sections" not in open(calls["ytdlp_log"]).read()


def test_gemini_watches_first_and_only_the_chosen_part_is_transcribed(rig, monkeypatch):
    """Gemini names a moment at 25-50 s of a 60 s video. Only that part (plus padding) may be downloaded and
    transcribed, and the clip must land on the right words after the part is moved back onto the video's timeline."""
    app, c, calls, tmp = rig
    seen = {"watch": 0, "parts": []}

    class Scout(FakeGemini):
        def generate_json(self, prompt, **kw):
            if isinstance(prompt, list):                                  # the "watch the video" request
                seen["watch"] += 1
                assert kw.get("media_low") is True
                assert prompt[0]["fileData"]["fileUri"] == "https://youtu.be/fakevid1"
                return {"clips": [{"start": "00:25", "end": "00:50", "title": "Late part", "score": 90, "reason": "r",
                                   "hook": "h", "description": "d", "hashtags": ["#x"]}]}
            return super().generate_json(prompt, **kw)
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Scout())

    def part_transcribe(audio, cfg, client, duration, jobdir):
        seen["parts"].append(round(duration))
        ws = []
        for i in range(int(duration)):                                    # one word per second, a sentence every 5 s, times are relative
            ws.append({"word": f"r{i}" + ("." if i % 5 == 4 else ""), "start": i + 0.05, "end": i + 0.9})
        segs = [{"start": ws[k]["start"], "end": ws[min(k + 4, len(ws) - 1)]["end"], "text": " ".join(w["word"] for w in ws[k:k + 5]),
                 "words": ws[k:k + 5]} for k in range(0, len(ws), 5)]
        return {"engine": "groq", "language": "en", "duration": duration, "segments": segs}
    monkeypatch.setattr(webapp.pipeline.transcription, "transcribe", part_transcribe)

    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1}})[1]
    v = settle(c, r["id"], "review")
    assert seen["watch"] == 1 and seen["parts"] == [45]                   # 25-50 s plus padding, clamped to the video: 15-60 s
    plan = app.store.job(r["id"])["data"]["plan"]
    assert len(plan) == 1 and 24 <= plan[0]["start"] <= 27 and 49 <= plan[0]["end"] <= 51
    tr = json.load(open(next(Path(tmp, "tmp").rglob("transcript.json"))))
    assert tr["partial"] is True and tr["windows"] == [[15.0, 60.0]]
    log = open(calls["ytdlp_log"]).read()
    assert "--download-sections *15.00-60.00 -f bestaudio/best" in log
    assert "*0.00-60.00" not in log and "sine" not in log


def test_falls_back_to_whole_video_transcript_when_gemini_cannot_watch(rig, monkeypatch):
    from modules.ai_analyzer import GeminiError
    app, c, calls, tmp = rig

    class Blind(FakeGemini):
        def generate_json(self, prompt, **kw):
            if isinstance(prompt, list):
                raise GeminiError("video not accessible")
            return super().generate_json(prompt, **kw)
    monkeypatch.setattr(webapp.pipeline, "make_client", lambda cfg: Blind())
    r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/fakevid1", "options": {"mode": "review", "clip_count": 1}})[1]
    v = settle(c, r["id"], "review")
    assert v["clips"][0]["status"] == "rendered"
    assert calls["transcribe"] == 1
    tr = json.load(open(next(Path(tmp, "tmp").rglob("transcript.json"))))
    assert not tr.get("partial")
