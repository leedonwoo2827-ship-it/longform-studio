"""python -m aside_long <명령> …   (run.bat <명령> … 과 같다)

만들기
  new <job> --from <원고.docx|폴더> [--title 롱폼 세계사] [--style textbook|ink] [--voice F4] [--minutes 15]
  all       --job J [--only 1-1,…] [--force]     원고 하나를 끝까지: 안전 개작 → 대본·채점 → 목소리 → 그림 → 영상 → 썸네일
  make      --job J [--only 1-1]                  대본 확인 뒤 딸깍: 목소리 → 그림 → 영상 → 썸네일
  prep      --job J                               모든 단원 개작·대본·채점만(그림 크레딧 아끼기)
  s0-rewrite | s1-script | s2-tts | s3-images [--scenes 3,7] | s4-render | s5-thumb   (단계별)
기타
  ui        대시보드 서버(브라우저 창)
  tts-setup SuperTonic3 모델·목소리 받기(쇼츠 프로젝트에 이미 있으면 그것을 같이 쓴다)
  doctor    Claude·Codex·SuperTonic3·ffmpeg 점검
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import config, styles
from .job import Job, need
from .log import detail, log

STAGES = ["s0-rewrite", "s1-script", "s2-tts", "s3-images", "s4-render", "s5-thumb"]
MAKE = STAGES[2:]
STEP_NAMES = {"s0-rewrite": "1/6 안전 개작", "s1-script": "2/6 대본·채점", "s2-tts": "3/6 목소리",
              "s3-images": "4/6 그림(Codex)", "s4-render": "5/6 영상 굽기", "s5-thumb": "6/6 썸네일"}


def _only(v: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def run_stage(name: str, job: Job, **kw) -> None:
    from . import s0_rewrite, s1_script, s2_tts, s3_images, s4_render, s5_thumb
    mod = {"s0-rewrite": s0_rewrite, "s1-script": s1_script, "s2-tts": s2_tts, "s3-images": s3_images,
           "s4-render": s4_render, "s5-thumb": s5_thumb}[name]
    log(f"── {STEP_NAMES.get(name, name)}")
    detail(f"stage {name} [{job.name}] {kw}")
    mod.run(job, **kw)


def cmd_new(a) -> None:
    from .manuscript import classify_sources, place_book
    if not a.src:
        raise SystemExit("--from <원고.docx 또는 폴더> 를 주세요")
    items = classify_sources(Path(a.src))
    if a.rewritten:
        items = [{**i, "stage": "rewrite"} for i in items]
    if not items:
        raise SystemExit(f"원고(.docx)를 찾지 못했습니다: {a.src}")
    if a.style and a.style not in styles.names():
        raise SystemExit(f"화풍이 없습니다: {a.style} (있는 것: {', '.join(styles.names())})")
    job = Job(a.name)
    job.dir.mkdir(parents=True, exist_ok=True)
    meta = config.read_json(job.dir / "job.json", {}) or {}
    meta["title"] = a.title or meta.get("title") or a.name
    if a.style:
        meta.setdefault("long", {})["style"] = a.style
    if a.minutes:
        meta.setdefault("long", {})["target_minutes"] = a.minutes
    if a.voice:
        meta.setdefault("tts", {})["voice"] = a.voice.upper()
    config.write_json(job.dir / "job.json", meta)
    for it in items:
        for out in place_book(job.dir, it["file"], it["stage"]):
            log(f"  {'원본  ' if it['stage'] == 'raw' else '개작본'} → {out.parent.parent.name}/{out.parent.name}/")
    log(f"단원 {len(Job(a.name).units)}개 준비 완료.")


def friendly(e: BaseException) -> str:
    from .llm.claude_cli import ClaudeError, ClaudeNotLoggedIn
    from .tts import TTSUnavailable
    if isinstance(e, SystemExit) and not isinstance(e.code, int):
        return str(e.code)
    if isinstance(e, ClaudeNotLoggedIn):
        return "✗ Claude 로그인이 필요해요 (터미널에서 claude auth login)."
    if isinstance(e, ClaudeError):
        return f"✗ Claude 가 일을 마치지 못했어요 — 같은 버튼을 한 번 더 눌러 주세요. ({str(e)[:120]})"
    if isinstance(e, TTSUnavailable):
        return f"✗ 목소리를 만들지 못했어요: {str(e)[:160]}"
    return f"✗ 문제가 생겨서 멈췄어요: {str(e)[:200]} (자세한 내용은 logs/detail.log)"


def _guard(what: str, fn) -> bool:
    try:
        fn()
        return True
    except KeyboardInterrupt:
        raise
    except BaseException as e:      # noqa: BLE001
        import traceback
        detail(f"{what} 실패: {traceback.format_exc()}")
        log(friendly(e))
        return False


def cmd_doctor(_a=None) -> int:
    from . import s3_images, tts
    from .llm import claude_cli
    ok = True
    st = claude_cli.auth_status()
    log(f"Claude CLI: {'있음' if st['installed'] else '없음'} · 로그인 {'됨 ' + st['email'] if st['logged_in'] else '안 됨'}")
    ok &= bool(st["logged_in"])
    cx = s3_images.exe()
    log(f"Codex CLI: {cx or '없음 — npm i -g @openai/codex 후 codex login'}")
    ok &= bool(cx)
    log(f"SuperTonic3: {'준비됨' if tts.available() else '없음 — run.bat tts-setup'} ({config.supertonic_dir()})")
    ok &= tts.available()
    ff = shutil.which("ffmpeg")
    log(f"ffmpeg: {ff or '없음 — winget install Gyan.FFmpeg'}")
    ok &= bool(ff)
    log("✓ 모두 준비됐어요" if ok else "△ 빠진 것을 먼저 준비해 주세요")
    return 0 if ok else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="aside_long", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("new")
    p.add_argument("name")
    p.add_argument("--from", dest="src")
    p.add_argument("--rewritten", action="store_true", help="이미 개작한 원고(02_rewrite 로)")
    p.add_argument("--title")
    p.add_argument("--style")
    p.add_argument("--voice")
    p.add_argument("--minutes", type=float)
    for name in ["all", "make", "prep", *STAGES]:
        p = sub.add_parser(name)
        p.add_argument("--job")
        p.add_argument("--only")
        p.add_argument("--force", action="store_true")
        if name == "s3-images":
            p.add_argument("--scenes", help="다시 그릴 씬 번호(예: 3,7)")
    sub.add_parser("ui")
    sub.add_parser("tts-setup")
    sub.add_parser("doctor")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    if a.cmd == "new":
        cmd_new(a)
        return 0
    if a.cmd == "ui":
        from .web.server import serve
        serve()
        return 0
    if a.cmd == "tts-setup":
        from .supertonic import engine as st
        if not st.missing():
            log(f"SuperTonic3 는 이미 있어요 ({config.supertonic_dir()})")
            return 0
        st.download(log=log)
        return 0
    if a.cmd == "doctor":
        return cmd_doctor()
    job = need(a.job)
    only = _only(a.only)
    kw = {"only": only, "force": a.force}
    if a.cmd in STAGES:
        if a.cmd == "s3-images" and a.scenes:
            kw["scenes_only"] = [int(x) for x in a.scenes.split(",") if x.strip().isdigit()]
        return 0 if _guard(a.cmd, lambda: run_stage(a.cmd, job, **kw)) else 1
    stages = STAGES if a.cmd == "all" else STAGES[:2] if a.cmd == "prep" else MAKE
    for u in job.pick(only):
        log(f"━━ {u.prefix} {u.section}")
        for s in stages:
            if not _guard(f"{u.prefix} {s}", lambda: run_stage(s, Job(job.name), only=[u.prefix], force=a.force)):
                log(f"  {u.prefix} 는 여기서 멈추고 다음 원고로 넘어가요.")
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
