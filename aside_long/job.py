"""작업(job) 폴더 하나 = 시리즈 하나(예: 롱폼 세계사) = 단원 폴더 여러 개. **단원 하나 = 롱폼 영상 한 편.**

jobs/<job>/
  job.json · job.local.json      시리즈 이름·화풍·목소리·목표 길이 (local 은 PC별 덮어쓰기)
  state.json                     업로드·예약 기록
  1-1_선사 문화와 국가의 출현/   ← 단원 하나 (영상 번호 = 단원 번호 "1-1")
    01_raw/       원본 docx (보관만)
    02_rewrite/   안전 개작 docx + sections.json + 검증.json        s0-rewrite
    03_script/    <id>.json(Claude 원안) · <id>.edit.json(사람 수정 — 언제나 이김) · 채점.json · _s1.json(캐시)
    04_bundle/    audio/ (s2) · images/ (s3) · out/ (s4 영상·s5 썸네일)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .manuscript import Unit, units_in

BUNDLE_KINDS = ("audio", "images", "out")
_STATE_LOCK = threading.Lock()


@dataclass
class Job:
    name: str

    @property
    def dir(self) -> Path:
        return config.JOBS / self.name

    def exists(self) -> bool:
        return (self.dir / "job.json").exists()

    @cached_property
    def meta(self) -> Dict[str, Any]:
        base = config.read_json(self.dir / "job.json", {}) or {}
        return config.deep_merge(base, config.read_json(self.dir / "job.local.json", {}) or {})

    def get(self, key: str, default: Any = None) -> Any:
        cur: Any = self.meta
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def setting(self, section: str, key: str) -> Any:
        """job.json 의 같은 이름 칸이 전체 설정을 이긴다 (예: long.style, tts.voice)."""
        v = self.get(f"{section}.{key}")
        return v if v is not None else config.load()[section].get(key)

    # ── 단원 = 영상 ──────────────────────────────────────────────────────────
    @property
    def units(self) -> List[Unit]:
        return units_in(self.dir)

    def unit(self, sid: str) -> Optional[Unit]:
        return next((u for u in self.units if u.prefix == sid), None)

    def unit_of(self, sid: str) -> Unit:
        u = self.unit(sid)
        if not u:
            raise SystemExit(f"{sid}: 단원 폴더를 찾지 못했어요")
        return u

    def pick(self, only: Optional[List[str]] = None) -> List[Unit]:
        us = self.units
        if not only:
            return us
        got = [u for u in us if u.prefix in set(only)]
        if not got:
            raise SystemExit(f"--only 에 맞는 단원이 없습니다: {', '.join(only)} "
                             f"(있는 것: {', '.join(u.prefix for u in us) or '없음'})")
        return got

    # ── 대본 ─────────────────────────────────────────────────────────────────
    def script_path(self, sid: str) -> Path:
        return self.unit_of(sid).script_dir / f"{sid}.json"

    def edit_path(self, sid: str) -> Path:
        return self.unit_of(sid).script_dir / f"{sid}.edit.json"

    def script(self, sid: str) -> Optional[Dict[str, Any]]:
        """s1 결과 — 사람이 고친 .edit.json 이 있으면 **통째로** 그것(장·씬 배열은 부분 덮기가 안 된다)."""
        ed = config.read_json(self.edit_path(sid))
        return ed if ed else config.read_json(self.script_path(sid))

    def save_edit(self, sid: str, data: Dict[str, Any]) -> None:
        config.write_json(self.edit_path(sid), data)

    def reset_edit(self, sid: str) -> None:
        self.edit_path(sid).unlink(missing_ok=True)

    # ── 번들 ─────────────────────────────────────────────────────────────────
    def sub(self, kind: str, sid: str) -> Path:
        if kind not in BUNDLE_KINDS:
            raise ValueError(kind)
        return self.unit_of(sid).bundle_dir / kind

    def video(self, sid: str) -> Path:
        return self.sub("out", sid) / f"{sid}.mp4"

    # ── 업로드 상태 (state.json — {id: {posted, scheduled, error}}) ─────────────
    def state(self) -> Dict[str, Any]:
        return config.read_json(self.dir / "state.json", {}) or {}

    def update_state(self, sid: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with _STATE_LOCK:
            st = self.state()
            cur = dict(st.get(sid) or {})
            for k, v in patch.items():
                if v is None:
                    cur.pop(k, None)
                else:
                    cur[k] = v
            st[sid] = cur
            config.write_json(self.dir / "state.json", st)
            return cur

    def url(self, path: Path) -> str:
        """대시보드 서버 주소 (/jobs/…)"""
        return "/jobs/" + path.resolve().relative_to(config.JOBS.resolve()).as_posix()


def scenes(script: Dict[str, Any]) -> List[Dict[str, Any]]:
    """장(chapter) 구조를 펼친 씬 목록 — 각 씬에 n(1부터 전체 번호)·chapter(장 번호)를 붙여서."""
    out = []
    for ci, ch in enumerate(script.get("chapters") or [], 1):
        for sc in ch.get("scenes") or []:
            out.append({**sc, "n": len(out) + 1, "chapter": ci, "chapter_title": ch.get("title", "")})
    return out


def all_jobs() -> List[Job]:
    if not config.JOBS.exists():
        return []
    return [Job(p.name) for p in sorted(config.JOBS.iterdir()) if p.is_dir() and (p / "job.json").exists()]


def need(name: Optional[str]) -> Job:
    if not name:
        jobs = all_jobs()
        if len(jobs) == 1:
            return jobs[0]
        raise SystemExit("--job 을 지정하세요. 있는 작업: " + (", ".join(j.name for j in jobs) or "없음"))
    job = Job(name)
    if not job.exists():
        raise SystemExit(f"작업이 없습니다: jobs/{name}/job.json — `run.bat new {name} --from <원고.docx>`")
    return job
