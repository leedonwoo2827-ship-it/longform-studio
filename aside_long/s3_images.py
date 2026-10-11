"""s3-images — 씬마다 그림 한 장을 **Codex**(ChatGPT 로그인한 codex CLI, 내장 이미지 생성)가 그린다.

참고 영상 방식 그대로: Claude 가 정한 대본·인물 고정 문장 → 인계 메모(HANDOFF.md) + 컷 프롬프트 →
Codex 를 **여러 개 동시에**(image.workers, 기본 3 = "탭 세 개") 돌려 한 장씩.
  images/HANDOFF.md      화풍 규격·금지 사항·인물 고정 문장 (styles.handoff)
  images/NNN.png         씬 번호(001…) 그림
  images/baked.json      {NNN: {hash, manual}} — 컷 프롬프트가 바뀐 그림만 다시 그린다. 사람이 바꿔 넣은 그림(manual)은 건드리지 않는다.
실패한 컷은 retries 만큼 다시 맡기고, 그래도 없으면 목록만 남긴다(영상은 빈 컷을 한지 바탕으로 굽는다).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config, styles
from .job import Job, scenes
from .log import detail, log

_LOCK = threading.Lock()
_CLAIMED: set = set()
CREDIT_RE = re.compile(r"out of credits|usage limit|rate limit|quota|hit your limit|too many requests", re.I)


class OutOfCredits(RuntimeError):
    """ChatGPT/Codex 사용량이 바닥 — 나머지 컷도 다 실패하니 바로 멈춘다."""
GEN_DIR = Path.home() / ".codex" / "generated_images"


def exe() -> Optional[str]:
    return shutil.which("codex.cmd") or shutil.which("codex")


def is_png(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(8)
        return head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] == b"RIFF"
    except OSError:
        return False


def _newest_generated(since: float) -> Optional[Path]:
    """Codex 가 저장을 빼먹었을 때 — 이 시각 이후 Codex 가 만든 그림 중 가장 새것."""
    if not GEN_DIR.exists():
        return None
    got = [p for p in GEN_DIR.rglob("*.png") if p.stat().st_mtime >= since]
    return max(got, key=lambda p: p.stat().st_mtime) if got else None


def draw(out: Path, handoff: str, cut: str, timeout: int) -> bool:
    """codex exec 한 번 = 그림 한 장. 성공하면 True."""
    path = exe()
    if not path:
        raise SystemExit("codex CLI 가 없어요 — npm i -g @openai/codex 후 codex login (ChatGPT 계정)")
    prompt = (
        "You are the illustrator. Follow the HANDOFF memo below exactly.\n"
        "Use your built-in image generation tool to create exactly ONE image for the cut below "
        "(do not draw with code, do not search the web), then copy the generated image file to "
        f"./{out.name} in the current working directory. Reply only with DONE.\n\n"
        f"{handoff}\n\n## This cut\n{cut}\n"
    )
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            [path, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write", "-C", str(out.parent), "-"],
            input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail = (proc.stdout + proc.stderr).decode("utf-8", "replace")[-600:]
        detail(f"  codex {out.name} code {proc.returncode} {time.time() - t0:.0f}초 …{tail[-200:]!r}")
        if CREDIT_RE.search(tail):
            raise OutOfCredits(tail[-200:])
    except subprocess.TimeoutExpired:
        detail(f"  codex {out.name} 시간 초과")
    if out.exists() and is_png(out) and out.stat().st_mtime >= t0 - 1:
        return True
    with _LOCK:     # 동시에 여러 개가 돌 때 남의 그림을 집지 않게 — 이미 가져간 파일은 빼고 가장 새것
        gen = _newest_generated(t0)
        if gen and gen not in _CLAIMED:
            _CLAIMED.add(gen)
            shutil.copy2(gen, out)
            detail(f"  codex {out.name}: 저장 누락 → {gen.name} 를 가져옴")
            return True
    return False


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def run(job: Job, only=None, force: bool = False, scenes_only: Optional[List[int]] = None, **_) -> None:
    cfg = config.load()["image"]
    workers = max(1, int(cfg["workers"]))
    timeout = int(cfg["timeout_sec"])
    for u in job.pick(only):
        sid = u.prefix
        script = job.script(sid)
        if not script:
            log(f"  · {sid} 대본이 없어요 — 「대본 쓰기」를 먼저 해 주세요.")
            continue
        d = job.sub("images", sid)
        d.mkdir(parents=True, exist_ok=True)
        hand = styles.handoff(job, script)
        (d / "HANDOFF.md").write_text(hand, encoding="utf-8")
        baked: Dict[str, Any] = config.read_json(d / "baked.json", {}) or {}
        todo = []
        for sc in scenes(script):
            key = f"{sc['n']:03d}"
            cut = styles.cut_prompt(job, script, sc)
            h = _hash(hand + cut)
            out = d / f"{key}.png"
            rec = baked.get(key) or {}
            if scenes_only:
                if sc["n"] in scenes_only:
                    todo.append((key, cut, h, out))
                continue
            if out.exists() and not force and (rec.get("manual") or rec.get("hash") == h):
                continue
            todo.append((key, cut, h, out))
        total = len(scenes(script))
        if not todo:
            log(f"  {sid} 그림 {total}장 모두 있어요.")
            continue
        log(f"  {sid} 그림 {len(todo)}장을 Codex {workers}개로 그려요 (장당 1~3분) …")
        fails = []

        stop = threading.Event()

        def one(item):
            key, cut, h, out = item
            for attempt in range(int(cfg["retries"]) + 1):
                if stop.is_set():
                    return key, h, False
                try:
                    if draw(out, hand, cut, timeout):
                        return key, h, True
                except OutOfCredits:
                    stop.set()
                    return key, h, False
                detail(f"  {key} 재시도 {attempt + 1}")
            return key, h, False

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(one, it) for it in todo]):
                key, h, ok = fut.result()
                if stop.is_set() and not ok:
                    fails.append(key)
                    continue
                done += 1
                if ok:
                    baked[key] = {"hash": h, "manual": False}
                    config.write_json(d / "baked.json", baked)
                    log(f"    ✓ {key} ({done}/{len(todo)})")
                else:
                    fails.append(key)
                    log(f"    ✗ {key} 못 그렸어요 ({done}/{len(todo)})")
        if stop.is_set():
            log(f"  ✗ Codex(ChatGPT) 사용량이 바닥났어요 — 그림 {len(fails)}장이 남았어요. "
                f"사용량이 다시 차면 「4 그림」을 누르면 남은 것만 이어서 그려요. (빈 컷은 한지 바탕으로 구워져요)")
        elif fails:
            log(f"  △ {sid} 그림 {len(fails)}장 실패: {', '.join(fails)} — 대시보드 「그림」에서 다시 그리거나 직접 넣어 주세요.")
        else:
            log(f"  ✓ {sid} 그림 완료 ({total}장)")
