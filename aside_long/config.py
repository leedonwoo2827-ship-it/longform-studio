"""설정 — `aside.config.json` 위에 `aside.config.local.json`(PC별, git 제외)을 덮는다.

job 설정은 `jobs/<job>/job.json` 위에 `job.local.json` 을 덮는다(aside-youtube-claude 와 같은 규칙).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "jobs"
LOGS = ROOT / "logs"
TEMPLATES = ROOT / "templates"
PROFILES = ROOT / "profiles"
LOCAL = ROOT / "local.json"
FONTS = TEMPLATES / "fonts"
# SuperTonic3 모델(약 380MB)은 쇼츠 프로젝트에 이미 받아 둔 것을 같이 쓴다 — 없으면 tts-setup 이 여기로 받는다
SIBLING_SUPERTONIC = Path(r"D:\00work\261004-aside-youtube-claude\assets\supertonic")

DEFAULTS: Dict[str, Any] = {
    # 재집필·대본·채점은 Claude(구독 OAuth 로그인한 claude CLI), 그림은 Codex(ChatGPT 로그인한 codex CLI)
    "claude": {"script_model": "opus", "effort": "high", "timeout_sec": 1500, "retries": 1,
               "limit_wait_hours": 72, "limit_poll_min": 30},
    "long": {"target_minutes": 15, "chars_per_sec": 4.5, "score_min": 80, "style": "textbook"},
    "tts": {"voice": "F4", "speed": 1.0, "gap": 0.35, "scene_gap": 0.8, "lead": 0.6, "tail": 1.5,
            "assets_dir": "", "timeout_sec": 900},
    "image": {"engine": "codex", "workers": 3, "timeout_sec": 600, "retries": 1},
    "render": {"w": 1920, "h": 1080, "fps": 30, "crf": 20, "preset": "medium",
               "image_h": 990, "caption_size": 68, "caption_font": "Pretendard SemiBold",
               "caption_margin": 34, "max_line_chars": 28, "fade": 0.5},
    # 업로드: 실제 Chrome(전용 프로필)에 CDP 로 붙어 YouTube Studio 를 조종한다(aside-youtube-claude 방식)
    "youtube": {"chrome": "", "visibility": "public", "base_port": 9361, "ai_disclosure": True,
                "pattern": "1-evening", "slots": ["19:00"], "weekend_slots": ["20:00"]},
    "ui": {"port": 5294, "width": 460},
}


def deep_merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(a)
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise SystemExit(f"JSON 이 깨졌습니다: {path} — {e}")


def write_json(path: Path, data: Any) -> None:
    """원자적 쓰기 — 서버와 하위 프로세스가 같은 파일을 만진다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load() -> Dict[str, Any]:
    cfg = deep_merge(DEFAULTS, read_json(ROOT / "aside.config.json", {}) or {})
    return deep_merge(cfg, read_json(ROOT / "aside.config.local.json", {}) or {})


def local() -> Dict[str, Any]:
    """PC별 상태(업로드 계정 목록 등) — git 제외."""
    return read_json(LOCAL, {}) or {}


def save_local(data: Dict[str, Any]) -> None:
    write_json(LOCAL, data)


def supertonic_dir() -> Path:
    """설정값 → 이 프로젝트 assets/supertonic(받아 둔 게 있으면) → 쇼츠 프로젝트 것."""
    set_ = (load()["tts"].get("assets_dir") or "").strip()
    if set_:
        return Path(set_)
    own = ROOT / "assets" / "supertonic"
    if (own / "onnx").exists() or not SIBLING_SUPERTONIC.exists():
        return own
    return SIBLING_SUPERTONIC
