"""QuickClip — turn your clips & photos into one polished video in minutes.

Public, no sign-up. Drop in clips/photos → optionally add music + a title →
one button → download. Smooth transitions, no black bars, gentle motion on
photos — all automatic.
"""

from __future__ import annotations

import json
import time
import shutil
import threading
import uuid
from pathlib import Path

import streamlit as st

from tools.assemble.assemble import assemble_simple, make_thumbnail, ffmpeg_available

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "data" / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)
LOGO = ROOT / "assets" / "logo.png"
FEEDBACK = ROOT / "data" / "feedback.jsonl"
ADMIN_KEY = "qc-admin-7f2b4e91"  # hidden dashboard: ?admin=qc-admin-7f2b4e91

_RENDER_LOCK = threading.Lock()
_ACTIVE_RENDERS = {"n": 0}

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
ASPECTS = {"Landscape (16:9)": "16:9", "Portrait — Reels/TikTok (9:16)": "9:16", "Square (1:1)": "1:1"}
STYLES = {"None (natural)": "none", "Cinematic": "cinematic", "Warm": "warm",
          "Cool": "cool", "Black & White": "bw", "Vibrant": "vibrant"}


def _cleanup(max_age_h=6):
    cutoff = time.time() - max_age_h * 3600
    for d in (SESSIONS.iterdir() if SESSIONS.exists() else []):
        try:
            marker = d / ".active"
            mt = marker.stat().st_mtime if marker.exists() else d.stat().st_mtime
            if d.is_dir() and mt < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


st.set_page_config(page_title="QuickClip", page_icon="🎬", layout="centered")
st.markdown("""<style>
.stApp{background:radial-gradient(900px 500px at 12% -8%,rgba(14,165,233,.14),transparent 60%),
radial-gradient(800px 500px at 95% 0%,rgba(56,189,248,.10),transparent 55%),#07070b;}
.qc-grad{background:linear-gradient(110deg,#38bdf8,#818cf8 40%,#0ea5e9);-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800;}
.steps{background:#0e0e15;border:1px solid #232334;border-radius:14px;padding:16px 20px;}
.steps b{color:#7dd3fc}
</style>""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("sid", uuid.uuid4().hex[:16])
ss.setdefault("nonce", 0)
ss.setdefault("order", [])
_cleanup()

sdir = SESSIONS / ss.sid
cdir = sdir / "clips"
cdir.mkdir(parents=True, exist_ok=True)
(sdir / ".active").write_text(str(time.time()))


def saved():
    return sorted(f.name for f in cdir.iterdir() if f.is_file() and f.name != ".active")


def thumb(name):
    td = sdir / "thumbs"; td.mkdir(exist_ok=True)
    tp = td / (name + ".jpg")
    if not tp.exists():
        try:
            make_thumbnail(str(cdir / name), str(tp), is_image=Path(name).suffix.lower() in IMG_EXTS)
        except Exception:  # noqa: BLE001
            return None
    return str(tp) if tp.exists() else None


def render_feedback():
    st.subheader("💡 Feedback & Suggestions")
    st.caption("Help us improve QuickClip — tell us what worked, what felt confusing, "
               "or what you wish it could do.")
    with st.form("feedback_form", clear_on_submit=True):
        rating = st.slider("How would you rate QuickClip so far?", 1, 5, 5,
                           help="1 = needs a lot of work · 5 = love it")
        msg = st.text_area("Your thoughts or suggestions",
                           placeholder="What worked well? What was confusing? What would make this more useful?",
                           height=150)
        fc = st.columns(2)
        name = fc[0].text_input("Name (optional)")
        contact = fc[1].text_input("Email (optional — only if you'd like a reply)")
        sent = st.form_submit_button("Send feedback", type="primary", use_container_width=True)
    if sent:
        if not msg.strip():
            st.error("Please add a short message before sending.")
        else:
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rating": rating,
                   "message": msg.strip(), "name": name.strip(), "contact": contact.strip()}
            try:
                FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
                with FEEDBACK.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                st.success("Thank you! Your feedback has been received.")
                st.balloons()
            except Exception as e:  # noqa: BLE001
                st.error(f"Couldn't save your feedback right now: {e}")
    st.divider()
    st.caption("QuickClip · free video tool")


def _load_feedback():
    records = []
    if FEEDBACK.exists():
        for line in FEEDBACK.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except Exception:  # noqa: BLE001
                pass
    return records


def render_admin():
    st.markdown("## 🔒 QuickClip — Feedback Dashboard")
    st.caption("Private view · not linked anywhere public.")
    records = _load_feedback()
    if not records:
        st.info("No feedback submitted yet.")
        return

    import pandas as pd
    df = pd.DataFrame(records)
    for col in ["ts", "rating", "message", "name", "contact"]:
        if col not in df.columns:
            df[col] = ""
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")

    ratings = df["rating"].dropna()
    m = st.columns(4)
    m[0].metric("Total responses", len(df))
    m[1].metric("Average rating", f"{ratings.mean():.1f} / 5" if len(ratings) else "—")
    promoters = int((df["rating"] >= 4).sum())
    m[2].metric("4–5 ★", f"{promoters} ({(promoters / len(df) * 100):.0f}%)")
    with_contact = int(df["contact"].astype(str).str.strip().ne("").sum())
    m[3].metric("Left an email", with_contact)

    st.caption("Rating distribution")
    dist = ratings.round().astype(int).value_counts().reindex([1, 2, 3, 4, 5], fill_value=0).sort_index()
    st.bar_chart(dist)

    st.caption("All responses (newest first)")
    show = df[["ts", "rating", "name", "contact", "message"]].iloc[::-1].reset_index(drop=True)
    st.dataframe(show, use_container_width=True, height=460)
    st.download_button("⬇ Download all feedback (CSV)",
                       df.to_csv(index=False).encode("utf-8"),
                       file_name="quickclip_feedback.csv", mime="text/csv")


# ---------------- hidden admin gate ----------------
if st.query_params.get("admin") == ADMIN_KEY:
    render_admin()
    st.stop()

# ---------------- header ----------------
c = st.columns([1, 5])
if LOGO.exists():
    c[0].image(str(LOGO), use_container_width=True)
c[1].markdown("# <span class='qc-grad'>QuickClip</span>", unsafe_allow_html=True)
c[1].caption("Turn your clips & photos into one polished video — in under 5 minutes. Free. No sign-up.")

# ---------------- navigation ----------------
page = st.sidebar.radio("Menu", ["🎬 Make a Video", "💡 Feedback & Suggestions"],
                        label_visibility="collapsed")
st.sidebar.markdown("---")
st.sidebar.caption("QuickClip · free video tool\nHave ideas? Use the Feedback page →")
if page.startswith("💡"):
    render_feedback()
    st.stop()

st.markdown("""<div class="steps">
<b>How it works</b><br>
1️⃣ <b>Add your clips & photos</b> (drop them all in at once)<br>
2️⃣ <b>(Optional)</b> add a song, a title, and pick a shape & style<br>
3️⃣ Click <b>✨ Make My Video</b> — we stitch it together, smooth the cuts, add gentle motion to photos, and remove black bars automatically<br>
4️⃣ <b>Preview, then DOWNLOAD it right away.</b>
</div>""", unsafe_allow_html=True)
st.warning("⚠️ This is a free public tool — your video is **not saved**. Download it immediately after it's made. "
           "If you refresh the page or close the tab, your clips and finished video are gone.")
st.write("")

if not ffmpeg_available():
    st.error("The video engine is starting up — please refresh in a moment.")
    st.stop()

# ---------------- 1. upload ----------------
st.subheader("1 · Add your clips & photos")
up = st.file_uploader("Drop all your clips and photos here at once",
                      type=["mp4", "mov", "webm", "m4v", "avi", "mkv", "jpg", "jpeg", "png", "webp", "bmp"],
                      accept_multiple_files=True, key=f"up_{ss.nonce}", label_visibility="collapsed")
if up:
    for f in up:
        (cdir / f.name).write_bytes(f.getvalue())
    ss.nonce += 1
    st.rerun()

files = saved()
order = [n for n in ss.order if n in files] + [n for n in files if n not in ss.order]
ss.order = order

if not files:
    st.info("⬆ Add a few clips and/or photos to get started.")
    st.stop()

h = st.columns([3, 1])
h[0].caption(f"{len(files)} item(s) added · use ▲ ▼ to reorder")
if h[1].button("Clear all", use_container_width=True):
    shutil.rmtree(sdir, ignore_errors=True); ss.order = []; ss.nonce += 1; ss.pop("result", None); st.rerun()

for i, name in enumerate(order):
    col = st.columns([0.4, 1, 3, 0.6, 0.6, 0.6])
    col[0].markdown(f"**{i+1}**")
    t = thumb(name)
    if t:
        col[1].image(t, use_container_width=True)
    else:
        col[1].markdown("🖼️" if Path(name).suffix.lower() in IMG_EXTS else "🎞️")
    col[2].caption(name[:30])
    if col[3].button("▲", key=f"u{name}", disabled=(i == 0)):
        order[i-1], order[i] = order[i], order[i-1]; ss.order = order; st.rerun()
    if col[4].button("▼", key=f"d{name}", disabled=(i == len(order)-1)):
        order[i+1], order[i] = order[i], order[i+1]; ss.order = order; st.rerun()
    if col[5].button("🗑", key=f"x{name}"):
        (cdir / name).unlink(missing_ok=True); st.rerun()

# ---------------- 2. options ----------------
st.subheader("2 · Options (all optional)")
title = st.text_input("Title to show at the start", placeholder="e.g. Our Trip to Bali")
oc = st.columns(2)
aspect_label = oc[0].selectbox("Video shape", list(ASPECTS), index=0)
style_label = oc[1].selectbox("Style / filter", list(STYLES), index=0)
tc = st.columns(2)
ken_burns = tc[0].toggle(
    "Gentle motion on photos", value=True,
    help="Slowly zooms and pans across each photo so still images feel alive and cinematic. "
         "Turn off to keep photos perfectly still.")
blur_bg = tc[1].toggle(
    "Fill empty space with blur", value=True,
    help="When a clip or photo doesn't match the chosen video shape, the gaps are filled with a soft blurred "
         "version of the same image instead of plain black bars.")
music_file = st.file_uploader("Add background music (optional)", type=["mp3", "wav", "m4a", "aac", "ogg"], key=f"mus_{ss.nonce}")

# ---------------- 3. make ----------------
st.subheader("3 · Make it")
with _RENDER_LOCK:
    _busy = _ACTIVE_RENDERS["n"]
if _busy >= 2:
    st.info(f"⏳ **It's busy right now** — {_busy} videos are being created at the same time, so yours may take "
            "a little longer than usual. It will still be made; thanks for your patience!")
if st.button("✨ Make My Video", type="primary", use_container_width=True):
    with _RENDER_LOCK:
        _ACTIVE_RENDERS["n"] += 1
    with st.spinner("Creating your video… this takes a minute or two."):
        try:
            clip_paths = [str(cdir / n) for n in order]
            song = None
            if music_file is not None:
                song = str(sdir / ("music" + Path(music_file.name).suffix.lower()))
                Path(song).write_bytes(music_file.getvalue())
            out = str(sdir / "quickclip.mp4")
            assemble_simple(
                clip_paths, song, out,
                aspect=ASPECTS[aspect_label],
                title=(title.strip() or None), title_subtitle=None,
                title_dur=3, end_text=None,
                ken_burns=ken_burns, blur_bg=blur_bg, still_dur=4,
                transition=0.5, fade=1.0, preset=STYLES[style_label],
            )
            ss.result = Path(out).read_bytes()
        except Exception as e:  # noqa: BLE001
            ss.result = None
            st.error(f"Something went wrong: {e}. Try fewer/smaller files, or refresh and start again.")
        finally:
            with _RENDER_LOCK:
                _ACTIVE_RENDERS["n"] = max(0, _ACTIVE_RENDERS["n"] - 1)
    if ss.get("result"):
        st.rerun()

if ss.get("result"):
    st.divider()
    st.subheader("🎉 Your video")
    st.video(ss.result)
    st.download_button("⬇ Download Video", data=ss.result, file_name="quickclip.mp4",
                       mime="video/mp4", use_container_width=True, type="primary")

st.divider()
st.caption("QuickClip · your files are temporary and auto-deleted after a few hours.")
