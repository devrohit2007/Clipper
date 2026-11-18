# AI YouTube Clipping Agent

URL → transcript → Gemini picks clips → yt-dlp downloads them → FFmpeg renders 9:16 with karaoke captions,
logo, music, outro → metadata → private, scheduled YouTube upload.

**Easiest way: the web app.** `python server.py`, then open http://localhost:8000 in the phone's browser
(see "Web app" below). The command line still works too:

```
python agent.py                      # asks for a URL, like the old script
python agent.py URL --style neon --clips 3 --srt
python agent.py --check              # dependency report
python agent.py --auth               # YouTube sign-in only
python agent.py URL --review         # ask before each upload
python agent.py URL --no-upload      # render only
```

## Security first
Your old `agent.py` contained a Gemini key and a Google OAuth client ID/secret in plain text. **Treat them as
leaked: delete/rotate the Gemini key in Google AI Studio and create a new OAuth client in Google Cloud Console.**
Nothing is hard-coded now; secrets come from environment variables / `.env` (git-ignored). Logs and errors pass
through a redaction filter, and the Gemini key is sent in a header, never in a URL. Old `token.json` files
still work (they are upgraded on first refresh).

## Termux install
```
pkg update && pkg install python ffmpeg git
pip install yt-dlp -U
pip install -r requirements.txt
mkdir -p ~/fonts ~/music        # put NotoSans-Bold.ttf + NotoSansDevanagari-Bold.ttf in ~/fonts, track1.mp3 in ~/music
cp .env.example .env && nano .env
python agent.py --check
```
Optional, try individually (each may fail on Android; the agent keeps working without them):
`pip install faster-whisper` (local transcription), `pkg install x11-repo` then `pkg install opencv-python python-numpy` (face-following crop; do not use pip).
Termux has no system fonts for Hindi: set `CAPTION_FONTS_DIR=~/fonts` and keep the font names in `.env`
equal to the fonts' family names.

## Web app
```
python server.py                     # http://localhost:8000  (or: sh start.sh, which also keeps the phone awake)
python server.py --port 8080
python server.py --set-password      # optional: ask for a password
```
Open the address in the phone's browser. Keep Termux running (use `sh start.sh`, which takes a wake lock).
Five screens:
- **Create**: paste a link, choose Review first or Upload automatically, template, caption language, caption
  style and type (word by word, one line, off), download quality, clip count and lengths. Per-video options live under "More options".
- **Clips**: every job with live progress. Open one to watch each rendered clip, edit title, description, hashtags,
  category, publish time, the look of that clip (template, caption type, tweet text, one-line caption) and the
  caption words, then Save, Re-render, Upload or Discard. A full activity log is there too.
- **Style**: every setting, with a live 9:16 preview of the captions (and logo). Caption language: As spoken,
  Hindi (Devanagari), English or Hinglish. Colours, size, position, font, outline, logo, outro, music, audio,
  schedule, visibility. Save your look as a preset. Upload music, a logo and fonts from the phone.
- **Accounts**: several YouTube channels; "Sign in with Google" opens Google and returns to the app (no copying).
- **Setup**: health check (Gemini, Groq, YouTube, FFmpeg, yt-dlp, Hindi font, storage), password, cleanup, theme.

How it behaves:
- Jobs run one at a time and are saved in `data/clipper.db`. If Termux is killed or the phone reboots, unfinished
  jobs resume on the next start. Cancel stops the running download/render within a moment.
- Settings order: built-in defaults, then `.env`, then what you save on the Style screen, then options chosen for
  one video. API keys stay in `.env` and are never sent to the browser or stored in the database.
- Caption language: "As spoken" and changing only the script (Hindi to Hinglish or the reverse) keep exact
  word timing. Translating between Hindi and English is done per phrase, so the words are spread across each
  phrase's real time span: accurate per phrase, approximate per word. Gemini does the conversion; with no Gemini
  the captions stay as spoken and the job log says so. Edited phrases are timed the same way.
- Security: it listens on this phone only (127.0.0.1), rejects requests with an unexpected Host header, needs a
  custom header on every change (blocks other websites from driving it), uses an HttpOnly, SameSite session cookie when
  a password is set, rate-limits password attempts, and only serves clip files from inside `output/clips`.
  `--host 0.0.0.0` (reachable from your Wi-Fi) refuses to start without a password; it is plain HTTP, so use it only
  on a network you trust.
- Start on boot: install Termux:Boot, then `mkdir -p ~/.termux/boot && cp termux-boot/start-clipper.sh ~/.termux/boot/`.
- Data lives in `data/` (database, uploaded assets, YouTube tokens, `server.log`). Back it up with `.env`.

## Templates, captions and quality
- **Where settings live**: the New clips screen has the common ones (template, tweet name/handle/picture, caption type,
  language and style, hook text, clip count, lengths, download quality) with a live preview. "Save these as my
  defaults" stores them. Style has every setting, including the small ones (fonts, colours, logo, outro, audio, encoding).
  Both use the same renderer, so what you see in the preview and save in Style is what gets rendered
  (`tests/test_style_parity.py` checks that each setting changes the output). Existing clips pick up Style changes when
  you press Re-render, or **Re-render all clips** on the video page.
- **Template** (New clips screen, Style > Look, or per clip on the review screen):
  - *Blurred background* (default): the video fills the 9:16 screen, as before.
  - *White with tweet card*: white screen, the video fitted in the middle (never cropped), and a tweet-style card
    right above its top-left corner: round picture (your logo, or `Tweet card picture`), name, handle and the clip
    title as the tweet text. Set the name, handle and picture on New clips or Style > Look, and change them per clip
    under "Look of this clip" (then Re-render). Edit the tweet text per clip too. On this
    template the logo becomes the card picture (no corner watermark) and captions use dark text with a blue
    highlight so they are readable on white, unless you chose your own colours.
  - *Plain black*: video fitted on black, logo watermark kept.
  On white and black the captions start just under the video.
  - *Full screen, follows the face*: no blur and no bars. A vertical strip is cut from the video and follows the face
    (needs OpenCV: `pkg install x11-repo` then `pkg install opencv-python python-numpy`). If no face is found it crops the centre instead.
- **Captions**: Off, Word by word (karaoke, follows the speech), or One line (a fixed text for the whole clip, the
  generated cover text by default, edit it on each clip). Choose per video, or per clip under "Look of this clip",
  then press Re-render.
- **Download quality**: 360p, 480p, 720p or 1080p ceiling for what yt-dlp fetches (default 1080p). The old
  behaviour had no ceiling. Lower is faster and uses less data. Changing it downloads the clip again on the next
  render. If a video has no stream that small, the next best is used.
- The tweet card is a look-alike layout drawn by this app. It is not X's real interface, has no verified badge,
  and shows only what you type. Use your own name and picture.

## Live streams
Paste the link of a stream that is still running. The app saves the last 15 minutes to 2 hours of it (your choice under
More options, default 1 hour) up to that moment, stops there, and cuts clips from that recording. The recording is kept
until you clean up temp files, so re-render works. Streams that have not started, or just ended and are still being
processed by YouTube, are refused with a message. Only streams that give one combined video+audio HLS stream are
supported (YouTube live is). Not tested against a real YouTube stream.

## Configuration
Everything is in `.env.example` with comments. Required: `GEMINI_API_KEY`, `GEMINI_MODEL`,
`YOUTUBE_CLIENT_SECRETS` (OAuth "Desktop app" JSON), `YOUTUBE_TOKEN_FILE`.
First upload run prints a Google URL; approve it, then paste the full address the browser ends on
(the page will fail to load, that's expected). The token is saved and auto-refreshed after that.
Every effect (captions, zoom, silence removal, ducking, normalisation, intro hook, outro, transition,
face tracking, upload, review mode, schedule spacing) is a setting.

## Gemini model fallback
`GEMINI_MODEL` is tried first, then each model in `GEMINI_MODELS` in order. On a rate limit (429), overload
(5xx), network error, empty answer or unsupported input the next model is used automatically. A rate-limited model
rests 90 s (1 h if the daily quota is hit) and is then tried again first; a retired model (404) is skipped for the
rest of the run. A bad API key stops immediately. Model names change often: check
ai.google.dev/gemini-api/docs/deprecations and edit `GEMINI_MODELS` in `.env`. Default chain (free tier as of
Nov 2026): gemini-3.8-flash, 3.6-flash, 3.5-flash-lite, 3.1-flash-lite, 2.5-flash, 2.5-flash-lite.
The agent sends the transcript text to Gemini, not the YouTube link, so every model in the chain works for it.

## Transcription engines
`TRANSCRIBE_ENGINE=auto` tries faster-whisper (if installed), then **Groq's free Whisper API** (`GROQ_API_KEY`,
free key at console.groq.com, no card), then Gemini audio as a last resort. Groq returns real word timestamps, so
captions sync properly; audio is sent in 8-minute chunks to stay under the 25 MB free-tier file limit, with
retry and a second Groq model (`GROQ_MODELS`) as backup. Set `TRANSCRIBE_ENGINE=groq` so Gemini is never used
for transcription (Gemini then only analyses the transcript). Free limits change: check Groq's rate-limit page.

## What runs where
| Step | Where |
|---|---|
| Choosing clips, metadata (titles, description, tags, hooks, cover text) | Gemini API |
| Transcription | local faster-whisper if installed; else Groq Whisper API (free, cloud); Gemini audio only as last resort |
| Download, audio extraction, rendering, captions, logo, music, normalisation | local (yt-dlp, FFmpeg) |
| Face tracking | local OpenCV (optional) |
| Upload | YouTube Data API |

No Docker, CUDA or GPU needed.

## Pipeline notes
- Gemini only sees the real transcript (lines carry exact seconds). Every returned timestamp is validated against
  the video duration, snapped to real word boundaries (start at a sentence start, end after a complete sentence),
  de-duplicated for overlap, and score-ranked. Invalid candidates are dropped, not repaired with guesses.
- Captions are real ASS/libass: karaoke `\kf` sweeps, per-word pop (neon), emphasised words (bold), max two lines.
  Hindi text switches to the Devanagari font. Captions move to the top if faces sit in the lower zone (crop mode).
- Upload: private + `publishAt`, resumable 8 MB chunks, retry on 5xx/connection errors with backoff.
  The local file is deleted only after YouTube returns a video id. Failures keep the file; re-running the same
  URL resumes (job state in `output/jobs/<video id>.json`, transcript cached in `output/tmp/`).

## Testing
```
pip install pytest && python -m pytest tests -q
```
Covers timestamp parsing, clip validation, caption generation (all 5 styles, Hindi, SRT), filter graphs, real FFmpeg
renders on synthetic video (blur/crop/silence-cut/outro/transition), failure handling, secret redaction, config and
OAuth-file validation, upload retry, and "failed upload keeps the file".
`tests/test_server.py` covers the database, settings order, scheduler, caption-language conversion (including bad
Gemini answers), and the HTTP server: Host and header checks, passwords, rate limit, Range streaming, path safety,
upload limits, presets, accounts, job queue, cancel, retry, restart recovery, and that keys never reach the browser.
`tests/test_templates.py` renders every template with real FFmpeg and checks pixels (white page, round avatar,
tweet text above the video, captions under it, black bars, watermark), plus layout, wrapping, ASS safety and quality.
`tests/test_e2e.py` runs the whole path (web API, worker, real pipeline code, real FFmpeg render with burned
captions, upload, scheduling, file deletion) against a stub `yt-dlp` and stubbed Gemini/Groq/YouTube. It takes a couple of minutes.

## Research summary (what was actually verified)
| Project | License | Used how |
|---|---|---|
| OpenShorts | MIT (its `cloud/` folder is under a separate commercial licence) | Ideas only: transcript windowing, word-boundary snapping, per-word ASS karaoke. Nothing copied; too heavy (Docker, torch). |
| AutoClip, Chopify | could not be reached/found | Not used. Unverified. |
| AI-Youtube-Shorts-Generator | MIT (README only; source not readable) | Idea: chunking + de-dup. |
| faster-whisper | MIT | Dependency (optional). CPU int8, word timestamps. |
| Whisper | MIT | Not used (needs torch; poor on Termux). |
| PySceneDetect | BSD-3 | Not used: scene-cut reset only matters for multi-camera tracking; adds OpenCV-dependent code. |
| MediaPipe | Apache-2.0 | Not used: no verified Termux wheel. OpenCV Haar cascade instead. |
| Auto-Editor | public domain (README) | Idea only (threshold + margin). Silence removal uses FFmpeg `silencedetect`. |

The research agent could not open several source files; those claims are marked unverified above, not assumed.

## Implemented
Env-based secrets, `.env`/.gitignore, modular code, dependency check, logging with redaction, persistent job state,
resume, temp cleanup; faster-whisper + Gemini fallback, word timestamps, language detection, Hindi/English/Hinglish
prompts; transcript-based scored clip selection (15/30/45/60 s); 5 caption styles + SRT; 9:16 crop with face-follow
or blurred background, zoom, silence removal, music ducking, loudnorm, logo, optional intro hook text, outro,
crossfade; per-clip metadata (5 titles, description, tags, hooks, cover text, category, platform fields);
scheduled private YouTube upload with retry, review mode.

## Remaining limitations (honest list)
- **Not tested against live services**: Gemini, YouTube OAuth/upload, yt-dlp downloads and faster-whisper were not run
  here (no keys, no network to those, faster-whisper and Google libs not installed). Those paths are tested with fakes only.
- Hinglish: Whisper often writes Hinglish in Latin or Devanagari inconsistently; quality depends on model size.
- Groq/faster-whisper give real word timings. The Gemini fallback gives sentence timings only; word timings are estimated, so karaoke sync is approximate. The Groq path is tested with fake responses only, not a live call.
- Speaker labels (podcast style) need diarisation, which isn't included; labels appear only if a transcript carries them.
- Face tracking is OpenCV Haar (frontal faces only), not MediaPipe/YOLO; it falls back to blur when faces are rare.
- Caption placement avoids faces only in crop mode and only top/bottom.
- Hashtags come from the transcript, not trend data.
- Not a single file: run `python server.py` or `python agent.py` from the folder (they need `modules/`).
- The web app UI was checked in desktop Chromium at phone width, not on an Android phone browser.
- The templates were built from your description; I did not have your template images, so spacing and sizes are my
  design. Tell me what to change. Fonts follow the Caption font settings; on Termux add fonts for Hindi.
- The tweet card shows at most 4 lines of text (longer titles end with an ellipsis). Vertical source videos shrink
  to fit inside the white and black templates.
- Caption translation (Hindi to English) is phrase-level, not word-level, by design (see Web app).
- One job runs at a time. A long video on a phone can take many minutes to render.
- YouTube: apps created in a Google Cloud project that has not passed Google's API audit may be forced to keep
  uploads private. Sources conflict on this and it was not verified here. If your uploads stay private, that is the cause;
  the clips still render and upload, but cannot be published automatically until the project is audited.
- The password is a simple local lock, not a replacement for network security; the app is plain HTTP.
- Termux compatibility of faster-whisper/OpenCV is unverified; both are optional.
