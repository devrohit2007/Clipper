#!/usr/bin/env python3
"""AI YouTube clipping agent.

URL -> transcript (faster-whisper, Gemini fallback) -> Gemini picks clips -> yt-dlp downloads them ->
FFmpeg renders 9:16 with karaoke captions, logo, music, outro -> metadata -> private scheduled YouTube upload.

    python agent.py                 # prompts for a URL (like the original script)
    python agent.py URL [--style neon] [--clips 3] [--review] [--no-upload] [--srt]
    python agent.py --check         # dependency report
    python agent.py --auth          # YouTube sign-in only
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules import captions, video_editor, youtube_uploader  # noqa: E402
from modules.pipeline import Ctx, require_tools, run_job, upload_and_cleanup  # noqa: E402,F401
from modules.scheduling import Scheduler  # noqa: E402
from modules.utilities import (ClipperError, ConfigError, Config, JobState, check_dependencies,  # noqa: E402
                               format_dependency_report, load_env_file, log, redact, setup_logging)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="AI YouTube clipping agent")
    p.add_argument("url", nargs="?", help="YouTube video URL")
    p.add_argument("--style", choices=captions.STYLE_NAMES, help="caption style (default: karaoke)")
    p.add_argument("--clips", type=int, help="number of clips (default 3)")
    p.add_argument("--durations", help="allowed clip lengths, e.g. 30,45,60")
    p.add_argument("--review", action="store_true", help="ask before uploading each clip")
    p.add_argument("--no-upload", action="store_true", help="render only, never upload")
    p.add_argument("--srt", action="store_true", help="also export .srt subtitle files")
    p.add_argument("--fresh", action="store_true", help="ignore saved transcript/clip plan for this video")
    p.add_argument("--check", action="store_true", help="print dependency report and exit")
    p.add_argument("--auth", action="store_true", help="authorise YouTube and exit")
    p.add_argument("--env-file", default=".env")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def apply_args(cfg: Config, a) -> Config:
    if a.style:
        cfg.caption_style = a.style
    if a.clips:
        cfg.clip_count = a.clips
    if a.durations:
        cfg.clip_durations = tuple(int(x) for x in a.durations.split(",") if x.strip())
    if a.review:
        cfg.review_mode = True
    if a.no_upload:
        cfg.auto_upload = False
    if a.srt:
        cfg.export_srt = True
    cfg.validate()
    return cfg


def run_pipeline(url: str, cfg: Config, a) -> int:
    require_tools()
    info = video_editor.get_info(url)
    state = JobState(os.path.join(cfg.output_dir, "jobs", f"{info['id']}.json"))   # one JSON file per video
    if a.fresh:
        state.data = {"clips": {}}
    state.set(info=info)

    def confirm(clip, meta):
        print(f"\n--- {clip['id']}: {meta['title']}\n{meta['description']}\n{' '.join(meta['hashtags'])}")
        return sys.stdin.isatty() and input("Upload and schedule this clip? [y/N] ").strip().lower() == "y"

    ctx = Ctx(url=url, cfg=cfg, state=state, scheduler=Scheduler(cfg), fresh=a.fresh,
              confirm=confirm if cfg.review_mode else None)
    result = run_job(ctx)
    print(f"\nFinished. Rendered {result['rendered']}, uploaded and scheduled "
          f"{result['uploaded']}/{result['total']}.")
    return 0 if (result["uploaded"] or (result["rendered"] and not cfg.auto_upload) or cfg.review_mode) else 1


def main(argv=None) -> int:
    a = parse_args(argv)
    load_env_file(a.env_file)
    try:
        cfg = apply_args(Config.from_env(), a)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    setup_logging(os.path.join(cfg.output_dir, "agent.log"), a.verbose)
    try:
        if a.check:
            print(format_dependency_report(check_dependencies(cfg)))
            return 0
        if a.auth:
            youtube_uploader.load_credentials(cfg)
            print("YouTube authorisation OK.")
            return 0
        url = a.url or input("\nPaste YouTube URL: ").strip()
        if not url:
            print("No URL given.", file=sys.stderr)
            return 2
        return run_pipeline(url, cfg, a)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; re-run the same URL to resume.")
        return 130
    except ClipperError as e:
        log.error("%s", redact(e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
