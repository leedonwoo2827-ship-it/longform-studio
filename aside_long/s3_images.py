"""s3-images — 씬마다 그림 한 장. **이미지 3장을 동시에**(image.workers) 그린다.

엔진(image.engine)
  codex-api  (기본) codex login 토큰으로 Codex 백엔드에 **그림 요청만** 보낸다(codex_api.py — backplate onlycodex 방식).
             장당 비용이 작아 같은 5시간 한도로 훨씬 많이 그린다.
  codex-cli  (예비) 장마다 `codex exec` 작업 대화 — 메모 읽기·그리기·셸로 파일 복사. 장당 1.5만~3.7만 토큰(2026-10-12 실측).

  images/prompts.json   이미지 JSON(backplate 형식) — {style_hint, aspect, size, negative, prompts:[{n, file, chars, prompt, auto}]}
                        **사람이 열어 prompt 를 고쳐도 된다** — 대본이 그대로면 고친 것이 남고, 고친 컷만 다시 그린다.
  images/NNN.png        씬 번호 그림
  images/baked.json     {NNN: {phash, manual}} — 그린 때의 프롬프트. 바뀐 컷만 다시. 사람이 바꿔 넣은 그림(manual)은 안 건드린다.
  images/HANDOFF.md     사람이 보는 화풍·인물 설명(codex-cli 엔진은 이것도 읽힌다)

한도(5시간 창)에 걸리면 배치를 바로 멈추고(남은 장을 헛되이 재시도하지 않는다), image.wait_on_limit 이면
리셋 시각까지 기다렸다 남은 것만 이어 그린다 — 밤새 무인 진행. 대시보드 「■ 멈춤」으로 언제든 중단.
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
GEN_DIR = Path.home() / ".codex" / "generated_images"


class OutOfCredits(RuntimeError):
    """사용 한도 소진 — 나머지 컷도 다 실패하니 바로 멈춘다. resets_at(epoch)을 알면 그때 풀린다."""

    def __init__(self, msg: str, resets_at: Optional[float] = None):
        super().__init__(msg)
        self.resets_at = resets_at


def exe() -> Optional[str]:
    return shutil.which("codex.cmd") or shutil.which("codex")


def is_png(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            head = f.read(8)
        return head.startswith(b"\x89PNG") or head[:3] == b"\xff\xd8\xff" or head[:4] == b"RIFF"
    except OSError:
        return False


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


# ── 이미지 JSON ───────────────────────────────────────────────────────────────
def build_prompts(job: Job, sid: str, script: Dict[str, Any]) -> Dict[str, Any]:
    """대본 → images/prompts.json. 이미 있으면 사람이 고친 prompt 는 남긴다(그 컷의 자동 프롬프트가 그대로일 때)."""
    d = job.sub("images", sid)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "prompts.json"
    old = {int(p["n"]): p for p in (config.read_json(path, {}) or {}).get("prompts") or [] if "n" in p}
    items = []
    for sc in scenes(script):
        auto = styles.cut_prompt(job, script, sc)
        o = old.get(sc["n"])
        keep = o and o.get("auto") == _hash(auto) and o.get("prompt")
        items.append({"n": sc["n"], "file": f"{sc['n']:03d}.png", "chapter": sc["chapter"],
                      "chars": (sc.get("image") or {}).get("chars") or [],
                      "prompt": o["prompt"] if keep else auto, "auto": _hash(auto),
                      **({"edited": True} if keep and o["prompt"] != auto else {})})
    data = {"title": script.get("title", ""), "style": styles.preset(job)["name"],
            "style_hint": styles.preset(job)["style"], "aspect": "16:9",
            "size": config.load()["image"].get("size") or "1536x864", "negative": styles.NEGATIVE,
            "note": "prompt 를 고쳐 저장하면 그 컷만 다시 그려요(대본을 다시 쓰면 그 컷은 자동 프롬프트로 돌아가요).",
            "prompts": items}
    config.write_json(path, data)
    styles.write_handoff(job, sid, script)
    return data


def final_prompt(data: Dict[str, Any], item: Dict[str, Any]) -> str:
    """backplate compose_prompt — style_hint 가 없으면 붙이고, negative 는 「Avoid: …」로."""
    p = item["prompt"].strip()
    hint = (data.get("style_hint") or "").strip()
    if hint and hint not in p:
        p += "\nArt style: " + hint
    neg = (item.get("negative") or data.get("negative") or "").strip()
    if neg:
        p += f"\nAvoid: {neg}."
    return p


# ── 엔진: codex-api ───────────────────────────────────────────────────────────
def draw_api(out: Path, prompt: str, size: str) -> None:
    from . import codex_api
    try:
        img = codex_api.generate(prompt, size)
    except codex_api.UsageLimit as e:
        raise OutOfCredits(str(e), e.resets_at)
    tmp = out.with_suffix(".part")
    tmp.write_bytes(img)
    os.replace(tmp, out)


# ── 엔진: codex-cli (예비) ────────────────────────────────────────────────────
def _newest_generated(since: float) -> Optional[Path]:
    if not GEN_DIR.exists():
        return None
    got = [p for p in GEN_DIR.rglob("*.png") if p.stat().st_mtime >= since]
    return max(got, key=lambda p: p.stat().st_mtime) if got else None


def draw_cli(out: Path, handoff: str, cut: str, timeout: int) -> bool:
    """codex exec 한 번 = 그림 한 장. 성공하면 True."""
    path = exe()
    if not path:
        raise SystemExit("codex CLI 가 없어요 — npm i -g @openai/codex 후 codex login (ChatGPT 계정)")
    prompt = ("You are the illustrator. Follow the HANDOFF memo below exactly.\n"
              "Use your built-in image generation tool to create exactly ONE image for the cut below "
              "(do not draw with code, do not search the web), then copy the generated image file to "
              f"./{out.name} in the current working directory. Reply only with DONE.\n\n"
              f"{handoff}\n\n## This cut\n{cut}\n")
    t0 = time.time()
    try:
        proc = subprocess.run(
            [path, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "workspace-write", "-C", str(out.parent), "-"],
            input=prompt.encode("utf-8"), capture_output=True, timeout=timeout,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tail = (proc.stdout + proc.stderr).decode("utf-8", "replace")[-600:]
        detail(f"  codex-cli {out.name} code {proc.returncode} {time.time() - t0:.0f}초 …{tail[-200:]!r}")
        if CREDIT_RE.search(tail):
            raise OutOfCredits(tail[-200:])
    except subprocess.TimeoutExpired:
        detail(f"  codex-cli {out.name} 시간 초과")
    if out.exists() and is_png(out) and out.stat().st_mtime >= t0 - 1:
        return True
    with _LOCK:     # 동시에 여러 개가 돌 때 남의 그림을 집지 않게
        gen = _newest_generated(t0)
        if gen and gen not in _CLAIMED:
            _CLAIMED.add(gen)
            shutil.copy2(gen, out)
            return True
    return False


# ── 한도 ──────────────────────────────────────────────────────────────────────
def _quota_used() -> Optional[float]:
    from . import codex_api
    return (codex_api.limits() or {}).get("used_5h")


def _wait_reset(resets_at: Optional[float], max_hours: float) -> bool:
    """리셋 시각까지 기다린다(모르면 30분마다 한도를 확인). 풀리면 True. 「■ 멈춤」은 프로세스를 죽이므로 따로 볼 것 없다."""
    from . import codex_api
    end = time.time() + max_hours * 3600
    if resets_at and resets_at > time.time():
        target = resets_at + 60
        log(f"  ⏳ Codex 한도가 {time.strftime('%H:%M', time.localtime(resets_at))} 에 풀려요 — 그때까지 기다렸다 이어 그려요 "
            f"(「■ 멈춤」으로 그만둘 수 있어요)")
        while time.time() < min(target, end):
            left = target - time.time()
            time.sleep(min(600, max(1, left)))
            if left > 600:
                log(f"    … 기다리는 중 ({int(left // 60)}분 남음)")
        return True
    log("  ⏳ Codex 한도 — 풀리는 시각을 몰라 30분마다 확인해요 (「■ 멈춤」으로 그만둘 수 있어요)")
    while time.time() < end:
        time.sleep(1800)
        try:
            lim = codex_api.probe()
            if (lim.get("used_5h") or 0) < 95:
                return True
            log(f"    … 아직이에요 — {codex_api.limits_line()}")
        except Exception as e:      # noqa: BLE001
            detail(f"  한도 확인 실패: {e}")
    return False


# ── 실행 ──────────────────────────────────────────────────────────────────────
def _todo(job: Job, sid: str, data: Dict[str, Any], baked: Dict[str, Any], force: bool,
          scenes_only: Optional[List[int]]) -> List[Dict[str, Any]]:
    d = job.sub("images", sid)
    out = []
    for it in data["prompts"]:
        key = f"{it['n']:03d}"
        png = d / it["file"]
        ph = _hash(final_prompt(data, it) + data["size"])
        rec = baked.get(key) or {}
        if scenes_only:
            if it["n"] in scenes_only:
                out.append({**it, "key": key, "png": png, "phash": ph})
            continue
        if png.exists() and not force:
            if rec.get("manual"):
                continue
            if "phash" not in rec:          # 예전 엔진으로 그린 그림 — 지금 프롬프트로 그린 것으로 치고 남긴다
                baked[key] = {**rec, "phash": ph}
                continue
            if rec["phash"] == ph:
                continue
        out.append({**it, "key": key, "png": png, "phash": ph})
    return out


def run(job: Job, only=None, force: bool = False, scenes_only: Optional[List[int]] = None, **_) -> None:
    cfg = config.load()["image"]
    engine = str(cfg.get("engine") or "codex-api")
    workers = max(1, min(6, int(cfg["workers"])))
    retries = int(cfg.get("retries", 2))
    for u in job.pick(only):
        sid = u.prefix
        script = job.script(sid)
        if not script:
            log(f"  · {sid} 대본이 없어요 — 「대본 쓰기」를 먼저 해 주세요.")
            continue
        d = job.sub("images", sid)
        data = build_prompts(job, sid, script)
        hand = (d / "HANDOFF.md").read_text(encoding="utf-8")
        baked: Dict[str, Any] = config.read_json(d / "baked.json", {}) or {}
        total = len(data["prompts"])
        first_used = None
        if engine == "codex-api":
            try:                                    # 시작 전 한도 — 이 편이 쓴 양을 재려고(그림 없는 아주 짧은 요청)
                from . import codex_api
                first_used = (codex_api.probe() or {}).get("used_5h")
                if first_used is not None:
                    log(f"  {codex_api.limits_line()}")
            except Exception as e:      # noqa: BLE001
                detail(f"  한도 확인 실패(그림은 계속): {e}")
        drawn: set = set()
        while True:
            todo = [t for t in _todo(job, sid, data, baked, force, scenes_only) if t["key"] not in drawn]
            config.write_json(d / "baked.json", baked)
            if not todo:
                log(f"  {sid} 그림 {total}장 모두 있어요.")
                break
            log(f"  {sid} 그림 {len(todo)}장을 {workers}장씩 동시에 그려요 ({'Codex 직접' if engine == 'codex-api' else 'codex exec'}) …")
            stop = threading.Event()
            limit_hit: Dict[str, Any] = {}

            def one(it):
                for attempt in range(retries + 1):
                    if stop.is_set():
                        return it, False
                    try:
                        if engine == "codex-api":
                            draw_api(it["png"], final_prompt(data, it), data["size"])
                            return it, True
                        if draw_cli(it["png"], hand, final_prompt(data, it), int(cfg["timeout_sec"])):
                            return it, True
                    except OutOfCredits as e:
                        stop.set()
                        limit_hit["resets_at"] = getattr(e, "resets_at", None)
                        limit_hit["msg"] = str(e)
                        return it, False
                    except Exception as e:      # noqa: BLE001 — 한 장 실패는 재시도, 로그인·모델 문제는 바로 멈춤
                        from .codex_api import Fatal, NotAuthenticated
                        if isinstance(e, (NotAuthenticated, Fatal)):
                            stop.set()
                            limit_hit["auth" if isinstance(e, NotAuthenticated) else "fatal"] = str(e)
                            return it, False
                        detail(f"  {it['key']} 실패 {attempt + 1}: {e}")
                        time.sleep(6 * 2 ** attempt)
                return it, False

            done, fails, t0 = 0, [], time.time()
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for fut in as_completed([ex.submit(one, it) for it in todo]):
                    it, ok = fut.result()
                    if ok:
                        done += 1
                        drawn.add(it["key"])
                        baked[it["key"]] = {"phash": it["phash"], "manual": False}
                        config.write_json(d / "baked.json", baked)
                        used = _quota_used()
                        if first_used is None and used is not None:
                            first_used = used
                        log(f"    ✓ {it['key']} ({done}/{len(todo)})" + (f" · 5시간 한도 {used:.0f}% 사용" if used is not None else ""))
                    elif not stop.is_set():
                        fails.append(it["key"])
                        log(f"    ✗ {it['key']} 못 그렸어요")
            if done:
                detail(f"  s3 {sid}: {done}장 {time.time() - t0:.0f}초")
            if limit_hit.get("fatal"):
                from .codex_api import model
                log(f"  ✗ {limit_hit['fatal']}")
                raise SystemExit(f"그림 모델({model()})을 Codex 가 받지 않아요 — 설정 탭 「그림 모델」을 바꾸거나 비워 두면 "
                                 f"Codex 설정(~/.codex/config.toml)의 모델을 써요. 고친 뒤 다시 눌러 주세요.")
            if limit_hit.get("auth"):
                log(f"  ✗ {limit_hit['auth']}")
                raise SystemExit("Codex 로그인이 필요해요 — 설정 탭 「Codex 계정」에서 로그인한 뒤 「4 그림」을 다시 눌러 주세요.")
            if stop.is_set():
                left = len([t for t in _todo(job, sid, data, baked, force, scenes_only) if t["key"] not in drawn])
                log(f"  ✗ Codex 사용 한도에 걸렸어요 — {sid} 그림 {left}장 남음")
                detail(f"  한도: {limit_hit.get('msg', '')[:300]}")
                if cfg.get("wait_on_limit", True) and _wait_reset(limit_hit.get("resets_at"), float(cfg.get("max_wait_hours", 24))):
                    force = False           # 다시 돌 때는 남은 것만
                    continue
                raise SystemExit("Codex 한도가 풀리면 「4 그림」을 다시 눌러 주세요 — 남은 것만 이어서 그려요.")
            if fails:
                log(f"  △ {sid} 그림 {len(fails)}장 실패: {', '.join(fails)} — 「4 그림」을 다시 누르거나 수정 탭에서 다시 그려 주세요.")
            else:
                have = len([p for p in d.glob("[0-9][0-9][0-9].png")])
                log(f"  ✓ {sid} 그림 {done}장 그렸어요 — 전체 {have}/{total}장" + (" 완성" if have >= total else ""))
            break
        used = _quota_used()
        if used is not None:
            from . import codex_api
            log(f"  {codex_api.limits_line()}" + (f" · 이 편 그림이 쓴 5시간 한도 약 {max(0, used - first_used):.0f}%p" if first_used is not None else ""))
