"use strict";
/* Clipper UI. Plain JavaScript, no build step. All server text is inserted with textContent (never innerHTML). */

const $ = (s, r = document) => r.querySelector(s);
const S = { schema: [], by: {}, categories: [], values: {}, overrides: {}, presets: {}, accounts: [], assets: null, status: {} };
let timers = [];
let routeToken = 0;

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected" || k === "required" || k === "open") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : ""), text: msg });
  $("#toasts").append(t);
  setTimeout(() => t.remove(), bad ? 6000 : 3000);
}

async function api(method, path, body, opts = {}) {
  const headers = { "X-Clipper": "1" };
  let payload;
  if (opts.raw) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers, body: payload });
  if (r.status === 401) { showLogin(); throw new Error("Sign in required"); }
  if (opts.blob) {
    if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || "HTTP " + r.status); }
    return r.blob();
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "HTTP " + r.status);
  return data;
}
const guard = (fn) => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, true); } };

const pad = (n) => String(n).padStart(2, "0");
const fmtTime = (s) => `${Math.floor(s / 60)}:${pad(Math.floor(s % 60))}`;
function ago(epoch) {
  const d = Math.max(0, Date.now() / 1000 - epoch);
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + " min ago";
  if (d < 86400) return Math.floor(d / 3600) + " h ago";
  return Math.floor(d / 86400) + " d ago";
}
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function toIso(local) { return local ? new Date(local).toISOString().replace(/\.\d+Z$/, "Z") : ""; }
function when(iso) { return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : ""; }

const STATUS_LABEL = { queued: "Waiting", running: "Working", review: "Ready to review", done: "Finished", failed: "Failed", cancelled: "Cancelled",
  planned: "Planned", rendering: "Rendering", rendered: "Ready to review", render_failed: "Render failed", upload_queued: "Waiting to upload",
  uploading: "Uploading", uploaded: "Uploaded", upload_failed: "Upload failed", discarded: "Discarded" };
const pill = (s) => h("span", { class: "pill " + s, text: STATUS_LABEL[s] || s });

// ---------------------------------------------------------------- auth
function showLogin() { $("#login").classList.remove("hidden"); setTimeout(() => $("#pw").focus(), 50); }
$("#loginform").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("POST", "/api/auth/login", { password: $("#pw").value });
    $("#login").classList.add("hidden"); $("#pw").value = ""; $("#loginerr").textContent = "";
    await boot();
  } catch (err) { $("#loginerr").textContent = err.message; }
});

// ---------------------------------------------------------------- data
async function loadAll() {
  const [sc, st, pr, ac, status] = await Promise.all([api("GET", "/api/schema"), api("GET", "/api/settings"), api("GET", "/api/presets"),
    api("GET", "/api/accounts"), api("GET", "/api/status")]);
  S.schema = sc.schema; S.categories = sc.categories; S.by = Object.fromEntries(sc.schema.map((f) => [f.key, f]));
  S.values = st.values; S.overrides = st.overrides; S.presets = pr.presets; S.accounts = ac.accounts; S.status = status;
  updateBadge();
}
async function loadAssets() { S.assets = await api("GET", "/api/assets"); }
function updateBadge() {
  const b = $("#qbadge"), n = S.status.queue || 0;
  b.textContent = n; b.classList.toggle("hidden", !n);
}
async function pollStatus() {
  try { S.status = await api("GET", "/api/status"); updateBadge(); } catch (e) { /* offline or signed out */ }
}
setInterval(pollStatus, 5000);

// ---------------------------------------------------------------- router
const ROUTES = { create: pageCreate, clips: pageClips, job: pageJob, style: pageStyle, accounts: pageAccounts, setup: pageSetup };
function clearTimers() { timers.forEach(clearInterval); timers = []; }
function every(ms, fn, token) { timers.push(setInterval(() => { if (token === routeToken) fn(); }, ms)); }

async function route() {
  clearTimers();
  const token = ++routeToken;
  const parts = (location.hash.replace(/^#\/?/, "") || "create").split("/");
  const name = ROUTES[parts[0]] ? parts[0] : "create";
  document.querySelectorAll("nav.tabs a").forEach((a) => {
    const on = a.dataset.tab === (name === "job" ? "clips" : name);
    on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current");
  });
  const view = $("#view");
  try {
    await ROUTES[name](view, parts.slice(1), token);
    window.scrollTo(0, 0);
  } catch (e) {
    if (token === routeToken) view.replaceChildren(h("div", { class: "card empty" }, h("p", { text: e.message }), h("button", { onclick: route, text: "Try again" })));
  }
}
window.addEventListener("hashchange", route);

async function boot() {
  const a = await api("GET", "/api/auth/status");
  if (a.password_set && !a.authed) return showLogin();
  await loadAll();
  route();
}

// ---------------------------------------------------------------- shared field widgets
function option(value, label, selected) { return h("option", { value, text: label, selected }); }

function fieldInput(f, val, onchange) {
  const t = f.type;
  const send = (v) => onchange(v);
  if (t === "bool") return h("div", { class: "check" }, h("input", { type: "checkbox", checked: !!val, onchange: (e) => send(e.target.checked) }));
  if (t === "select") return h("select", { onchange: (e) => send(e.target.value) }, f.choices.map(([v, l]) => option(v, l, String(v) === String(val))));
  if (t === "int" || t === "float") {
    return h("input", { type: "number", value: val, min: f.min, max: f.max, step: f.step || (t === "int" ? 1 : "any"), inputmode: "decimal",
      onchange: (e) => e.target.value !== "" && send(Number(e.target.value)) });
  }
  if (t === "textarea") return h("textarea", { value: val || "", oninput: (e) => send(e.target.value) });
  if (t === "csv_int") return h("input", { type: "text", value: Array.isArray(val) ? val.join(",") : (val || ""), inputmode: "numeric", onchange: (e) => send(e.target.value) });
  if (t === "color") {
    const pick = h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(val || "") ? val : "#ffffff", oninput: (e) => { auto.textContent = "Auto"; send(e.target.value); } });
    const auto = h("button", { type: "button", class: "sm ghost", text: val ? "Use style default" : "Auto", onclick: () => { send(""); auto.textContent = "Auto"; } });
    return h("div", { class: "row" }, pick, auto);
  }
  if (t === "music" || t === "logo") return assetPicker(t, val, send, f.key === "tweet_avatar" ? "Use logo" : "");
  const list = f.key === "caption_font" || f.key === "caption_font_hindi" ? "fontlist" : null;
  const ph = { tweet_name: "Your Name", tweet_handle: "@yourhandle" }[f.key];
  return h("input", { type: "text", value: val || "", list, placeholder: ph, autocomplete: "off", oninput: (e) => send(e.target.value) });
}

function assetPicker(kind, val, send, none) {
  const items = (S.assets && S.assets[kind]) || [];
  const sel = h("select", { onchange: (e) => send(e.target.value) },
    option("", none || (kind === "music" ? "No music" : "No logo"), !val),
    !val || items.some((i) => i.path === val) ? null : option(val, val.split("/").pop() + " (from .env)", true),
    items.map((i) => option(i.path, i.name, i.path === val)));
  const file = h("input", { type: "file", class: "hidden", accept: kind === "music" ? "audio/*,.mp3,.m4a,.wav" : "image/*",
    onchange: guard(async (e) => {
      const fl = e.target.files[0]; if (!fl) return;
      const r = await uploadAsset(kind, fl);
      await loadAssets();
      send(r.path);
      sel.append(option(r.path, r.name, true)); sel.value = r.path;
    }) });
  return h("div", { class: "row" }, h("div", { class: "grow" }, sel), h("button", { type: "button", class: "sm", onclick: () => file.click(), text: "Upload" }), file);
}

async function uploadAsset(kind, file) {
  toast("Uploading " + file.name + "...");
  const r = await api("POST", `/api/assets?kind=${kind}&name=${encodeURIComponent(file.name)}`, file, { raw: true });
  toast("Uploaded " + r.name);
  return r;
}

// ---------------------------------------------------------------- Create
async function pageCreate(view, _p, token) {
  await loadAssets();
  const v = () => S.values;                       // saved settings; the page only sends what differs
  const opts = { mode: "review", account: S.accounts[0] && S.accounts[0].id };
  const COMMON = ["template", "tweet_name", "tweet_handle", "tweet_avatar", "caption_enabled", "caption_mode", "caption_language", "caption_style",
    "intro_hook", "clip_count", "clip_durations", "max_height", "live_minutes"];
  const state = {};
  const reset = () => COMMON.forEach((k) => (state[k] = Array.isArray(v()[k]) ? v()[k].join(",") : v()[k]));
  reset();
  const diff = () => Object.fromEntries(COMMON.filter((k) => String(state[k]) !== String(Array.isArray(v()[k]) ? v()[k].join(",") : v()[k])).map((k) => [k, state[k]]));

  // live preview: the same renderer the real clips use, with this page's choices on top of the saved settings
  const stageImg = h("img", { alt: "Preview of the clip look", width: 300, height: 533 });
  const stage = h("div", { class: "stage" }, stageImg);
  let seq = 0, objUrl = null, timer = null;
  function preview() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const my = ++seq;
      stage.classList.add("busy");
      try {
        const ov = {};
        for (const [k, val] of Object.entries(diff())) if (/^(caption_|template|tweet_|intro_)/.test(k)) ov[k] = val;
        const blob = await api("POST", "/api/preview", { overrides: ov }, { blob: true });
        if (my !== seq || token !== routeToken) return;
        const u = URL.createObjectURL(blob); stageImg.src = u;
        if (objUrl) URL.revokeObjectURL(objUrl); objUrl = u;
      } catch (e) { if (my === seq) toast("Preview: " + e.message, true); }
      stage.classList.remove("busy");
    }, 300);
  }
  const set = (k, val) => { state[k] = val; preview(); defaultsBtn.disabled = !Object.keys(diff()).length; };

  const segment = (label, choices, get, put) => {
    const box = h("div", { class: "seg", role: "group", "aria-label": label });
    const paint = () => box.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === get())));
    choices.forEach(([k, l]) => box.append(h("button", { type: "button", "data-k": k, text: l, onclick: () => { put(k); paint(); } })));
    paint();
    return h("div", { class: "stack", style: "gap:6px" }, h("div", { class: "l muted small", text: label }), box);
  };
  const field = (key, label) => h("label", { class: "f" }, h("span", { class: "l", text: label || S.by[key].label }),
    fieldInput(S.by[key], state[key], (x) => set(key, x)));

  const url = h("input", { type: "url", placeholder: "https://www.youtube.com/watch?v=...", required: true, autocomplete: "off", inputmode: "url" });
  const MODE_HELP = { review: "Clips wait for you to check and upload them.", auto: "Clips upload and schedule as soon as they are ready." };
  const modeHelp = h("div", { class: "small muted", text: MODE_HELP.review });
  const mode = segment("After rendering", [["review", "Review first"], ["auto", "Upload automatically"]], () => opts.mode, (k) => { opts.mode = k; modeHelp.textContent = MODE_HELP[k]; });

  const capMode = () => (state.caption_enabled ? state.caption_mode : "off");
  const captions = segment("Captions", [["words", "Word by word"], ["single", "One line"], ["off", "Off"]], capMode, (k) => {
    state.caption_enabled = k !== "off"; if (k !== "off") state.caption_mode = k; set("caption_enabled", state.caption_enabled); });
  const template = segment("Template", [["blur", "Blurred"], ["tweet", "White + tweet"], ["black", "Black"], ["face", "Face follow"]], () => state.template, (k) => { set("template", k); syncLook(); });
  const who = h("div", { class: "stack soft" },
    h("div", { class: "small muted", text: "Shown on the tweet card. You can also change it for each clip after it is rendered." }),
    h("div", { style: "display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(150px,1fr))" },
      field("tweet_name", "Name on the card"), field("tweet_handle", "Handle"), field("tweet_avatar", "Picture")));
  const syncLook = () => who.classList.toggle("hidden", state.template !== "tweet");
  syncLook();

  const acct = S.accounts.length > 1 ? h("label", { class: "f" }, h("span", { class: "l", text: "YouTube account" }),
    h("select", { onchange: (e) => (opts.account = Number(e.target.value)) }, S.accounts.map((a) => option(a.id, a.name + (a.authorized ? "" : " (not signed in)"), a.id === opts.account)))) : null;
  const focus = h("textarea", { placeholder: "e.g. funny moments, strong opinions, tips people can use", value: "" });
  const fresh = h("input", { type: "checkbox" });
  const vis = h("select", {}, S.by.publish_mode.choices.map(([k, l]) => option(k, l, k === v().publish_mode)));
  const defaultsBtn = h("button", { type: "button", class: "sm", disabled: true, text: "Save these as my defaults", onclick: guard(async () => {
    const d = diff(); if (!Object.keys(d).length) return;
    const r = await api("PUT", "/api/settings", { overrides: { ...S.overrides, ...d } });
    S.overrides = r.overrides; S.values = r.values; reset(); defaultsBtn.disabled = true;
    toast("Saved. New clips start from these."); }) });

  const form = h("form", { class: "stack", onsubmit: guard(async (e) => {
    e.preventDefault();
    const o = { mode: opts.mode, ...diff() };
    if (opts.account) o.account = opts.account;
    if (focus.value.trim()) o.clip_focus = focus.value.trim();
    if (fresh.checked) o.fresh = true;
    if (vis.value !== v().publish_mode) o.publish_mode = vis.value;
    const r = await api("POST", "/api/jobs", { url: url.value.trim(), options: o });
    toast("Started");
    location.hash = "#/job/" + r.id;
  }) },
    h("label", { class: "f" }, h("span", { class: "l", text: "YouTube link" }), url),
    h("div", { class: "maker" },
      h("div", { class: "stack", style: "grid-area:look" },
        h("h2", { class: "sec", text: "Look" }), template, who, captions,
        h("div", { class: "grid2" }, field("caption_language"), field("caption_style")),
        h("label", { class: "check" }, fieldInput(S.by.intro_hook, state.intro_hook, (x) => set("intro_hook", x)), h("span", { text: "Show the hook text in the first seconds" }))),
      h("div", { class: "stagecol stack", style: "grid-area:stage" }, stage,
        h("div", { class: "small muted", style: "text-align:center", text: "Preview on a sample frame. Your video uses the same look." }),
        h("div", { class: "row", style: "justify-content:center" }, defaultsBtn,
          h("a", { class: "small", href: "#/style", text: "More style settings" }))),
      h("div", { class: "stack", style: "grid-area:clips" },
        h("h2", { class: "sec", text: "Clips" }),
        h("div", { class: "grid2" }, field("clip_count"), field("max_height", "Download quality")),
        field("clip_durations", "Allowed lengths (seconds)"), acct,
        mode, modeHelp)),
    h("details", { class: "card" }, h("summary", { text: "More options for this video" }),
      h("div", { class: "stack", style: "margin-top:12px" },
        h("label", { class: "f" }, h("span", { class: "l", text: "What kind of moments to look for" }), focus),
        field("live_minutes", "If the link is a live stream, use the last"),
        h("div", { class: "small muted", text: "A stream that is still running is saved up to this moment, then clips are cut from it." }),
        h("label", { class: "f" }, h("span", { class: "l", text: "Visibility" }), vis),
        h("label", { class: "check" }, fresh, h("span", { text: "Ignore saved transcript and clip plan for this video" })))),
    h("div", {}, h("button", { class: "primary", type: "submit", text: "Find clips" })));

  const missing = [];
  if (!S.status.gemini_key) missing.push("GEMINI_API_KEY is missing in .env, so clips cannot be chosen.");
  if (!S.status.groq_key) missing.push("GROQ_API_KEY is not set. Captions will use the weaker Gemini transcript unless faster-whisper is installed.");
  view.replaceChildren(h("header", { class: "page" }, h("h1", { text: "New clips" })),
    missing.length ? h("div", { class: "card small", style: "margin-bottom:14px;border-color:var(--warn)" }, missing.map((m) => h("div", { text: m }))) : null,
    h("div", { class: "card" }, form));
  preview();
}

// ---------------------------------------------------------------- Clips list
async function pageClips(view, _p, token) {
  const draw = async () => {
    const { jobs } = await api("GET", "/api/jobs");
    if (token !== routeToken) return;
    const list = jobs.length ? jobs.map((j) => h("a", { class: "job", href: "#/job/" + j.id },
      h("div", { class: "row" }, h("div", { class: "t grow", text: j.title || j.url }), pill(j.status)),
      j.status === "running" || j.status === "queued" ? h("div", { class: "stack", style: "gap:6px" }, h("div", { class: "bar" }, h("i", { style: `width:${j.progress}%` })),
        h("div", { class: "small muted", text: j.message || STATUS_LABEL[j.status] })) : null,
      j.error ? h("div", { class: "small", style: "color:var(--bad)", text: j.error }) : null,
      h("div", { class: "small muted", text: `${j.clip_count ? `${j.rendered}/${j.clip_count} rendered, ${j.uploaded} uploaded · ` : ""}${ago(j.updated)}` })))
      : h("div", { class: "empty" }, h("p", { text: "No clips yet. Paste a YouTube link to start." }), h("a", { href: "#/create", text: "Create clips" }));
    view.replaceChildren(h("header", { class: "page" }, h("h1", { text: "Clips" }), h("a", { href: "#/create", text: "New" })), h("div", { class: "card flat" }, list));
  };
  await draw();
  every(2500, () => draw().catch(() => {}), token);
}

// ---------------------------------------------------------------- Job detail + review
async function pageJob(view, [id], token) {
  if (!S.assets) await loadAssets();
  let job = await api("GET", `/api/jobs/${id}`);
  let logSince = 0;
  const head = h("div", { class: "stack" });
  const clipsBox = h("div", { class: "stack" });
  const logPre = h("pre", { class: "log", text: "" });
  const logBox = h("details", { class: "card" }, h("summary", { text: "Activity log" }), h("div", { style: "margin-top:10px" }, logPre));
  view.replaceChildren(h("header", { class: "page" }, h("a", { href: "#/clips", text: "‹ All clips" })), head, clipsBox, logBox);
  const cards = {};

  const act = (label, fn, cls) => h("button", { class: cls || "", text: label, onclick: guard(async () => { await fn(); await refresh(); }) });
  function drawHead() {
    const active = job.status === "running" || job.status === "queued";
    head.replaceChildren(h("div", { class: "card stack" },
      h("div", { class: "row" }, h("h2", { class: "grow", text: job.title || "Video" }), pill(job.status)),
      h("a", { class: "small", href: job.url, target: "_blank", rel: "noopener noreferrer", text: job.url }),
      active ? h("div", { class: "stack", style: "gap:6px" }, h("div", { class: "bar" }, h("i", { style: `width:${job.progress}%` })), h("div", { class: "small muted", text: job.message || STATUS_LABEL[job.status] })) : null,
      job.error ? h("div", { style: "color:var(--bad)", text: job.error }) : null,
      job.language ? h("div", { class: "small muted", text: `Spoken language: ${job.language}${job.engine ? " · transcript from " + job.engine : ""}` }) : null,
      h("div", { class: "row" },
        active ? act("Cancel", () => api("POST", `/api/jobs/${id}/cancel`), "danger") : null,
        !active && !job.busy && job.clips.some((c) => ["rendered", "upload_failed", "render_failed"].includes(c.status) && c.meta)
          ? h("button", { text: "Re-render all clips", title: "Apply your current Style settings to every clip not yet uploaded", onclick: guard(async () => {
            await api("POST", `/api/jobs/${id}/rerender-all`, {}); toast("Rendering again"); await refresh(true); }) }) : null,
        !active && !job.busy && (job.status === "failed" || job.status === "cancelled") ? act("Try again", () => api("POST", `/api/jobs/${id}/retry`), "primary") : null,
        !active && !job.busy ? h("button", { class: "danger ghost", text: "Delete", onclick: guard(async () => {
          if (!confirm("Delete this job and its rendered clips from the phone?")) return;
          await api("DELETE", `/api/jobs/${id}`); location.hash = "#/clips"; }) }) : null)));
  }

  function clipSig(c) { return JSON.stringify([c.status, c.video_url, c.error, c.youtube_id, c.publish_at, job.busy, !!c.meta, c.look]); }
  function drawClips() {
    for (const c of job.clips) {
      const sig = clipSig(c);
      let card = cards[c.id];
      if (!card) { card = cards[c.id] = { el: h("div", { class: "card" }), sig: "", dirty: false }; clipsBox.append(card.el); }
      if (card.sig === sig || card.dirty) continue;   // never overwrite text the user is editing
      card.sig = sig;
      buildClip(card, c);
    }
  }

  function buildClip(card, c) {
    const busy = job.busy;
    const meta = c.meta;
    const title = meta && h("input", { type: "text", value: meta.title, maxlength: 95 });
    const desc = meta && h("textarea", { value: meta.description || "" });
    const tags = meta && h("input", { type: "text", value: (meta.hashtags || []).join(" ") });
    const cat = meta && h("select", {}, S.categories.map((k) => option(k, k, k === meta.category)));
    const pub = h("input", { type: "datetime-local", value: toLocalInput(c.publish_at) });
    const phrases = (c.captions || []).map((p, i) => ({ p, i, area: h("textarea", { value: p.text, rows: 1 }) }));
    const touch = () => (card.dirty = true);
    const lk = c.look || {}, base = c.look_base || {}, dflt = c.look_defaults || {};
    const TPL = { blur: "Blurred background", tweet: "White with tweet card", black: "Plain black", face: "Face follow (full screen)" };
    const CAP = { words: "Word by word", single: "One line for the whole video", off: "No captions" };
    const tplSel = h("select", {}, option("", `Default (${TPL[base.template] || ""})`, !lk.template), Object.entries(TPL).map(([k, l]) => option(k, l, lk.template === k)));
    const capSel = h("select", {}, option("", `Default (${CAP[base.caption_mode] || ""})`, !lk.caption_mode), Object.entries(CAP).map(([k, l]) => option(k, l, lk.caption_mode === k)));
    const tweetTxt = h("textarea", { rows: 2, maxlength: 200, value: lk.tweet_text || "", placeholder: dflt.tweet_text || "" });
    const capTxt = h("textarea", { rows: 2, maxlength: 200, value: lk.caption_text || "", placeholder: dflt.caption_text || "" });
    const tweetRow = h("label", { class: "f" }, h("span", { class: "l", text: "Tweet text" }), tweetTxt, h("span", { class: "help", text: "Empty = the clip title." }));
    const capRow = h("label", { class: "f" }, h("span", { class: "l", text: "Caption text (shown the whole video)" }), capTxt, h("span", { class: "help", text: "Empty = the cover text." }));
    const nameIn = h("input", { type: "text", maxlength: 40, value: lk.tweet_name || "", placeholder: base.tweet_name || "Your Name" });
    const handleIn = h("input", { type: "text", maxlength: 40, value: lk.tweet_handle || "", placeholder: base.tweet_handle || "@yourhandle" });
    const logos = (S.assets && S.assets.logo) || [];
    const picSel = h("select", {}, option("", `Default${base.tweet_avatar ? " (" + base.tweet_avatar + ")" : " (blue circle)"}`, !lk.tweet_avatar),
      logos.map((l) => option(l.name, l.name, lk.tweet_avatar === l.name)));
    const idRow = h("div", { style: "display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(160px,1fr))" },
      h("label", { class: "f" }, h("span", { class: "l", text: "Name on the card" }), nameIn),
      h("label", { class: "f" }, h("span", { class: "l", text: "Handle" }), handleIn),
      h("label", { class: "f" }, h("span", { class: "l", text: "Picture" }), picSel));
    const syncLook = () => { idRow.classList.toggle("hidden", (tplSel.value || base.template) !== "tweet"); tweetRow.classList.toggle("hidden", (tplSel.value || base.template) !== "tweet"); capRow.classList.toggle("hidden", (capSel.value || base.caption_mode) !== "single"); };
    tplSel.addEventListener("change", syncLook); capSel.addEventListener("change", syncLook); syncLook();
    [title, desc, tags, cat, pub, tplSel, capSel, tweetTxt, capTxt, nameIn, handleIn, picSel, ...phrases.map((x) => x.area)].forEach((el) => el && el.addEventListener("input", touch));

    const collect = () => {
      const body = {};
      if (meta) body.meta = { title: title.value, description: desc.value, hashtags: tags.value.split(/[\s,]+/).filter(Boolean), category: cat.value };
      const ed = {};
      phrases.forEach((x) => { if (x.area.value.trim() !== x.p.text.trim()) ed[x.i] = x.area.value; });
      if (Object.keys(ed).length) body.captions = ed;
      body.look = { template: tplSel.value, caption_mode: capSel.value, tweet_text: tweetTxt.value, caption_text: capTxt.value, tweet_name: nameIn.value, tweet_handle: handleIn.value, tweet_avatar: picSel.value };
      if (pub.value) body.publish_at = toIso(pub.value);
      else if (c.publish_at) body.publish_at = "";
      return body;
    };
    const save = async () => { await api("PUT", `/api/jobs/${id}/clips/${c.id}`, collect()); card.dirty = false; };

    const phone = h("div", { class: "phone" }, c.video_url ? h("video", { src: c.video_url, controls: true, playsinline: true, preload: "metadata" })
      : h("div", { class: "ph", text: c.status === "rendering" ? "Rendering..." : c.status === "render_failed" ? "Render failed" : "Not rendered yet" }));

    const regen = h("input", { type: "checkbox" });
    const actions = meta && h("div", { class: "stack" },
      h("div", { class: "row" },
        h("button", { text: "Save edits", disabled: busy, onclick: guard(async () => { await save(); toast("Saved"); await refresh(true); }) }),
        h("button", { text: "Re-render", disabled: busy, onclick: guard(async () => { await save(); await api("POST", `/api/jobs/${id}/clips/${c.id}/rerender`, { regenerate_captions: regen.checked }); await refresh(true); }) }),
        h("label", { class: "check small" }, regen, h("span", { text: "Redo captions (use after changing language)" }))),
      h("div", { class: "row" },
        c.status === "uploaded" ? null : h("button", { class: "primary", disabled: busy || !c.video_url, text: pub.value ? "Upload and schedule" : "Upload at next free slot", onclick: guard(async () => {
          await save(); await api("POST", `/api/jobs/${id}/clips/${c.id}/upload`, pub.value ? { publish_at: toIso(pub.value) } : {}); toast("Upload queued"); await refresh(true); }) }),
        h("button", { class: "danger ghost", disabled: busy, text: "Discard clip", onclick: guard(async () => {
          if (!confirm("Discard this clip and delete its file?")) return;
          await api("POST", `/api/jobs/${id}/clips/${c.id}/discard`); await refresh(true); }) })));

    card.el.replaceChildren(h("div", { class: "clip" }, phone, h("div", { class: "stack" },
      h("div", { class: "row" }, h("h3", { class: "grow", text: `${c.id.replace("clip", "Clip ")} · ${fmtTime(c.start)}–${fmtTime(c.end)} · ${Math.round(c.duration)}s` }), h("span", { class: "pill", text: "Score " + c.score }), pill(c.status)),
      c.reason ? h("div", { class: "small muted", text: c.reason }) : null,
      c.error ? h("div", { class: "small", style: "color:var(--bad)", text: c.error }) : null,
      c.status === "uploaded" && c.youtube_id ? h("div", {}, h("a", { href: "https://youtu.be/" + c.youtube_id, target: "_blank", rel: "noopener noreferrer", text: "Open on YouTube" }),
        c.publish_at ? h("span", { class: "muted", text: " · goes public " + when(c.publish_at) }) : null) : null,
      meta && h("label", { class: "f" }, h("span", { class: "l", text: "Title" }), title),
      meta && meta.titles && meta.titles.length > 1 ? h("div", { class: "row" }, meta.titles.map((t) => h("button", { type: "button", class: "chip", text: t, onclick: () => { title.value = t; touch(); } }))) : null,
      meta && h("label", { class: "f" }, h("span", { class: "l", text: "Description" }), desc),
      meta && h("div", { style: "display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))" },
        h("label", { class: "f" }, h("span", { class: "l", text: "Hashtags" }), tags),
        h("label", { class: "f" }, h("span", { class: "l", text: "Category" }), cat),
        h("label", { class: "f" }, h("span", { class: "l", text: "Publish time (your time)" }), pub, h("span", { class: "help", text: "Leave empty to use the next free slot." }))),
      h("details", { open: !!Object.keys(lk).length }, h("summary", { text: "Look of this clip" }),
        h("div", { class: "stack", style: "margin-top:10px" },
          h("div", { style: "display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(200px,1fr))" },
            h("label", { class: "f" }, h("span", { class: "l", text: "Template" }), tplSel), h("label", { class: "f" }, h("span", { class: "l", text: "Captions" }), capSel)),
          idRow, tweetRow, capRow, h("div", { class: "small muted", text: "Press Re-render to apply." }))),
      phrases.length ? h("details", {}, h("summary", { text: `Word captions (${phrases.length} phrases)` }),
        (c.caption_notes || []).length ? h("div", { class: "small", style: "color:var(--warn);margin:8px 0" }, c.caption_notes.map((n) => h("div", { text: n }))) : null,
        h("div", { class: "small muted", style: "margin:8px 0", text: "Edit the words, then Re-render. Dashed boxes are translated or edited: their timing is spread across the phrase." }),
        h("div", { class: "phrases" }, phrases.map((x) => h("div", { class: "phrase" + (x.p.approx ? " approx" : "") }, h("div", { class: "tm", text: fmtTime(x.p.s) }), x.area)))) : null,
      actions)));
  }

  async function refresh(force) {
    job = await api("GET", `/api/jobs/${id}`);
    drawHead(); drawClips();
    const r = await api("GET", `/api/jobs/${id}/log?since=${logSince}`);
    if (r.lines.length) {
      const stick = logPre.scrollTop + logPre.clientHeight >= logPre.scrollHeight - 30;
      for (const l of r.lines) {
        logPre.append(h("span", { class: l.level, text: `${new Date(l.ts * 1000).toLocaleTimeString()}  ${l.msg}\n` }));
        logSince = l.id;
      }
      if (stick) logPre.scrollTop = logPre.scrollHeight;
    }
  }
  drawHead(); drawClips();
  await refresh();
  if (!logBox.open && (job.status === "running" || job.status === "failed")) logBox.open = true;
  every(2000, () => refresh().catch(() => {}), token);
}

// ---------------------------------------------------------------- Style
async function pageStyle(view, _p, token) {
  await loadAssets();
  const draft = {};
  const merged = () => ({ ...S.overrides, ...draft });
  const cur = () => ({ ...S.values, ...draft });
  const PREVIEW = /^(caption_|logo_|template|tweet_)/;
  const stageImg = h("img", { alt: "Caption preview", width: 300, height: 533 });
  const stage = h("div", { class: "stage" }, stageImg);
  let seq = 0, url = null, timer = null;
  const saveBar = h("div", { class: "savebar hidden" }, h("span", { text: "You have unsaved changes" }),
    h("div", { class: "row" }, h("button", { text: "Discard", onclick: () => { Object.keys(draft).forEach((k) => delete draft[k]); build(); preview(); } }),
      h("button", { class: "primary", text: "Save", onclick: guard(async () => { await commit(); toast("Saved"); }) })));

  async function commit() {
    if (!Object.keys(draft).length) return;
    const r = await api("PUT", "/api/settings", { overrides: merged() });
    S.overrides = r.overrides; S.values = r.values;
    Object.keys(draft).forEach((k) => delete draft[k]);
    build(); saveBar.classList.add("hidden");
  }
  function preview() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const my = ++seq;
      stage.classList.add("busy");
      try {
        const ov = {};
        for (const k of Object.keys(draft)) if (PREVIEW.test(k)) ov[k] = draft[k];
        const blob = await api("POST", "/api/preview", { overrides: ov }, { blob: true });
        if (my !== seq || token !== routeToken) return;
        const u = URL.createObjectURL(blob); stageImg.src = u;
        if (url) URL.revokeObjectURL(url); url = u;
      } catch (e) { if (my === seq) toast("Preview: " + e.message, true); }
      stage.classList.remove("busy");
    }, 350);
  }
  function setVal(key, v) {
    draft[key] = v;
    if (PREVIEW.test(key)) preview();
    saveBar.classList.remove("hidden");
    const el = document.querySelector(`[data-key="${key}"]`);
    if (el) el.classList.add("changed");
  }

  const groupsEl = h("div", { class: "card flat" });
  const fontsEl = h("div", { class: "stack", style: "padding:0 14px 16px" });
  const datalist = h("datalist", { id: "fontlist" });
  const presetsEl = h("div", { class: "card stack" });

  function drawFonts() {
    const fonts = (S.assets && S.assets.font) || [];
    datalist.replaceChildren(...fonts.filter((f) => f.family).map((f) => h("option", { value: f.family })));
    const file = h("input", { type: "file", class: "hidden", accept: ".ttf,.otf", onchange: guard(async (e) => {
      const fl = e.target.files[0]; if (!fl) return;
      const r = await uploadAsset("font", fl);
      await loadAssets();
      if (!cur().caption_fonts_dir) setVal("caption_fonts_dir", S.assets.font_dir);
      drawFonts(); build();
    }) });
    fontsEl.replaceChildren(h("div", { class: "small muted", text: "Add a .ttf or .otf font for Hindi or English captions. Then pick it for English or Hindi text." }),
      fonts.map((f) => h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { text: f.family || f.name }), h("div", { class: "small muted", text: f.name })),
        f.family ? h("button", { class: "sm", text: "Use for English", onclick: () => { setVal("caption_font", f.family); if (!cur().caption_fonts_dir) setVal("caption_fonts_dir", S.assets.font_dir); build(); } }) : null,
        f.family ? h("button", { class: "sm", text: "Use for Hindi", onclick: () => { setVal("caption_font_hindi", f.family); if (!cur().caption_fonts_dir) setVal("caption_fonts_dir", S.assets.font_dir); build(); preview(); } }) : null,
        h("button", { class: "sm danger ghost", text: "Remove", onclick: guard(async () => { await api("DELETE", `/api/assets?kind=font&name=${encodeURIComponent(f.name)}`); await loadAssets(); drawFonts(); }) }))),
      h("div", {}, h("button", { class: "sm", text: "Add font", onclick: () => file.click() }), file));
  }

  function drawPresets() {
    const names = Object.keys(S.presets);
    const sel = h("select", {}, names.length ? names.map((n) => option(n, n)) : option("", "No presets yet"));
    presetsEl.replaceChildren(h("h3", { text: "Presets" }), h("div", { class: "small muted", text: "A preset is a saved copy of all your settings." }), sel,
      h("div", { class: "row" },
        h("button", { class: "sm", disabled: !names.length, text: "Apply", onclick: guard(async () => {
          const r = await api("POST", "/api/presets/apply", { name: sel.value }); S.overrides = r.overrides; S.values = r.values;
          Object.keys(draft).forEach((k) => delete draft[k]); build(); preview(); saveBar.classList.add("hidden"); toast("Applied " + sel.value); }) }),
        h("button", { class: "sm", text: "Save current as...", onclick: guard(async () => {
          const name = prompt("Preset name"); if (!name) return;
          await commit(); const r = await api("POST", "/api/presets", { name }); S.presets = r.presets; drawPresets(); toast("Saved preset"); }) }),
        h("button", { class: "sm danger ghost", disabled: !names.length, text: "Delete", onclick: guard(async () => {
          if (!confirm("Delete preset " + sel.value + "?")) return;
          const r = await api("DELETE", "/api/presets/" + encodeURIComponent(sel.value)); S.presets = r.presets; drawPresets(); }) })));
  }

  function build() {
    const groups = [];
    for (const f of S.schema) { let g = groups.find((x) => x[0] === f.group); if (!g) groups.push((g = [f.group, []])); g[1].push(f); }
    const vals = cur(), ov = merged();
    const wasOpen = new Set([...groupsEl.querySelectorAll("details.grp[open]")].map((d) => d.firstChild.textContent));
    const first = !groupsEl.children.length;
    groupsEl.replaceChildren(...groups.map(([name, fields], gi) => {
      const basic = fields.filter((f) => !f.advanced), adv = fields.filter((f) => f.advanced);
      const one = (f) => {
        const changed = f.key in ov;
        return h("div", { class: "fld" + (changed ? " changed" : ""), "data-key": f.key },
          h("label", { class: "f" }, h("span", { class: "l", text: f.label }), fieldInput(f, vals[f.key], (v) => setVal(f.key, v)), f.help ? h("span", { class: "help", text: f.help }) : null),
          changed && !(f.key in draft) ? h("button", { type: "button", class: "reset", text: "Reset", title: "Back to default", onclick: guard(async () => {
            const r = await api("POST", "/api/settings/reset", { keys: [f.key] }); S.overrides = r.overrides; S.values = r.values; build(); preview(); }) }) : null);
      };
      return h("details", { class: "grp", open: first ? gi === 0 : wasOpen.has(name) }, h("summary", { text: name }),
        h("div", { class: "fields" }, basic.map(one)),
        adv.length ? h("details", { style: "padding:0 14px 14px" }, h("summary", { class: "small muted", style: "cursor:pointer;padding:6px 0", text: "Advanced" }), h("div", { class: "fields", style: "padding:10px 0 0" }, adv.map(one))) : null,
        name === "Captions" ? fontsEl : null);
    }));
  }

  view.replaceChildren(h("header", { class: "page" }, h("h1", { text: "Style" })),
    h("p", { class: "small muted", style: "margin:0 0 14px", text: "The most used choices are on New clips. Everything here is the default for each new video, and the preview uses the real renderer. Clips you already made pick up changes when you press Re-render." }), datalist,
    h("div", { class: "styler" }, h("div", { class: "stagecol stack" }, stage, h("div", { class: "small muted", style: "text-align:center", text: "Sample frame. Real videos use the same caption style and logo." }), presetsEl),
      h("div", {}, groupsEl, saveBar)));
  build(); drawFonts(); drawPresets(); preview();
}

// ---------------------------------------------------------------- Accounts
async function pageAccounts(view) {
  const draw = async () => {
    const [{ accounts }, status] = await Promise.all([api("GET", "/api/accounts"), api("GET", "/api/status")]);
    S.accounts = accounts; S.status = status;
    const name = h("input", { type: "text", placeholder: "Channel name, e.g. Main channel", maxlength: 40 });
    view.replaceChildren(h("header", { class: "page" }, h("h1", { text: "YouTube accounts" })),
      status.youtube_secrets ? null : h("div", { class: "card small", style: "margin-bottom:14px;border-color:var(--warn)", text: "client_secret.json was not found next to the app. Download the OAuth client from Google Cloud and put it there first." }),
      h("div", { class: "card flat" }, accounts.map((a) => h("div", { class: "job" },
        h("div", { class: "row" }, h("div", { class: "t grow", text: a.name }), h("span", { class: "pill " + (a.authorized ? "done" : "failed"), text: a.authorized ? "Signed in" : "Not signed in" })),
        h("div", { class: "row" },
          h("button", { class: "sm primary", text: a.authorized ? "Sign in again" : "Sign in with Google", onclick: guard(async () => { const r = await api("POST", `/api/accounts/${a.id}/auth`); location.href = r.url; }) }),
          h("button", { class: "sm", text: "Rename", onclick: guard(async () => { const n = prompt("New name", a.name); if (!n) return; await api("PUT", `/api/accounts/${a.id}`, { name: n }); await draw(); }) }),
          accounts.length > 1 ? h("button", { class: "sm danger ghost", text: "Remove", onclick: guard(async () => { if (!confirm("Remove " + a.name + "?")) return; await api("DELETE", `/api/accounts/${a.id}`); await draw(); }) }) : null)))),
      h("div", { class: "card stack", style: "margin-top:14px" }, h("h3", { text: "Add another channel" }),
        h("div", { class: "row" }, h("div", { class: "grow" }, name), h("button", { text: "Add", onclick: guard(async () => { await api("POST", "/api/accounts", { name: name.value }); await draw(); }) })),
        h("div", { class: "small muted", text: "Each Google account you sign in with must be listed under Test users on the OAuth consent screen. Signing in opens Google, then returns here." })));
  };
  await draw();
}

// ---------------------------------------------------------------- Setup
async function pageSetup(view) {
  const auth = await api("GET", "/api/auth/status");
  const out = h("div", { class: "card flat checks" });
  const disk = h("div", { class: "small muted", text: "..." });
  const loadDisk = async () => { const d = await api("GET", "/api/disk"); disk.textContent = `Free space ${d.free_mb} MB · finished clips ${d.clips_mb} MB · working files ${d.temp_mb} MB`; };
  const run = h("button", { class: "primary", text: "Run checks", onclick: guard(async () => {
    run.disabled = true; run.textContent = "Checking...";
    try {
      const r = await api("POST", "/api/diagnostics", {});
      out.replaceChildren(...r.checks.map((c) => h("div", { class: "c" }, h("span", { class: "dot " + (c.ok ? "ok" : "bad") }), h("div", { text: c.name }), h("div", { class: "muted small", text: c.detail }))));
    } finally { run.disabled = false; run.textContent = "Run checks"; }
  }) });
  const cur = h("input", { type: "password", placeholder: "Current password", autocomplete: "current-password" });
  const nw = h("input", { type: "password", placeholder: "New password (6+ characters)", autocomplete: "new-password" });
  const theme = h("select", { onchange: (e) => setTheme(e.target.value) },
    option("auto", "Match my phone", true), option("dark", "Dark"), option("light", "Light"));
  try { theme.value = localStorage.getItem("theme") || "auto"; } catch (e) { /* storage blocked */ }
  view.replaceChildren(h("header", { class: "page" }, h("h1", { text: "Setup" })),
    h("div", { class: "stack" },
      h("div", { class: "card stack" }, h("div", { class: "row" }, h("h2", { class: "grow", text: "Health check" }), run),
        h("div", { class: "small muted", text: "Tests Gemini, Groq, YouTube, FFmpeg, yt-dlp, fonts and storage. API keys live in .env and are never shown here." })), out,
      h("div", { class: "card stack" }, h("h2", { text: "Storage" }), disk,
        h("div", {}, h("button", { text: "Clean old working files", onclick: guard(async () => { const r = await api("POST", "/api/maintenance/cleanup", {}); toast(`Freed ${r.freed_mb} MB`); await loadDisk(); }) }))),
      h("div", { class: "card stack" }, h("h2", { text: "Password" }),
        h("div", { class: "small muted", text: auth.password_set ? "The app asks for a password." : "No password is set. Anyone using this phone's browser can open the app." }),
        auth.password_set ? cur : null, nw,
        h("div", { class: "row" },
          h("button", { text: auth.password_set ? "Change password" : "Set password", onclick: guard(async () => { await api("POST", "/api/auth/password", { current: cur.value, password: nw.value }); toast("Password saved"); pageSetup(view); }) }),
          auth.password_set ? h("button", { class: "danger ghost", text: "Remove password", onclick: guard(async () => { await api("POST", "/api/auth/password", { current: cur.value, password: "" }); toast("Password removed"); pageSetup(view); }) }) : null,
          auth.password_set ? h("button", { class: "ghost", text: "Sign out", onclick: guard(async () => { await api("POST", "/api/auth/logout"); location.reload(); }) }) : null)),
      h("div", { class: "card stack" }, h("h2", { text: "Appearance" }), theme)));
  loadDisk();
}
function setTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme"); else document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("theme", t); } catch (e) { /* storage blocked */ }
}
try { const t = localStorage.getItem("theme"); if (t && t !== "auto") setTheme(t); } catch (e) { /* storage blocked */ }

boot().catch((e) => { $("#view").replaceChildren(h("div", { class: "card empty", text: "Cannot reach the server: " + e.message })); });
