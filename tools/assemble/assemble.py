"""Video Assembler — combine uploaded clips into one cut with FFmpeg.

Pipeline (all local, no paid APIs):
  1. normalize each clip to a common canvas / fps / codec (so mismatched clips splice cleanly)
  2. concat them in order
  3. optionally burn per-clip captions (timed to each clip's duration via an .srt)
  4. optionally lay a song MP3 over the whole thing as the soundtrack

Requires the `ffmpeg`/`ffprobe` binaries on PATH (installed in the container).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CANVAS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(args[:6])}…\n{proc.stderr[-800:]}")


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:  # noqa: BLE001
        return 0.0


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _build_srt(captions: list[str], durations: list[float]) -> str:
    blocks, t, n = [], 0.0, 1
    for cap, dur in zip(captions, durations):
        start, end = t, t + dur
        t = end
        if cap and cap.strip():
            blocks.append(f"{n}\n{_ts(start)} --> {_ts(end)}\n{cap.strip()}\n")
            n += 1
    return "\n".join(blocks)


def _escape_filter_path(p: Path) -> str:
    return str(p).replace("\\", "/").replace(":", "\\:")


def _build_srt_abs(entries: list[dict]) -> str:
    blocks, n = [], 1
    for e in entries:
        if (e.get("text") or "").strip():
            blocks.append(f"{n}\n{_ts(e['start'])} --> {_ts(e['end'])}\n{e['text'].strip()}\n")
            n += 1
    return "\n".join(blocks)


CAPTION_COLORS = {"white": "&H00FFFFFF", "yellow": "&H0000FFFF", "cyan": "&H00FFFF00",
                  "green": "&H0000FF00", "black": "&H00000000"}
CAPTION_ALIGN = {"bottom": 2, "top": 8, "middle": 5}


def combine_synced(
    timeline: list[dict],
    song_path: str | Path,
    out_path: str | Path,
    aspect: str = "16:9",
    song_duration: float | None = None,
    burn_captions: bool = True,
    caption_size: int = 22,
    caption_position: str = "bottom",
    caption_color: str = "white",
) -> str:
    if not timeline:
        raise ValueError("Empty timeline.")
    width, height = CANVAS.get(aspect, CANVAS["16:9"])
    if song_duration is None:
        song_duration = _duration(Path(song_path)) if song_path else timeline[-1]["end"]
    work = Path(tempfile.mkdtemp(prefix="synced_"))
    try:
        normvf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                  f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p,setsar=1")

        segs: list[tuple[str | None, float]] = []
        first_start = max(0.0, timeline[0]["start"])
        prev_clip = timeline[0].get("clip")
        if first_start > 0.4:
            segs.append((prev_clip, first_start))
        for e in timeline:
            dur = max(0.4, float(e["end"]) - float(e["start"]))
            clip = e.get("clip") or prev_clip
            segs.append((clip, dur))
            if e.get("clip"):
                prev_clip = e["clip"]
        last_end = float(timeline[-1]["end"])
        if song_duration > last_end + 0.4:
            segs.append((prev_clip, song_duration - last_end))

        norm_files = []
        for i, (clip, dur) in enumerate(segs):
            seg = work / f"s{i:03d}.mp4"
            if clip:
                _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{dur:.3f}",
                      "-vf", normvf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(seg)])
            else:
                _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30",
                      "-t", f"{dur:.3f}", "-pix_fmt", "yuv420p",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(seg)])
            norm_files.append(seg)

        listf = work / "list.txt"
        listf.write_text("".join(f"file '{n.as_posix()}'\n" for n in norm_files), encoding="utf-8")
        combined = work / "combined.mp4"
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(combined)])

        scored = work / "scored.mp4"
        _run(["ffmpeg", "-y", "-i", str(combined), "-i", str(song_path), "-map", "0:v", "-map", "1:a",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(scored)])
        current = scored

        if burn_captions and any((e.get("text") or "").strip() for e in timeline):
            srt = work / "caps.srt"
            srt.write_text(_build_srt_abs(timeline), encoding="utf-8")
            primary = CAPTION_COLORS.get(caption_color, CAPTION_COLORS["white"])
            align = CAPTION_ALIGN.get(caption_position, 2)
            style = (f"FontName=DejaVu Sans,FontSize={int(caption_size)},PrimaryColour={primary},"
                     f"BorderStyle=3,Outline=1,Shadow=0,Alignment={align},MarginV=48")
            final = work / "final.mp4"
            _run(["ffmpeg", "-y", "-i", str(scored),
                  "-vf", f"subtitles='{_escape_filter_path(srt)}':force_style='{style}'",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", str(final)])
            current = final

        shutil.copy(current, out_path)
        return str(out_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _load_font(size: int):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    from PIL import ImageFont as _F
    return _F.load_default()


def _make_card_png(title: str, subtitle: str | None, width: int, height: int, out_png: Path) -> None:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (width, height), (7, 7, 12))
    d = ImageDraw.Draw(img)
    f = _load_font(max(28, int(height * 0.075)))
    fs = _load_font(max(16, int(height * 0.032)))

    def tw(s, font):
        b = d.textbbox((0, 0), s, font=font)
        return b[2] - b[0], b[3] - b[1]

    words, lines, cur = (title or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if tw(t, f)[0] > width * 0.85 and cur:
            lines.append(cur); cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    lines = lines or [""]

    line_hs = [tw(l, f)[1] for l in lines]
    gap = int(height * 0.02)
    block_h = sum(line_hs) + gap * (len(lines) - 1)
    y = (height - block_h) // 2 - (int(height * 0.05) if subtitle else 0)
    for l, lh in zip(lines, line_hs):
        w, _ = tw(l, f)
        d.text(((width - w) // 2, y), l, font=f, fill=(237, 237, 242))
        y += lh + gap
    aw = int(width * 0.12)
    d.rectangle([(width - aw) // 2, y + 6, (width + aw) // 2, y + 12], fill=(14, 165, 233))
    if subtitle:
        w, _ = tw(subtitle, fs)
        d.text(((width - w) // 2, y + 30), subtitle, font=fs, fill=(154, 154, 174))
    img.save(out_png)


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTS


def _fit_vf(width: int, height: int, blur_bg: bool) -> str:
    if blur_bg:
        return (f"split=2[bg][fg];"
                f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=22:4[bgb];"
                f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,fps=30,format=yuv420p,setsar=1")
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p,setsar=1")


def _normalize_video(clip: str | Path, width: int, height: int, blur_bg: bool, out: Path, mute: bool = True) -> None:
    audio_args = ["-an"] if mute else ["-c:a", "aac", "-b:a", "128k"]
    _run(["ffmpeg", "-y", "-i", str(clip), "-vf", _fit_vf(width, height, blur_bg),
          *audio_args, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


_KB_SPEEDS = {1: (0.0002, 1.08), 2: (0.0005, 1.12), 3: (0.0009, 1.18), 4: (0.0015, 1.25), 5: (0.002, 1.35)}


def _kenburns(image: str | Path, dur: float, width: int, height: int, out: Path, speed: int = 3) -> None:
    frames = max(2, int(dur * 30))
    rate, max_z = _KB_SPEEDS.get(speed, _KB_SPEEDS[3])
    vf = (f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,crop={width*2}:{height*2},"
          f"zoompan=z='min(1+{rate}*on,{max_z})':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"s={width}x{height}:fps=30,format=yuv420p,setsar=1")
    _run(["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", f"{dur:.2f}", "-vf", vf, "-an",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def _static_image(image: str | Path, dur: float, width: int, height: int, blur_bg: bool, out: Path) -> None:
    _run(["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", f"{dur:.2f}",
          "-vf", _fit_vf(width, height, blur_bg), "-an",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def make_thumbnail(src: str | Path, out: str | Path, is_image: bool, width: int = 240) -> str:
    if is_image:
        _run(["ffmpeg", "-y", "-i", str(src), "-vf", f"scale={width}:-1", "-frames:v", "1", str(out)])
    else:
        _run(["ffmpeg", "-y", "-ss", "0.5", "-i", str(src), "-frames:v", "1",
              "-vf", f"scale={width}:-1", "-q:v", "5", str(out)])
    return str(out)


PRESET_VF = {
    "cinematic": "eq=contrast=1.08:saturation=1.05:gamma=0.98,colorbalance=rs=0.03:bs=-0.03",
    "warm": "colorbalance=rs=0.07:gs=0.02:bs=-0.05,eq=saturation=1.1",
    "cool": "colorbalance=rs=-0.05:bs=0.07,eq=saturation=1.05",
    "bw": "hue=s=0,eq=contrast=1.05",
    "vintage": "curves=preset=vintage",
    "vibrant": "eq=saturation=1.4:contrast=1.08",
}

TRANSITIONS = {"crossfade": "fade", "dissolve": "dissolve", "slide": "slideleft",
               "wipe": "wipeleft", "zoom": "circleopen", "fade to black": "fadeblack"}


def _xfade_chain(segs: list[Path], durations: list[float], transition: float, out: Path, style: str = "fade") -> None:
    inputs = []
    for s in segs:
        inputs += ["-i", str(s)]
    parts, cur, acc = [], "[0:v]", durations[0]
    for i in range(1, len(segs)):
        off = max(0.0, acc - transition)
        nl = f"[x{i}]"
        parts.append(f"{cur}[{i}:v]xfade=transition={style}:duration={transition:.3f}:offset={off:.3f}{nl}")
        cur = nl
        acc = acc + durations[i] - transition
    graph = ";".join(parts)
    _run(["ffmpeg", "-y", *inputs, "-filter_complex", graph, "-map", cur,
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def assemble_simple(
    clip_paths: list[str | Path],
    song_path: str | Path | None,
    out_path: str | Path,
    aspect: str = "16:9",
    title: str | None = None,
    title_subtitle: str | None = None,
    title_dur: float = 4.0,
    end_text: str | None = None,
    end_dur: float = 4.0,
    ken_burns: bool = True,
    blur_bg: bool = True,
    still_dur: float = 5.0,
    transition: float = 0.0,
    fade: float = 0.0,
    subtitles: list[dict] | None = None,
    preset: str = "none",
    vignette: bool = False,
    grain: bool = False,
    transition_style: str = "crossfade",
    logo_path=None,
    logo_pos: str = "bottom-right",
    overlay_text=None,
    text_pos: str = "bottom",
    ken_burns_speed: int = 3,
    mute_video_audio: bool = True,
) -> str:
    if not clip_paths:
        raise ValueError("No clips provided.")
    width, height = CANVAS.get(aspect, CANVAS["16:9"])
    work = Path(tempfile.mkdtemp(prefix="simple_"))
    try:
        segs = []

        def card(text, sub, dur, name):
            png = work / f"{name}.png"
            _make_card_png(text, sub, width, height, png)
            seg = work / f"{name}.mp4"
            _run(["ffmpeg", "-y", "-loop", "1", "-i", str(png), "-t", f"{dur:.2f}",
                  "-vf", "fps=30,format=yuv420p,setsar=1", "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "20", str(seg)])
            segs.append(seg)

        if title and title.strip():
            card(title.strip(), (title_subtitle or "").strip() or None, title_dur, "title")
        for i, clip in enumerate(clip_paths):
            seg = work / f"c{i:03d}.mp4"
            if _is_image(clip):
                if ken_burns:
                    _kenburns(clip, still_dur, width, height, seg, speed=ken_burns_speed)
                else:
                    _static_image(clip, still_dur, width, height, blur_bg, seg)
            else:
                _normalize_video(clip, width, height, blur_bg, seg, mute=mute_video_audio)
            segs.append(seg)
        if end_text and end_text.strip():
            card(end_text.strip(), None, end_dur, "end")

        combined = work / "combined.mp4"
        if transition and transition > 0 and len(segs) > 1:
            durations = [_duration(s) for s in segs]
            _xfade_chain(segs, durations, transition, combined, style=TRANSITIONS.get(transition_style, "fade"))
        else:
            listf = work / "list.txt"
            listf.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(combined)])

        return _finish(combined, song_path, out_path, work, preset, vignette, grain, subtitles, fade,
                       logo_path=logo_path, logo_pos=logo_pos, overlay_text=overlay_text, text_pos=text_pos)
    finally:
        shutil.rmtree(work, ignore_errors=True)


LOGO_POS = {"top-left": "20:20", "top-right": "W-w-20:20",
            "bottom-left": "20:H-h-20", "bottom-right": "W-w-20:H-h-20"}
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def mix_audio(music, voiceover, out, music_vol=1.0, vo_vol=1.0, duck=True, vo_start=0.0):
    out = str(out)
    if voiceover:
        delay_ms = int(max(0.0, vo_start) * 1000)
        if duck:
            fc = (f"[0:a]volume={music_vol}[m];"
                  f"[1:a]adelay={delay_ms}:all=1,volume={vo_vol}[vo];"
                  f"[vo]asplit=2[vmix][vsc];"
                  f"[m][vsc]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=400[md];"
                  f"[md][vmix]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")
        else:
            fc = (f"[0:a]volume={music_vol}[m];"
                  f"[1:a]adelay={delay_ms}:all=1,volume={vo_vol}[vo];"
                  f"[m][vo]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]")
        _run(["ffmpeg", "-y", "-i", str(music), "-i", str(voiceover), "-filter_complex", fc,
              "-map", "[a]", "-c:a", "libmp3lame", "-q:a", "2", out])
    else:
        _run(["ffmpeg", "-y", "-i", str(music), "-filter:a", f"volume={music_vol}",
              "-c:a", "libmp3lame", "-q:a", "2", out])
    return out


def _finish(combined, song_path, out_path, work, preset, vignette, grain, subtitles, fade,
            logo_path=None, logo_pos="bottom-right", overlay_text=None, text_pos="bottom"):
    cdur = _duration(combined)
    chain = []
    if preset and preset != "none" and preset in PRESET_VF:
        chain.append(PRESET_VF[preset])
    if vignette:
        chain.append("vignette=PI/4.5")
    if grain:
        chain.append("noise=alls=10:allf=t")
    if overlay_text and overlay_text.strip():
        txt = overlay_text.strip().replace("\\", "").replace(":", r"\:").replace("'", "'").replace("%", "")
        ypos = {"top": "60", "middle": "(h-th)/2", "bottom": "h-th-50"}.get(text_pos, "h-th-50")
        chain.append(f"drawtext=fontfile='{FONT_BOLD}':text='{txt}':fontsize=44:fontcolor=white:"
                     f"borderw=2:bordercolor=black@0.6:x=(w-text_w)/2:y={ypos}")
    if subtitles:
        srt = Path(work) / "subs.srt"
        srt.write_text(_build_srt_abs(subtitles), encoding="utf-8")
        chain.append(f"subtitles='{_escape_filter_path(srt)}':force_style='"
                     "FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF,"
                     "BorderStyle=3,Outline=1,Shadow=0,Alignment=2,MarginV=48'")
    if fade and fade > 0:
        chain.append(f"fade=t=in:st=0:d={fade:.2f},fade=t=out:st={max(0, cdur - fade):.2f}:d={fade:.2f}")

    has_logo = bool(logo_path) and Path(logo_path).exists()
    if chain or has_logo:
        proc = Path(work) / "proc.mp4"
        if has_logo:
            base = ("[0:v]" + ",".join(chain) + "[base]") if chain else "[0:v]null[base]"
            pos = LOGO_POS.get(logo_pos, LOGO_POS["bottom-right"])
            fc = (f"{base};movie='{_escape_filter_path(Path(logo_path))}'[lgo];"
                  f"[lgo]scale=iw*0.13:-1[lg];[base][lg]overlay={pos}[out]")
            _run(["ffmpeg", "-y", "-i", str(combined), "-filter_complex", fc, "-map", "[out]",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(proc)])
        else:
            _run(["ffmpeg", "-y", "-i", str(combined), "-vf", ",".join(chain),
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(proc)])
        combined = proc

    out_path = Path(out_path)
    if song_path:
        vdur = _duration(combined)
        args = ["ffmpeg", "-y", "-i", str(combined), "-i", str(song_path),
                "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
        if fade and fade > 0:
            args += ["-af", f"afade=t=in:d={fade:.2f},afade=t=out:st={max(0, vdur - fade):.2f}:d={fade:.2f}"]
        args += ["-shortest", str(out_path)]
        _run(args)
    else:
        shutil.copy(combined, out_path)
    return str(out_path)


def _clip_segment(clip, dur, width, height, blur_bg, ken_burns, out):
    if _is_image(clip):
        if ken_burns:
            _kenburns(clip, dur, width, height, out)
        else:
            _static_image(clip, dur, width, height, blur_bg, out)
    else:
        _run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(clip), "-t", f"{dur:.3f}",
              "-vf", _fit_vf(width, height, blur_bg), "-an",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def assemble_beatsync(
    clip_paths: list[str | Path],
    song_path: str | Path,
    out_path: str | Path,
    beat_times: list[float],
    beats_per_cut: int = 4,
    aspect: str = "16:9",
    title: str | None = None,
    title_subtitle: str | None = None,
    title_dur: float = 4.0,
    blur_bg: bool = True,
    ken_burns: bool = True,
    preset: str = "none",
    vignette: bool = False,
    grain: bool = False,
    fade: float = 0.0,
    subtitles: list[dict] | None = None,
    logo_path=None,
    logo_pos: str = "bottom-right",
    overlay_text=None,
    text_pos: str = "bottom",
) -> str:
    if not clip_paths:
        raise ValueError("No clips provided.")
    width, height = CANVAS.get(aspect, CANVAS["16:9"])
    song_dur = _duration(Path(song_path))
    beats = [b for b in (beat_times or []) if b > title_dur + 0.05]
    cuts = beats[::max(1, beats_per_cut)]
    points = [title_dur] + cuts + [song_dur]
    points = sorted({round(p, 3) for p in points if 0 <= p <= song_dur})
    if len(points) < 2:
        points = [title_dur, song_dur]

    work = Path(tempfile.mkdtemp(prefix="beat_"))
    try:
        segs = []
        if title and title.strip():
            png = work / "title.png"
            _make_card_png(title.strip(), (title_subtitle or "").strip() or None, width, height, png)
            tseg = work / "title.mp4"
            _run(["ffmpeg", "-y", "-loop", "1", "-i", str(png), "-t", f"{title_dur:.2f}",
                  "-vf", "fps=30,format=yuv420p,setsar=1", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(tseg)])
            segs.append(tseg)

        for k in range(len(points) - 1):
            dur = max(0.3, points[k + 1] - points[k])
            clip = clip_paths[k % len(clip_paths)]
            seg = work / f"b{k:03d}.mp4"
            _clip_segment(clip, dur, width, height, blur_bg, ken_burns, seg)
            segs.append(seg)

        listf = work / "list.txt"
        listf.write_text("".join(f"file '{s.as_posix()}'\n" for s in segs), encoding="utf-8")
        combined = work / "combined.mp4"
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(combined)])
        return _finish(combined, song_path, out_path, work, preset, vignette, grain, subtitles, fade,
                       logo_path=logo_path, logo_pos=logo_pos, overlay_text=overlay_text, text_pos=text_pos)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_proxy(clip_path: str | Path, out_path: str | Path) -> str:
    _run(["ffmpeg", "-y", "-i", str(clip_path), "-vf", "scale=-2:360,fps=8", "-an",
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "32", "-t", "12", str(out_path)])
    return str(out_path)


def combine(
    clip_paths: list[str | Path],
    captions: list[str] | None,
    song_path: str | Path | None,
    out_path: str | Path,
    aspect: str = "16:9",
    burn_captions: bool = True,
) -> str:
    if not clip_paths:
        raise ValueError("No clips provided.")
    width, height = CANVAS.get(aspect, CANVAS["16:9"])
    work = Path(tempfile.mkdtemp(prefix="assemble_"))
    try:
        norm_files, durations = [], []
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,fps=30,format=yuv420p,setsar=1")
        for i, clip in enumerate(clip_paths):
            norm = work / f"n{i:03d}.mp4"
            _run(["ffmpeg", "-y", "-i", str(clip), "-vf", vf, "-an",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(norm)])
            norm_files.append(norm)
            durations.append(_duration(norm))

        listf = work / "list.txt"
        listf.write_text("".join(f"file '{n.as_posix()}'\n" for n in norm_files), encoding="utf-8")
        current = work / "combined.mp4"
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listf), "-c", "copy", str(current)])

        caps = list(captions or [])
        caps += [""] * (len(norm_files) - len(caps))
        if burn_captions and any(c.strip() for c in caps if c):
            srt = work / "caps.srt"
            srt.write_text(_build_srt(caps, durations), encoding="utf-8")
            capd = work / "capd.mp4"
            style = ("FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF,"
                     "BorderStyle=3,Outline=1,Shadow=0,Alignment=2,MarginV=48")
            _run(["ffmpeg", "-y", "-i", str(current),
                  "-vf", f"subtitles='{_escape_filter_path(srt)}':force_style='{style}'",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(capd)])
            current = capd

        out_path = Path(out_path)
        if song_path:
            _run(["ffmpeg", "-y", "-i", str(current), "-i", str(song_path),
                  "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                  "-shortest", str(out_path)])
        else:
            shutil.copy(current, out_path)
        return str(out_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
