"""롱폼 공방 대시보드 서버 — 127.0.0.1 전용.

무거운 일(Claude·Codex·SuperTonic3·ffmpeg)은 전부 `python -m aside_long …` 하위 프로세스로 돌리고 로그만 읽는다
(aside-youtube-claude 와 같은 Runner 방식). 한 번에 하나만 돈다.
"""
from __future__ import annotations

import base64
import os
import re
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .. import config, styles, tts
from ..job import Job, all_jobs, scenes
from ..log import detail, log

STATIC = Path(__file__).parent / "static"
ALLOWED = {"post", "login", "yt-meta", "probe", "all", "make", "prep", "s0-rewrite", "s1-script", "s2-tts", "s3-images", "s4-render", "s5-thumb", "doctor"}
NAMES = {"post": "유튜브 올리기", "login": "유튜브 로그인 창", "yt-meta": "유튜브 문구 쓰기", "probe": "업로드 창 확인", "all": "전체 1→6", "prep": "1→3 개작·대본·목소리", "make": "4→6 그림·영상·썸네일", "s0-rewrite": "안전 개작", "s1-script": "대본·채점",
         "s2-tts": "목소리", "s3-images": "그림(Codex)", "s4-render": "영상 굽기", "s5-thumb": "썸네일", "doctor": "점검"}

app = FastAPI(title="aside-long")


def child_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        return str(exe.parent / "python.exe")
    return sys.executable


class Runner:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.lines: List[str] = []
        self.base = 0           # 앞에서 잘라낸 줄 수 — 클라이언트의 since 가 계속 맞게
        self.cmd: List[str] = []
        self.lock = threading.Lock()
        self.stopped = False

    @property
    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > 4000:
            del self.lines[:1000]
            self.base += 1000

    def start(self, args: List[str]) -> None:
        with self.lock:
            if self.busy:
                raise HTTPException(409, "지금 다른 일을 하는 중이에요. 끝나면 다시 눌러 주세요.")
            env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            self.cmd = args
            self.stopped = False
            self.add(f"▶ {NAMES.get(args[0], args[0])} {' '.join(a for a in args[1:] if not a.startswith('--job'))}")
            detail(f"$ aside_long {' '.join(args)}")
            self.proc = subprocess.Popen(
                [child_python(), "-m", "aside_long", *args], cwd=str(config.ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.add(line.rstrip("\n"))
        code = proc.wait()
        if not self.stopped:
            self.add("✓ 다 됐어요" if code == 0 else "✗ 중간에 멈췄어요 — 바로 위 줄을 확인해 주세요")

    def stop(self) -> None:
        if self.busy:
            self.stopped = True
            self.add("■ 멈췄어요. 이미 된 건 남아 있고, 다시 누르면 남은 것만 이어서 해요.")
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


RUN = Runner()


def _job(name: str) -> Job:
    job = Job(name)
    if not job.exists():
        raise HTTPException(404, f"작업을 찾을 수 없어요: {name}")
    return job


def _unit_status(job: Job, u) -> Dict[str, Any]:
    sid = u.prefix
    sc = config.read_json(u.script_dir / "채점.json", {}) or {}
    script = job.script(sid) or {}
    n = len(scenes(script)) if script else 0
    img_dir = u.bundle_dir / "images"
    imgs = len([p for p in img_dir.glob("[0-9][0-9][0-9].png")]) if img_dir.exists() else 0
    timing = config.read_json(u.bundle_dir / "audio" / "timing.json", {}) or {}
    video = u.bundle_dir / "out" / f"{sid}.mp4"
    return {"prefix": sid, "section": u.section, "name": u.name,
            "raw": bool(u.raw), "rewrite": bool(u.rewrite),
            "rewrite_passed": (config.read_json(u.dir / "02_rewrite" / "검증.json", {}) or {}).get("passed"),
            "script": bool(script), "edited": job.edit_path(sid).exists(), "score": sc.get("score"),
            "scenes": n, "images": imgs, "audio_min": round(timing.get("duration", 0) / 60, 1) if timing else 0,
            "video": video.exists(), "chars": len(u.text) if u.rewrite else _raw_chars(u),
            "posted": ((job.state().get(sid) or {}).get("posted") or {}).get("url") or None,
            "scheduled": ((job.state().get(sid) or {}).get("scheduled") or {}).get("when"),
            "upload_error": bool((job.state().get(sid) or {}).get("error"))}


def _raw_chars(u) -> int:
    from ..manuscript import read_text
    try:
        return len(read_text(u.raw)) if u.raw else 0
    except Exception:       # noqa: BLE001
        return 0


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/state")
def state() -> Dict[str, Any]:
    cfg = config.load()
    jobs = []
    for j in all_jobs():
        jobs.append({"name": j.name, "title": j.get("title", j.name),
                     "settings": {"style": j.setting("long", "style"), "target_minutes": j.setting("long", "target_minutes"),
                                  "target_max_minutes": j.setting("long", "target_max_minutes"),
                                  "confirmed": bool(j.get("long.plan_confirmed")),
                                  "voice": j.setting("tts", "voice"), "speed": j.setting("tts", "speed"),
                                  "thumb_scene": j.get("long.thumb_scene") or 1},
                     "units": [_unit_status(j, u) for u in j.units]})
    return {"jobs": jobs, "busy": RUN.busy, "cmd": RUN.cmd, "voices": tts.VOICES,
            "styles": [{"id": k, "name": v["name"]} for k, v in styles.PRESETS.items()],
            "defaults": {"target_minutes": cfg["long"]["target_minutes"], "voice": cfg["tts"]["voice"]}}


@app.get("/api/skeleton/{name}")
def skeleton(name: str) -> Dict[str, Any]:
    """원고 뼈대 — 단원(주제)마다 소제목 목록·글자 수. 원본 docx 의 Heading 2 를 읽는다."""
    job = _job(name)
    out = []
    for u in job.units:
        heads: List[str] = []
        if u.raw and u.raw.suffix.lower() == ".docx":
            import docx
            from ..manuscript import _unit_info
            merged = len(_unit_info(u.dir).get("members") or []) > 1
            for p in docx.Document(str(u.raw)).paragraphs:
                st, t = (p.style.name or "").lower(), p.text.strip()
                if t and st.startswith("heading 2"):
                    heads.append(t)
                elif t and merged and st.startswith("heading 1"):
                    heads.append("■ " + t)          # 묶은 단원 — 주제 경계
        from ..manuscript import _unit_info as _ui
        out.append({"prefix": u.prefix, "section": u.section, "heads": heads, "chars": _raw_chars(u),
                    "members": len(_ui(u.dir).get("members") or []) or 1})
    return {"units": out, "title": job.get("title", name)}


@app.get("/api/analyze/{name}")
def analyze(name: str) -> Dict[str, Any]:
    """원고 분석 — 단원마다 글자 수·예상 길이, 목표(분)와 비교한 판단, 추천 묶음."""
    from ..manuscript import _unit_info, est_minutes, propose_groups
    job = _job(name)
    lo = float(job.setting("long", "target_minutes"))
    hi = float(job.setting("long", "target_max_minutes") or lo + 5)
    expand = float(job.setting("long", "natural_expand") or 1.4)
    cps = float(job.setting("long", "chars_per_sec"))
    rows = []
    for u in job.units:
        c = _raw_chars(u)
        rows.append({"prefix": u.prefix, "section": u.section, "chars": c,
                     "min": round(est_minutes(c, expand, cps), 1),
                     "members": len(_unit_info(u.dir).get("members") or []) or 1,
                     "started": (u.dir / "02_rewrite").exists() or (u.dir / "03_script").exists()})
    if not rows:
        return {"rows": [], "groups": [], "lo": lo, "hi": hi}
    total = sum(r["chars"] for r in rows)
    mins = [r["min"] for r in rows]
    avg = sum(mins) / len(mins)
    short = [r for r in rows if r["min"] < lo * 0.8]
    long_ = [r for r in rows if r["min"] > hi * 1.3]
    groups = propose_groups(rows, lo, hi)
    by = {r["prefix"]: r for r in rows}
    gview = [{"prefixes": g, "chars": sum(by[x]["chars"] for x in g), "min": round(sum(by[x]["min"] for x in g), 1),
              "titles": [by[x]["section"] for x in g]} for g in groups]
    merges = sum(1 for g in groups if len(g) > 1)
    already = sum(1 for r in rows if r["members"] > 1)
    out_of = sum(1 for r in rows if not lo * 0.8 <= r["min"] <= hi * 1.3)
    if already or job.get("long.plan_confirmed"):
        verdict = (f"지금 구성: {len(rows)}편 · 편당 평균 {avg:.0f}분" + (f" ({already}편이 묶음)" if already else "")
                   + (f" — 목표({lo:g}~{hi:g}분)에서 많이 벗어난 편 {out_of}개" if out_of else f" — 모두 목표({lo:g}~{hi:g}분) 근처예요."))
    elif not short:
        verdict = f"단원 대부분이 그대로 {lo:g}~{hi:g}분에 맞아요 — 합치지 않아도 돼요."
    elif merges:
        verdict = (f"단원 하나로는 평균 {avg:.0f}분이라 목표({lo:g}~{hi:g}분)보다 짧아요 — "
                   f"이웃한 단원을 묶으면 {len(rows)}편 → {len(groups)}편, 편당 {lo:g}~{hi:g}분에 맞아요(추천).")
    else:
        verdict = f"짧은 단원이 있지만 묶을 짝이 마땅치 않아요 — 그대로 만들면 일부는 {lo:g}분보다 짧아요."
    for r in rows:      # 대본이 실제로 목표로 삼을 길이(원고 길이를 목표 범위 안으로)
        r["target"] = round(max(lo, min(hi, r["min"])), 1)
    return {"rows": rows, "total_chars": total, "avg_min": round(avg, 1), "lo": lo, "hi": hi,
            "short": len(short), "long": len(long_), "groups": gview, "merges": merges, "verdict": verdict,
            "basis": f"원고 1자 ≈ 내레이션 {expand:g}자, 목소리 초당 {cps:g}자로 계산 — 지어내지 않고 풀어 읽을 때의 길이예요."}


class MergeReq(BaseModel):
    prefixes: List[str] = []
    groups: List[List[str]] = []
    force: bool = False


@app.post("/api/job/{name}/merge")
def merge(name: str, req: MergeReq) -> Dict[str, Any]:
    """prefixes 하나를 묶거나, groups(추천 묶음) 여러 개를 한 번에."""
    from ..manuscript import merge_units
    job = _job(name)
    if RUN.busy:
        raise HTTPException(409, "작업이 도는 중에는 묶을 수 없어요")
    groups = [g for g in (req.groups or [req.prefixes]) if len(g) > 1]
    made = []
    try:
        for g in groups:
            made.append(merge_units(job.dir, g, force=req.force).name)
    except SystemExit as e:
        raise HTTPException(400, str(e.code))
    RUN.add(f"단원을 묶었어요: {len(made)}편 — " + ", ".join(made[:4]) + (" …" if len(made) > 4 else ""))
    return {"ok": True, "made": made}


class UnmergeReq(BaseModel):
    prefixes: List[str]
    force: bool = False


@app.post("/api/job/{name}/unmerge")
def unmerge(name: str, req: UnmergeReq) -> Dict[str, Any]:
    from ..manuscript import _unit_info, unmerge_unit
    job = _job(name)
    if RUN.busy:
        raise HTTPException(409, "작업이 도는 중에는 풀 수 없어요")
    targets = req.prefixes or [u.prefix for u in job.units if len(_unit_info(u.dir).get("members") or []) > 1]
    n = 0
    try:
        for p in targets:
            n += len(unmerge_unit(job.dir, p, force=req.force))
    except SystemExit as e:
        raise HTTPException(400, str(e.code))
    RUN.add(f"묶음을 풀었어요 → 단원 {n}개")
    return {"ok": True}


@app.delete("/api/job/{name}")
def delete_job(name: str) -> Dict[str, Any]:
    import shutil
    job = _job(name)
    if RUN.busy:
        raise HTTPException(409, "작업이 도는 중에는 지울 수 없어요")
    shutil.rmtree(job.dir)
    RUN.add(f"작업을 지웠어요: {name}")
    return {"ok": True}


EDITABLE = {"render": ["caption_font", "caption_size", "image_h", "max_line_chars"],
            "image": ["workers"], "youtube": ["visibility", "ai_disclosure"],
            "long": ["target_minutes", "style"], "tts": ["voice", "speed"]}


@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    cfg = config.load()
    return {sec: {k: cfg[sec].get(k) for k in keys} for sec, keys in EDITABLE.items()}


@app.put("/api/config")
def put_config(body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """전체 기본값 — aside.config.local.json 에 덮어 쓴다(작업별 설정이 있으면 그것이 이긴다)."""
    path = config.ROOT / "aside.config.local.json"
    cur = config.read_json(path, {}) or {}
    for sec, keys in EDITABLE.items():
        for k in keys:
            if k in (body.get(sec) or {}):
                cur.setdefault(sec, {})[k] = body[sec][k]
    config.write_json(path, cur)
    return {"ok": True}


@app.get("/api/tools")
def tools() -> Dict[str, Any]:
    import shutil
    from ..llm import claude_cli
    from ..s3_images import exe as codex_exe
    cx = codex_exe()
    cx_status = ""
    if cx:
        try:
            r = subprocess.run([cx, "login", "status"], capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            cx_status = (r.stdout + r.stderr).decode("utf-8", "replace").strip()[:120]
        except Exception as e:      # noqa: BLE001
            cx_status = str(e)[:120]
    return {"claude": claude_cli.auth_status(), "codex": {"installed": bool(cx), "status": cx_status},
            "tts": tts.available(), "ffmpeg": bool(shutil.which("ffmpeg"))}


@app.get("/api/log")
def get_log(since: int = 0) -> Dict[str, Any]:
    start = max(0, since - RUN.base)
    return {"lines": RUN.lines[start:], "next": RUN.base + len(RUN.lines), "busy": RUN.busy}


class RunReq(BaseModel):
    args: List[str]


@app.post("/api/run")
def run(req: RunReq) -> Dict[str, Any]:
    if not req.args or req.args[0] not in ALLOWED:
        raise HTTPException(400, "허용되지 않은 명령이에요")
    RUN.start(req.args)
    return {"ok": True}


@app.post("/api/stop")
def stop() -> Dict[str, Any]:
    RUN.stop()
    return {"ok": True}


class UploadReq(BaseModel):
    job: str
    title: Optional[str] = None
    filename: str
    data: str               # data URL 또는 base64
    stage: str = "raw"


@app.post("/api/upload")
def upload(req: UploadReq) -> Dict[str, Any]:
    from ..manuscript import EXTS, place_book
    name = re.sub(r'[\\/:*?"<>|]', "", req.job).strip()
    if not name:
        raise HTTPException(400, "작업 이름을 정해 주세요")
    fn = os.path.basename(req.filename)
    if Path(fn).suffix.lower() not in EXTS:
        raise HTTPException(400, "docx / txt / md 만 넣을 수 있어요")
    job = Job(name)
    job.dir.mkdir(parents=True, exist_ok=True)
    meta = config.read_json(job.dir / "job.json", {}) or {}
    meta["title"] = (req.title or "").strip() or meta.get("title") or name
    config.write_json(job.dir / "job.json", meta)
    inbox = job.dir / "_inbox"
    inbox.mkdir(exist_ok=True)
    raw = req.data.split(",", 1)[1] if req.data.startswith("data:") else req.data
    src = inbox / fn
    src.write_bytes(base64.b64decode(raw))
    outs = place_book(job.dir, src, "rewrite" if req.stage == "rewrite" else "raw")
    if len(outs) > 1:
        RUN.add(f"원고를 주제 {len(outs)}개로 나눠 넣었어요 → {outs[0].parent.parent.name} … {outs[-1].parent.parent.name}")
    else:
        RUN.add(f"원고를 넣었어요 → {outs[0].parent.parent.name}")
    return {"ok": True, "unit": outs[0].parent.parent.name, "count": len(outs)}


@app.put("/api/job/{name}/settings")
def settings(name: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    job = _job(name)
    meta = config.read_json(job.dir / "job.json", {}) or {}
    lg = meta.setdefault("long", {})
    tt = meta.setdefault("tts", {})
    if body.get("style") in styles.PRESETS:
        lg["style"] = body["style"]
    if body.get("target_minutes"):
        lg["target_minutes"] = float(body["target_minutes"])
    if body.get("target_max_minutes"):
        lg["target_max_minutes"] = float(body["target_max_minutes"])
    if "confirmed" in body:              # 영상 구성(묶기) 확정 — 확정 전에는 만들기 버튼을 막는다
        lg["plan_confirmed"] = bool(body["confirmed"])
    if body.get("thumb_scene"):
        lg["thumb_scene"] = int(body["thumb_scene"])
    if body.get("voice") in tts.VOICES:
        tt["voice"] = body["voice"]
    if body.get("speed"):
        tt["speed"] = float(body["speed"])
    if body.get("title"):
        meta["title"] = str(body["title"])
    config.write_json(job.dir / "job.json", meta)
    return {"ok": True}


@app.get("/api/unit/{name}/{sid}")
def unit(name: str, sid: str) -> Dict[str, Any]:
    job = _job(name)
    u = job.unit(sid)
    if not u:
        raise HTTPException(404, "단원이 없어요")
    from ..manuscript import read_text
    from ..s0_rewrite import load_sections
    script = job.script(sid)
    img_dir = u.bundle_dir / "images"
    baked = config.read_json(img_dir / "baked.json", {}) or {}
    imgs = []
    for sc in scenes(script or {}):
        p = img_dir / f"{sc['n']:03d}.png"
        imgs.append({"n": sc["n"], "chapter": sc["chapter"], "prompt": (sc.get("image") or {}).get("prompt", ""),
                     "chars": (sc.get("image") or {}).get("chars", []),
                     "first": ((sc.get("lines") or [{}])[0]).get("cap", ""),
                     "url": job.url(p) + f"?v={int(p.stat().st_mtime)}" if p.exists() else None,
                     "manual": (baked.get(f"{sc['n']:03d}") or {}).get("manual", False)})
    out = u.bundle_dir / "out"
    video = out / f"{sid}.mp4"
    thumb = out / f"{sid}_thumb.jpg"
    yt = out / f"{sid}_youtube.txt"
    v = lambda p: job.url(p) + f"?v={int(p.stat().st_mtime)}"      # noqa: E731
    return {
        "status": _unit_status(job, u),
        "raw_text": read_text(u.raw) if u.raw else "",
        "rewrite": load_sections(u) if u.rewrite else None,
        "check": config.read_json(u.dir / "02_rewrite" / "검증.json"),
        "script": script, "edited": job.edit_path(sid).exists(),
        "score": config.read_json(u.script_dir / "채점.json"),
        "images": imgs,
        "timing": {k: v_ for k, v_ in (config.read_json(u.bundle_dir / "audio" / "timing.json", {}) or {}).items()
                   if k in ("duration", "voice", "speed", "chapters")},
        "video": v(video) if video.exists() else None,
        "thumb": v(thumb) if thumb.exists() else None,
        "contacts": [v(p) for p in sorted(out.glob("contact_*.jpg"))] if out.exists() else [],
        "youtube": yt.read_text(encoding="utf-8") if yt.exists() else "",
        "folder": str(u.dir),
        "upload": _upload_info(job, sid, script),
    }


def _upload_info(job: Job, sid: str, script: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from ..youtube import meta
    yt = (script or {}).get("youtube") or {}
    m = meta(script, config.read_json(job.sub("audio", sid) / "timing.json")) if script else None
    return {"meta": m, "ready": "hook_lines" in yt, "check": yt.get("check"),
            "state": job.state().get(sid) or {}}


@app.get("/api/accounts")
def accounts_list() -> Dict[str, Any]:
    from .. import accounts
    return {"accounts": accounts.all_(), "current": config.local().get("current")}


class AccReq(BaseModel):
    name: str


class AccAddReq(BaseModel):
    name: str
    label: str = ""
    profile: str = ""


@app.post("/api/accounts/add")
def accounts_add(req: AccAddReq) -> Dict[str, Any]:
    from .. import accounts
    try:
        return {"ok": True, "account": accounts.add(req.name, req.label, req.profile)}
    except SystemExit as e:
        raise HTTPException(400, str(e.code))


@app.post("/api/accounts/use")
def accounts_use(req: AccReq) -> Dict[str, Any]:
    from .. import accounts
    accounts.set_current(req.name)
    return {"ok": True}


@app.put("/api/unit/{name}/{sid}/script")
def save_script(name: str, sid: str, body: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    job = _job(name)
    if not isinstance(body.get("chapters"), list) or not body["chapters"]:
        raise HTTPException(400, "대본 형식이 맞지 않아요(chapters)")
    job.save_edit(sid, body)
    return {"ok": True}


@app.delete("/api/unit/{name}/{sid}/script")
def reset_script(name: str, sid: str) -> Dict[str, Any]:
    _job(name).reset_edit(sid)
    return {"ok": True}


class ImageReq(BaseModel):
    data: str


@app.post("/api/unit/{name}/{sid}/image/{n}")
def replace_image(name: str, sid: str, n: int, req: ImageReq) -> Dict[str, Any]:
    job = _job(name)
    d = job.sub("images", sid)
    d.mkdir(parents=True, exist_ok=True)
    raw = req.data.split(",", 1)[1] if req.data.startswith("data:") else req.data
    key = f"{n:03d}"
    (d / f"{key}.png").write_bytes(base64.b64decode(raw))
    baked = config.read_json(d / "baked.json", {}) or {}
    baked[key] = {"hash": "", "manual": True}
    config.write_json(d / "baked.json", baked)
    return {"ok": True}


class OpenReq(BaseModel):
    path: str


@app.post("/api/open")
def open_folder(req: OpenReq) -> Dict[str, Any]:
    p = Path(req.path).resolve()
    if config.JOBS.resolve() not in p.parents and p != config.JOBS.resolve():
        raise HTTPException(400, "작업 폴더만 열 수 있어요")
    os.startfile(str(p))     # noqa: S606 — 로컬 전용 도구
    return {"ok": True}


@app.get("/jobs/{path:path}")
def job_file(path: str):
    p = (config.JOBS / path).resolve()
    if config.JOBS.resolve() not in p.parents or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def serve(window: bool = True) -> None:
    import uvicorn
    want = int(os.environ.get("ASIDE_LONG_PORT") or config.load()["ui"]["port"])
    port = next((p for p in range(want, want + 20) if _port_free(p)), None)
    if port is None:
        raise SystemExit(f"{want}~{want + 19} 포트가 모두 쓰이고 있어요.")
    url = f"http://127.0.0.1:{port}/"
    if window:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    log(f"롱폼 공방이 켜졌어요 — {url}  (이 창을 닫으면 꺼져요)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)
