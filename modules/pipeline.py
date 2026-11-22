"""The clipping pipeline, shared by the command line (agent.py) and the web app (server.py).

State lives in a JobState (JSON file for the CLI, SQLite for the web app):
  state.data["info"], ["plan"] (list of clips), ["clips"][clip id] -> status, file, captions, meta, ...
Clip statuses: planned, rendering, rendered, uploading, uploaded, render_failed, upload_failed, discarded.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from typing import Callable, Optional

from . import (caption_lang, captions, clip_selector, content_generator, templates, transcription, video_editor, video_scout,
               youtube_uploader)
from .ai_analyzer import make_client
from .scheduling import Scheduler, to_iso
from .utilities import (Cancelled, ClipperError, ConfigError, Config, JobState, file_ok, probe, redact,
                        safe_remove)

log = logging.getLogger("clipper")


@dataclass
class Ctx:
    url: str
    cfg: Config
    state: JobState
    tag: str = ""                                   # file-name prefix, e.g. "j12_" for web jobs
    progress: Callable = lambda stage, pct, msg="": None
    check_cancel: Callable = lambda: None
    scheduler: Optional[Scheduler] = None
    confirm: Optional[Callable] = None              # CLI review mode: confirm(clip, meta) -> bool
    on_uploaded: Optional[Callable] = None          # on_uploaded(clip_id, publish_at, youtube_id)
    on_info: Optional[Callable] = None
    interactive_auth: bool = True
    fresh: bool = False
    _client: object = field(default=None, repr=False)

    @property
    def client(self):
        if self._client is None:
            self._client = make_client(self.cfg) or False
        return self._client or None

    @property
    def ask_json(self):
        return self.client.generate_json if self.client else None


# ----------------------------------------------------------------------------- upload safety
def upload_and_cleanup(path: str, upload_fn: Callable[[], str], job: JobState, cid: str, cleanup=()) -> bool:
    """Upload, and delete the local file ONLY if YouTube confirmed with a video id."""
    try:
        vid = upload_fn()
    except Cancelled:
        raise
    except Exception as e:  # upload failure of any kind keeps the file
        job.update_clip(cid, status="upload_failed", error=redact(f"{type(e).__name__}: {e}")[:400])
        log.error("Upload failed for %s: %s", os.path.basename(path), redact(e))
        log.info("Keeping %s so you can retry.", path)
        return False
    if not vid:
        job.update_clip(cid, status="upload_failed", error="no video id returned")
        log.error("Upload for %s returned no video id; keeping the file.", path)
        return False
    job.update_clip(cid, status="uploaded", youtube_id=vid, error="")
    log.info("Uploaded: https://youtube.com/watch?v=%s", vid)
    safe_remove(path, *cleanup)
    log.info("Deleted local file %s", os.path.basename(path))
    return True


# ----------------------------------------------------------------------------- paths / helpers
def clip_paths(ctx: Ctx, info: dict, cid: str) -> tuple[str, str]:
    final = os.path.abspath(os.path.join(ctx.cfg.output_dir, "clips", f"{info['id']}_{ctx.tag}{cid}.mp4"))
    work = os.path.abspath(os.path.join(ctx.cfg.temp_dir, info["id"], f"{ctx.tag}{cid}"))
    return final, work


def clip_excerpt(transcript: dict, clip: dict) -> str:
    return " ".join(s["text"] for s in transcript["segments"] if s["end"] > clip["start"] and s["start"] < clip["end"])


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if not shutil.which(tool):
            raise ConfigError(f"'{tool}' is not installed or not on PATH")


def transcript_path(ctx: Ctx, info: dict) -> str:
    return os.path.join(ctx.cfg.temp_dir, info["id"], "transcript.json")


def load_transcript(ctx: Ctx, info: dict) -> Optional[dict]:
    p = transcript_path(ctx, info)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except ValueError:
            return None
    return None


def live_source(info: dict) -> Optional[str]:
    """Path of the saved recording when this job comes from a live stream, else None."""
    if not info.get("live_file"):
        return None
    if not file_ok(info["live_file"]):
        raise ClipperError("The saved recording of this live stream was deleted. Start a new job from the finished video.")
    return info["live_file"]


def get_info(ctx: Ctx) -> dict:
    info = ctx.state.data.get("info")
    if not info or ctx.fresh:
        info = video_editor.get_info(ctx.url)
        ctx.state.set(info=info)
    if info.get("is_live") and not info.get("live_file"):
        ctx.progress("info", 3, "Saving the live stream up to now")
        log.info("Live stream: saving the last %s of it...", f"{ctx.cfg.live_minutes} minutes" if ctx.cfg.live_minutes else "part available")
        dst = os.path.join(ctx.cfg.temp_dir, info["id"], "live_source.mp4")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        secs = video_editor.record_live(ctx.url, dst, ctx.cfg.live_minutes, ctx.cfg.max_height)
        info = {**info, "live_file": dst, "duration": secs}
        ctx.state.set(info=info)
        log.info("Saved %.0f minutes of the live stream", secs / 60)
    live_source(info)
    if ctx.on_info:
        ctx.on_info(info)
    return info


def get_transcript(ctx: Ctx, info: dict) -> dict:
    cache = transcript_path(ctx, info)
    if not ctx.fresh:
        tr = load_transcript(ctx, info)
        if tr and not tr.get("partial"):                   # a partial one only covers the chosen moments
            log.info("Using saved transcript")
            return tr
    jobdir = os.path.dirname(cache)
    os.makedirs(jobdir, exist_ok=True)
    ctx.progress("audio", 6, "Downloading audio")
    log.info("Downloading audio...")
    live = live_source(info)
    src = live or video_editor.download_audio(ctx.url, os.path.join(jobdir, "audio_src"))
    ctx.check_cancel()
    audio = transcription.extract_audio(src, os.path.join(jobdir, "audio.flac"))
    duration = info.get("duration") or probe(audio)["duration"]
    ctx.progress("transcribe", 14, "Transcribing speech")
    tr = transcription.transcribe(audio, ctx.cfg, ctx.client, duration, jobdir)
    tr["duration"] = duration or tr["duration"]
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(tr, f, ensure_ascii=False)
    safe_remove(audio) if live else safe_remove(src, audio)       # never delete the saved live recording
    log.info("Transcript: %s, language=%s, %d segments", tr["engine"], tr["language"], len(tr["segments"]))
    return tr


def find_first(ctx: Ctx, info: dict) -> tuple[dict, list[dict]]:
    """Gemini watches the video and picks the moments; only those parts are downloaded and transcribed."""
    cfg = ctx.cfg
    duration = float(info["duration"])
    ctx.progress("select", 6, "Gemini is watching the video")
    log.info("Gemini is watching the video to find the best moments...")
    raw = video_scout.scout(ctx.url, info, ctx.client.generate_json, cfg.clip_durations, cfg.clip_count, cfg.clip_focus,
                            progress=lambda i, n: (ctx.check_cancel(), ctx.progress("select", 6 + 14 * i / n, f"Gemini is watching the video ({i + 1}/{n})")))
    cands = video_scout.shortlist(raw, duration, cfg.clip_count, cfg.max_overlap)
    spans = video_scout.windows(cands, duration)
    total = sum(e - s for s, e in spans)
    log.info("Transcribing %d part(s), %.0f s of audio instead of the whole %.0f s", len(spans), total, duration)
    jobdir = os.path.dirname(transcript_path(ctx, info))
    os.makedirs(jobdir, exist_ok=True)
    pieces = []
    for i, (s, e) in enumerate(spans):
        ctx.check_cancel()
        ctx.progress("audio", 22 + 6 * i / len(spans), f"Downloading part {i + 1} of {len(spans)}")
        wd = os.path.join(jobdir, f"part{i}")
        os.makedirs(wd, exist_ok=True)
        src = video_editor.download_audio_section(ctx.url, s, e, os.path.join(wd, "audio_src"))
        audio = transcription.extract_audio(src, os.path.join(wd, "audio.flac"))
        ctx.progress("transcribe", 28 + 10 * i / len(spans), f"Transcribing part {i + 1} of {len(spans)}")
        tr = transcription.transcribe(audio, cfg, ctx.client, e - s, wd)
        pieces.append(video_scout.shift_transcript(tr, s))
        safe_remove(src, audio)
    transcript = video_scout.merge_transcripts(pieces, duration, spans)
    words = clip_selector.flatten_words(transcript)
    if not words:
        raise ClipperError("no speech was found in the chosen parts")
    ctx.progress("select", 40, "Checking the moments against the speech")
    plan = clip_selector.validate_candidates(cands, words, duration, cfg.clip_durations, cfg.max_overlap, tol=8.0)
    if cfg.min_score and plan:
        plan = [c for c in plan if c["score"] >= cfg.min_score] or plan[:1]
    plan = plan[:cfg.clip_count]
    if not plan:
        raise ClipperError("none of Gemini's moments lined up with real speech")
    for i, c in enumerate(plan, 1):
        c["id"] = f"clip{i}"
    with open(transcript_path(ctx, info), "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False)
    ctx.state.set(plan=plan, language=transcript["language"], transcript_engine=transcript["engine"])
    return transcript, plan


def plan_clips(ctx: Ctx, info: dict, transcript: dict) -> list[dict]:
    st = ctx.state
    if st.data.get("plan") and not ctx.fresh:
        return st.data["plan"]
    if ctx.client is None:
        raise ConfigError("GEMINI_API_KEY is required to choose clips")
    ctx.progress("select", 40, "Choosing the best moments")
    cfg = ctx.cfg
    plan = clip_selector.select_clips(transcript, info["duration"] or transcript["duration"], info,
                                      ctx.client.generate_json, cfg.clip_durations, cfg.clip_count, cfg.max_overlap,
                                      cfg.clip_focus, cfg.min_score)
    st.set(plan=plan)
    return plan


def ensure_captions(ctx: Ctx, clip: dict, transcript: dict, force: bool = False) -> list[dict]:
    """Caption phrases for a clip (language-converted), cached in the clip state so edits survive re-renders."""
    cs = ctx.state.clip(clip["id"])
    if cs.get("captions") and not force and cs.get("captions_lang") == ctx.cfg.caption_language:
        return cs["captions"]
    phrases, notes = caption_lang.make_plan(transcript, clip, ctx.cfg.caption_language, ctx.ask_json)
    for n in notes:
        log.warning("%s captions: %s", clip["id"], n)
    ctx.state.update_clip(clip["id"], captions=phrases, captions_lang=ctx.cfg.caption_language, caption_notes=notes)
    return phrases


def ensure_meta(ctx: Ctx, info: dict, clip: dict, transcript: dict, force: bool = False) -> dict:
    cs = ctx.state.clip(clip["id"])
    if cs.get("meta") and not force:
        return cs["meta"]
    cfg = ctx.cfg
    meta = content_generator.generate_metadata(clip, clip_excerpt(transcript, clip), info, ctx.ask_json,
                                               cfg.extra_tags, cfg.description_footer, cfg.credit_source,
                                               cfg.category_override)
    ctx.state.update_clip(clip["id"], meta=meta)
    meta_dir = os.path.join(cfg.output_dir, "metadata")
    os.makedirs(meta_dir, exist_ok=True)
    with open(os.path.join(meta_dir, f"{info['id']}_{ctx.tag}{clip['id']}.json"), "w", encoding="utf-8") as f:
        json.dump({"clip": clip, "metadata": meta}, f, ensure_ascii=False, indent=2)
    return meta


# ----------------------------------------------------------------------------- rendering
CAPTION_MODES = ("words", "single", "off")


def resolve_look(ctx: Ctx, info: dict, clip: dict, transcript: dict) -> dict:
    """What this clip looks like: template, caption type and the two texts. A clip's own choices (set on the review
    screen) win over the global settings; texts default to the clip title (tweet) and cover text (single caption)."""
    cfg = ctx.cfg
    look = ctx.state.clip(clip["id"]).get("look") or {}
    template = look.get("template") or cfg.template
    mode = look.get("caption_mode") or ("off" if not cfg.caption_enabled else cfg.caption_mode)
    meta = ctx.state.clip(clip["id"]).get("meta")
    if (template == "tweet" or mode == "single") and not meta:
        try:
            meta = ensure_meta(ctx, info, clip, transcript)
        except ClipperError as e:            # no Gemini right now: fall back to the plain clip title
            log.warning("Could not write the title and cover text yet (%s); using the clip title", redact(e))
    title = (meta or {}).get("title") or clip.get("title") or info.get("title", "")
    cover = (meta or {}).get("cover_text") or clip.get("hook") or title
    avatar = look.get("tweet_avatar") or cfg.tweet_avatar
    if avatar and not os.path.isfile(avatar):
        log.warning("The picture for the tweet card is gone (%s); using the default picture", os.path.basename(avatar))
        avatar = cfg.tweet_avatar if cfg.tweet_avatar != avatar and os.path.isfile(cfg.tweet_avatar or "") else ""
    return {"template": template, "caption_mode": mode, "tweet_text": look.get("tweet_text") or title,
            "caption_text": look.get("caption_text") or cover,
            "tweet_name": look.get("tweet_name") or cfg.tweet_name, "tweet_handle": look.get("tweet_handle") or cfg.tweet_handle,
            "tweet_avatar": avatar}


def render_clip(ctx: Ctx, info: dict, clip: dict, transcript: dict, final: str, work: str) -> dict:
    cfg = ctx.cfg
    os.makedirs(work, exist_ok=True)
    raw = os.path.join(work, f"raw_{cfg.max_height}.mp4")       # per quality, so changing it downloads again
    processed = os.path.join(work, "processed.mp4")
    outro = os.path.join(work, "outro.mp4")
    if file_ok(raw):
        log.info("Reusing downloaded clip for %s", clip["id"])
    else:
        log.info("Downloading %s (%.1fs-%.1fs)...", clip["id"], clip["start"], clip["end"])
        live = live_source(info)
        if live:
            video_editor.cut_local(live, clip["start"], clip["end"], raw)
        else:
            video_editor.download_segment(ctx.url, clip["start"], clip["end"], raw, cfg.max_height)
    ctx.check_cancel()
    vinfo = probe(raw)

    keep = None
    if cfg.remove_silence:
        sil = video_editor.detect_silences(raw, cfg.silence_db, cfg.silence_min_sec)
        keep = video_editor.keep_intervals(sil, vinfo["duration"], cfg.silence_keep_sec)
        log.info("silence removal: %s", f"{len(keep)} kept intervals" if keep else "nothing to cut")

    look = resolve_look(ctx, info, clip, transcript)
    template, cap_mode = look["template"], look["caption_mode"]
    fit = template in templates.FIT_TEMPLATES
    samples: list = []
    keys = None
    cw = int(vinfo["height"] * 9 / 16) // 2 * 2
    face_tpl = template == "face"            # always full screen: follow the face, or the centre if none is found
    if not fit and vinfo["width"] > cw and (face_tpl or (cfg.face_tracking and cfg.fit_mode != "blur")):
        samples = video_editor.detect_faces(raw)
        keys = video_editor.build_keyframes(samples, vinfo["duration"], vinfo["width"], cw)
        if face_tpl and keys is None:
            log.warning("No face found (or OpenCV missing: pkg install x11-repo opencv-python python-numpy); cropping the centre instead")
    mode = "fit" if fit else ("crop" if face_tpl else video_editor.mode_for(cfg, vinfo, keys is not None))
    if mode == "crop" and keys is not None:
        keys = video_editor.remap_keyframes(keys, keep)
    log.info("template: %s, framing: %s, captions: %s, quality: up to %dp", template, mode, cap_mode, cfg.max_height)

    layout = tweet_ass_path = None
    if fit:
        layout = templates.plan_layout(template, vinfo["width"], vinfo["height"], look["tweet_text"])
        if template == "tweet":
            if not look["tweet_name"]:
                log.warning("No name set for the tweet card; set it on the Create screen or on this clip.")
            tweet_ass_path = os.path.join(work, "tweet.ass")
            with open(tweet_ass_path, "w", encoding="utf-8") as f:
                f.write(templates.tweet_ass(layout, look["tweet_name"], look["tweet_handle"], font=cfg.caption_font,
                                            font_hindi=cfg.caption_font_hindi))

    over = captions.overrides_from_cfg(cfg)
    for k, v in templates.caption_defaults(template).items():
        if not over.get(k):
            over[k] = v
    pos = cfg.caption_position
    if pos == "auto":
        pos = "bottom" if fit else video_editor.choose_caption_position(samples, mode)
        if fit and layout.cap_top and not over.get("margin"):
            pos, over["margin"] = "top", layout.cap_top     # start just under the video and grow downward

    ass_path = None
    out_dur = sum(b - a for a, b in keep) if keep else vinfo["duration"]
    if cap_mode == "words":
        phrases = ensure_captions(ctx, clip, transcript)
        words = captions.remap_words(caption_lang.flatten(phrases), keep)
        if words:
            if not transcript.get("word_level"):
                log.warning("Transcript has estimated word timings; captions sync approximately.")
            ass = captions.generate_ass(
                words, cfg.caption_style, position=pos, font=cfg.caption_font, font_hindi=cfg.caption_font_hindi,
                hook_text=clip.get("hook", "") if cfg.intro_hook else "", hook_sec=cfg.intro_hook_sec, over=over)
            ass_path = os.path.join(work, "captions.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass)
            if cfg.export_srt:
                srt_dir = os.path.join(cfg.output_dir, "srt")
                os.makedirs(srt_dir, exist_ok=True)
                with open(os.path.join(srt_dir, os.path.basename(final).replace(".mp4", ".srt")), "w",
                          encoding="utf-8") as f:
                    f.write(captions.generate_srt(words))
        else:
            log.warning("No words found for %s; rendering without captions", clip["id"])
    elif cap_mode == "single":
        ass = captions.generate_single_ass(look["caption_text"], out_dur, cfg.caption_style, position=pos,
                                           font=cfg.caption_font, font_hindi=cfg.caption_font_hindi, over=over)
        ass_path = os.path.join(work, "captions.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass)

    log.info("Rendering vertical clip...")
    video_editor.render_main(raw, processed, cfg=dc_replace(cfg, tweet_avatar=look["tweet_avatar"] or ""), ass_path=ass_path, keep=keep, mode=mode, keys=keys,
                             workdir=work, template=template, layout=layout, tweet_ass_path=tweet_ass_path)
    ctx.check_cancel()
    os.makedirs(os.path.dirname(final), exist_ok=True)
    tmp_final = final + ".part.mp4"
    if cfg.outro_enabled:
        video_editor.make_outro(outro, cfg)
        video_editor.join_clips(processed, outro, tmp_final, cfg.transition_sec, work, cfg)
    else:
        shutil.copyfile(processed, tmp_final)
    if not file_ok(tmp_final):
        raise video_editor.EditError("final clip missing or empty")
    os.replace(tmp_final, final)                    # only a verified clip ever lands at the final path
    if cfg.keep_raw:
        safe_remove(processed, outro, ass_path or "", tweet_ass_path or "")
    else:
        safe_remove(work)
    return {"mode": mode, "silence_cuts": bool(keep), "caption_style": cfg.caption_style if ass_path else None,
            "template": template, "caption_mode": cap_mode}


# ----------------------------------------------------------------------------- upload
def get_service(ctx: Ctx):
    return youtube_uploader.build_service(youtube_uploader.load_credentials(ctx.cfg, interactive=ctx.interactive_auth))


def upload_clip(ctx: Ctx, info: dict, clip_id: str, service, publish_at: Optional[str] = None) -> bool:
    cfg = ctx.cfg
    cs = ctx.state.clip(clip_id)
    final, work = clip_paths(ctx, info, clip_id)
    if not file_ok(final):
        ctx.state.update_clip(clip_id, status="upload_failed", error="clip file is missing; render it again")
        return False
    meta = cs["meta"]
    privacy = "public" if cfg.publish_mode == "public" else "private"
    when, explicit = None, False
    if cfg.publish_mode == "scheduled":
        when = publish_at or cs.get("publish_at")
        explicit = bool(when)
        if not when:
            if ctx.scheduler is None:
                ctx.scheduler = Scheduler(cfg)
            when = ctx.scheduler.peek()
    ctx.state.update_clip(clip_id, status="uploading", error="")
    ok = upload_and_cleanup(
        final, lambda: youtube_uploader.upload_video(service, final, meta, when, retries=cfg.upload_retries,
                                                     privacy=privacy, title_suffix=cfg.title_suffix),
        ctx.state, clip_id, cleanup=[work])
    if ok:
        ctx.state.update_clip(clip_id, publish_at=when, privacy=privacy)
        if when and ctx.scheduler is not None:
            ctx.scheduler.commit(when)
        if ctx.on_uploaded:
            ctx.on_uploaded(clip_id, when, ctx.state.clip(clip_id).get("youtube_id", ""))
    return ok


# ----------------------------------------------------------------------------- whole job
def run_job(ctx: Ctx) -> dict:
    cfg, st = ctx.cfg, ctx.state
    require_tools()
    if ctx.client is None:
        log.warning("GEMINI_API_KEY is not set: clip selection and metadata need it.")
    ctx.progress("info", 2, "Reading video details")
    info = get_info(ctx)
    log.info("Video: %s (%.0fs)", info["title"], info["duration"])
    st.set(url=ctx.url, title=info["title"], duration=info["duration"], status="running")
    ctx.check_cancel()

    transcript = None if ctx.fresh else load_transcript(ctx, info)
    plan_saved = bool(st.data.get("plan")) and not ctx.fresh
    if transcript and (plan_saved or not transcript.get("partial")):
        log.info("Using saved transcript")
    else:
        transcript = None
        if cfg.clip_finder == "video" and ctx.client is not None and not info.get("live_file") and info.get("duration"):
            try:
                transcript, _ = find_first(ctx, info)
            except Cancelled:
                raise
            except ClipperError as e:
                log.warning("Finding clips by watching the video did not work (%s). Transcribing the whole video instead.", redact(e))
                transcript = None
    if transcript is None:
        transcript = get_transcript(ctx, info)
    st.set(language=transcript["language"], transcript_engine=transcript["engine"])
    ctx.check_cancel()
    plan = plan_clips(ctx, info, transcript)
    for c in plan:
        log.info("%s  %.1f-%.1fs  score=%d  %s", c["id"], c["start"], c["end"], c["score"], c["title"])

    service = None
    if cfg.auto_upload and ctx.confirm is None:       # fail early, before spending render time
        ctx.progress("auth", 48, "Checking YouTube sign-in")
        service = get_service(ctx)
    uploaded = rendered = 0
    n = max(1, len(plan))
    for i, clip in enumerate(plan):
        cid = clip["id"]
        ctx.check_cancel()
        state = st.clip(cid)
        if state.get("status") in ("uploaded", "discarded"):
            uploaded += state.get("status") == "uploaded"
            continue
        final, work = clip_paths(ctx, info, cid)
        ctx.progress("render", 50 + 45 * i / n, f"Rendering clip {i + 1} of {len(plan)}")
        try:
            if not (state.get("status") in ("rendered", "upload_failed") and file_ok(final)):
                st.update_clip(cid, status="rendering", error="", file=final)
                details = render_clip(ctx, info, clip, transcript, final, work)
                st.update_clip(cid, status="rendered", file=final, **details)
            rendered += 1
            meta = ensure_meta(ctx, info, clip, transcript)
        except Cancelled:
            st.update_clip(cid, status="planned")
            raise
        except ClipperError as e:
            st.update_clip(cid, status="render_failed", error=redact(e)[:400])
            log.error("%s failed: %s (temporary files kept for debugging)", cid, redact(e))
            continue

        if not cfg.auto_upload:
            log.info("Rendered %s (waiting for your review)", final)
            continue
        if ctx.confirm is not None:
            if not ctx.confirm(clip, meta):
                log.info("Skipped upload for %s (review mode); file kept.", cid)
                continue
            if service is None:
                service = get_service(ctx)
        ctx.progress("upload", 50 + 45 * (i + 0.6) / n, f"Uploading clip {i + 1} of {len(plan)}")
        if service is not None and upload_clip(ctx, info, cid, service):
            uploaded += 1
    ctx.progress("done", 100, "Finished")
    return {"rendered": rendered, "uploaded": uploaded, "total": len(plan)}


# ----------------------------------------------------------------------------- single-clip actions (web app)
def load_context(ctx: Ctx):
    info = ctx.state.data.get("info")
    transcript = load_transcript(ctx, info) if info else None
    if not info or not transcript:
        raise ClipperError("Saved transcript not found. Run the job again.")
    return info, transcript


def find_clip(ctx: Ctx, clip_id: str) -> dict:
    for c in ctx.state.data.get("plan", []):
        if c["id"] == clip_id:
            return c
    raise ClipperError(f"unknown clip {clip_id}")


def rerender(ctx: Ctx, clip_id: str, regenerate_captions: bool = False) -> None:
    """Render one clip again with the current settings and its (possibly edited) caption phrases."""
    info, transcript = load_context(ctx)
    clip = find_clip(ctx, clip_id)
    final, work = clip_paths(ctx, info, clip_id)
    st = ctx.state
    if regenerate_captions:
        ensure_captions(ctx, clip, transcript, force=True)
    elif st.clip(clip_id).get("captions_lang") != ctx.cfg.caption_language:
        old = st.clip(clip_id).get("captions")
        if not any(p.get("edited") for p in (old or [])):      # never overwrite the user's edits silently
            ensure_captions(ctx, clip, transcript, force=True)
    st.update_clip(clip_id, status="rendering", error="")
    try:
        details = render_clip(ctx, info, clip, transcript, final, work)
    except Cancelled:
        st.update_clip(clip_id, status="rendered" if file_ok(final) else "planned")
        raise
    except ClipperError as e:
        st.update_clip(clip_id, status="rendered" if file_ok(final) else "render_failed", error=redact(e)[:400])
        raise
    st.update_clip(clip_id, status="rendered", file=final, **details)
    ensure_meta(ctx, info, clip, transcript)


def upload_existing(ctx: Ctx, clip_id: str, publish_at: Optional[str] = None) -> bool:
    info, _ = load_context(ctx)
    service = get_service(ctx)
    return upload_clip(ctx, info, clip_id, service, publish_at)


def discard_clip(ctx: Ctx, clip_id: str) -> None:
    info = ctx.state.data.get("info")
    if info:
        final, work = clip_paths(ctx, info, clip_id)
        safe_remove(final, work)
    ctx.state.update_clip(clip_id, status="discarded", file="")
