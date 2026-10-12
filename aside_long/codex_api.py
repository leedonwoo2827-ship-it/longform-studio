"""Codex 그림 엔진 — ChatGPT 구독 OAuth 로 Codex 백엔드에 **그림 요청만** 직접 보낸다. API 키를 쓰지 않는다.

`~/.codex/auth.json`(codex login 이 남긴 토큰)을 읽어 `https://chatgpt.com/backend-api/codex/responses` 에
image_generation 도구 하나만 붙여 POST 하고, SSE 응답에서 base64 PNG 를 꺼낸다.
`codex exec`(작업 대화 — 메모 읽기·셸로 파일 복사 등, 장당 1.5만~3.7만 토큰)와 달리 그림 요청 한 번뿐이라
같은 5시간 한도로 훨씬 많이 그린다.

이식 원본: D:\\00work\\260920-backplate_showcase-master-onlycodex
  llm/codex_transport.py (토큰·갱신 잠금·헤더·SSE·한도 헤더) + imgstudio/services/codex_image.py (페이로드·응답 파싱).
  수백 장을 구워 검증된 로직이라 **그대로 옮겼다.** 더한 것: 429 본문에서 리셋 시각 읽기(UsageLimit), 한도 확인 요청(probe).

> 주의: codex/responses 는 Codex CLI 가 쓰는 **내부(비공식) 엔드포인트**다. 바뀌면 이 파일 하나만 고치면 된다.
  막히면 설정의 그림 엔진을 `codex-cli`(예전 방식)로 돌린다.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

CODEX_BACKEND = "https://chatgpt.com/backend-api/codex/responses"
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"        # Codex CLI 공개 client id
FALLBACK_VERSION = "0.130.0"
FALLBACK_UA = f"codex_cli_rs/{FALLBACK_VERSION} (Windows 11; x64) aside-long"
AUTH_PATH = Path(os.environ.get("CODEX_AUTH_PATH", str(Path.home() / ".codex" / "auth.json")))
VERSION_PATH = Path.home() / ".codex" / "version.json"
CODEX_MODEL = os.environ.get("CODEX_IMAGE_MODEL", "gpt-5.5")   # 요청 모델 — 그림은 그 안의 image_generation 도구가
BACKGROUND = "opaque"            # 기본 auto 면 가끔 투명 PNG 가 나온다(backplate 실측) — 못박는다
TOTAL_TIMEOUT = 300
STALL_TIMEOUT = 120.0
MAX_W, MAX_H = 3840, 2160


class CodexHTTPError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NotAuthenticated(CodexHTTPError):
    """로그인이 없거나 만료 → 설정 탭 「Codex 계정」에서 로그인."""


class UsageLimit(CodexHTTPError):
    """5시간/7일 한도 소진. resets_at(epoch 초)을 알면 그때 풀린다."""

    def __init__(self, message: str, resets_at: Optional[float] = None, plan: str = ""):
        super().__init__(message, status=429)
        self.resets_at = resets_at
        self.plan = plan


# ── 토큰 ──────────────────────────────────────────────────────────────────────
def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_auth() -> Dict[str, Any]:
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        raise NotAuthenticated("Codex 로그인이 없어요 — 설정 탭 「Codex 계정」에서 로그인해 주세요.")
    except Exception as e:      # noqa: BLE001
        raise CodexHTTPError(f"auth.json 읽기 실패: {e}")


def _extract(auth: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    t = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    pick = lambda k: t.get(k) if isinstance(t.get(k), str) else None      # noqa: E731
    return pick("access_token"), pick("account_id"), pick("refresh_token")


def _refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    data = urllib.parse.urlencode({"client_id": OAUTH_CLIENT_ID, "grant_type": "refresh_token",
                                   "refresh_token": refresh_token, "scope": "openid profile email"}).encode()
    req = urllib.request.Request(OAUTH_TOKEN_URL, data=data, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": FALLBACK_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        err = ""
        try:
            err = json.loads(body).get("error", "") if body else ""
        except Exception:       # noqa: BLE001
            pass
        # 갱신이 401 이면 다시 로그인하는 길밖에 없다 — 재시도하지 않고 멈추게 인증 오류로(backplate 2026-09-20 실측)
        if e.code == 401 or err == "invalid_grant":
            raise NotAuthenticated(f"로그인이 만료됐어요 — 다시 로그인해 주세요. ({err or e.code})", status=e.code)
        raise CodexHTTPError(f"토큰 갱신 실패: HTTP {e.code} {err}", status=e.code)


# refresh_token 은 쓰면 회전한다 — 워커 셋이 동시에 갱신하면 하나만 이기고 나머지는 401. 한 번에 하나만.
_refresh_lock = threading.Lock()


def refresh_once(stale_access: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    with _refresh_lock:
        auth = _load_auth()
        access, account_id, refresh = _extract(auth)
        if access and access != stale_access:
            return access, account_id            # 남이 이미 갱신했다
        if not refresh:
            raise NotAuthenticated("refresh_token 이 없어요 — 다시 로그인해 주세요.")
        got = _refresh_access_token(refresh)
        new = got.get("access_token")
        if not isinstance(new, str) or not new:
            raise NotAuthenticated("토큰을 갱신하지 못했어요 — 다시 로그인해 주세요.")
        tokens = auth.setdefault("tokens", {})
        for k in ("access_token", "refresh_token", "id_token"):
            if isinstance(got.get(k), str):
                tokens[k] = got[k]
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            _atomic_write_json(AUTH_PATH, auth)
        except Exception:       # noqa: BLE001 — 저장이 실패해도 이번 요청은 새 토큰으로
            pass
        return new, account_id


# ── 헤더 ──────────────────────────────────────────────────────────────────────
_version: Optional[str] = None


def _codex_version() -> str:
    """헤더에 실을 CLI 버전 — 실제 `codex --version` → version.json → 하한값."""
    global _version
    if _version:
        return _version
    best = None
    exe = shutil.which("codex.cmd") or shutil.which("codex")
    if exe:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                               encoding="utf-8", errors="replace",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            m = re.search(r"\d+\.\d+\.\d+", r.stdout or r.stderr or "")
            best = m.group(0) if m else None
        except Exception:       # noqa: BLE001
            pass
    if not best and VERSION_PATH.exists():
        try:
            best = json.loads(VERSION_PATH.read_text(encoding="utf-8")).get("latest_version")
        except Exception:       # noqa: BLE001
            pass
    sv = lambda v: tuple(int(x) for x in v.split(".")) if v and re.fullmatch(r"\d+\.\d+\.\d+", v) else None  # noqa: E731
    _version = best if sv(best) and sv(best) >= sv(FALLBACK_VERSION) else FALLBACK_VERSION
    return _version


def _headers(token: str, account_id: Optional[str]) -> Dict[str, str]:
    v = _codex_version()
    sid = str(uuid.uuid4())
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {token}", "Accept": "text/event-stream",
         "Connection": "Keep-Alive", "version": v, "session_id": sid, "x-client-request-id": sid,
         "User-Agent": f"codex_cli_rs/{v} (Windows 11; x64) aside-long", "originator": "codex_cli_rs"}
    if account_id:
        h["chatgpt-account-id"] = account_id
    return h


# ── 한도(응답 헤더) ───────────────────────────────────────────────────────────
_LIMITS: Dict[str, Any] = {}


def _capture_limits(headers) -> None:
    def num(k, cast=float):
        try:
            return cast(headers.get(k))
        except (TypeError, ValueError):
            return None
    got = {"plan": headers.get("x-codex-plan-type"),
           "used_5h": num("x-codex-primary-used-percent"), "reset_5h_sec": num("x-codex-primary-reset-after-seconds", int),
           "used_7d": num("x-codex-secondary-used-percent"), "reset_7d_sec": num("x-codex-secondary-reset-after-seconds", int),
           "at": time.time()}
    if got["used_5h"] is None and got["used_7d"] is None:
        return
    _LIMITS.update({k: v for k, v in got.items() if v is not None})


def limits() -> Dict[str, Any]:
    """가장 최근 응답의 한도. reset 은 지금 기준으로 다시 계산해 돌려준다(초)."""
    d = dict(_LIMITS)
    if d.get("at"):
        gone = time.time() - d["at"]
        for k in ("reset_5h_sec", "reset_7d_sec"):
            if d.get(k) is not None:
                d[k] = max(0, int(d[k] - gone))
    return d


def limits_line() -> str:
    d = limits()
    if not d:
        return ""
    left = lambda u: f"{max(0.0, 100 - float(u)):.0f}%" if u is not None else "?"     # noqa: E731
    reset = ""
    if d.get("reset_5h_sec") is not None:
        reset = f" ({time.strftime('%H:%M', time.localtime(time.time() + d['reset_5h_sec']))} 리셋)"
    return f"Codex 한도 — 5시간 {left(d.get('used_5h'))} 남음{reset} · 7일 {left(d.get('used_7d'))} 남음"


# ── SSE ───────────────────────────────────────────────────────────────────────
def _stream(headers: Dict[str, str], body: Dict[str, Any], deadline: float) -> Iterator[Dict[str, Any]]:
    req = urllib.request.Request(CODEX_BACKEND, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=max(1.0, min(30.0, deadline - time.monotonic())))
    _capture_limits(resp.headers)
    try:
        resp.fp.raw._sock.settimeout(min(STALL_TIMEOUT, max(1.0, deadline - time.monotonic())))  # type: ignore[attr-defined]
    except Exception:       # noqa: BLE001
        pass
    try:
        buf: List[str] = []
        for raw in resp:
            if time.monotonic() >= deadline:
                raise CodexHTTPError("응답이 너무 오래 걸려요(시간 초과).")
            line = raw.decode("utf-8", "ignore").rstrip("\r\n")
            if line == "":
                if not buf:
                    continue
                payload, buf = "\n".join(buf), []
                if payload == "[DONE]":
                    return
                try:
                    yield json.loads(payload)
                except Exception:       # noqa: BLE001
                    pass
                continue
            if line.startswith(":") or line.startswith("event:"):
                continue
            if line.startswith("data:"):
                c = line[5:]
                buf.append(c[1:] if c.startswith(" ") else c)
    finally:
        resp.close()


def _http_error(e: urllib.error.HTTPError) -> CodexHTTPError:
    body = e.read().decode("utf-8", "ignore")
    _capture_limits(e.headers or {})
    if e.code == 429 or re.search(r"usage_limit|usage limit|insufficient_quota|quota|rate limit", body, re.I):
        resets, plan = None, ""
        try:
            j = json.loads(body)
            err = j.get("error") if isinstance(j.get("error"), dict) else j
            resets = err.get("resets_at") or (time.time() + float(err["resets_in_seconds"]) if err.get("resets_in_seconds") else None)
            plan = err.get("plan_type") or ""
        except Exception:       # noqa: BLE001
            pass
        if not resets and limits().get("reset_5h_sec") is not None and (limits().get("used_5h") or 0) >= 99:
            resets = time.time() + limits()["reset_5h_sec"]
        return UsageLimit(f"Codex 사용 한도에 걸렸어요: {body[:200]}", resets_at=float(resets) if resets else None, plan=plan)
    if e.code == 401:
        return CodexHTTPError(f"HTTP 401: {body[:200]}", status=401)
    return CodexHTTPError(f"HTTP {e.code}: {body[:300]}", status=e.code)


# ── 그림 ──────────────────────────────────────────────────────────────────────
def check_size(size: str) -> None:
    m = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", (size or "").strip())
    if not m:
        raise CodexHTTPError(f"크기 형식이 잘못됐어요: {size} (예: 1536x864)")
    w, h = int(m.group(1)), int(m.group(2))
    if w % 16 or h % 16 or not (1 / 3) <= w / h <= 3 or w > MAX_W or h > MAX_H:
        raise CodexHTTPError(f"{size} — 16의 배수, 비율 1:3~3:1, 최대 {MAX_W}x{MAX_H}")


def _payload(prompt: str, size: str) -> Dict[str, Any]:
    text = (f"Use the image_generation tool to render the following. Request: {prompt}. "
            f"Output format: png. Size: {size}. Do not include explanatory text — produce only the image.")
    return {"model": CODEX_MODEL, "stream": True, "instructions": "You are an image generation assistant.",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
            "tools": [{"type": "image_generation", "output_format": "png", "background": BACKGROUND, "size": size}],
            "tool_choice": "auto", "parallel_tool_calls": False, "store": False,
            "reasoning": {"effort": "low", "summary": "auto"}, "include": ["reasoning.encrypted_content"],
            "text": {"verbosity": "low"}}


def _post_image(headers: Dict[str, str], payload: Dict[str, Any], deadline: float) -> bytes:
    b64, fail, seen = None, None, {}
    try:
        for evt in _stream(headers, payload, deadline):
            t = evt.get("type", "?")
            seen[t] = seen.get(t, 0) + 1
            if t in ("error", "response.failed"):
                d = None
                if isinstance(evt.get("response"), dict) and isinstance(evt["response"].get("error"), dict):
                    d = evt["response"]["error"].get("message") or evt["response"]["error"].get("code")
                if isinstance(evt.get("error"), dict):
                    d = evt["error"].get("message") or d
                fail = str(d or evt.get("message") or evt.get("code") or "")
            if t == "response.output_item.done":
                item = evt.get("item")
                if isinstance(item, dict) and item.get("type") == "image_generation_call" and isinstance(item.get("result"), str):
                    b64 = item["result"]
    except urllib.error.HTTPError as e:
        raise _http_error(e)
    except (TimeoutError, ConnectionError):
        raise CodexHTTPError("그림 응답이 끊겼어요 — 다시 시도해요.")
    except urllib.error.URLError as e:
        raise CodexHTTPError(f"네트워크 오류: {e.reason}")
    if not b64:
        if fail and re.search(r"usage|limit|quota", fail, re.I):
            raise UsageLimit(f"Codex 사용 한도: {fail}")
        raise CodexHTTPError(f"그림이 오지 않았어요: {fail or ', '.join(sorted(seen)) or '(빈 응답)'}")
    try:
        return base64.b64decode(b64, validate=True)
    except Exception:       # noqa: BLE001
        raise CodexHTTPError("그림 데이터(base64)가 깨져 왔어요.")


def _call(fn) -> Any:
    """토큰을 읽어 호출, 401 이면 (잠금 걸고) 한 번 갱신해 다시."""
    auth = _load_auth()
    access, account_id, _ = _extract(auth)
    if not access:
        raise NotAuthenticated("Codex 로그인이 없어요 — 설정 탭 「Codex 계정」에서 로그인해 주세요.")
    try:
        return fn(_headers(access, account_id))
    except CodexHTTPError as e:
        if e.status == 401 and not isinstance(e, NotAuthenticated):
            new, account_id = refresh_once(access)
            return fn(_headers(new, account_id))
        raise


def generate(prompt: str, size: str = "1536x864") -> bytes:
    """그림 한 장(PNG 바이트). 16:9 는 1536x864 로 보내면 1672x941 로 온다."""
    check_size(size)
    payload = _payload(prompt, size)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    return _call(lambda h: _post_image(h, payload, deadline))


def probe() -> Dict[str, Any]:
    """한도만 알고 싶을 때 — 아주 짧은 글 요청 하나(그림 없음)로 응답 헤더를 받는다."""
    body = {"model": CODEX_MODEL, "stream": True, "instructions": "Reply with OK.",
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "OK"}]}],
            "store": False, "reasoning": {"effort": "low", "summary": "auto"}, "text": {"verbosity": "low"}}
    deadline = time.monotonic() + 60

    def go(h):
        try:
            for _ in _stream(h, body, deadline):
                pass
        except urllib.error.HTTPError as e:
            err = _http_error(e)
            if not isinstance(err, UsageLimit):
                raise err
    _call(go)
    return limits()
