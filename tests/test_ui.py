"""The three-step Create screen, driven in a real browser. Skipped when Playwright/Chromium is not installed."""
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

pw = pytest.importorskip("playwright.sync_api")
from modules import webapp  # noqa: E402
from test_server import UI, free_port  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.fixture
def site(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("GEMINI_", "GROQ_", "YOUTUBE_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.chdir(tmp_path)
    port = free_port()
    app = webapp.App(str(tmp_path / "data"), UI, "127.0.0.1", port)
    httpd = webapp.Server(("127.0.0.1", port), app)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    vid = tmp_path / "talk video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=125", "-f", "lavfi", "-i",
                    "sine=d=125", "-pix_fmt", "yuv420p", "-shortest", str(vid)], check=True)
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:                       # browser not installed
            pytest.skip(f"no browser: {e}")
        yield f"http://127.0.0.1:{port}/", browser, vid
        browser.close()
    httpd.shutdown()
    httpd.server_close()
    app.stop()


def open_page(browser, url, route="#/create"):
    pg = browser.new_page(viewport={"width": 390, "height": 800})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url + route)
    pg.wait_for_selector("form.make")
    return pg, errs


def capture_job_post(pg, reply_id=7):
    sent = {}

    def handler(route):
        sent["body"] = json.loads(route.request.post_data)
        route.fulfill(status=201, content_type="application/json", body=json.dumps({"id": reply_id}))
    pg.route("**/api/jobs", lambda r: handler(r) if r.request.method == "POST" else r.continue_())
    return sent


def test_link_with_best_moments_and_a_time_range(site):
    url, browser, vid = site
    pg, errs = open_page(browser, url)
    sent = capture_job_post(pg)
    pg.fill("input[type=url]", "https://youtu.be/abc")
    pg.check("label:has-text('Use only part') input")
    pg.fill("input[placeholder='0:00']", "1:30")
    pg.fill("input[placeholder='end of video']", "10:00")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_url("**/#/job/7")
    b = sent["body"]
    assert b["url"] == "https://youtu.be/abc" and b["options"]["make_mode"] == "ai"
    assert b["options"]["range_start"] == 90 and b["options"]["range_end"] == 600
    assert errs == []


def test_equal_parts_sends_no_ai_options_and_shows_the_badge_in_the_preview(site):
    url, browser, vid = site
    pg, errs = open_page(browser, url)
    sent = capture_job_post(pg)
    pg.click("button.choice[data-k=parts]")
    assert pg.is_hidden("text=What to look for")                      # AI-only fields are gone
    assert pg.is_visible("text=Show Part 1, Part 2")
    pg.click("button.chip:has-text('45 s')")
    pg.fill("input[type=url]", "https://youtu.be/abc")
    pg.fill("label:has-text('Word') input", "Bhaag")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_url("**/#/job/7")
    o = sent["body"]["options"]
    assert o["make_mode"] == "parts" and o["part_seconds"] == 45 and o["part_word"] == "Bhaag"
    assert "clip_count" not in o and "clip_durations" not in o and "range_start" not in o
    assert errs == []


def test_bad_input_is_stopped_before_anything_is_sent(site):
    url, browser, vid = site
    pg, errs = open_page(browser, url)
    sent = capture_job_post(pg)
    pg.click("button:has-text('Make clips')")
    pg.wait_for_selector(".toast.bad:has-text('Paste a YouTube link')")
    pg.fill("input[type=url]", "https://youtu.be/abc")
    pg.check("label:has-text('Use only part') input")
    pg.fill("input[placeholder='0:00']", "abc")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_selector(".toast.bad:has-text('Time range')")
    pg.fill("input[placeholder='0:00']", "5:00")
    pg.fill("input[placeholder='end of video']", "2:00")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_selector(".toast.bad:has-text('end must be after')")
    pg.click("button[data-k=file]")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_selector(".toast.bad:has-text('Choose a video file')")
    assert "body" not in sent and errs == []


def test_choosing_a_file_uploads_it_and_estimates_the_parts(site):
    url, browser, vid = site
    pg, errs = open_page(browser, url)
    sent = capture_job_post(pg)
    pg.click("button[data-k=file]")
    pg.set_input_files("input[type=file]", str(vid))
    pg.wait_for_selector(".picked:has-text('talk video.mp4')")
    assert pg.is_visible("text=2:05")
    pg.click("button.choice[data-k=parts]")
    pg.fill("label:has-text('Length of each part') input", "60")
    pg.wait_for_selector("text=About 2 parts from 2:05")
    pg.check("label:has-text('Use only part') input")
    pg.fill("input[placeholder='0:00']", "0:05")
    pg.fill("input[placeholder='end of video']", "1:05")
    pg.wait_for_selector("text=About 1 part from 1:00")
    pg.click("button:has-text('Make clips')")
    pg.wait_for_url("**/#/job/7")
    b = sent["body"]
    assert b["file"] == "talk video.mp4" and "url" not in b and b["options"]["range_end"] == 65
    assert errs == []
    # the same file shows up under Settings > My videos and can be removed
    pg.goto(url + "#/settings/videos")
    pg.wait_for_selector("text=talk video.mp4")
    pg.once("dialog", lambda d: d.accept())
    pg.click("button:has-text('Remove')")
    pg.wait_for_selector("text=No videos yet")


def test_settings_hub_and_old_links_still_work(site):
    url, browser, vid = site
    pg = browser.new_page(viewport={"width": 390, "height": 800})
    pg.goto(url + "#/settings")
    pg.wait_for_selector(".hub")
    assert pg.locator(".hub a").count() == 4
    pg.goto(url + "#/style")                                           # old address
    pg.wait_for_selector("text=Look and defaults")
    assert pg.url.endswith("#/settings/look")
    pg.goto(url + "#/setup")
    pg.wait_for_selector("text=Health check")
    assert pg.locator("nav.tabs a").count() == 3
    assert pg.get_attribute("nav.tabs a[data-tab=settings]", "aria-current") == "page"


def test_no_horizontal_scroll_on_a_phone(site):
    url, browser, vid = site
    for route in ("#/create", "#/settings", "#/clips"):
        pg, _ = open_page(browser, url, route) if route == "#/create" else (browser.new_page(viewport={"width": 360, "height": 740}), None)
        if route != "#/create":
            pg.goto(url + route)
            pg.wait_for_timeout(600)
        assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), route
