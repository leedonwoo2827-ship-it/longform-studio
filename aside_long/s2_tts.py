"""s2-tts — 대본 문장을 SuperTonic3 로 읽히고 한 줄로 이어 붙이며 **문장·씬·장별 시작·끝 시각**을 적는다.

  audio/lines/<해시>.wav   문장별 음성 — 문장·목소리·속도가 같으면 다시 만들지 않는다(긴 영상 이어 하기)
  audio/narration.wav      lead 무음 + 문장들(문장 사이 gap, 씬 사이 scene_gap) + tail 무음
  audio/timing.json        {duration, cues:[{i,scene,chapter,cap,start,end}], scenes:[{n,start,end}], chapters:[…]}
"""
from __future__ import annotations

import hashlib
import json
import shutil
import wave
from pathlib import Path
from typing import Any, Dict, List

from . import config, tts
from .job import Job, scenes
from .log import detail, log
from .supertonic import engine as st


def _h(*parts: Any) -> str:
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _wav_info(p: Path) -> tuple:
    with wave.open(str(p), "rb") as w:
        return w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes()


def spoken(say: str) -> str:
    """엔진이 실제로 읽을 글(발음 사전·약어·연도 적용 뒤) — 캐시 열쇠. 사전을 고치면 그 말이 든 문장만 다시 합성된다."""
    from . import pron
    from .supertonic.pronunciation import load_pronunciation_map
    key = pron.version()
    if _PM.get("v") != key:
        _PM["v"], _PM["m"] = key, load_pronunciation_map(pron.dict_path())
    return _PM["m"].apply(say, spell_unknown_acronyms=True, convert_years=True)


_PM: Dict[str, Any] = {}


def line_file(d: Path, say: str, voice: str, speed: float) -> Path:
    return d / "lines" / f"{_h(spoken(say), voice, speed)}.wav"


def run(job: Job, only=None, force: bool = False, **_) -> None:
    c = config.load()["tts"]
    voice = str(job.setting("tts", "voice")).upper()
    speed = float(job.setting("tts", "speed"))
    for u in job.pick(only):
        sid = u.prefix
        script = job.script(sid)
        if not script:
            log(f"  · {sid} 대본이 없어요 — 「대본 쓰기」를 먼저 해 주세요.")
            continue
        ss = scenes(script)
        items = [(sc, ln) for sc in ss for ln in sc.get("lines") or []]
        d = job.sub("audio", sid)
        ld = d / "lines"
        ld.mkdir(parents=True, exist_ok=True)
        key = _h([spoken(ln.get("say") or ln.get("cap") or "") for _, ln in items], voice, speed,
                 c["gap"], c["scene_gap"], c["lead"], c["tail"])
        old = config.read_json(d / "timing.json", {}) or {}
        if old.get("hash") == key and (d / "narration.wav").exists() and not force:
            log(f"  {sid} 음성은 그대로예요 ({old.get('duration', 0) / 60:.1f}분).")
            continue
        if not tts.available():
            raise tts.TTSUnavailable("SuperTonic3 모델이 없어요 — setup.bat 을 다시 실행하거나 run.bat tts-setup")
        eng = st.Engine.get()
        files: List[Path] = []
        made = 0
        for i, (sc, ln) in enumerate(items):
            say = (ln.get("say") or ln.get("cap") or "").strip() or "..."
            p = line_file(d, say, voice, speed)
            old = ld / f"{_h(say, voice, speed)}.wav"       # 예전 열쇠(사전 적용 전 글)로 만든 것 — 소리는 같다, 옮겨 쓴다
            if not p.exists() and old.exists() and not force:
                shutil.copy2(old, p)
            if not p.exists() or force:
                st.write_wav(p, eng.synth(say, voice=voice, speed=speed), eng.sample_rate)
                made += 1
                if made % 20 == 0:
                    log(f"    … 문장 {i + 1}/{len(items)}")
            files.append(p)
        detail(f"s2 {sid}: 새로 {made} · 재사용 {len(files) - made}")
        rate, width, ch, _n = _wav_info(files[0])
        silence = lambda sec: b"\x00" * (int(rate * sec) * width * ch)   # noqa: E731
        cues, t = [], float(c["lead"])
        with wave.open(str(d / "narration.wav"), "wb") as wo:
            wo.setnchannels(ch)
            wo.setsampwidth(width)
            wo.setframerate(rate)
            wo.writeframes(silence(float(c["lead"])))
            for i, ((sc, ln), f) in enumerate(zip(items, files)):
                if i:
                    gap = float(c["scene_gap"]) if sc["n"] != items[i - 1][0]["n"] else float(c["gap"])
                    wo.writeframes(silence(gap))
                    t += gap
                with wave.open(str(f), "rb") as wi:
                    n = wi.getnframes()
                    wo.writeframes(wi.readframes(n))
                cues.append({"i": i, "scene": sc["n"], "chapter": sc["chapter"], "cap": ln.get("cap") or ln.get("say", ""),
                             "start": round(t, 3), "end": round(t + n / rate, 3), "file": f.name})
                t += n / rate
            wo.writeframes(silence(float(c["tail"])))
            total = t + float(c["tail"])
        sc_rows = []
        for sc in ss:
            mine = [q for q in cues if q["scene"] == sc["n"]]
            sc_rows.append({"n": sc["n"], "chapter": sc["chapter"], "start": mine[0]["start"] if mine else 0})
        for k, row in enumerate(sc_rows):     # 씬은 다음 씬 첫 문장 직전까지 — 첫 씬은 0초부터, 마지막은 끝까지
            row["start"] = 0.0 if k == 0 else round(row["start"] - float(c["scene_gap"]) / 2, 3)
        for k, row in enumerate(sc_rows):
            row["end"] = sc_rows[k + 1]["start"] if k + 1 < len(sc_rows) else round(total, 3)
        chapters = []
        for ci, chp in enumerate(script.get("chapters") or [], 1):
            first = next((r for r in sc_rows if r["chapter"] == ci), None)
            if first:
                chapters.append({"i": ci, "title": chp.get("title", ""), "start": first["start"]})
        config.write_json(d / "timing.json", {"duration": round(total, 3), "voice": voice, "speed": speed,
                                              "cues": cues, "scenes": sc_rows, "chapters": chapters, "hash": key})
        log(f"  ✓ {sid} 음성 {total / 60:.1f}분 (문장 {len(cues)}개, 새로 만든 문장 {made}개)")
