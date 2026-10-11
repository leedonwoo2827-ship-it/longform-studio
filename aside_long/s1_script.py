"""s1-script — 개작본(02_rewrite)으로 Claude 가 롱폼 대본을 쓰고, 다른 Claude 가 100점 채점한다.

  03_script/<id>.json    장(chapters) → 씬(scenes: 그림 한 장 + 문장들) · 인물 고정 문장 · 썸네일·유튜브 문구
  03_script/채점.json     점수·항목·고칠 점 + 원문 겹침 검사 결과
흐름: 대본 → 원문 겹침 검사(15자) + 채점 → (score_min 미만이거나 겹침이 있으면) 문제를 붙여 한 번 고쳐 쓰기 → 다시 채점.
캐시: _s1.json 에 개작본 해시를 적어 두고, 개작본이 그대로면 건너뛴다(--force 로 다시).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from . import config, styles
from .job import Job, scenes
from .llm import claude_cli
from .log import detail, log
from .s0_rewrite import MAX_RUN, load_sections, longest_run, paragraphs

PROMPT = config.ROOT / "aside_long" / "prompts" / "longform_script.md"
SCORE_PROMPT = config.ROOT / "aside_long" / "prompts" / "score.md"

_STR = {"type": "string"}
SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["title", "topic", "characters", "chapters", "thumbnail", "youtube"],
    "properties": {
        "title": _STR, "topic": _STR,
        "characters": {"type": "array", "items": {
            "type": "object", "required": ["id", "name", "look"],
            "properties": {"id": _STR, "name": _STR, "look": _STR}}},
        "chapters": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["title", "scenes"],
            "properties": {"title": _STR, "scenes": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["image", "lines"],
                "properties": {
                    "image": {"type": "object", "required": ["prompt", "chars"],
                              "properties": {"prompt": _STR, "chars": {"type": "array", "items": _STR}}},
                    "lines": {"type": "array", "minItems": 1, "items": {
                        "type": "object", "required": ["say", "cap"],
                        "properties": {"say": _STR, "cap": _STR}}}}}}}}},
        "thumbnail": {"type": "object", "required": ["line1", "line2", "keyword"],
                      "properties": {"line1": _STR, "line2": _STR, "keyword": _STR}},
        "youtube": {"type": "object", "required": ["title", "description"],
                    "properties": {"title": _STR, "description": _STR}},
    },
}
SCORE_SCHEMA: Dict[str, Any] = {
    "type": "object", "required": ["score", "items", "fixes"],
    "properties": {
        "score": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "object", "required": ["name", "score", "max", "comment"],
                                              "properties": {"name": _STR, "score": {"type": "integer"},
                                                             "max": {"type": "integer"}, "comment": _STR}}},
        "fixes": {"type": "array", "items": _STR},
    },
}


def system_prompt(job: Job, source_chars: int) -> str:
    minutes = float(job.setting("long", "target_minutes"))
    cps = float(job.setting("long", "chars_per_sec"))
    target = int(minutes * 60 * cps)
    n_scenes = max(6, round(6 + (minutes * 60 - 60) / 22))
    detail(f"s1 목표 {minutes}분 · {target}자 · 씬 {n_scenes} (원고 {source_chars}자)")
    return (PROMPT.read_text(encoding="utf-8").replace("{MINUTES}", f"{minutes:g}")
            .replace("{TARGET_CHARS}", str(target)).replace("{CPS}", f"{cps:g}").replace("{SCENES}", str(n_scenes)))


def overlaps(script: Dict[str, Any], raw_text: str) -> List[str]:
    """씬 단위로 이어 붙인 say/cap 이 원문과 MAX_RUN 자 넘게 겹치는 곳."""
    probs = []
    for sc in scenes(script):
        for key in ("say", "cap"):
            joined = " ".join(ln.get(key, "") for ln in sc.get("lines") or [])
            run, frag = longest_run(joined, raw_text)
            if run > MAX_RUN:
                probs.append(f"{sc['chapter']}장 {sc['n']}번 씬 {key} 가 원문과 {run}자 겹칩니다: 「{frag[:40]}」 — 다른 표현으로")
    return probs


def stats(script: Dict[str, Any], cps: float) -> Dict[str, Any]:
    ss = scenes(script)
    chars = sum(len(ln.get("say", "")) for sc in ss for ln in sc.get("lines") or [])
    return {"chapters": len(script.get("chapters") or []), "scenes": len(ss),
            "lines": sum(len(sc.get("lines") or []) for sc in ss), "say_chars": chars,
            "est_minutes": round(chars / cps / 60, 1)}


def score(source: str, script: Dict[str, Any], model: str) -> Dict[str, Any]:
    prompt = f"[원고 — 재집필본]\n{source}\n\n[대본 JSON]\n{json.dumps(script, ensure_ascii=False)}"
    return claude_cli.structured(prompt, SCORE_PROMPT.read_text(encoding="utf-8"), SCORE_SCHEMA, model=model)


def _clean(script: Dict[str, Any]) -> Dict[str, Any]:
    ids = {c.get("id") for c in script.get("characters") or []}
    for sc in (s for ch in script.get("chapters") or [] for s in ch.get("scenes") or []):
        im = sc.setdefault("image", {})
        im["chars"] = [c for c in im.get("chars") or [] if c in ids]
    return script


def run(job: Job, only=None, force: bool = False, **_) -> None:
    units = job.pick(only)
    if not units:
        raise SystemExit("원고가 없습니다 — 대시보드에서 원고(docx)를 넣어 주세요")
    model = config.load()["claude"]["script_model"]
    cps = float(job.setting("long", "chars_per_sec"))
    smin = int(job.setting("long", "score_min"))
    for u in units:
        sid = u.prefix
        if not u.source:
            log(f"  · {sid} 는 아직 안전 개작본(02_rewrite)이 없어요 — 「안전 개작」을 먼저 해 주세요.")
            continue
        cache = config.read_json(u.script_dir / "_s1.json", {}) or {}
        if job.script_path(sid).exists() and cache.get("hash") == u.hash and not force:
            log(f"「{u.section or u.name}」 대본은 이미 있어요.")
            continue
        secs = (load_sections(u) or {}).get("sections") or []
        if secs:
            source = "\n\n".join(f"{s['label']} {s.get('title', '')}\n" + "\n".join(s.get("paragraphs") or []) for s in secs)
        else:
            source = u.text
        raw = u.raw
        raw_text = "\n".join(paragraphs(raw)) if raw and raw.suffix == ".docx" else (raw.read_text(encoding="utf-8", errors="ignore") if raw else "")
        head = (f"[시리즈] {job.get('title', '')}\n[단원] {u.section}\n"
                + (f"[장 구성 — 아래 소제목 {len(secs)}개마다 장 하나, 같은 순서]\n"
                   + "\n".join(f"{s['label']} {s.get('title', '')}" for s in secs) + "\n" if secs else "")
                + "\n[원고]\n")
        system = system_prompt(job, len(source))
        log(f"「{u.section or u.name}」 롱폼 대본을 쓰는 중이에요 (Claude, 5~10분) …")
        res = _clean(claude_cli.structured(head + source, system, SCHEMA, model=model))
        st = stats(res, cps)
        log(f"  초안: {st['chapters']}장 · 씬 {st['scenes']} · 문장 {st['lines']} · 약 {st['est_minutes']}분")
        log("  다른 Claude 가 100점 채점 중이에요 …")
        sc = score(source, res, model)
        ov = overlaps(res, raw_text)
        log(f"  채점 {sc['score']}점 · 원문 겹침 {len(ov)}곳")
        first = sc
        if sc["score"] < smin or ov:
            log(f"  {smin}점 미만이거나 겹침이 있어 한 번 고쳐 써요 …")
            fix = (head + source + "\n\n[앞선 대본]\n" + json.dumps(res, ensure_ascii=False)
                   + "\n\n[고칠 점 — 모두 반영해 대본 전체를 다시]\n- " + "\n- ".join((sc.get("fixes") or []) + ov))
            res = _clean(claude_cli.structured(fix, system, SCHEMA, model=model))
            sc = score(source, res, model)
            ov = overlaps(res, raw_text)
            st = stats(res, cps)
            log(f"  고친 뒤 채점 {sc['score']}점 · 원문 겹침 {len(ov)}곳 · 약 {st['est_minutes']}분")
        res["id"] = sid
        res["unit"] = u.section
        config.write_json(job.script_path(sid), res)
        config.write_json(u.script_dir / "채점.json", {
            "score": sc["score"], "passed": sc["score"] >= smin and not ov, "items": sc.get("items") or [],
            "fixes": sc.get("fixes") or [], "overlaps": ov, "first_score": first["score"], "stats": st,
            "note": "원문 겹침은 자동 검사이며 법적 판단이 아닙니다 — 최종 확인은 사람이 합니다."})
        config.write_json(u.script_dir / "_s1.json", {"hash": u.hash, "source": u.source.name})
        if job.edit_path(sid).exists():
            log("  △ 사람이 고친 대본(.edit.json)이 있어 그것이 계속 쓰여요. 새 대본을 쓰려면 대시보드에서 「수정 되돌리기」.")
        log(f"  ✓ {sid} 대본 완료 — {res.get('title', '')} ({sc['score']}점)")
        styles.write_handoff(job, sid, res)
        from . import ytmeta     # 대본 다음 바로 유튜브 업로드 문구(제목·설명·해시태그·태그·고정 댓글)
        ytmeta.write(job, u, model)
