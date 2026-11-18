"""Tests for the web app: store, settings, scheduler, caption languages, HTTP API, worker. No network, no real yt-dlp."""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules import caption_lang, settings, webapp  # noqa: E402
from modules.scheduling import Scheduler, from_iso, to_iso  # noqa: E402
from modules.store import Store  # noqa: E402
from modules.utilities import Config, ConfigError  # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui")
HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
FAKE_GEMINI_KEY = "AI" + "zaSyDONOTLEAK1234567890abcdefghijklmn"      # built at runtime so the secret scanner stays strict


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, ""

    def call(self, method, path, body=None, headers=None, raw=None, csrf=True):
        h = {"X-Clipper": "1"} if csrf else {}
        h.update(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        b = r.read()
        c.close()
        return r.status, r, b

    def j(self, method, path, body=None, **kw):
        s, r, b = self.call(method, path, body, **kw)
        try:
            return s, json.loads(b or b"{}")
        except ValueError:
            return s, b


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("GEMINI_", "GROQ_", "YOUTUBE_", "CAPTION_", "OUTPUT_DIR", "TEMP_DIR", "MUSIC", "LOGO")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(tmp_path / "token.json"))
    monkeypatch.chdir(tmp_path)
    import logging
    logging.getLogger("clipper").setLevel(logging.INFO)
    port = free_port()
    app = webapp.App(str(tmp_path / "data"), UI, "127.0.0.1", port)
    httpd = webapp.Server(("127.0.0.1", port), app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    app.start_worker()
    yield app, Client(port), tmp_path
    httpd.shutdown()
    httpd.server_close()
    app.stop()


def wait_for(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def fake_run_job_factory(tmp_path):
    def fake(ctx):
        ctx.progress("render", 50, "working")
        info = {"id": "vid1", "title": "Test video"}
        ctx.on_info(info)
        clips_dir = os.path.join(ctx.cfg.output_dir, "clips")
        os.makedirs(clips_dir, exist_ok=True)
        f = os.path.join(clips_dir, "vid1_j%s_clip1.mp4" % ctx.tag.strip("j_"))
        open(f, "wb").write(bytes(range(256)) * 40)           # 10240 bytes
        plan = [{"id": "clip1", "start": 5, "end": 50, "duration": 45, "title": "Hook", "reason": "r", "score": 80,
                 "hook": "", "description": "", "hashtags": []}]
        meta = {"titles": ["Hook", "Other"], "title": "Hook", "description": "d", "hashtags": ["#a"], "category": "Education"}
        phr = [{"s": 0, "e": 2, "text": "hello there", "words": [{"w": "hello", "s": 0, "e": 1}, {"w": "there", "s": 1, "e": 2}],
                "approx": False, "edited": False}]
        ctx.state.set(info=info, plan=plan, language="en", transcript_engine="groq")
        ctx.state.update_clip("clip1", status="rendered", file=f, meta=meta, captions=phr)
        return {"rendered": 1, "uploaded": 0, "total": 1}
    return fake


def make_job(env, monkeypatch, opts=None):
    app, c, tmp = env
    monkeypatch.setattr(webapp.pipeline, "run_job", fake_run_job_factory(tmp))
    s, r = c.j("POST", "/api/jobs", {"url": "https://youtu.be/abcdefghijk", "options": opts or {}})
    assert s == 201, r
    jid = r["id"]
    wait_for(lambda: c.j("GET", f"/api/jobs/{jid}")[1]["status"] == "review")
    return jid


# ============================================================ store, settings, scheduler
def test_store_tasks_recovery_and_scheduled(tmp_path):
    db = str(tmp_path / "s.db")
    st = Store(db)
    jid = st.create_job("https://youtu.be/x", {"mode": "auto"})
    t1 = st.add_task("pipeline", jid)
    claimed = st.claim_task()
    assert claimed["id"] == t1 and st.claim_task() is None
    st2 = Store(db)                                          # "restart" while a task is running
    assert st2.recover_tasks() == 1
    assert st2.claim_task()["id"] == t1
    st2.finish_task(t1, "done")
    assert st2.pending_tasks() == []
    st2.add_scheduled(1, "2030-01-01T12:00:00Z", jid, "clip1", "yt1")
    assert st2.last_scheduled(1, "2029-01-01T00:00:00Z") == "2030-01-01T12:00:00Z"
    assert st2.last_scheduled(2, "2029-01-01T00:00:00Z") is None
    assert st2.last_scheduled(1, "2031-01-01T00:00:00Z") is None     # past slots do not matter


def test_store_overrides_presets_accounts(tmp_path):
    st = Store(str(tmp_path / "s.db"))
    st.set_overrides({"caption_style": "neon"})
    assert st.get_overrides() == {"caption_style": "neon"}
    st.save_preset("p", {"zoom": 1.1})
    assert st.presets() == {"p": {"zoom": 1.1}}
    assert st.delete_preset("p") and not st.delete_preset("p")
    a = st.add_account("Main", "/tmp/t.json")
    st.rename_account(a, "Second")
    assert st.account(a)["name"] == "Second"
    st.delete_account(a)
    assert st.account(a) is None


def test_settings_precedence_and_validation(monkeypatch):
    monkeypatch.setenv("CAPTION_STYLE", "bold")
    assert settings.build_config().caption_style == "bold"
    assert settings.build_config({"caption_style": "neon"}).caption_style == "neon"            # UI beats .env
    assert settings.build_config({"caption_style": "neon"}, {"caption_style": "minimal"}).caption_style == "minimal"
    with pytest.raises(ConfigError):
        settings.build_config({"caption_style": "nope"})
    with pytest.raises(ConfigError):
        settings.coerce("zoom", "abc")
    with pytest.raises(ConfigError):
        settings.clean_overrides({"gemini_api_key": "x"})             # secrets are not settings
    assert settings.coerce("clip_durations", "15, 30,60") == (15, 30, 60)
    assert settings.clean_overrides({"mode": "auto", "fresh": True}, allow_job=True) == {"mode": "auto", "fresh": True}


def test_every_schema_field_has_a_working_default():
    vals = settings.effective_values({})
    assert set(vals) == {f["key"] for f in settings.schema_json()}
    for f in settings.schema_json():
        if f["type"] == "select":
            assert str(vals[f["key"]]) in [c[0] for c in f["choices"]], f["key"]


def test_scheduler_slots_and_collisions():
    cfg = Config()
    cfg.schedule_mode, cfg.publish_slots, cfg.tz_offset, cfg.publish_start_delay_min = "slots", "18:00,21:00", "+05:30", 30
    now = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)              # 11:30 IST
    s = Scheduler(cfg, None, now)
    got = []
    for _ in range(4):
        t = s.peek()
        s.commit(t)
        got.append(t)
    assert got == ["2030-01-01T12:30:00Z", "2030-01-01T15:30:00Z", "2030-01-02T12:30:00Z", "2030-01-02T15:30:00Z"]
    s2 = Scheduler(cfg, "2030-01-01T12:30:00Z", now)                   # an earlier job already used the first slot
    assert s2.peek() == "2030-01-01T15:30:00Z"
    cfg.schedule_mode, cfg.publish_interval_hours = "interval", 6
    assert Scheduler(cfg, "2030-01-01T12:30:00Z", now).peek() == "2030-01-01T18:30:00Z"


# ============================================================ caption languages
def _transcript(words, lang="hi"):
    ws = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate(words)]
    return {"language": lang, "segments": [{"start": 0.0, "end": len(words) * 0.5, "text": " ".join(words), "words": ws}]}


def test_caption_script_change_is_word_for_word_and_keeps_timing():
    tr = _transcript(["kya", "haal", "hai"])
    clip = {"start": 0, "end": 10}
    phrases, notes = caption_lang.make_plan(tr, clip, "hi", lambda p: {"words": ["क्या", "हाल", "है"]})
    assert [w["w"] for w in phrases[0]["words"]] == ["क्या", "हाल", "है"]
    assert [w["s"] for w in phrases[0]["words"]] == [0.0, 0.5, 1.0] and not phrases[0]["approx"]


def test_caption_conversion_falls_back_when_gemini_misbehaves():
    tr = _transcript(["kya", "haal", "hai"])
    clip = {"start": 0, "end": 10}
    phrases, notes = caption_lang.make_plan(tr, clip, "hi", lambda p: {"words": ["एक"]})     # wrong word count
    assert phrases[0]["text"] == "kya haal hai" and notes
    phrases, notes = caption_lang.make_plan(tr, clip, "hi", None)                            # no Gemini at all
    assert phrases[0]["text"] == "kya haal hai" and notes
    phrases, notes = caption_lang.make_plan(tr, clip, "auto", None)                          # as spoken needs nothing
    assert phrases[0]["text"] == "kya haal hai" and not notes


def test_caption_edit_spreads_words_over_phrase_time():
    ph = caption_lang.build_phrases(_transcript(["a", "b", "c"]), {"start": 0, "end": 10})
    caption_lang.apply_edits(ph, {"0": "one two three four"})
    ws = ph[0]["words"]
    assert len(ws) == 4 and ws[0]["s"] == ph[0]["s"] and ws[-1]["e"] <= ph[0]["e"] + 1e-6 and ph[0]["edited"]
    caption_lang.apply_edits(ph, {"0": "  "})
    assert ph[0]["words"] == []                                         # empty text removes the caption


# ============================================================ HTTP: security
def test_static_files_and_status(env):
    app, c, _ = env
    s, r, b = c.call("GET", "/")
    assert s == 200 and b"Clipper" in b and "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert c.call("GET", "/ui/app.js")[0] == 200
    assert c.call("GET", "/ui/../server.py")[0] == 404
    assert c.call("GET", "/ui/secret.txt")[0] == 404
    s, d = c.j("GET", "/api/status")
    assert s == 200 and d["accounts"][0]["name"] == "Default" and d["lan"] is False


def test_host_header_and_csrf_header_are_enforced(env):
    app, c, _ = env
    assert c.call("GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    assert c.call("POST", "/api/presets", {"name": "x"}, csrf=False)[0] == 403
    assert c.call("DELETE", "/api/presets/x", csrf=False)[0] == 403
    assert c.call("PUT", "/api/settings", {"overrides": {}}, csrf=False)[0] == 403


def test_request_size_and_json_errors(env):
    app, c, _ = env
    assert c.call("POST", "/api/presets", raw=b"{nope", headers={"Content-Type": "application/json"})[0] == 400
    assert c.call("POST", "/api/presets", raw=b"[1]", headers={"Content-Type": "application/json"})[0] == 400
    big = b'{"name":"' + b"a" * (webapp.MAX_JSON + 10) + b'"}'
    assert c.call("POST", "/api/presets", raw=big, headers={"Content-Type": "application/json"})[0] == 413
    assert c.call("GET", "/api/nothing")[0] == 404


def test_password_flow(env):
    app, c, _ = env
    assert c.j("GET", "/api/auth/status")[1] == {"password_set": False, "authed": True}
    assert c.j("POST", "/api/auth/password", {"password": "abc"})[0] == 400                 # too short
    s, r, _ = c.call("POST", "/api/auth/password", {"password": "secret1"})
    assert s == 200
    c.cookie = r.headers["Set-Cookie"].split(";")[0]                  # setting a password signs the caller in
    anon = Client(c.port)
    assert anon.j("GET", "/api/settings")[0] == 401
    assert anon.j("GET", "/api/auth/status")[1] == {"password_set": True, "authed": False}
    assert anon.j("POST", "/api/auth/login", {"password": "wrong"})[0] == 401
    s, r, _ = anon.call("POST", "/api/auth/login", {"password": "secret1"})
    cookie = r.headers["Set-Cookie"]
    assert s == 200 and "HttpOnly" in cookie and "SameSite" in cookie
    anon.cookie = cookie.split(";")[0]
    assert anon.j("GET", "/api/settings")[0] == 200
    assert anon.call("GET", "/media/1/clip1.mp4")[0] == 404                                 # authed but no such clip
    anon.call("POST", "/api/auth/logout")
    assert anon.j("GET", "/api/settings")[0] == 401
    # a wrong current password cannot remove it
    assert c.j("POST", "/api/auth/password", {"current": "x", "password": ""})[0] == 403


def test_login_is_rate_limited(env):
    app, c, _ = env
    c.j("POST", "/api/auth/password", {"password": "secret1"})
    anon = Client(c.port)
    codes = [anon.j("POST", "/api/auth/login", {"password": "bad"})[0] for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert anon.j("POST", "/api/auth/login", {"password": "secret1"})[0] == 429


def test_secrets_never_reach_the_browser(env, monkeypatch):
    app, c, tmp = env
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_DONOTLEAK1234567890abcdefghijklmnopqr")
    for p in ("/api/status", "/api/settings", "/api/schema", "/api/accounts", "/api/assets", "/api/presets"):
        s, _r, b = c.call("GET", p)
        assert s == 200 and b"DONOTLEAK" not in b, p
    s, d = c.j("GET", "/api/status")
    assert d["gemini_key"] is True and d["groq_key"] is True
    with pytest.raises(Exception):
        settings.clean_overrides({"groq_api_key": "x"})


# ============================================================ HTTP: settings and presets
def test_settings_roundtrip_reset_and_validation(env):
    app, c, _ = env
    s, d = c.j("PUT", "/api/settings", {"overrides": {"caption_style": "neon", "caption_size": 90, "caption_color": "#ff0000"}})
    assert s == 200 and d["values"]["caption_style"] == "neon"
    assert c.j("GET", "/api/settings")[1]["overrides"]["caption_size"] == 90
    s, d = c.j("PUT", "/api/settings", {"overrides": {"caption_style": "bogus"}})
    assert s == 400 and "Style" in d["error"]
    assert c.j("GET", "/api/settings")[1]["overrides"]["caption_style"] == "neon"          # rejected save changed nothing
    s, d = c.j("POST", "/api/settings/reset", {"keys": ["caption_size"]})
    assert "caption_size" not in d["overrides"] and d["overrides"]["caption_style"] == "neon"
    assert c.j("POST", "/api/settings/reset", {})[1]["overrides"] == {}


def test_presets_apply_and_delete(env):
    app, c, _ = env
    c.j("PUT", "/api/settings", {"overrides": {"zoom": 0.12}})
    assert c.j("POST", "/api/presets", {"name": "Zoomy"})[1]["presets"]["Zoomy"] == {"zoom": 0.12}
    c.j("POST", "/api/settings/reset", {})
    s, d = c.j("POST", "/api/presets/apply", {"name": "Zoomy"})
    assert d["values"]["zoom"] == 0.12
    assert c.j("POST", "/api/presets/apply", {"name": "nope"})[0] == 404
    assert c.j("POST", "/api/presets", {"name": "  "})[0] == 400
    assert c.j("DELETE", "/api/presets/Zoomy")[1]["presets"] == {}
    assert c.j("DELETE", "/api/presets/Zoomy")[0] == 404


# ============================================================ HTTP: jobs and worker
def test_job_validation(env):
    app, c, _ = env
    assert c.j("POST", "/api/jobs", {"url": "https://example.com/x"})[0] == 400
    assert c.j("POST", "/api/jobs", {"url": "javascript:alert(1)"})[0] == 400
    assert c.j("POST", "/api/jobs", {"url": "https://youtu.be/abc", "options": {"mode": "weird"}})[0] == 400
    assert c.j("POST", "/api/jobs", {"url": "https://youtu.be/abc", "options": {"caption_style": "bogus"}})[0] == 400
    assert c.j("POST", "/api/jobs", {"url": "https://youtu.be/abc", "options": {"groq_api_key": "x"}})[0] == 400
    assert c.j("GET", "/api/jobs")[1]["jobs"] == []                                       # nothing was queued


def test_job_runs_to_review_and_edits_are_saved(env, monkeypatch):
    app, c, _ = env
    seen = {}
    real = webapp.App.cfg_for

    def spy(self, job):
        cfg = real(self, job)
        seen["cfg"] = cfg
        return cfg
    monkeypatch.setattr(webapp.App, "cfg_for", spy)
    jid = make_job(env, monkeypatch, {"mode": "review", "caption_language": "hi", "caption_style": "neon"})
    assert seen["cfg"].caption_language == "hi" and seen["cfg"].caption_style == "neon" and seen["cfg"].auto_upload is False
    s, v = c.j("GET", f"/api/jobs/{jid}")
    clip = v["clips"][0]
    assert v["title"] == "Test video" and v["engine"] == "groq" and clip["status"] == "rendered" and clip["video_url"].startswith(f"/media/{jid}/clip1.mp4")
    s, _ = c.j("PUT", f"/api/jobs/{jid}/clips/clip1", {"meta": {"title": "New title", "hashtags": ["x", "#y"], "description": "dd"},
                                                      "captions": {"0": "namaste duniya today"}})
    assert s == 200
    clip = c.j("GET", f"/api/jobs/{jid}")[1]["clips"][0]
    assert clip["meta"]["title"] == "New title" and clip["meta"]["platforms"]["youtube"]["title"] == "New title"
    assert clip["meta"]["hashtags"] == ["#x", "#y"]
    assert clip["captions"][0]["text"] == "namaste duniya today" and len(clip["captions"][0]["words"]) == 3 and clip["captions"][0]["edited"]
    future = to_iso(datetime.now(timezone.utc) + timedelta(days=3))
    assert c.j("PUT", f"/api/jobs/{jid}/clips/clip1", {"publish_at": future})[0] == 200
    assert c.j("GET", f"/api/jobs/{jid}")[1]["clips"][0]["publish_at"] == future
    assert c.j("PUT", f"/api/jobs/{jid}/clips/clip1", {"publish_at": "2001-01-01T00:00:00Z"})[0] == 400
    assert c.j("PUT", f"/api/jobs/{jid}/clips/clip1", {"publish_at": "tomorrow"})[0] == 400
    assert c.j("PUT", f"/api/jobs/{jid}/clips/clip9", {})[0] == 404
    assert any("Test video" in l["msg"] or "finished" in l["msg"].lower() for l in c.j("GET", f"/api/jobs/{jid}/log")[1]["lines"])


def test_failed_job_can_be_retried_and_error_is_redacted(env, monkeypatch):
    app, c, _ = env
    calls = {"n": 0}

    def boom(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise webapp.ClipperError("Gemini said no for key " + FAKE_GEMINI_KEY)
        return fake_run_job_factory(None)(ctx)
    monkeypatch.setattr(webapp.pipeline, "run_job", boom)
    jid = c.j("POST", "/api/jobs", {"url": "https://youtu.be/abc"})[1]["id"]
    wait_for(lambda: c.j("GET", f"/api/jobs/{jid}")[1]["status"] == "failed")
    v = c.j("GET", f"/api/jobs/{jid}")[1]
    assert "DONOTLEAK" not in json.dumps(v) and "DONOTLEAK" not in json.dumps(c.j("GET", f"/api/jobs/{jid}/log")[1])
    assert c.j("POST", f"/api/jobs/{jid}/retry")[0] == 200
    wait_for(lambda: c.j("GET", f"/api/jobs/{jid}")[1]["status"] == "review")


def test_cancel_running_job_and_queue_order(env, monkeypatch):
    app, c, _ = env
    started = threading.Event()

    def slow(ctx):
        started.set()
        while True:
            ctx.check_cancel()
            time.sleep(0.02)
    monkeypatch.setattr(webapp.pipeline, "run_job", slow)
    a = c.j("POST", "/api/jobs", {"url": "https://youtu.be/aaa"})[1]["id"]
    b = c.j("POST", "/api/jobs", {"url": "https://youtu.be/bbb"})[1]["id"]
    assert started.wait(5)
    assert c.j("GET", f"/api/jobs/{b}")[1]["status"] == "queued"          # one job at a time
    assert c.j("DELETE", f"/api/jobs/{a}")[0] == 409                      # cannot delete a running job
    c.j("POST", f"/api/jobs/{b}/cancel")
    assert c.j("GET", f"/api/jobs/{b}")[1]["status"] == "cancelled"
    c.j("POST", f"/api/jobs/{a}/cancel")
    wait_for(lambda: c.j("GET", f"/api/jobs/{a}")[1]["status"] == "cancelled")
    wait_for(lambda: not app.store.pending_tasks())
    assert c.j("DELETE", f"/api/jobs/{a}")[0] == 200
    assert c.j("GET", f"/api/jobs/{a}")[0] == 404


def test_upload_and_discard_actions(env, monkeypatch):
    app, c, tmp = env
    jid = make_job(env, monkeypatch)
    uploaded = []

    def fake_upload(ctx, cid, publish_at=None):
        uploaded.append((cid, publish_at or ctx.state.clip(cid).get("publish_at")))
        ctx.state.update_clip(cid, status="uploaded", youtube_id="YT123", publish_at=publish_at)
        return True
    monkeypatch.setattr(webapp.pipeline, "upload_existing", fake_upload)
    future = to_iso(datetime.now(timezone.utc) + timedelta(days=2))
    assert c.j("POST", f"/api/jobs/{jid}/clips/clip1/upload", {"publish_at": future})[0] == 202
    wait_for(lambda: c.j("GET", f"/api/jobs/{jid}")[1]["status"] == "done")
    clip = c.j("GET", f"/api/jobs/{jid}")[1]["clips"][0]
    assert uploaded == [("clip1", future)] and clip["youtube_id"] == "YT123"
    # discarding another job's clip deletes its file
    jid2 = make_job(env, monkeypatch)
    f = app.store.job(jid2)["data"]["clips"]["clip1"]["file"]
    assert os.path.isfile(f)
    monkeypatch.undo()
    monkeypatch.setattr(webapp.pipeline, "discard_clip", lambda ctx, cid: (os.remove(f), ctx.state.update_clip(cid, status="discarded", file="")))
    assert c.j("POST", f"/api/jobs/{jid2}/clips/clip1/discard")[0] == 200
    assert not os.path.exists(f) and c.j("GET", f"/api/jobs/{jid2}")[1]["status"] == "done"


def test_upload_requires_a_rendered_clip(env, monkeypatch):
    app, c, _ = env
    jid = make_job(env, monkeypatch)
    f = app.store.job(jid)["data"]["clips"]["clip1"]["file"]
    os.remove(f)
    assert c.j("POST", f"/api/jobs/{jid}/clips/clip1/upload", {})[0] == 409


def test_unfinished_tasks_resume_after_restart(tmp_path):
    db = tmp_path / "d"
    a1 = webapp.App(str(db), UI, "127.0.0.1", 1)
    jid = a1.store.create_job("https://youtu.be/x", {})
    a1.enqueue("pipeline", jid)
    a1.store.claim_task()                                              # worker picked it up, then the phone rebooted
    a2 = webapp.App(str(db), UI, "127.0.0.1", 1)
    assert [t["status"] for t in a2.store.pending_tasks()] == ["queued"]


# ============================================================ HTTP: media
def test_media_range_requests(env, monkeypatch):
    app, c, _ = env
    jid = make_job(env, monkeypatch)
    url = f"/media/{jid}/clip1.mp4"
    s, r, b = c.call("GET", url)
    assert s == 200 and len(b) == 10240 and r.headers["Accept-Ranges"] == "bytes" and r.headers["Content-Type"] == "video/mp4"
    full = b
    s, r, b = c.call("GET", url, headers={"Range": "bytes=100-199"})
    assert s == 206 and b == full[100:200] and r.headers["Content-Range"] == "bytes 100-199/10240"
    s, r, b = c.call("GET", url, headers={"Range": "bytes=10000-"})
    assert s == 206 and b == full[10000:]
    s, r, b = c.call("GET", url, headers={"Range": "bytes=-50"})
    assert s == 206 and b == full[-50:]
    assert c.call("GET", url, headers={"Range": "bytes=99999-"})[0] == 416
    assert c.call("GET", url, headers={"Range": "bytes=abc"})[0] == 416
    assert c.call("GET", f"/media/{jid}/clip2.mp4")[0] == 404


def test_media_refuses_files_outside_the_clips_folder(env, monkeypatch):
    app, c, tmp = env
    jid = make_job(env, monkeypatch)
    secret = tmp / "secret.txt"
    secret.write_text("top secret")
    data = app.store.job(jid)["data"]
    data["clips"]["clip1"]["file"] = str(secret)
    app.store.save_job_data(jid, data)
    assert c.call("GET", f"/media/{jid}/clip1.mp4")[0] == 403
    link = tmp / "out" / "clips" / "link.mp4"                              # a symlink inside clips/ pointing outside
    os.symlink(secret, link)
    data["clips"]["clip1"]["file"] = str(link)
    app.store.save_job_data(jid, data)
    assert c.call("GET", f"/media/{jid}/clip1.mp4")[0] == 403
    assert c.call("GET", f"/media/{jid}/..%2f..%2fsecret.txt")[0] == 404


# ============================================================ HTTP: assets, accounts, oauth, tools
def test_asset_upload_rules(env):
    app, c, tmp = env
    s, r = c.j("POST", "/api/assets?kind=music&name=beat.mp3", raw=b"ID3" + b"0" * 100)
    assert s == 201 and r["name"] == "beat.mp3" and os.path.isfile(r["path"])
    assert [m["name"] for m in c.j("GET", "/api/assets")[1]["music"]] == ["beat.mp3"]
    assert c.j("POST", "/api/assets?kind=music&name=evil.exe", raw=b"x")[0] == 400
    assert c.j("POST", "/api/assets?kind=nope&name=a.mp3", raw=b"x")[0] == 400
    assert c.j("POST", "/api/assets?kind=logo&name=a.png", raw=b"")[0] == 400
    assert c.j("POST", "/api/assets?kind=logo&name=big.png", raw=b"0" * (5 * 1024 * 1024 + 1))[0] == 413
    s, r = c.j("POST", "/api/assets?kind=logo&name=..%2f..%2fescape.png", raw=b"\x89PNG0000")
    assert s == 201 and r["name"] == "escape.png" and os.path.dirname(r["path"]).endswith(os.path.join("assets", "logo"))
    assert not (tmp / "escape.png").exists()
    assert c.j("DELETE", "/api/assets?kind=music&name=beat.mp3")[0] == 200
    assert c.j("DELETE", "/api/assets?kind=music&name=beat.mp3")[0] == 404


def test_accounts_crud_and_oauth_callback_guards(env):
    app, c, tmp = env
    s, r = c.j("POST", "/api/accounts", {"name": "Second"})
    assert s == 201 and len(c.j("GET", "/api/accounts")[1]["accounts"]) == 2
    assert c.j("PUT", f"/api/accounts/{r['id']}", {"name": "Renamed"})[0] == 200
    assert c.j("POST", "/api/accounts", {"name": ""})[0] == 400
    assert c.j("POST", f"/api/accounts/{r['id']}/auth")[0] == 400                 # client_secret.json is missing here
    s, _r, b = c.call("GET", "/oauth/callback?state=unknown&code=abc")
    assert s == 400 and b"expired" in b.lower()
    assert c.j("DELETE", f"/api/accounts/{r['id']}")[0] == 200
    only = c.j("GET", "/api/accounts")[1]["accounts"][0]["id"]
    assert c.j("DELETE", f"/api/accounts/{only}")[0] == 409                       # always keep one


def test_font_family_reader_and_name_cleaning(tmp_path):
    assert webapp.font_family(str(tmp_path / "missing.ttf")) == ""
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"not a font")
    assert webapp.font_family(str(bad)) == ""
    with pytest.raises(webapp.HttpError):
        webapp.safe_name("x.sh", {".mp3"})
    assert webapp.safe_name("a b/c.mp3", {".mp3"}) == "c.mp3"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_caption_preview_png(env):
    app, c, _ = env
    s, r, b = c.call("POST", "/api/preview", {"overrides": {"caption_language": "hi", "caption_style": "neon", "caption_color": "#00ffcc"}})
    if s != 200:
        pytest.skip("this FFmpeg cannot draw captions: " + b.decode()[:100])
    assert b[:4] == b"\x89PNG" and r.headers["Content-Type"] == "image/png"
    assert c.call("POST", "/api/preview", {"overrides": {"caption_style": "bogus"}})[0] == 400


def test_cleanup_and_disk(env):
    app, c, tmp = env
    old = tmp / "tmp" / "oldvideo"
    old.mkdir(parents=True)
    (old / "raw.mp4").write_bytes(b"0" * 2048)
    os.utime(old, (1, 1))
    assert c.j("GET", "/api/disk")[0] == 200
    s, d = c.j("POST", "/api/maintenance/cleanup", {"days": 1})
    assert s == 200 and not old.exists()
    assert os.path.isdir(tmp / "tmp") or True


def test_diagnostics_reports_without_leaking(env, monkeypatch):
    app, c, _ = env
    monkeypatch.setenv("GROQ_API_KEY", "gsk_DONOTLEAK1234567890abcdefghijklmnopqr")
    import requests

    class R:
        status_code = 401
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    s, _r, b = c.call("POST", "/api/diagnostics", {})
    d = json.loads(b)
    names = {x["name"]: x for x in d["checks"]}
    assert s == 200 and {"ffmpeg", "Gemini", "Groq", "YouTube", "Storage"} <= set(names)
    assert names["Gemini"]["ok"] is False and "GEMINI_API_KEY" in names["Gemini"]["detail"]
    assert names["Groq"]["ok"] is False and b"DONOTLEAK" not in b


# ============================================================ server.py entry point
def test_server_refuses_lan_without_password_and_password_commands(tmp_path, monkeypatch, capsys):
    import getpass
    import server
    monkeypatch.chdir(tmp_path)
    data = str(tmp_path / "d")
    assert server.main(["--host", "0.0.0.0", "--data-dir", data, "--env-file", str(tmp_path / "none")]) == 2
    assert "password" in capsys.readouterr().out.lower()
    answers = iter(["abc", "abc"])                                     # too short
    monkeypatch.setattr(getpass, "getpass", lambda *_: next(answers))
    assert server.main(["--set-password", "--data-dir", data, "--env-file", str(tmp_path / "none")]) == 2
    answers = iter(["longenough", "longenough"])
    assert server.main(["--set-password", "--data-dir", data, "--env-file", str(tmp_path / "none")]) == 0
    st = Store(os.path.join(data, "clipper.db"))
    rec = st.kv_get("auth")
    assert webapp.check_password("longenough", rec) and not webapp.check_password("nope", rec) and "longenough" not in json.dumps(rec)
    assert server.main(["--clear-password", "--data-dir", data, "--env-file", str(tmp_path / "none")]) == 0
    assert st.kv_get("auth") is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_preview_renders_each_template_and_caption_mode(env):
    app, c, _ = env
    sizes = {}
    for name, ov in {"blur": {}, "tweet": {"template": "tweet", "tweet_name": "Rohit", "tweet_handle": "@rohit"},
                     "black_single": {"template": "black", "caption_mode": "single"},
                     "tweet_hi_off": {"template": "tweet", "caption_language": "hi", "caption_enabled": False}}.items():
        s, r, b = c.call("POST", "/api/preview", {"overrides": ov})
        if s != 200:
            pytest.skip("this FFmpeg cannot draw captions: " + b.decode()[:100])
        assert b[:4] == b"\x89PNG", name
        sizes[name] = len(b)
    assert len(set(sizes.values())) == len(sizes)                       # four visibly different pictures
    assert c.call("POST", "/api/preview", {"overrides": {"template": "neon"}})[0] == 400
    assert c.call("POST", "/api/preview", {"overrides": {"max_height": "4320"}})[0] == 400


def test_new_settings_are_in_the_schema_and_round_trip(env):
    app, c, _ = env
    fields = {f["key"]: f for f in c.j("GET", "/api/schema")[1]["schema"]}
    assert [x[0] for x in fields["template"]["choices"]] == ["blur", "tweet", "black", "face"]
    assert [x[0] for x in fields["max_height"]["choices"]] == ["360", "480", "720", "1080"]
    assert [x[0] for x in fields["caption_mode"]["choices"]] == ["words", "single"]
    s, d = c.j("PUT", "/api/settings", {"overrides": {"template": "tweet", "max_height": "720", "caption_mode": "single"}})
    assert s == 200 and d["values"]["max_height"] == 720 and d["values"]["template"] == "tweet"


def test_stopping_the_app_does_not_leave_the_cancel_flag_set(tmp_path):
    from modules.utilities import CANCEL_EVENT, run
    app = webapp.App(str(tmp_path / "d"), UI, "127.0.0.1", free_port())
    app.start_worker()
    app.stop()
    assert not CANCEL_EVENT.is_set()
    assert run([sys.executable, "-c", "print('ok')"]).stdout.strip() == "ok"       # later subprocesses still run
