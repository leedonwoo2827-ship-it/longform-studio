"""s4-render — 그림 + 음성 + 자막 → 1920×1080 롱폼 mp4. **ffmpeg 만** 쓴다(15~20분을 프레임 캡처로 뽑으면 너무 느리다).

화면(참고 캡처 기준):
  ┌──────────────────────────────┐
  │ [1장 · 장 이름]                 │  좌상단 장 이름
  │      그림 (상단 image_h px)     │  느린 줌/팬(켄번스 4종 순환)
  │ ~~~~ 붓으로 찢긴 듯 번지는 아래 가장자리 ~~~~ │  브러시 엣지 마스크(모든 컷 동일)
  │        큰 손글씨 자막 132px      │  흰 한지 바탕
  └──────────────────────────────┘
씬마다 클립 하나(자막까지 구워서) → out/_clips/NNN.mp4 (그림·문장·길이가 같으면 다시 굽지 않는다)
→ concat(복사) + narration.wav → out/<id>.mp4 → 검수용 contact sheet out/contact_NN.jpg
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from . import config
from .job import Job, scenes
from .log import detail, log

PAPER_RGB = (247, 243, 234)
NL = r"\N"         # ASS 줄바꿈


# ── 바탕·마스크 PNG (numpy → PNG, 외부 라이브러리 없이) ─────────────────────────
def _png(path: Path, arr: np.ndarray) -> None:
    arr = np.ascontiguousarray(arr.astype(np.uint8))
    h, w = arr.shape[:2]
    ctype = 0 if arr.ndim == 2 else 2
    raw = b"".join(b"\x00" + arr[y].tobytes() for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _noise1d(n: int, rng: np.random.Generator, octaves=((180, 1.0), (60, 0.45), (18, 0.2), (5, 0.08))) -> np.ndarray:
    out = np.zeros(n)
    for step, amp in octaves:
        k = n // step + 2
        pts = rng.uniform(-1, 1, k)
        out += amp * np.interp(np.arange(n) / step, np.arange(k), pts)
    return out / sum(a for _, a in octaves)


def make_paper(path: Path, w: int, h: int) -> None:
    rng = np.random.default_rng(7)
    base = np.array(PAPER_RGB, dtype=float)
    grain = rng.normal(0, 2.2, (h, w))
    fib = np.repeat(_noise1d(w, rng, ((3, 1.0), (1, 0.5)))[None, :], h, 0) * 1.2   # 아주 옅은 세로 결
    img = base[None, None, :] + (grain + fib)[..., None]
    _png(path, np.clip(img, 0, 255))


def make_mask(path: Path, w: int, h: int) -> None:
    """그림 영역 알파: 위는 꽉 차고, 아래·좌우 가장자리가 붓질처럼 들쭉날쭉 번지며 사라진다."""
    rng = np.random.default_rng(11)
    ys, xs = np.mgrid[0:h, 0:w].astype(float)
    # 아래 가장자리: 붓 끝선 y(x) + 가로 붓결(행마다 다른 결)
    edge = h - 70 + 40 * _noise1d(w, rng)
    streak = np.repeat(rng.uniform(0, 1, (h, 1)), w, 1) * 0.6 + rng.uniform(0, 1, (h, w)) * 0.4
    soft = 26 + 18 * (0.5 + 0.5 * _noise1d(w, rng))
    a_bottom = np.clip((edge[None, :] - ys) / soft[None, :] + 0.5, 0, 1)
    tail = np.clip((edge[None, :] + 50 - ys) / 50, 0, 1) * (streak > 0.72)       # 끝선 아래로 삐져나온 붓 자국
    a_bottom = np.maximum(a_bottom, tail * 0.85)
    # 좌우: 얇게 번지는 가장자리
    side = 30 + 22 * _noise1d(h, rng)[:, None]
    a_left = np.clip(xs / side, 0, 1)
    a_right = np.clip((w - 1 - xs) / side, 0, 1)
    a_top = np.clip(ys / 6, 0, 1)
    alpha = a_bottom * a_left * a_right * a_top
    _png(path, alpha * 255)


# ── 자막(ASS) ────────────────────────────────────────────────────────────────
def wrap(text: str, maxc: int) -> List[str]:
    """띄어쓰기에서만 끊는다(단어 중간 금지). 줄 길이를 고르게."""
    words = (text or "").split()
    if not words:
        return []
    total = len(text)
    n = max(1, math.ceil(total / maxc))
    target = total / n
    lines, cur = [], ""
    for wd in words:
        cand = (cur + " " + wd).strip()
        if cur and (len(cand) > maxc or (len(cur) >= target and len(lines) < n - 1)):
            lines.append(cur)
            cur = wd
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def _t(sec: float) -> str:
    sec = max(0.0, sec)
    h, m = int(sec // 3600), int(sec % 3600 // 60)
    return f"{h}:{m:02d}:{sec % 60:05.2f}"


def _esc(s: str) -> str:
    return s.replace("\\", "＼").replace("{", "(").replace("}", ")")


def scene_ass(cues: List[Dict[str, Any]], t0: float, t1: float, chapter_label: str, cfg: Dict[str, Any]) -> str:
    W, H = int(cfg["w"]), int(cfg["h"])
    size = int(cfg["caption_size"])
    font = cfg["caption_font"]
    maxc = int(cfg["max_line_chars"])
    paper = "&H00EAF3F7"
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Cap,{font},{size},&H00262626,&H00262626,{paper},&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,80,80,{int(cfg['caption_margin'])},1\n"
            f"Style: Chap,{font},40,&H00303030,&H00303030,&H28EAF3F7,&H28EAF3F7,0,0,0,0,100,100,0,0,3,12,0,7,46,46,34,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    ev = []
    if chapter_label:
        ev.append(f"Dialogue: 1,{_t(0)},{_t(t1 - t0)},Chap,,0,0,0,,{_esc(chapter_label)}")
    for k, q in enumerate(cues):
        start = q["start"] - t0
        end = min(cues[k + 1]["start"] - t0 if k + 1 < len(cues) else t1 - t0, q["end"] - t0 + 0.6)
        lines = wrap(q["cap"], maxc)
        pages = [lines[i:i + 2] for i in range(0, len(lines), 2)] or [[]]
        weights = [max(1, sum(len(x) for x in p)) for p in pages]
        tt = start
        for p, wgt in zip(pages, weights):
            dur = (end - start) * wgt / sum(weights)
            ev.append(f"Dialogue: 0,{_t(tt)},{_t(tt + dur)},Cap,,0,0,0,,{NL.join(_esc(x) for x in p)}")
            tt += dur
    return head + "\n".join(ev) + "\n"


# ── ffmpeg ───────────────────────────────────────────────────────────────────
def ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise SystemExit("ffmpeg 이 없어요 — winget install Gyan.FFmpeg")
    return p


def _run(args: List[str], cwd: Path) -> None:
    detail("  $ ffmpeg " + " ".join(args[1:])[:400])
    r = subprocess.run(args, cwd=str(cwd), capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace")[-1500:]
        detail(err)
        raise RuntimeError(f"ffmpeg 실패: {err[-300:]}")


def kenburns(n: int, frames: int) -> tuple:
    """씬 번호로 정해지는 느린 움직임(줌 인 / 줌 아웃 / 오른쪽 팬 / 왼쪽 팬). zoompan 식 (z, x, y)."""
    t = f"(on/{max(1, frames - 1)})"
    v = (n - 1) % 4
    if v == 0:
        return f"1.0+0.07*{t}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if v == 1:
        return f"1.07-0.07*{t}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if v == 2:
        return "1.06", f"(iw-iw/zoom)*{t}", "ih/2-(ih/zoom/2)"
    return "1.06", f"(iw-iw/zoom)*(1-{t})", "ih/2-(ih/zoom/2)"


def render_clip(work: Path, out: Path, img: Path | None, n: int, frames: int, ass: str, cfg: Dict[str, Any]) -> None:
    W, H, IH, fps = int(cfg["w"]), int(cfg["h"]), int(cfg["image_h"]), int(cfg["fps"])
    dur = frames / fps
    fade = min(float(cfg["fade"]), dur / 3)
    (work / "scene.ass").write_text(ass, encoding="utf-8-sig")
    args = [ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", "paper.png"]
    if img and img.exists():
        z, x, y = kenburns(n, frames)
        up_w, up_h = int(W * 1.5), int(IH * 1.5)
        args += ["-i", str(img), "-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}", "-i", f"mask_{W}x{IH}.png"]
        fc = (f"[1:v]scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
              f"crop={up_w}:{up_h}:(iw-{up_w})/2:(ih-{up_h})*0.3,setsar=1,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={W}x{IH}:fps={fps},format=rgba[img];"
              f"[2:v]format=gray,scale={W}:{IH}[m];[img][m]alphamerge,"
              f"fade=t=in:st=0:d={fade:.2f}:alpha=1,fade=t=out:st={max(0.0, dur - fade * 0.7):.2f}:d={fade * 0.7:.2f}:alpha=1[fg];"
              f"[0:v][fg]overlay=0:0:shortest=1,subtitles=scene.ass:fontsdir=fonts,format=yuv420p[v]")
    else:
        fc = "[0:v]subtitles=scene.ass:fontsdir=fonts,format=yuv420p[v]"
    args += ["-filter_complex", fc, "-map", "[v]", "-frames:v", str(frames), "-r", str(fps),
             "-c:v", "libx264", "-preset", str(cfg["preset"]), "-crf", str(cfg["crf"]), "-pix_fmt", "yuv420p",
             "-video_track_timescale", "15360", out.name]
    _run(args, work)
    shutil.move(str(work / out.name), str(out))


def _h(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:16]


def contact_sheets(video: Path, out_dir: Path, every: float = 5.0) -> List[Path]:
    for old in out_dir.glob("contact_*.jpg"):
        old.unlink()
    _run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", video.name,
          "-vf", f"fps=1/{every},scale=384:-2,tile=5x8:padding=4:margin=8:color=white", "-q:v", "4",
          "contact_%02d.jpg"], out_dir)
    return sorted(out_dir.glob("contact_*.jpg"))


def run(job: Job, only=None, force: bool = False, **_) -> None:
    cfg = config.load()["render"]
    W, H, IH, fps = int(cfg["w"]), int(cfg["h"]), int(cfg["image_h"]), int(cfg["fps"])
    for u in job.pick(only):
        sid = u.prefix
        script = job.script(sid)
        timing = config.read_json(job.sub("audio", sid) / "timing.json")
        if not script or not timing:
            log(f"  · {sid} 대본·음성이 없어요 — 「대본 쓰기」「목소리」를 먼저 해 주세요.")
            continue
        out_dir = job.sub("out", sid)
        clips = out_dir / "_clips"
        work = out_dir / "_work"
        for d in (clips, work, work / "fonts"):
            d.mkdir(parents=True, exist_ok=True)
        for f in [*config.FONTS.glob("*.ttf"), *config.FONTS.glob("*.otf")]:
            if not (work / "fonts" / f.name).exists():
                shutil.copy2(f, work / "fonts" / f.name)
        if not (work / "paper.png").exists() or force:
            make_paper(work / "paper.png", W, H)
        if not (work / f"mask_{W}x{IH}.png").exists() or force:
            make_mask(work / f"mask_{W}x{IH}.png", W, IH)
        img_dir = job.sub("images", sid)
        ss = {s["n"]: s for s in scenes(script)}
        rows = timing["scenes"]
        chap_titles = {c["i"]: c["title"] for c in timing.get("chapters") or []}
        log(f"  {sid} 씬 {len(rows)}개를 굽는 중이에요 (그림이 바뀐 씬만) …")
        files, made = [], 0
        for k, row in enumerate(rows):
            n = row["n"]
            f0, f1 = round(row["start"] * fps), round(row["end"] * fps)
            frames = max(1, f1 - f0)
            t0, t1 = f0 / fps, f1 / fps
            cues = [q for q in timing["cues"] if q["scene"] == n]
            label = f"{row['chapter']}장 · {chap_titles.get(row['chapter'], '')}".strip(" ·")
            ass = scene_ass(cues, t0, t1, label, cfg)
            img = img_dir / f"{n:03d}.png"
            key = _h(cfg, frames, ass, img.stat().st_mtime if img.exists() else None, n)
            clip = clips / f"{n:03d}.mp4"
            stamp = clips / f"{n:03d}.key"
            if clip.exists() and stamp.exists() and stamp.read_text() == key and not force:
                files.append(clip)
                continue
            if not img.exists():
                log(f"    △ {n:03d} 그림이 없어 한지 바탕으로 구워요")
            render_clip(work, clip, img if img.exists() else None, n, frames, ass, cfg)
            stamp.write_text(key)
            files.append(clip)
            made += 1
            if made % 5 == 0 or k == len(rows) - 1:
                log(f"    … {k + 1}/{len(rows)} 씬")
        lst = work / "clips.txt"
        # ★ 목록은 work 기준 **상대 경로**로 — 폴더 이름에 작은따옴표가 있으면(「'보국안민'의」) concat 목록의
        #   'file …' 따옴표가 깨져 경로가 잘린다(2026-10-16 1-24 실패). 그래도 남는 따옴표는 concat 규칙대로 '\'' 로.
        def rel(p: Path) -> str:
            return os.path.relpath(p.resolve(), work.resolve()).replace(os.sep, "/").replace("'", "'\\''")
        lst.write_text("".join(f"file '{rel(p)}'\n" for p in files), encoding="utf-8")
        audio = (job.sub("audio", sid) / "narration.wav").resolve()
        final = out_dir / f"{sid}.mp4"
        log("  씬을 이어 붙이고 목소리를 입히는 중이에요 …")
        _run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", "clips.txt",
              "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-shortest", "-movflags", "+faststart", final.name], work)
        shutil.move(str(work / final.name), str(final))
        sheets = contact_sheets(final, out_dir)
        log(f"  ✓ {sid} 영상 완료 — {final.name} ({timing['duration'] / 60:.1f}분, 새로 구운 씬 {made}개, 검수 시트 {len(sheets)}장)")
