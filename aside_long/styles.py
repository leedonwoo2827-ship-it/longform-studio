"""화풍 프리셋 + Codex 인계 메모(HANDOFF.md).

참고 영상의 방식: 화풍은 취향이 아니라 **규격**으로 고정하고, 인물마다 생김새 문장 하나를 정해 매 컷 프롬프트 맨 앞에 붙인다.
그림을 넘길 때는 인계 메모를 한 장 꼭 써서 Codex 가 지시를 잘못 읽어도 규칙대로 그리게 한다.
화풍은 **글로만** 정의한다 — 교과서·다른 채널 그림을 참조 이미지로 넣지 않는다(구도·인물 배치 모방 금지).
"""
from __future__ import annotations

from typing import Any, Dict, List

from .job import Job

PRESETS: Dict[str, Dict[str, str]] = {
    "textbook": {
        "name": "교과서 상상도",
        "style": ("Warm hand-painted digital illustration in the manner of an educational history book's "
                  "'imagined scene': gouache and soft watercolor texture, gentle clean outlines, bright natural "
                  "daylight, earthy ochre / sage green / sky blue palette, friendly expressive people at work, "
                  "slightly elevated viewpoint, rich but uncluttered detail."),
    },
    "ink": {
        "name": "수묵담채",
        "style": ("Korean ink-wash painting with light watercolor (sumuk-damchae): expressive black ink brush "
                  "lines with soft color bleeding into textured hanji paper, muted warm palette, misty atmospheric "
                  "depth, semi-realistic people, painterly and calm."),
    },
}

RULES = [
    "16:9 landscape, at least 1536x864.",
    "Absolutely no text, letters, numbers, labels, captions, signatures or watermarks anywhere in the image.",
    "Not photorealistic. No real or famous people's faces. Anatomically correct hands (five fingers).",
    "Keep the bottom fifth of the frame calm and simple (subtitles go there); keep faces and key artifacts in the upper three quarters.",
    "Characters: when a cut lists a character, start from that character's fixed look sentence exactly. "
    "Never copy a character into a cut that does not list them.",
    "Historical setting must match the era described in the cut (no anachronistic objects).",
]


def names() -> List[str]:
    return list(PRESETS)


def preset(job: Job) -> Dict[str, str]:
    return PRESETS.get(str(job.setting("long", "style")), PRESETS["textbook"])


# 모든 컷에 붙는 금지어 — 그림 도구에 negative 칸이 없어 프롬프트 끝에 「Avoid: …」로 붙인다(backplate compose_prompt 방식)
NEGATIVE = ("text, letters, numbers, captions, labels, signage, signature, watermark, logo, speech bubble, frame, border, "
            "photorealistic photo, 3d render, real or famous person's face, modern objects, anachronism, "
            "extra fingers, extra limbs, distorted hands, deformed face, low quality, blurry, transparent background")


def cut_prompt(job: Job, script: Dict[str, Any], scene: Dict[str, Any]) -> str:
    """한 컷의 최종 프롬프트 — backplate 식 한 줄 한 항목(인물/장면/구도/색·톤/글자/여백/규격). 모든 컷이 같은 줄 규칙을 받아 톤이 고르다."""
    looks = {c["id"]: c for c in script.get("characters") or []}
    im = scene.get("image") or {}
    who = [f"{looks[c]['name']} ({c}): {looks[c]['look']}" for c in im.get("chars") or [] if c in looks]
    lines = []
    if who:
        lines.append("Characters (draw exactly as described; nobody else from the cast): " + " | ".join(who))
    else:
        lines.append("Characters: none of the recurring cast appear in this cut.")
    lines += [
        "Scene: " + (im.get("prompt") or "").strip(),
        "Era: " + (script.get("topic") or "Korean history"),
        "Art style: " + preset(job)["style"],
        "Color/tone: consistent across the whole series — same palette, same brushwork, same lighting mood.",
        "Text: absolutely no letters, numbers or labels anywhere in the image.",
        "Layout: keep faces and key objects in the upper three quarters; the bottom fifth stays calm and simple (subtitles go there).",
        "Format: 16:9 landscape illustration, full-bleed, opaque background.",
    ]
    return "\n".join(lines)


def handoff(job: Job, script: Dict[str, Any]) -> str:
    p = preset(job)
    chars = "\n".join(f"- {c['id']} {c['name']}: {c['look']}" for c in script.get("characters") or []) or "- (none)"
    rules = "\n".join(f"- {r}" for r in RULES)
    return (f"# HANDOFF — image cuts for「{script.get('title', '')}」\n\n"
            f"Era/topic: {script.get('topic', '')}\n\n"
            f"## Fixed art style ({p['name']})\n{p['style']}\n\n"
            f"## Rules (must follow)\n{rules}\n\n"
            f"## Fixed character looks (prepend exactly when the cut lists the id)\n{chars}\n")


def write_handoff(job: Job, sid: str, script: Dict[str, Any]) -> None:
    d = job.sub("images", sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "HANDOFF.md").write_text(handoff(job, script), encoding="utf-8")
