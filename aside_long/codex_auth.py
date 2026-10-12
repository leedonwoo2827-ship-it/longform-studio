"""Codex(그림) 로그인 — ChatGPT OAuth. `codex login` / `codex logout` / `codex login status` 를 대신 돌린다.

status() 는 ~/.codex/auth.json 의 id_token 에서 **이메일·요금제만** 읽는다(토큰 값은 밖으로 내보내지 않는다).
요금제가 team/enterprise 면 회사 워크스페이스 크레딧을 쓴다 — 바닥나면 「out of credits」로 그림이 멈춘다.
"""
from __future__ import annotations

import base64
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .log import detail
from .s3_images import exe

AUTH = Path.home() / ".codex" / "auth.json"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_login: Optional[subprocess.Popen] = None


def _claims() -> Dict[str, Any]:
    try:
        tok = (json.loads(AUTH.read_text(encoding="utf-8")).get("tokens") or {}).get("id_token") or ""
        part = tok.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:       # noqa: BLE001 — 없거나 깨졌으면 로그인 안 된 것으로
        return {}


def status() -> Dict[str, Any]:
    path = exe()
    if not path:
        return {"installed": False, "logged_in": False, "text": "codex CLI 없음 — npm i -g @openai/codex"}
    try:
        r = subprocess.run([path, "login", "status"], capture_output=True, timeout=20, creationflags=_NOWIN)
        text = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    except Exception as e:      # noqa: BLE001
        text = str(e)
    c = _claims()
    auth = c.get("https://api.openai.com/auth") or {}
    plan = auth.get("chatgpt_plan_type") or ""
    return {"installed": True, "logged_in": "logged in" in text.lower(), "text": text[:120],
            "email": c.get("email") or "", "plan": plan,
            "workspace": plan in ("team", "enterprise", "business", "edu"),
            "until": auth.get("chatgpt_subscription_active_until") or "",
            "logging_in": logging_in()}


def logging_in() -> bool:
    return _login is not None and _login.poll() is None


def login(emit: Callable[[str], None], timeout: int = 600) -> None:
    """`codex login` — 브라우저가 열리고 ChatGPT 로 로그인하면 끝난다. 출력(주소 등)은 emit 으로 로그 창에."""
    global _login
    path = exe()
    if not path:
        raise RuntimeError("codex CLI 가 없어요 — npm i -g @openai/codex")
    if logging_in():
        raise RuntimeError("이미 로그인 창이 열려 있어요 — 브라우저에서 마무리해 주세요")
    _login = subprocess.Popen([path, "login"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, creationflags=_NOWIN)
    proc = _login

    def pump():
        t0 = time.time()
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                emit("  [Codex] " + line)
        proc.wait()
        detail(f"codex login 끝 code={proc.returncode} {time.time() - t0:.0f}초")
        emit("✓ Codex 로그인 됐어요" if proc.returncode == 0 else "✗ Codex 로그인이 끝나지 않았어요 — 다시 눌러 주세요")

    def guard():
        end = time.time() + timeout
        while proc.poll() is None and time.time() < end:
            time.sleep(2)
        if proc.poll() is None:
            proc.kill()
            emit("✗ Codex 로그인 시간이 지나 창을 닫았어요 — 다시 눌러 주세요")

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=guard, daemon=True).start()


def cancel_login() -> None:
    if logging_in():
        _login.kill()


def logout() -> str:
    path = exe()
    if not path:
        raise RuntimeError("codex CLI 가 없어요")
    r = subprocess.run([path, "logout"], capture_output=True, timeout=30, creationflags=_NOWIN)
    return (r.stdout + r.stderr).decode("utf-8", "replace").strip()[:200]
