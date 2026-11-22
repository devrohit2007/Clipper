"""Downloading (yt-dlp) and local FFmpeg rendering: 9:16 conversion with face-follow crop or
blurred background, zoom, silence removal, captions (ASS), logo, music ducking, loudness
normalisation, outro and crossfade."""
from __future__ import annotations

import logging
import os
import re
import statistics
import time
from typing import Optional

from . import templates
from .utilities import ClipperError, ToolError, file_ok, parse_ts, probe, run, safe_remove

log = logging.getLogger("clipper")
W, H = 1080, 1920


class EditError(ClipperError):
    pass


# ------------------------------------------------------------------ yt-dlp
def get_info(url: str) -> dict:
    import json
    p = run(["yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download", url], timeout=180)
    d = json.loads(p.stdout)
    status = d.get("live_status")
    if status == "is_upcoming":
        raise EditError("This live stream has not started yet. Try again once it is live or finished.")
    if status == "post_live":
        raise EditError("This live stream just ended and YouTube is still processing it. Try again in a little while.")
    info = {"id": d.get("id") or "video", "title": d.get("title") or "", "description": d.get("description") or "",
            "duration": float(d.get("duration") or 0), "uploader": d.get("uploader") or "",
            "url": d.get("webpage_url") or url}
    if d.get("is_live") or status == "is_live":
        # A running stream is cut off at this moment and treated as its own video, so its files never mix with
        # the finished recording YouTube publishes later.
        info.update(is_live=True, duration=0.0, live_of=info["id"], id=f"{info['id']}_live{time.strftime('%Y%m%d%H%M')}")
    return info


def _fetch_text(src: str) -> str:
    if os.path.isfile(src):
        return open(src, encoding="utf-8", errors="replace").read()
    import urllib.request
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def _join(base: str, ref: str) -> str:
    from urllib.parse import urljoin
    return os.path.join(os.path.dirname(base), ref) if os.path.isfile(base) else urljoin(base, ref)


def hls_durations(text: str) -> list[float]:
    out = []
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                out.append(float(line[8:].split(",")[0]))
            except ValueError:
                pass
    return out


def record_live(url: str, dst: str, minutes: int = 60, max_height: int = 1080) -> float:
    """Save what a live stream has already shown, up to now, as a local file; returns its length in seconds.
    minutes=0 means everything the stream still offers (at most 4 hours). It reads the stream's HLS playlist
    (the part YouTube keeps for rewinding), takes the newest `minutes` of it and stops there."""
    h = int(max_height)
    p = run(["yt-dlp", "--no-playlist", "-g", "-f", f"b[height<={h}]/b", url], timeout=180)
    urls = [u for u in p.stdout.split() if u.startswith(("http", "/"))]
    if len(urls) != 1:
        raise EditError("This live stream type is not supported (it needs one combined video+audio stream).")
    manifest = urls[0]
    text = _fetch_text(manifest)
    if "#EXT-X-STREAM-INF" in text:                           # master playlist: pick the best size that fits
        best, best_h = None, -1
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines):
                m = re.search(r"RESOLUTION=\d+x(\d+)", line)
                vh = int(m.group(1)) if m else 0
                if (vh <= h and vh > best_h) or best is None:
                    best, best_h = lines[i + 1].strip(), vh
        manifest = _join(manifest, best)
        text = _fetch_text(manifest)
    durs = hls_durations(text)
    if not durs:
        raise EditError("Could not read the live stream playlist.")
    want = (minutes * 60.0) if minutes > 0 else 4 * 3600.0
    got, k = 0.0, 0
    for d in reversed(durs):
        got += d
        k += 1
        if got >= want:
            break
    take = min(want, got)
    safe_remove(dst)
    if "#EXT-X-ENDLIST" in text:                             # stream already over: plain seek from the start
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, sum(durs) - take):.2f}", "-i", manifest]
    else:
        cmd = ["ffmpeg", "-y", "-live_start_index", str(-k), "-i", manifest]
    run([*cmd, "-t", f"{take:.2f}", "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dst],
        timeout=take * 3 + 900)
    if not file_ok(dst):
        raise EditError("Recording the live stream failed.")
    return float(probe(dst)["duration"])


def cut_local(src: str, start: float, end: float, raw: str) -> str:
    """Cut one section out of the saved live recording (frame accurate, re-encoded)."""
    safe_remove(raw)
    run(["ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", raw], timeout=1800)
    if not file_ok(raw):
        raise EditError("Cutting the clip from the live recording failed.")
    return raw


def download_audio(url: str, dst_noext: str) -> str:
    run(["yt-dlp", "--no-playlist", "-f", "bestaudio/best", "--force-overwrites", "-o", dst_noext + ".%(ext)s", url],
        timeout=3600)
    for ext in ("m4a", "webm", "opus", "mp3", "mp4", "ogg", "wav"):
        if file_ok(f"{dst_noext}.{ext}"):
            return f"{dst_noext}.{ext}"
    raise EditError("yt-dlp did not produce an audio file")


def download_audio_section(url: str, start: float, end: float, dst_noext: str) -> str:
    """Download only the audio between start and end (seconds) of a video."""
    run(["yt-dlp", "--no-playlist", "--force-overwrites", "--force-keyframes-at-cuts",
         "--download-sections", f"*{start:.2f}-{end:.2f}", "-f", "bestaudio/best", "-o", dst_noext + ".%(ext)s", url],
        timeout=1800)
    for ext in ("m4a", "webm", "opus", "mp3", "mp4", "ogg", "wav"):
        if file_ok(f"{dst_noext}.{ext}"):
            return f"{dst_noext}.{ext}"
    raise EditError("yt-dlp did not produce an audio file for that part")


def format_for(max_height: int) -> str:
    """Best video up to max_height plus best audio; falls back so a video without that size still downloads."""
    h = int(max_height)
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"


def download_segment(url: str, start: float, end: float, raw: str, max_height: int = 1080) -> str:
    """Download one section to a local file (same approach as the original: never let FFmpeg read an
    expiring googlevideo URL). --force-keyframes-at-cuts keeps the cut frame-accurate so captions
    stay in sync with the transcript."""
    safe_remove(raw)
    run(["yt-dlp", "--no-playlist", "--no-part", "--force-overwrites", "--force-keyframes-at-cuts",
         "--download-sections", f"*{start:.2f}-{end:.2f}", "-f", format_for(max_height),
         "--merge-output-format", "mp4", "--recode-video", "mp4", "-o", raw, url], timeout=1800)
    if not file_ok(raw):
        raise EditError("yt-dlp failed to create the local clip. Try updating yt-dlp.")
    return raw


# ------------------------------------------------------------------ face tracking (optional)
CASCADE = "haarcascade_frontalface_default.xml"


def find_cascade(cv2=None) -> Optional[str]:
    """Locate OpenCV's frontal-face detector file. Some builds (Termux) have no cv2.data, so look in the usual places."""
    dirs = [os.environ.get("CLIPPER_CASCADE_DIR", ""), os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")]
    data = getattr(cv2, "data", None)
    if data is not None:
        dirs.append(data.haarcascades)
    prefix = os.environ.get("PREFIX", "/usr")
    for root in (prefix, "/usr", "/usr/local"):
        dirs += [os.path.join(root, "share", d, "haarcascades") for d in ("opencv5", "opencv4", "opencv", "OpenCV")]
        dirs.append(os.path.join(root, "share", "opencv4"))
    for d in dirs:
        if d and os.path.isfile(os.path.join(d, CASCADE)):
            return os.path.join(d, CASCADE)
    return None


def detect_faces(video: str, step: float = 1.0) -> list[dict]:
    """OpenCV Haar frontal-face samples every `step` s -> [{t, cx, cy, w, h}] (normalised).
    Returns [] if OpenCV is unavailable or nothing is found."""
    try:
        import cv2
    except Exception:
        log.info("OpenCV not installed; skipping face tracking")
        return []
    path = find_cascade(cv2)
    if not path:
        log.warning("OpenCV is installed but its face detector file (%s) was not found; see README, Face follow", CASCADE)
        return []
    cascade = cv2.CascadeClassifier(path)
    if cascade.empty():
        return []
    cap = cv2.VideoCapture(video)
    out, dur = [], 0.0
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        dur = (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / fps
        t = 0.0
        while t < dur:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            h0, w0 = frame.shape[:2]
            scale = 480.0 / w0
            small = cv2.resize(frame, (480, int(h0 * scale)))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(24, 24))
            if len(faces):
                x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
                sh, sw = small.shape[:2]
                out.append({"t": t, "cx": (x + fw / 2) / sw, "cy": (y + fh / 2) / sh, "w": fw / sw, "h": fh / sh})
            t += step
    finally:
        cap.release()
    out_len = max(1, int(dur / step))
    log.info("face samples: %d/%d", len(out), out_len)
    for s in out:
        s["coverage"] = len(out) / out_len
    return out


def build_keyframes(samples: list[dict], duration: float, src_w: int, crop_w: int,
                    min_coverage: float = 0.3, alpha: float = 0.45, step: float = 1.0) -> Optional[list[tuple]]:
    """Smoothed [(t, crop_x_px)] or None if faces were too rare to trust."""
    if not samples or samples[0].get("coverage", 1.0) < min_coverage:
        return None
    max_x = max(0, src_w - crop_w)
    by_t = {round(s["t"] / step): s for s in samples}
    xs, last = [], None
    n = max(1, int(duration / step) + 1)
    for i in range(n):
        s = by_t.get(i)
        if s is not None:
            last = min(max(s["cx"] * src_w - crop_w / 2, 0), max_x)
        xs.append(last)
    first = next((x for x in xs if x is not None), None)
    if first is None:
        return None
    xs = [first if x is None else x for x in xs]
    ema, sm = xs[0], []
    for x in xs:
        ema = alpha * x + (1 - alpha) * ema
        sm.append(ema)
    if max(sm) - min(sm) < 0.04 * src_w:       # barely moves: one fixed crop is steadier
        return [(0.0, statistics.median(sm))]
    return [(i * step, x) for i, x in enumerate(sm)]


def remap_keyframes(keys: list[tuple], keep: Optional[list[tuple]]) -> list[tuple]:
    from .captions import remap_time
    if not keep:
        return keys
    out: list[tuple] = []
    for t, x in keys:
        nt = remap_time(t, keep)
        if nt is None:
            continue
        if out and abs(out[-1][0] - nt) < 1e-3:
            out[-1] = (nt, x)
        else:
            out.append((nt, x))
    if out and out[0][0] > 0:
        out.insert(0, (0.0, out[0][1]))
    return out or [(0.0, keys[0][1])]


def lerp_expr(keys: list[tuple], var: str = "t", max_keys: int = 24) -> str:
    """Piecewise-linear FFmpeg expression through (t, value) keys."""
    if len(keys) > max_keys:
        step = len(keys) / max_keys
        keys = [keys[int(i * step)] for i in range(max_keys)] + [keys[-1]]
    if len(keys) == 1:
        return f"{keys[0][1]:.1f}"
    expr = f"{keys[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(keys, keys[1:]))):
        if t1 - t0 < 1e-3:
            continue
        expr = f"if(lt({var},{t1:.3f}),{x0:.2f}+({x1 - x0:.3f})*({var}-{t0:.3f})/{t1 - t0:.3f},{expr})"
    return expr


def choose_caption_position(samples: list[dict], mode: str) -> str:
    """Captions go to the bottom unless faces sit in the lower caption zone (crop mode only)."""
    if mode != "crop" or not samples:
        return "bottom"
    bottoms = [s["cy"] + s["h"] / 2 for s in samples]
    return "top" if statistics.median(bottoms) > 0.66 else "bottom"


# ------------------------------------------------------------------ silence
def detect_silences(path: str, noise_db: float, min_sec: float) -> list[tuple]:
    p = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af",
             f"silencedetect=noise={noise_db}dB:d={min_sec}", "-f", "null", "-"], check=False)
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", p.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        out.append((max(0.0, s), e))
    return out


def keep_intervals(silences: list[tuple], duration: float, keep_pad: float, min_gain: float = 0.3) -> Optional[list[tuple]]:
    """Complement of the silences, leaving `keep_pad` seconds of room each side. None = nothing to cut."""
    cuts = []
    for s, e in silences:
        e = duration if e is None else min(e, duration)
        cs, ce = s + keep_pad, e - keep_pad
        if ce - cs >= min_gain:
            cuts.append((cs, ce))
    if not cuts:
        return None
    keep, pos = [], 0.0
    for cs, ce in sorted(cuts):
        if cs > pos:
            keep.append((round(pos, 3), round(cs, 3)))
        pos = ce
    if pos < duration:
        keep.append((round(pos, 3), round(duration, 3)))
    return [(a, b) for a, b in keep if b - a > 0.05] or None


def select_expr(keep: list[tuple]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in keep)


# ------------------------------------------------------------------ filter graph
def ff_path(p: str) -> str:
    """Escape a path for use inside a filter option (inside single quotes)."""
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def drawtext_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
             .replace("%", "\\%").replace(",", "\\,"))


LOGO_XY = {"top-right": "W-w-20:120", "top-left": "20:120", "bottom-right": "W-w-20:H-h-120",
           "bottom-left": "20:H-h-120"}


def build_video_graph(*, mode: str, src_w: int, src_h: int, out_dur: float, keys: Optional[list],
                      keep: Optional[list], zoom: float, ass_name: Optional[str], fonts_dir: str,
                      logo_idx: Optional[int], logo_size: int, logo_pos: str = "top-right",
                      logo_opacity: float = 1.0, template: str = "blur", layout=None,
                      tweet_ass_name: Optional[str] = None, avatar_idx: Optional[int] = None,
                      avatar_default: bool = False) -> list[str]:
    s: list[str] = []
    src = "[0:v]"
    if keep:
        s.append(f"[0:v]select='{select_expr(keep)}',setpts=N/FRAME_RATE/TB[sel]")
        src = "[sel]"
    fit = template in templates.FIT_TEMPLATES and layout is not None
    if fit:                                     # white / black template: whole picture fitted, nothing cropped
        zoom = 0.0
        bgc = "white" if template == "tweet" else "black"
        s.append(f"{src}scale={layout.vw}:{layout.vh}:flags=lanczos,setsar=1[fg]")
        s.append(f"color=c={bgc}:s={W}x{H}:r=30:d={out_dur:.3f}[bg]")
        s.append(f"[bg][fg]overlay={layout.vx}:{layout.vy}[base]")
    elif mode == "crop":
        cw = int(src_h * 9 / 16) // 2 * 2
        x = lerp_expr(keys or [(0.0, (src_w - cw) / 2)])
        s.append(f"{src}crop={cw}:{src_h}:x='{x}':y=0,scale={W}:{H}:flags=lanczos,setsar=1[base]")
    else:
        s.append(f"{src}split=2[s1][s2]")
        s.append(f"[s1]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:2,"
                 f"scale={W}:{H},setsar=1[bg]")
        s.append(f"[s2]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fg]")
        s.append("[bg][fg]overlay=(W-w)/2:(H-h)/2[base]")
    cur = "base"
    if zoom > 0 and out_dur > 0:
        z = f"(1+{zoom}*min(t/{out_dur:.3f},1))"
        s.append(f"[{cur}]scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame,crop={W}:{H}[zoomed]")
        cur = "zoomed"
    s.append(f"[{cur}]fps=30[v30]")
    cur = "v30"
    if fit and tweet_ass_name:
        opt = f"filename='{ff_path(tweet_ass_name)}'"
        if fonts_dir:
            opt += f":fontsdir='{ff_path(fonts_dir)}'"
        s.append(f"[{cur}]ass={opt}[tw]")
        s += templates.avatar_filters(avatar_idx if not avatar_default else None, layout.card_x, layout.card_y, "tw", "twa")
        cur = "twa"
    if ass_name:
        opt = f"filename='{ff_path(ass_name)}'"
        if fonts_dir:
            opt += f":fontsdir='{ff_path(fonts_dir)}'"
        s.append(f"[{cur}]ass={opt}[capt]")
        cur = "capt"
    if logo_idx is not None:
        opacity = f",format=rgba,colorchannelmixer=aa={logo_opacity:.2f}" if logo_opacity < 1 else ""
        s.append(f"[{logo_idx}:v]scale={logo_size}:{logo_size}:force_original_aspect_ratio=decrease{opacity}[logo]")
        xy = LOGO_XY.get(logo_pos, LOGO_XY["top-right"])
        s.append(f"[{cur}][logo]overlay={xy}:format=auto,format=yuv420p[vout]")
    else:
        s.append(f"[{cur}]format=yuv420p[vout]")
    return s


def build_audio_graph(*, keep: Optional[list], music_idx: Optional[int], silent_idx: Optional[int],
                      music_volume: float, duck: bool, normalize: bool, duck_strength: str = "normal") -> list[str]:
    s: list[str] = []
    fmt = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    chain = []
    if silent_idx is None:
        if keep:
            chain += [f"aselect='{select_expr(keep)}'", "asetpts=N/SR/TB"]
        src = "0:a"
    else:
        src = f"{silent_idx}:a"
    if normalize:
        chain.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    chain.append(fmt)
    s.append(f"[{src}]{','.join(chain)}[sp]")
    if music_idx is None:
        s.append("[sp]anull[aout]")
        return s
    s.append(f"[{music_idx}:a]{fmt},volume={music_volume}[m]")
    if duck:
        s.append("[sp]asplit=2[sp1][sc]")
        thr, ratio = {"light": (0.06, 4), "strong": (0.015, 20)}.get(duck_strength, (0.03, 10))
        s.append(f"[m][sc]sidechaincompress=threshold={thr}:ratio={ratio}:attack=15:release=350[duck]")
        s.append("[sp1][duck]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]")
    else:
        s.append("[sp][m]amix=inputs=2:duration=first:normalize=0:dropout_transition=0[aout]")
    return s


def encode_args(cfg=None) -> list:
    preset = getattr(cfg, "x264_preset", "veryfast")
    crf = getattr(cfg, "crf", 21)
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart"]


def render_main(raw: str, out: str, *, cfg, ass_path: Optional[str], keep: Optional[list],
                mode: str, keys: Optional[list], workdir: str, template: str = "blur", layout=None,
                tweet_ass_path: Optional[str] = None) -> str:
    raw, out = os.path.abspath(raw), os.path.abspath(out)
    info = probe(raw)
    if not info["has_video"]:
        raise EditError("downloaded clip has no video stream")
    out_dur = sum(b - a for a, b in keep) if keep else info["duration"]
    inputs: list = ["-i", raw]
    idx = 1
    music_idx = logo_idx = silent_idx = None
    if os.path.isfile(cfg.music_file):
        inputs += ["-stream_loop", "-1", "-i", os.path.abspath(cfg.music_file)]
        music_idx, idx = idx, idx + 1
    else:
        log.warning("Music file not found (%s); rendering without background music", cfg.music_file)
    avatar_idx, avatar_default = None, False
    tweet = template == "tweet" and layout is not None
    if tweet:                                   # the logo becomes the round picture on the card, so no corner watermark
        pic = next((p for p in (cfg.tweet_avatar, cfg.logo_file) if p and os.path.isfile(p)), "")
        if pic:
            inputs += ["-i", os.path.abspath(pic)]
            avatar_idx, idx = idx, idx + 1
        else:
            avatar_default = True
            log.warning("No logo or tweet picture found; the tweet card shows a plain blue circle")
    elif os.path.isfile(cfg.logo_file):
        inputs += ["-i", os.path.abspath(cfg.logo_file)]
        logo_idx, idx = idx, idx + 1
    else:
        log.warning("Logo file not found (%s); rendering without logo", cfg.logo_file)
    if not info["has_audio"]:
        inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        silent_idx, idx = idx, idx + 1
    vg = build_video_graph(mode=mode, src_w=info["width"], src_h=info["height"], out_dur=out_dur, keys=keys,
                           keep=keep, zoom=cfg.zoom, ass_name=os.path.basename(ass_path) if ass_path else None,
                           fonts_dir=cfg.caption_fonts_dir, logo_idx=logo_idx, logo_size=cfg.logo_size,
                           logo_pos=cfg.logo_position, logo_opacity=cfg.logo_opacity, template=template, layout=layout,
                           tweet_ass_name=os.path.basename(tweet_ass_path) if tweet_ass_path else None,
                           avatar_idx=avatar_idx, avatar_default=avatar_default)
    ag = build_audio_graph(keep=keep, music_idx=music_idx, silent_idx=silent_idx, music_volume=cfg.music_volume,
                           duck=cfg.duck_music, normalize=cfg.normalize_audio, duck_strength=cfg.duck_strength)
    graph = ";".join(vg + ag)
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
           *encode_args(cfg), "-t", f"{out_dur:.3f}", "-shortest", out]
    run(cmd, cwd=workdir, timeout=3600)
    if not file_ok(out):
        raise EditError("FFmpeg produced an empty clip")
    return out


def make_outro(out: str, cfg) -> str:
    lines = [l for l in cfg.outro_lines.split("|")] + ["", "", ""]
    font = f":fontfile='{ff_path(cfg.outro_font)}'" if cfg.outro_font else ""
    spec = [(lines[0], 60, "white", 700), (lines[1], 45, "white", 820), (lines[2], 45, "yellow", 900)]
    dts = [f"drawtext=text='{drawtext_escape(t)}':fontcolor={c}:fontsize={sz}:x=(w-text_w)/2:y={y}{font}"
           for t, sz, c, y in spec if t.strip()]
    vf = ",".join(dts) if dts else "null"
    sec = float(getattr(cfg, "outro_sec", 2.0))
    bg = (getattr(cfg, "outro_bg", "") or "#000000").replace("#", "0x")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:r=30:d={sec}",
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100", "-vf", vf,
         "-t", str(sec), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", out])
    if not file_ok(out):
        raise EditError("outro creation failed")
    return out


def join_clips(main: str, outro: str, final: str, transition: float, workdir: str, cfg=None) -> str:
    main_dur = probe(main)["duration"]
    transition = min(transition, max(0.0, probe(outro)["duration"] - 0.3))
    if transition <= 0 or main_dur <= transition + 0.5:
        lst = os.path.join(workdir, "concat.txt")
        with open(lst, "w") as f:
            f.write(f"file '{os.path.abspath(main)}'\nfile '{os.path.abspath(outro)}'\n")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final])
    else:
        g = (f"[0:v]settb=AVTB,fps=30,format=yuv420p[v0];[1:v]settb=AVTB,fps=30,format=yuv420p[v1];"
             f"[v0][v1]xfade=transition=fade:duration={transition}:offset={main_dur - transition:.3f}[v];"
             f"[0:a][1:a]acrossfade=d={transition}[a]")
        run(["ffmpeg", "-y", "-i", main, "-i", outro, "-filter_complex", g, "-map", "[v]", "-map", "[a]",
             *encode_args(cfg), final], timeout=1800)
    if not file_ok(final):
        raise EditError("final clip creation failed")
    return final


def mode_for(cfg, info: dict, keys_available: bool) -> str:
    if cfg.fit_mode in ("crop", "blur"):
        return cfg.fit_mode
    horizontal = info["width"] / max(1, info["height"]) > 9 / 16 * 1.05
    return "crop" if (horizontal and keys_available) else "blur"
