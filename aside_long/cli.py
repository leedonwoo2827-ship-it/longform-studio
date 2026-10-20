"""python -m aside_long <명령> …   (run.bat <명령> … 과 같다)

만들기
  new <job> --from <원고.docx|폴더> [--title 롱폼 세계사] [--style textbook|ink] [--voice F4] [--minutes 15]
  all       --job J [--only 1-1,…] [--force]     1→7: 안전 개작 → 대본·채점 → 목소리 → 그림 → 영상 → 썸네일 → 유튜브 문구
  make      --job J [--only 1-1]                  4→7: 그림 → 영상 → 썸네일 → 유튜브 문구 (목소리 없으면 먼저)
  prep      --job J [--only 1-1,…]                1→3: 개작 → 대본·채점·유튜브 문구 → 목소리 (그림 크레딧 안 씀)
  s0-rewrite | s1-script | s2-tts | s3-images [--scenes 3,7] | s4-render | s5-thumb | yt-meta   (단계별 1~7)
올리기 (YouTube Studio — 실제 Chrome 전용 프로필)
  account add <이름> [--label @채널] [--profile <Chrome 프로필 폴더>] [--port 9361] | account list | account use <이름> | account rm <이름>
  login   [--account A]                 Chrome 에서 Google 로그인·채널 선택(한 번)
  yt-meta --job J [--only 1-1] [--force]  7단계 유튜브 문구(제목·설명·목차·해시태그·태그·고정 댓글)
  post    --job J --only 1-1 [--account A] [--at "2026-10-12 19:00"] [--visibility public|unlisted|private] [--dry-run] [--force]
  probe   [--account A]                 업로드 창 구조 떠 두기(셀렉터 맞출 때)
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

STAGES = ["s0-rewrite", "s1-script", "s2-tts", "s3-images", "s4-render", "s5-thumb", "yt-meta"]
MAKE = STAGES[2:]      # 4→7 (목소리는 이미 있으면 캐시로 바로 지나간다)
STEP_NAMES = {"s0-rewrite": "1/7 안전 개작", "s1-script": "2/7 대본·채점", "s2-tts": "3/7 목소리",
              "s3-images": "4/7 그림(Codex)", "s4-render": "5/7 영상 굽기", "s5-thumb": "6/7 썸네일",
              "yt-meta": "7/7 유튜브 문구"}


def _only(v: Optional[str]) -> Optional[List[str]]:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def run_stage(name: str, job: Job, **kw) -> None:
    from . import s0_rewrite, s1_script, s2_tts, s3_images, s4_render, s5_thumb, ytmeta
    mod = {"s0-rewrite": s0_rewrite, "s1-script": s1_script, "s2-tts": s2_tts, "s3-images": s3_images,
           "s4-render": s4_render, "s5-thumb": s5_thumb, "yt-meta": ytmeta}[name]
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


def cmd_account(a) -> None:
    from . import accounts
    if a.action == "add":
        acc = accounts.add(a.name, a.label or "", a.profile or "", a.port or 0)
        log(f"계정 추가: {acc['name']} (포트 {acc['port']}) — 이제 `run.bat login --account {acc['name']}`")
    elif a.action == "rm":
        accounts.remove(a.name)
    elif a.action == "use":
        accounts.set_current(a.name)
    cur = config.local().get("current")
    for acc in accounts.all_():
        log(f"  {'▶' if acc['name'] == cur else ' '} {acc['name']} {acc.get('label', '')} (포트 {acc['port']})")


def ready(job: Job, only: Optional[List[str]] = None) -> List[str]:
    """영상이 있고 아직 안 올렸고 예약도 안 건 편 — 편 번호 순서."""
    st = job.state()
    out = []
    for u in job.units:
        s = st.get(u.prefix) or {}
        if job.video(u.prefix).exists() and not s.get("posted") and not (s.get("scheduled") or {}).get("when"):
            out.append(u.prefix)
    return [x for x in out if not only or x in only]


def cmd_plan(a) -> None:
    """남은 편을 패턴의 빈 시각 칸에 차례로 — --apply 면 한 편씩 Studio 예약 공개로 올린다(한 편이 실패해도 다음 편 계속)."""
    import argparse
    from . import accounts, schedule
    job = need(a.job)
    acc = accounts.get(a.account)
    left = ready(job, _only(a.only))
    if not left:
        log("올릴 편이 없어요 — 영상이 있고 아직 안 올린 편만 예약해요.")
        return
    start = schedule.parse_when(a.start) if a.start else None
    playlist = a.playlist if a.playlist is not None else (job.get("youtube.playlist") or "")
    at = a.time or (schedule.playlist_times().get(playlist) if playlist else None)
    if a.time and playlist:
        schedule.set_playlist_time(playlist, a.time)
    slots = schedule.next_slots(len(left), acc["name"], start=start, pat=a.pattern, at=at)
    log(f"{'예약 걸기' if a.apply else '미리보기'} — {len(left)}편 · {acc.get('label', acc['name'])}"
        + (f" · 재생목록 「{playlist}」" if playlist else "") + f" · {schedule.PATTERNS.get(a.pattern or schedule.DEFAULT, {}).get('label', '')}"
        + (f" · {at}" if at else ""))
    for sid, when in zip(left, slots):
        log(f"  {when:%m-%d(%a) %H:%M}  {sid}  {(job.unit(sid).section if job.unit(sid) else '')[:40]}")
    if not a.apply:
        log("미리보기예요 — 「예약 걸기」를 누르면 위 시각대로 한 편씩 올려요.")
        return
    fails: List[str] = []
    for sid, when in zip(left, slots):
        ok = _guard(f"{sid} 예약 업로드", lambda sid=sid, when=when: cmd_post(argparse.Namespace(
            job=job.name, only=sid, account=acc["name"], at=when.strftime(schedule.FMT), visibility="public",
            dry_run=False, force=False, playlist=playlist)))
        if not ok:
            fails.append(sid)
    if fails:
        log(f"예약 {len(left) - len(fails)}편 완료 · 실패 {len(fails)}편: {', '.join(fails)} — 다시 누르면 남은 것만 해요")
        raise SystemExit("일부 편을 예약하지 못했어요")
    log(f"✓ {len(left)}편 모두 예약 공개로 올렸어요")


def cmd_post(a) -> None:
    from . import accounts, youtube
    from datetime import datetime
    job = need(a.job)
    acc = accounts.get(a.account)
    vis = a.visibility or config.load()["youtube"].get("visibility", "public")
    units = job.pick(_only(a.only)) if a.only else []
    if not units:
        raise SystemExit("--only 로 올릴 단원 하나를 고르세요")
    for u in units:
        sid = u.prefix
        st = job.state().get(sid) or {}
        if st.get("posted") and not a.force and not a.dry_run:
            log(f"· {sid} 는 이미 올렸어요 ({(st['posted'] or {}).get('url', '')}). 다시 올리려면 --force")
            continue
        script = job.script(sid)
        video = job.video(sid)
        if not script or not video.exists():
            raise SystemExit(f"{sid}: 대본이나 영상이 없습니다 — 먼저 영상을 만들어 주세요")
        when = None
        if a.at:
            when = datetime.strptime(a.at.strip().replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        timing = config.read_json(job.sub("audio", sid) / "timing.json")
        thumb = job.sub("out", sid) / f"{sid}_thumb.jpg"
        log(f"▶ {sid} " + ("미리 채워 보기" if a.dry_run else f"예약 공개({when:%m월 %d일 %H:%M})" if when else f"올리기({vis})")
            + f" → {acc.get('label', acc['name'])}")
        try:
            playlist = getattr(a, "playlist", None)
            playlist = playlist if playlist is not None else (job.get("youtube.playlist") or "")
            res = youtube.upload(acc, script, video, timing=timing, thumb=thumb, when=when,
                                 visibility=vis, dry_run=a.dry_run, playlist=playlist)
        except youtube.PostError as e:
            job.update_state(sid, {"error": str(e)[:300]})
            raise SystemExit(f"✗ 올리지 못했어요: {e}")
        if res.get("dry_run"):
            continue
        rec = {"account": acc["name"], **res}
        patch = {"posted": rec, "error": None}
        if when:
            patch["scheduled"] = {"when": when.strftime("%Y-%m-%d %H:%M"), "account": acc["name"], "mode": "native"}
        job.update_state(sid, patch)
        log(f"✓ 올렸어요 — {res.get('url') or 'Studio 「콘텐츠」에서 확인'}")
        if external(u) and res.get("url") and not when:    # 해설북 단원: 바로 이북 연동(예약이면 공개 전엔 재생이 안 되니 나중에)
            try:
                cmd_ebook_link(Job(job.name), [sid])
            except (Exception, SystemExit) as e:      # noqa: BLE001 — 업로드는 이미 됐다
                log(f"  △ 이북 연동은 못 했어요({e}) — 「📖 이북 연동」을 눌러 주세요")


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
    p = sub.add_parser("account")
    p.add_argument("action", choices=["add", "list", "use", "rm"])
    p.add_argument("name", nargs="?")
    p.add_argument("--label")
    p.add_argument("--profile")
    p.add_argument("--port", type=int)
    for name in ("login", "probe"):
        p = sub.add_parser(name)
        p.add_argument("--account")
    p = sub.add_parser("post")
    p.add_argument("--job")
    p.add_argument("--only")
    p.add_argument("--account")
    p.add_argument("--at")
    p.add_argument("--visibility", choices=["public", "unlisted", "private"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--playlist", help="재생목록 이름(없으면 만든다). 비우면 작업 설정 youtube.playlist")
    p = sub.add_parser("ebook-link", help="해설북에서 넣은 단원: 올린 영상을 해설북 이북에 연동")
    p.add_argument("--job")
    p.add_argument("--only")
    p = sub.add_parser("plan")
    p.add_argument("--job")
    p.add_argument("--only", help="올릴 편(쉼표). 비우면 영상이 있고 아직 안 올린 것 전부")
    p.add_argument("--account")
    p.add_argument("--pattern", help="예약 패턴: " + ", ".join(__import__("aside_long.schedule", fromlist=["x"]).PATTERNS))
    p.add_argument("--start", help="이 날짜부터(2026-10-22)")
    p.add_argument("--time", help="올릴 시각 HH:MM — 비우면 재생목록별로 정해 둔 시각(playlists.json), 없으면 패턴 시각")
    p.add_argument("--playlist")
    p.add_argument("--apply", action="store_true", help="없으면 미리보기만")
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
    if a.cmd == "account":
        cmd_account(a)
        return 0
    if a.cmd in ("login", "probe"):
        from . import accounts, youtube
        acc = accounts.get(a.account)
        youtube.open_login(acc) if a.cmd == "login" else youtube.probe(acc)
        return 0
    if a.cmd == "plan":
        return 0 if _guard("한 번에 예약", lambda: cmd_plan(a)) else 1
    if a.cmd == "post":
        return 0 if _guard("올리기", lambda: cmd_post(a)) else 1
    if a.cmd == "ebook-link":
        return 0 if _guard("이북 연동", lambda: cmd_ebook_link(need(a.job), _only(a.only))) else 1
    job = need(a.job)
    only = _only(a.only)
    kw = {"only": only, "force": a.force}
    if a.cmd in STAGES and a.cmd != "yt-meta":
        ext = [u.prefix for u in job.pick(only) if external(u)]
        if ext:
            log(f"· {', '.join(ext)} 는 해설북에서 만든 단원이라 「{a.cmd}」를 건너뛰어요 (유튜브 문구·올리기만 해요)")
            rest = [u.prefix for u in job.pick(only) if not external(u)]
            if not rest:
                return 0
            only = kw["only"] = rest
    if a.cmd in STAGES:
        if a.cmd == "s3-images" and a.scenes:
            kw["scenes_only"] = [int(x) for x in a.scenes.split(",") if x.strip().isdigit()]
        return 0 if _guard(a.cmd, lambda: run_stage(a.cmd, job, **kw)) else 1
    stages = STAGES if a.cmd == "all" else STAGES[:3] if a.cmd == "prep" else MAKE
    for u in job.pick(only):
        log(f"━━ {u.prefix} {u.section}")
        if external(u):
            log("  해설북에서 만든 단원 — 영상·썸네일은 그대로 두고 유튜브 문구만 해요")
        for s in ([x for x in stages if x == "yt-meta"] if external(u) else stages):
            if not _guard(f"{u.prefix} {s}", lambda: run_stage(s, Job(job.name), only=[u.prefix], force=a.force)):
                log(f"  {u.prefix} 는 여기서 멈추고 다음 원고로 넘어가요.")
                break
    return 0


def cmd_ebook_link(job, only) -> None:
    """해설북 단원만: <해설북>/.venv 파이썬으로 `python -m studio link --job <작업>` (해설북이 공방 업로드 주소를 읽는다)"""
    import os
    import subprocess
    from pathlib import Path
    done = 0
    for u in job.pick(only):
        info = config.read_json(u.dir / "unit.json", {}) or {}
        if info.get("from") != "haeseolbook":
            log(f"· {u.prefix} 는 해설북 단원이 아니라 건너뛰어요")
            continue
        root = Path(info["root"])
        py = root / ".venv" / "Scripts" / "python.exe"
        if not py.exists():
            raise SystemExit(f"해설북 폴더를 찾지 못했어요: {root} (해설북 setup.bat 확인)")
        if not (job.state().get(u.prefix) or {}).get("posted"):
            log(f"  △ {u.prefix} 는 아직 유튜브에 안 올렸어요 — 영상 없이 교재만 미리 게시해요")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        lan = root / "server" / "config.lan.php"
        if lan.exists():
            env["HSB_CONFIG"] = str(lan)
        log(f"━━ {u.prefix} 이북 연동 (해설북 작업 {info['job']})")
        p = subprocess.run([str(py), "-m", "studio", "link", "--job", info["job"]], cwd=str(root), env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (p.stdout or "").splitlines():
            log("  " + line)
        if p.returncode != 0:
            raise SystemExit(f"이북 연동 실패: {(p.stderr or p.stdout)[-300:]}")
        done += 1
    log(f"✓ 이북 연동 {done}편")


def external(u) -> bool:
    """해설북(다른 도구)에서 완성해 넣은 단원인가 — unit.json 의 from"""
    return bool((config.read_json(u.dir / "unit.json", {}) or {}).get("from"))


if __name__ == "__main__":
    sys.exit(main())
