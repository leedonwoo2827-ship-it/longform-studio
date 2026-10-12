"""발음 — 자막(눈으로 읽는 글) → 읽는 소리(TTS 가 글자 그대로 읽는 글). 발음 사전.

규칙은 backplate_showcase-master 의 core/honorific.py(speak_numbers·sino) · core/roman.py(speak_roman·find_roman·spell)
를 그대로 옮겼다(그쪽에서 실제 소리로 검증된 규칙). 문체 바꾸기(하십시오체)는 뺐다 — 롱폼 대본은 이미 구어체다.

사전: 프로젝트의 `pronunciation.yaml`(`rules: {영문: 읽는 소리}`) — 처음엔 엔진 기본 사전을 복사해 만든다.
SuperTonic3 엔진도 합성 직전에 같은 사전을 한 번 더 적용한다(supertonic/engine.py).
"""
from __future__ import annotations

import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

from . import config

BASE_DICT = Path(__file__).parent / "supertonic" / "pronunciation_map.yaml"


def dict_path() -> Path:
    p = config.ROOT / "pronunciation.yaml"
    if not p.exists() and BASE_DICT.exists():
        shutil.copy2(BASE_DICT, p)
    return p


_cache: Dict[str, object] = {}


def rules() -> Dict[str, str]:
    p = dict_path()
    m = p.stat().st_mtime if p.exists() else 0
    if _cache.get("m") != m:
        try:
            raw = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("rules") or {}
        except Exception:       # noqa: BLE001
            raw = {}
        _cache["m"], _cache["r"] = m, {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    return dict(_cache["r"])        # type: ignore[arg-type]


def save_rules(new: Dict[str, str]) -> int:
    p = dict_path()
    if p.exists():
        shutil.copy2(p, p.with_suffix(".yaml.bak"))
    clean = {str(k).strip(): str(v).strip() for k, v in (new or {}).items() if str(k).strip() and str(v).strip()}
    body = yaml.safe_dump({"rules": dict(sorted(clean.items(), key=lambda kv: kv[0].lower()))},
                          allow_unicode=True, sort_keys=False)
    p.write_text("# 발음 사전 — 영문(또는 표기) : 읽는 소리. 대시보드 「발음 사전」에서 고친다.\n" + body, encoding="utf-8")
    _cache.clear()
    return len(clean)


def version() -> str:
    """사전이 바뀌었는지 — 목소리 캐시 열쇠에 넣는다(사전을 고치면 그 말이 든 문장만 다시 합성)."""
    p = dict_path()
    return str(int(p.stat().st_mtime)) if p.exists() else "0"


# ── 숫자 (honorific.py) ──────────────────────────────────────────────────────
_SINO_D = "영일이삼사오육칠팔구"
_SINO_U = ["", "십", "백", "천"]
_SINO_G = ["", "만", "억", "조", "경"]
_NATIVE_UNIT = ("시간", "차례", "군데", "켤레", "가지", "그루", "송이", "봉지", "상자", "자루", "갈래", "마리", "사람",
                "개", "명", "번", "살", "곳", "달", "권", "배", "쪽", "줄", "칸", "판", "대", "채", "벌", "쌍", "병", "잔", "척", "통", "시")
_NATIVE = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열", "열한", "열두", "열세", "열네",
           "열다섯", "열여섯", "열일곱", "열여덟", "열아홉", "스무"]
_JOSA_HEADS = ("으로", "부터", "까지", "보다", "처럼", "이나", "이며", "이고", "로", "은", "는", "이", "가", "을", "를",
               "의", "에", "와", "과", "만", "씩", "째", "나", "밖", "입", "였")
_NUM_RE = re.compile(r"(\d[\d,]*)(\.\d+)?")


def _sino4(n: int) -> str:
    out = ""
    for p in range(3, -1, -1):
        d = (n // 10 ** p) % 10
        if d:
            out += ("" if (d == 1 and p) else _SINO_D[d]) + _SINO_U[p]
    return out


def sino(n: int) -> str:
    """한자어 수. 19→십구, 2026→이천이십육, 12345→일만이천삼백사십오."""
    n = int(n)
    if n < 0:
        return "마이너스 " + sino(-n)
    if n == 0:
        return "영"
    if n < 10000:
        return _sino4(n)
    parts: List[str] = []
    g = 0
    while n and g < len(_SINO_G):
        chunk = n % 10000
        if chunk:
            parts.append(_sino4(chunk) + _SINO_G[g])
        n //= 10000
        g += 1
    return (str(n) if n else "") + "".join(reversed(parts))


def _josa_next(s: str) -> bool:
    return any(s.startswith(j) for j in _JOSA_HEADS)


def speak_numbers(text: str) -> str:
    """아라비아 숫자를 소리대로. 1960년대→천구백육십 년대, 2.0→이 쩜 영, 3개→세 개, 250%→이백오십 퍼센트."""
    def one(m: "re.Match") -> str:
        head, frac, tail = m.group(1).replace(",", ""), m.group(2), text[m.end():]
        unit = ""
        for u in _NATIVE_UNIT:
            if tail.startswith(u):
                rest = tail[len(u):]
                if not rest or not ("가" <= rest[0] <= "힣") or _josa_next(rest):
                    unit = u
                    break
        try:
            n = int(head)
        except ValueError:
            return m.group(0)
        if frac:
            said = f"{sino(n)} 쩜 " + " ".join(_SINO_D[int(c)] for c in frac[1:])
        elif unit and 1 <= n <= 20:
            said = _NATIVE[n]
        else:
            said = sino(n)
        nxt = tail[:1]
        gap = nxt and not nxt.isspace() and nxt not in ",.·)]%" and not _josa_next(tail)
        return said + (" " if gap else "")

    out = _NUM_RE.sub(one, text or "")
    return re.sub(r"\s*%", lambda m: "퍼센트" if m.start() == 0 else " 퍼센트", out)


# ── 영문 (roman.py) ──────────────────────────────────────────────────────────
LETTER = {"A": "에이", "B": "비", "C": "씨", "D": "디", "E": "이", "F": "에프", "G": "지", "H": "에이치", "I": "아이",
          "J": "제이", "K": "케이", "L": "엘", "M": "엠", "N": "엔", "O": "오", "P": "피", "Q": "큐", "R": "알", "S": "에스",
          "T": "티", "U": "유", "V": "브이", "W": "더블유", "X": "엑스", "Y": "와이", "Z": "제트"}
_REST_RE = re.compile(r"(?<![A-Za-z])[A-Za-z][A-Za-z_]{1,}(?![A-Za-z])")
_JOSA = ("은|는|이가|이|가|을|를|와|과|의|에서|에게|에|으로|로|도|만|밖에|부터|까지|처럼|같이|보다|이나|나|이란|란|이라|라"
         "|인지|인|이고|고|이며|며|이든|든|조차|마저|이야|야|이다|다")
_TAIL = r"(?:[ \t]+(" + _JOSA + r")(?![가-힣]))?"


def is_acronym(word: str) -> bool:
    w = word.replace("_", "")
    return w.isupper() and 2 <= len(w) <= 4


def spell(word: str) -> str:
    """짧은 대문자 약어만 글자 이름으로(SQL→에스큐엘). 아니면 빈 값 — 엉뚱한 제안은 없느니만 못하다."""
    return "".join(LETTER.get(c.upper(), c) for c in word.replace("_", " ")) if is_acronym(word) else ""


def find_roman(text: str) -> List[str]:
    return _REST_RE.findall(text or "")


def speak_roman(text: str) -> str:
    """영문을 한국어 소리로(사전 → 짧은 약어는 글자 이름). 바꾼 자리 뒤에 띄어 쓴 조사는 붙인다(「NULL 을」→「널을」)."""
    if not text:
        return text
    tbl = rules()
    if tbl:
        keys = sorted(tbl, key=len, reverse=True)
        pat = re.compile(r"(?<![A-Za-z])(" + "|".join(map(re.escape, keys)) + r")(?![A-Za-z])" + _TAIL)
        text = pat.sub(lambda m: tbl[m.group(1)] + (m.group(2) or ""), text)
    rest = re.compile(r"(?<![A-Za-z])([A-Za-z][A-Za-z_]{1,})(?![A-Za-z])" + _TAIL)
    text = rest.sub(lambda m: (spell(m.group(1)) + (m.group(2) or "")) if is_acronym(m.group(1)) else m.group(0), text)
    return re.sub(r"[ \t]{2,}", " ", text)


_PAREN = re.compile(r"[ \t]*[(\[（［][^)\]）］]*[)\]）］]")


def for_speech(text: str) -> str:
    """자막 → 읽는 소리: 괄호 걷기 → 숫자 → 영문. 따옴표도 소리에는 군더더기라 뺀다."""
    t = _PAREN.sub("", text or "")
    t = re.sub(r"[\"“”'‘’「」『』]", "", t)
    return speak_roman(speak_numbers(t)).strip()


def missing(texts: Iterable[str], limit: int = 200) -> List[Dict[str, object]]:
    """글들에 나오는데 사전에 없는 영문 — 많이 나온 순서로, 첫 제안(spell)과 함께."""
    tbl = rules()
    cnt = Counter(w for t in texts for w in find_roman(t))
    out = [{"word": w, "n": n, "guess": spell(w)} for w, n in cnt.most_common() if w not in tbl]
    return out[:limit]
