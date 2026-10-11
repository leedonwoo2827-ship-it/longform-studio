"""s5-thumb — 썸네일 1280×720: 대표 그림(첫 씬 = 훅) + 두 줄 문구(키워드 한 단어만 강조색). ffmpeg + ASS 로 굽는다.

  out/<id>_thumb.jpg
대표 그림을 바꾸려면 job.json 의 long.thumb_scene(씬 번호)을 정하거나 대시보드에서 번호를 고른다.
"""
from __future__ import annotations

import shutil

from . import config
from .job import Job
from .log import log
from .s4_render import _esc, _run, ffmpeg

W, H = 1280, 720
KEY = "&H0000C8FF"      # 노랑(#FFC800) — ASS 는 BGR


def _line(text: str, kw: str) -> str:
    t = _esc(text)
    if kw and kw in text:
        k = _esc(kw)
        return t.replace(k, f"{{\\c{KEY}}}{k}{{\\c&H00FFFFFF}}", 1)
    return t


def run(job: Job, only=None, **_) -> None:
    for u in job.pick(only):
        sid = u.prefix
        script = job.script(sid)
        if not script:
            continue
        th = script.get("thumbnail") or {}
        n = int(job.get("long.thumb_scene") or 1)
        img = job.sub("images", sid) / f"{n:03d}.png"
        if not img.exists():
            log(f"  · {sid} 썸네일용 그림({n:03d})이 아직 없어요.")
            continue
        out_dir = job.sub("out", sid)
        work = out_dir / "_work"
        (work / "fonts").mkdir(parents=True, exist_ok=True)
        for f in [*config.FONTS.glob("*.ttf"), *config.FONTS.glob("*.otf")]:
            if not (work / "fonts" / f.name).exists():
                shutil.copy2(f, work / "fonts" / f.name)
        kw = th.get("keyword", "")
        ass = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\nWrapStyle: 2\n\n"
               "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
               "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
               "Alignment, MarginL, MarginR, MarginV, Encoding\n"
               "Style: T,Black Han Sans,118,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,9,4,1,60,60,52,1\n\n"
               "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               f"Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{_line(th.get('line1', ''), kw)}\\N{_line(th.get('line2', ''), kw)}\n")
        (work / "thumb.ass").write_text(ass, encoding="utf-8-sig")
        name = f"{sid}_thumb.jpg"
        _run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(img.resolve()),
              "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                     f"drawbox=x=0:y={H // 2}:w={W}:h={H // 2}:color=black@0.25:t=fill,"
                     "subtitles=thumb.ass:fontsdir=fonts",
              "-frames:v", "1", "-q:v", "2", name], work)
        shutil.move(str(work / name), str(out_dir / name))
        log(f"  ✓ {sid} 썸네일 — {th.get('line1', '')} / {th.get('line2', '')}")
