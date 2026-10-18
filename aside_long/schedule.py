"""예약 — 올릴 영상들을 정해 둔 시각 칸(패턴)에 차례로 배치하고, YouTube Studio 「예약 공개」로 넘긴다.

업로드는 지금 하고 **공개만 그 시각에** — PC 를 꺼도 그 시각에 공개된다(Studio native 예약).
이미 예약해 둔 칸(모든 작업의 state.json scheduled)은 비켜 간다 — 한국사1·한국사2 를 같은 채널에 섞어 걸어도 안 겹친다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Dict, List, Optional

from .job import all_jobs

FMT = "%Y-%m-%d %H:%M"

# 롱폼은 쇼츠보다 드물게 — 하루 1편 이하가 기본. weekdays: 0=월 … 6=일
PATTERNS: Dict[str, Dict] = {
    "daily":      {"label": "매일 1편 · 정한 시각 (재생목록별 시각)", "days": [0, 1, 2, 3, 4, 5, 6], "time": "17:00"},
    "daily-19":   {"label": "매일 1편 · 19:00 (주말 20:00)", "days": [0, 1, 2, 3, 4, 5, 6], "time": "19:00", "weekend": "20:00"},
    "mwf-19":     {"label": "주 3편 · 월·수·금 19:00 (추천)", "days": [0, 2, 4], "time": "19:00"},
    "tf-19":      {"label": "주 2편 · 화·금 19:00", "days": [1, 4], "time": "19:00"},
    "thu-19":     {"label": "주 1편 · 목 19:00", "days": [3], "time": "19:00"},
    "daily-12":   {"label": "매일 1편 · 12:30", "days": [0, 1, 2, 3, 4, 5, 6], "time": "12:30"},
}
DEFAULT = "daily"


# ── 재생목록별 시작 시각 — 여러 시리즈를 같은 채널에 매일 올릴 때 5분씩 비켜 두기(17:00 · 17:05 · 17:10 …) ──
def _times_path():
    from . import config
    return config.ROOT / "playlists.json"


def playlist_times() -> Dict[str, str]:
    from . import config
    return dict(config.read_json(_times_path(), {}) or {})


def set_playlist_time(name: str, hm: str) -> None:
    from . import config
    name = (name or "").strip()
    if not name or not re.fullmatch(r"\d{1,2}:\d{2}", hm or ""):
        return
    d = playlist_times()
    d[name] = hm
    config.write_json(_times_path(), d)


def time_for(playlist: str, fallback: str = "17:00") -> str:
    return playlist_times().get((playlist or "").strip(), fallback)


def parse_when(text: str) -> datetime:
    text = (text or "").strip().replace("T", " ")
    for f in (FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, f)
        except ValueError:
            pass
    raise SystemExit(f"시각 형식은 2026-10-22 19:00 꼴이에요: {text}")


def taken(account: Optional[str] = None) -> List[str]:
    out = []
    for job in all_jobs():
        for st in job.state().values():
            sc = st.get("scheduled") or {}
            if sc.get("when") and (account is None or sc.get("account") in (None, account)):
                out.append(sc["when"])
    return out


def next_slots(n: int, account: Optional[str] = None, start: Optional[datetime] = None,
               pat: Optional[str] = None, at: Optional[str] = None) -> List[datetime]:
    """패턴의 빈 칸 n 개. start(날짜)가 있으면 그날부터, 없으면 지금+30분부터. at(HH:MM)이 있으면 패턴 시각 대신 그 시각."""
    p = dict(PATTERNS.get(pat or DEFAULT, PATTERNS[DEFAULT]))
    if at and re.fullmatch(r"\d{1,2}:\d{2}", at):
        p["time"], p["weekend"] = at, None
    busy = set(taken(account))
    earliest = datetime.now() + timedelta(minutes=30)       # Studio 는 너무 가까운 예약을 받지 않는다
    cur = max(start, earliest) if start else earliest
    day = cur.replace(hour=0, minute=0, second=0, microsecond=0)
    out: List[datetime] = []
    for _ in range(800):
        if day.weekday() in p["days"]:
            hm = p.get("weekend") if day.weekday() >= 5 and p.get("weekend") else p["time"]
            h, m = map(int, hm.split(":"))
            d = day.replace(hour=h, minute=m)
            if d >= cur and d.strftime(FMT) not in busy:
                out.append(d)
                if len(out) >= n:
                    break
        day += timedelta(days=1)
    return out
