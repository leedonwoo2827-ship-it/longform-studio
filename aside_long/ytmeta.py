"""유튜브 업로드 문구 — 제목·후크 2줄·소개 문단·해시태그·태그·고정 댓글 (목차 타임스탬프는 youtube.meta 가 timing 으로 붙인다).

설명 소개 글은 **원고와 전혀 다른 글**이어야 한다 — 원본(01_raw)·개작본(02_rewrite)과 비교해 검사하고, 걸리면 다시 쓰게 한다:
  · 띄어쓰기 빼고 15자 넘게 같은 구간 없음 · 문단 유사도 0.5 이하
결과는 대본 JSON 의 `youtube` 칸(사람이 고친 .edit.json 이 있으면 거기에도). s1 이 대본을 쓴 뒤 부르고, 따로: `run.bat yt-meta`.
"""
from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Dict, List

from . import config
from .job import Job, scenes
from .llm import claude_cli
from .log import detail, log
from .manuscript import read_text
from .s0_rewrite import MAX_RUN, SIM_MAX, longest_run

PROMPT = config.ROOT / "aside_long" / "prompts" / "ytmeta.md"
_STR = {"type": "string"}
_ARR = {"type": "array", "items": _STR}
SCHEMA = {"type": "object",
          "required": ["title", "hook_lines", "summary", "desc_tags", "tags", "pinned_comment"],
          "properties": {"title": _STR, "hook_lines": _ARR, "summary": _ARR, "desc_tags": _ARR,
                         "tags": _ARR, "pinned_comment": _STR}}


def check(it: Dict[str, Any], sources: List[str]) -> List[str]:
    probs = []
    paras = [p for s in sources for p in s.splitlines() if len(p.strip()) > 20]
    for k, para in enumerate([*(it.get("hook_lines") or []), *(it.get("summary") or [])], 1):
        for src in sources:
            n, frag = longest_run(para, src)
            if n > MAX_RUN:
                probs.append(f"{k}번째 문장/문단이 원고와 {n}자 겹침: 「{frag[:40]}」 — 완전히 다른 표현으로")
                break
        sim = max((SequenceMatcher(None, para, p).ratio() for p in paras), default=0)
        if sim > SIM_MAX:
            probs.append(f"{k}번째 문단이 원고 문단과 비슷함(유사도 {sim:.2f}) — 문장 구조부터 새로")
    if len(it.get("hook_lines") or []) < 2:
        probs.append("hook_lines 는 2줄")
    return probs


def write(job: Job, u, model: str) -> bool:
    sid = u.prefix
    script = job.script(sid)
    if not script:
        return False
    sources = [read_text(p) for p in (u.raw, u.rewrite) if p]
    says = " ".join(ln.get("say", "") for sc in scenes(script) for ln in sc.get("lines") or [])
    prompt = (f"[시리즈] {job.get('title', '')}\n[단원] {u.section}\n[영상 제목 후보] {script.get('title', '')}\n"
              f"[썸네일 문구] {(script.get('thumbnail') or {}).get('line1', '')} / {(script.get('thumbnail') or {}).get('line2', '')}\n"
              f"[장] " + " · ".join(c.get("title", "") for c in script.get("chapters") or []) + "\n\n"
              f"[원고 — 이 문장들을 옮기지 말 것]\n{sources[-1] if sources else ''}\n\n[대본 내레이션]\n{says[:6000]}")
    system = PROMPT.read_text(encoding="utf-8")
    log(f"「{u.section}」 유튜브 문구(제목·설명·해시태그·태그·고정 댓글)를 쓰는 중이에요 …")
    it = claude_cli.structured(prompt, system, SCHEMA, model=model)
    probs = check(it, sources)
    if probs:
        log(f"  원고와 겹치는 곳이 {len(probs)}군데 — 다시 맡겨요 …")
        detail(f"  ytmeta 문제: {probs}")
        it = claude_cli.structured(prompt + "\n\n[앞선 결과의 문제 — 모두 고쳐서 전체를 다시]\n- " + "\n- ".join(probs),
                                   system, SCHEMA, model=model)
        probs = check(it, sources)
    it["check"] = {"passed": not probs, "problems": probs, "max_run": MAX_RUN, "max_sim": SIM_MAX}
    for path in (job.script_path(sid), job.edit_path(sid)):
        data = config.read_json(path)
        if data:
            data["youtube"] = {**(data.get("youtube") or {}), **it}
            config.write_json(path, data)
    write_txt(job, sid)
    log(f"  {'✓' if not probs else '△'} {sid} 유튜브 문구 — {it.get('title', '')}" + (f" (겹침 {len(probs)}건 남음)" if probs else ""))
    return not probs


def write_txt(job: Job, sid: str) -> None:
    """업로드 때 쓰는 것과 똑같은 문구(목차 포함)를 out/<id>_youtube.txt 로."""
    from .youtube import meta
    m = meta(job.script(sid), config.read_json(job.sub("audio", sid) / "timing.json"))
    out = job.sub("out", sid)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{sid}_youtube.txt").write_text(
        f"[제목]\n{m['title']}\n\n[설명]\n{m['description']}\n\n[태그]\n{', '.join(m['tags'])}\n\n[고정 댓글]\n{m['pinned_comment']}\n",
        encoding="utf-8")


def run(job: Job, only=None, force: bool = False, **_) -> None:
    model = config.load()["claude"]["script_model"]
    for u in job.pick(only):
        sc = job.script(u.prefix)
        if not sc:
            log(f"  · {u.prefix} 대본이 없어요.")
            continue
        if not force and "hook_lines" in (sc.get("youtube") or {}):
            write_txt(job, u.prefix)        # 문구는 그대로, 목차(영상의 실제 장 시작 시각)만 새로 붙여 파일을 다시 쓴다
            log(f"  ✓ {u.prefix} 유튜브 문구 — 있던 문구에 목차를 붙여 out/{u.prefix}_youtube.txt 로 (새로 쓰려면 「이미 된 것도 다시」)")
            continue
        write(job, u, model)
