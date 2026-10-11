"""원고 — 절(1장 2절) 하나 = 단원 폴더 하나.

jobs/<job>/<장>-<절>_<절 제목>/          예: 1-2_통합적 관점의 필요성과 적용
  01_raw/<원본>.docx       교과서 원문 — **보관만, 절대 고치지 않는다**
  02_rewrite/<원본>.docx   안전 개작본(개념·정의 유지, 사례 교체) + 검증.json   ← s0-rewrite
  03_script/<id>.json      관점별 쇼츠 대본(s1) · <id>.edit.json(사람 수정)
  04_bundle/<id>/          쇼츠 한 편의 모든 것: audio/ images/ motion/ out/<id>.mp4

쇼츠 번호 <장>-<절>-<NN> (예: 1-2-04 = 1장 2절의 4번째 관점).
원고 파일 이름 `<N>장_<장 제목>-<M>절_<절 제목>.docx` 에서 장·절을 읽는다.
"""
from __future__ import annotations

import hashlib
import html
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

EXTS = (".docx", ".txt", ".md")
RAW, REWRITE, SCRIPT, BUNDLE = "01_raw", "02_rewrite", "03_script", "04_bundle"
_NAME = re.compile(r"^\s*(\d+)\s*장[_\s]*(.*?)\s*-\s*(\d+)\s*절[_\s]*(.*)$")
_NAME2 = re.compile(r"^\s*(\d+)\s+(.+?)\s*-\s*(\d+)\s*(.*)$")       # 「2 장제목-1 절제목」
_UNIT = re.compile(r"^(\d+)-(\d+)_(.*)$")


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:br\s*/?>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/?>", "\t", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return docx_text(path)
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def parse_name(stem: str) -> Optional[tuple]:
    """'1장_통합적 관점-2절_통합적 관점의 필요성과 적용' → ('1-2', '1장 통합적 관점', '2절 통합적 관점의 필요성과 적용')

    「장」「절」 글자가 없는 꼴도 읽는다(2026-10-05 실제 원고 이름):
      '2 인간,사회,환경과행복-1 행복의 기준과 의미'  → 2장 1절
      '4 문화의 다양성 - 2 문화 변동의 양상'          → 4장 2절
      '3 자연환경과 인간-4환경 문제의 발생과 …'       → 3장 4절"""
    m = _NAME.match(stem) or _NAME2.match(stem)
    if not m:
        return None
    ch, ch_t, sec, sec_t = m.groups()
    return f"{int(ch)}-{int(sec)}", f"{int(ch)}장 {ch_t}".strip(), f"{int(sec)}절 {sec_t}".strip()


def unit_dirname(prefix: str, section_title: str) -> str:
    t = re.sub(r"^\d+절\s*", "", section_title).strip()
    t = re.sub(r'[\\/:*?"<>|]', "", t)[:40].strip()
    return f"{prefix}_{t}" if t else prefix


def _docs_in(d: Path) -> List[Path]:
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in EXTS and not p.name.startswith("~$"))


@dataclass
class Unit:
    dir: Path
    prefix: str             # "1-2"
    chapter: str            # "1장 통합적 관점"
    section: str            # "2절 통합적 관점의 필요성과 적용"

    @property
    def raw(self) -> Optional[Path]:
        f = _docs_in(self.dir / RAW)
        return f[0] if f else None

    @property
    def rewrite(self) -> Optional[Path]:
        f = _docs_in(self.dir / REWRITE)
        return f[0] if f else None

    @property
    def source(self) -> Optional[Path]:
        """대본(s1)의 재료 — **개작본만**. 원문은 쓰지 않는다."""
        return self.rewrite

    @property
    def text(self) -> str:
        return read_text(self.source) if self.source else ""

    @property
    def hash(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:12]

    @property
    def script_dir(self) -> Path:
        return self.dir / SCRIPT

    @property
    def bundle_dir(self) -> Path:
        return self.dir / BUNDLE

    @property
    def name(self) -> str:
        return self.dir.name


def _unit_info(d: Path) -> dict:
    import json
    try:
        return json.loads((d / "unit.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def units_in(job_dir: Path) -> List[Unit]:
    out = []
    if not job_dir.exists():
        return out
    for d in sorted(job_dir.iterdir(), key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p.name)]):
        m = _UNIT.match(d.name)
        if not d.is_dir() or not m:
            continue
        prefix = f"{int(m.group(1))}-{int(m.group(2))}"
        any_doc = (_docs_in(d / RAW) or _docs_in(d / REWRITE) or [None])[0]
        meta = parse_name(any_doc.stem) if any_doc else None
        ch, sec = (meta[1], meta[2]) if meta else ("", m.group(3))
        info = _unit_info(d)
        if info.get("title"):       # 묶은 단원·책에서 나눈 단원 — 폴더 이름(40자로 잘림) 대신 저장해 둔 제목
            sec = info["title"]
        out.append(Unit(d, prefix, ch, sec))
    return out


# ── 가져오기 ─────────────────────────────────────────────────────────────────
def classify_sources(src: Path) -> List[dict]:
    """--from 에 준 곳에서 원고를 찾아 [{file, stage: raw|rewrite}] 로.

    260711 형식(절 폴더 안의 01_raw · 02_)이면 01_raw → 원본, 02_ → 개작본으로 나눠 둘 다 가져온다.
    그냥 폴더·파일이면 모두 **원본**(s0-rewrite 가 개작본을 만든다)."""
    src = Path(src)
    if src.is_file():
        return [{"file": src, "stage": "raw"}]
    if not src.is_dir():
        return []
    out = []
    raw_dirs = [p for p in src.rglob("01_raw") if p.is_dir()]
    rw_dirs = [p for p in src.rglob("02_") if p.is_dir()]
    if raw_dirs or rw_dirs:
        for d in raw_dirs:
            out += [{"file": p, "stage": "raw"} for p in _docs_in(d) if "복사본" not in p.stem]
        for d in rw_dirs:
            out += [{"file": p, "stage": "rewrite"} for p in _docs_in(d) if "복사본" not in p.stem]
        return out
    return [{"file": p, "stage": "raw"} for p in _docs_in(src)]


_TOPIC = re.compile(r"^\s*주제\s*(\d+)\s*(.*)$")


def topics(file: Path) -> List[tuple]:
    """책 한 권 docx → [(주제 번호, 제목, [(스타일, 글)…])]. 「주제 NN」 Heading 1 이 둘 이상일 때만(아니면 빈 목록)."""
    if file.suffix.lower() != ".docx":
        return []
    import docx
    out: List[tuple] = []
    cur = None
    for p in docx.Document(str(file)).paragraphs:
        style = (p.style.name or "").lower()
        t = p.text.strip()
        m = _TOPIC.match(t) if style.startswith("heading 1") else None
        if m:
            cur = (int(m.group(1)), m.group(2).strip(), [])
            out.append(cur)
        elif style.startswith("heading 1"):
            cur = None                  # 대단원 제목 등 — 주제 밖
        elif cur is not None and t:
            cur[2].append(("h2" if style.startswith("heading 2") else "p", t))
    return out if len(out) >= 2 else []


def place_book(job_dir: Path, file: Path, stage: str) -> List[Path]:
    """원고를 넣는다. 책 한 권(주제 여러 개)이면 **주제마다 단원 하나**로 나눠 넣는다 — 단원 번호 <권>-<주제 번호>."""
    tops = topics(file)
    if not tops:
        return [place(job_dir, file, stage)]
    import docx
    units = units_in(job_dir)
    tag = f"_{file.stem}"[:60]
    mine = {u.prefix.split("-")[0] for u in units
            if any(tag in p.stem for p in _docs_in(u.dir / RAW) + _docs_in(u.dir / REWRITE))}
    used = {u.prefix.split("-")[0] for u in units}
    book = next(iter(mine)) if mine else str(next(i for i in range(1, 100) if str(i) not in used))
    split = job_dir / "_inbox" / "split"
    split.mkdir(parents=True, exist_ok=True)
    outs = []
    for num, title, body in tops:
        d = docx.Document()
        d.add_heading(f"주제 {num:02d}  {title}", level=1)
        for kind, t in body:
            d.add_heading(t, level=2) if kind == "h2" else d.add_paragraph(t)
        f = split / f"주제{num:02d}{tag}.docx"
        d.save(str(f))
        out = place(job_dir, f, stage, prefix=f"{book}-{num}", title=title)
        _write_info(out.parent.parent, {"title": title, "members": [{"prefix": f"{book}-{num}", "title": title,
                                                                     "file": str(f), "stage": stage}]})
        outs.append(out)
    return outs


def _write_info(d: Path, info: dict) -> None:
    import json
    (d / "unit.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 원고 분석 · 합치기 ─────────────────────────────────────────────────────────
def clamp_minutes(est: float, lo: float, hi: float) -> float:
    """대본 목표 길이 = 원고가 허락하는 길이를 [lo, hi] 안으로 — 짧은 원고는 lo 까지 풀어 쓰고, 긴 원고는 hi 로 압축."""
    return max(lo, min(hi, est))


def est_minutes(chars: int, expand: float, cps: float) -> float:
    """원고 글자 수 → 지어내지 않고 풀어 읽을 때의 영상 길이(분). 내레이션 ≈ 원고 × expand, TTS 초당 cps 자."""
    return chars * expand / cps / 60


def propose_groups(rows: List[dict], lo: float, hi: float) -> List[List[str]]:
    """앞에서부터 이어 붙여 [lo, hi]분에 들도록 묶는다. 이미 lo 이상인 단원은 혼자. 끝에 짧게 남으면 앞 묶음에 붙인다."""
    groups: List[List[dict]] = []
    cur: List[dict] = []
    for r in rows:
        now = sum(x["min"] for x in cur)
        if cur and now + r["min"] > hi * (1.15 if now < lo else 1.0):     # 아직 목표 미달이면 조금 넘쳐도 붙인다
            groups.append(cur)
            cur = []
        cur.append(r)
        if sum(x["min"] for x in cur) >= lo:
            groups.append(cur)
            cur = []
    if cur:
        if groups and sum(x["min"] for x in cur) < lo * 0.6:
            groups[-1] += cur
        else:
            groups.append(cur)
    return [[x["prefix"] for x in g] for g in groups]


def merge_units(job_dir: Path, prefixes: List[str], force: bool = False) -> Path:
    """단원 여러 개 → 영상 한 편. 각 주제는 Heading 1 로 남아 개작·대본에서 장(chapter)이 된다.
    이미 개작·대본이 있는 단원은 force 일 때만(그 결과는 버린다 — 원고가 바뀌니까)."""
    import shutil
    import docx
    us = {u.prefix: u for u in units_in(job_dir)}
    sel = [us[p] for p in prefixes if p in us]
    if len(sel) < 2:
        raise SystemExit("묶으려면 단원을 둘 이상 고르세요")
    sel.sort(key=lambda u: [int(x) for x in u.prefix.split("-")])
    done = [u.prefix for u in sel if (u.dir / REWRITE).exists() or (u.dir / SCRIPT).exists()]
    if done and not force:
        raise SystemExit(f"이미 개작·대본이 있는 단원이 있어요({', '.join(done)}) — 묶으면 그 결과는 지워져요")
    members = []
    keep = job_dir / "_inbox" / "split"
    keep.mkdir(parents=True, exist_ok=True)
    for u in sel:
        info = _unit_info(u.dir)
        if not info.get("members"):     # 이 기능 전에 넣은 단원 — 원고를 _inbox/split 에 남겨 두고 풀 때 쓴다
            kept = keep / u.raw.name
            if not kept.exists():
                shutil.copy2(u.raw, kept)
            info["members"] = [{"prefix": u.prefix, "title": u.section, "file": str(kept), "stage": "raw"}]
        members += info["members"]
    d = docx.Document()
    for u in sel:
        src = docx.Document(str(u.raw))
        first = True
        for p in src.paragraphs:
            t = p.text.strip()
            if not t:
                continue
            style = (p.style.name or "").lower()
            if style.startswith("heading 1") or first and _TOPIC.match(t):
                d.add_heading(t, level=1)
            elif style.startswith("heading 2"):
                d.add_heading(t, level=2)
            else:
                d.add_paragraph(t)
            first = False
    nums = [m["prefix"].split("-")[1] for m in members]
    title = " · ".join(m["title"] for m in members)
    mdir = job_dir / "_inbox" / "merged"
    mdir.mkdir(parents=True, exist_ok=True)
    f = mdir / f"주제{int(nums[0]):02d}-{int(nums[-1]):02d}.docx"
    d.save(str(f))
    for u in sel:
        shutil.rmtree(u.dir)
    out = place(job_dir, f, "raw", prefix=sel[0].prefix, title=title)
    _write_info(out.parent.parent, {"title": title, "members": members})
    return out.parent.parent


def unmerge_unit(job_dir: Path, prefix: str, force: bool = False) -> List[Path]:
    """묶음 풀기 — 저장해 둔 주제별 원고로 다시 단원 여러 개."""
    import shutil
    u = next((x for x in units_in(job_dir) if x.prefix == prefix), None)
    if not u:
        raise SystemExit(f"단원이 없어요: {prefix}")
    members = _unit_info(u.dir).get("members") or []
    if len(members) < 2:
        raise SystemExit("묶은 단원이 아니에요")
    if ((u.dir / REWRITE).exists() or (u.dir / SCRIPT).exists()) and not force:
        raise SystemExit("이미 개작·대본이 있어요 — 풀면 그 결과는 지워져요")
    shutil.rmtree(u.dir)
    outs = []
    for m in members:
        out = place(job_dir, Path(m["file"]), m.get("stage", "raw"), prefix=m["prefix"], title=m["title"])
        _write_info(out.parent.parent, {"title": m["title"], "members": [m]})
        outs.append(out.parent.parent)
    return outs


def place(job_dir: Path, file: Path, stage: str, n: int = 1, prefix: Optional[str] = None,
          title: Optional[str] = None) -> Path:
    """원고 하나를 단원 폴더의 01_raw 또는 02_rewrite 에 복사한다. 단원 폴더가 없으면 만든다."""
    import shutil
    meta = parse_name(file.stem)
    if prefix:
        sec = title or file.stem
    elif meta:
        prefix, sec = meta[0], meta[2]
    else:       # 「Scan_…_본문.docx」처럼 장·절이 없는 이름 — 다음 번호 + 문서 첫 줄(대제목)을 단원 이름으로
        same = next((u for u in units_in(job_dir)
                     if any(p.name == file.name for p in _docs_in(u.dir / RAW) + _docs_in(u.dir / REWRITE))), None)
        used = {u.prefix for u in units_in(job_dir)}
        prefix = same.prefix if same else f"1-{next(i for i in range(1, 1000) if f'1-{i}' not in used)}"
        first = next((t.strip() for t in read_text(file).splitlines() if t.strip()), file.stem)
        sec = re.sub(r"^주제\s*\d+\s*", "", first)
    unit = next((u for u in units_in(job_dir) if u.prefix == prefix), None)
    d = unit.dir if unit else job_dir / unit_dirname(prefix, sec)
    target = d / (RAW if stage == "raw" else REWRITE)
    target.mkdir(parents=True, exist_ok=True)
    for old in _docs_in(target):          # 단계 폴더 하나에 원고는 하나
        if old.name != file.name:
            old.unlink()
    out = target / file.name
    shutil.copy2(file, out)
    return out
