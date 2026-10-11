# 롱폼 공방 (aside-long)

교과서 원고 docx 를 넣으면 **저작권 안전하게 다시 쓴 내레이션 롱폼 영상(1920×1080 mp4)** 과 썸네일이 나오는 웹 콘솔.
구조는 `D:\00work\261004-aside-youtube-claude`(통합사회 쇼츠)에서 가져왔고, 세로 쇼츠 → 가로 롱폼으로 바꿨다.
제작 방식은 참고 영상(리드어세상 「시니어 롱폼 딸깍」)을 따른다: Claude 가 지휘(대본·채점), Codex 가 그림, 인계 메모, 3개 동시.

## 처음 한 번
1. `setup.bat` — 가상환경·패키지, SuperTonic3(쇼츠 프로젝트에 받아 둔 것을 같이 씀), 점검
2. `claude auth login` (Claude 구독) · `codex login` (ChatGPT 계정 — 그림)
3. `run.bat` — 대시보드가 브라우저로 열린다 (http://127.0.0.1:5294/)

## 흐름 (단원 하나 = 영상 한 편)
| 단계 | 하는 일 | 결과 (jobs/<작업>/<단원>/) |
|---|---|---|
| 1 안전 개작 | Claude 가 원문을 다른 글로 재집필. 코드가 검사: 원문과 15자 넘게 같은 구간 없음, 문단 유사도 ≤ 0.5, 핵심어 보존 (실패 시 2번까지 다시) | `02_rewrite/*.docx`, `sections.json`, `검증.json` |
| 2 대본·채점 | 개작본으로 장→씬→문장 대본 + 인물 고정 문장 + 썸네일·유튜브 문구. 다른 Claude 가 100점 채점, 80점 미만이거나 원문 겹침이 있으면 한 번 고쳐 씀 | `03_script/<id>.json`, `채점.json` |
| 3 목소리 | SuperTonic3 문장별 합성(문장 캐시 — 고친 문장만 다시) | `04_bundle/audio/narration.wav`, `timing.json` |
| 4 그림 | 인계 메모 `images/HANDOFF.md` + 컷 프롬프트 → `codex exec` 3개 동시, 씬당 1장 | `04_bundle/images/NNN.png` |
| 5 영상 | ffmpeg: 켄번스 줌/팬 + 붓 가장자리 마스크 + 한지 바탕 + 고딕 자막(Pretendard SemiBold 68px), 씬별 클립 캐시 | `04_bundle/out/<id>.mp4`, `contact_NN.jpg` |
| 6 썸네일 | 대표 그림 + 두 줄 문구(키워드 강조색) | `04_bundle/out/<id>_thumb.jpg` |

- **책 한 권 docx 를 넣으면 「주제 NN」(Heading 1)마다 단원 하나로 자동 분리**된다(예: 고등학교 한국사1 → 1-1 … 1-31). 원본 분할본은 `_inbox/split/`.
- 「모든 단원 1→6」은 단원을 차례로 끝까지, 「개작·대본만」(`prep`)은 그림 크레딧을 쓰기 전에 개작·대본만 먼저.
- 대시보드 「대본」 탭에서 고친 내용은 `<id>.edit.json` 으로 저장되고 언제나 원안을 이긴다.
- 「그림」 탭: 컷별 다시 그리기 / 직접 바꿔 넣기(직접 넣은 그림은 다시 그리지 않음).
- 「▶ 3→6 딸깍」은 대본 확인 뒤, 「▶ 전체 1→6」은 개작부터 한 번에.

## 명령줄
```
run.bat new 세계사 --from "원고.docx" --title "롱폼 세계사" --minutes 15 --style textbook
run.bat all --job 세계사 --only 1-1
run.bat prep --job 한국사1                 # 31개 단원 개작·대본만
run.bat s3-images --job 세계사 --only 1-1 --scenes 3,7
run.bat doctor
```

## 설정 (`aside.config.json`, PC별은 `aside.config.local.json`)
- `long.target_minutes` 목표 길이(기본 15) · `long.style` 화풍 `textbook`(교과서 상상도) / `ink`(수묵담채)
- `tts.voice` F1~F5/M1~M5 · `tts.speed`
- `image.workers` Codex 동시 개수(기본 3)
- `render.caption_font`(Pretendard SemiBold) · `render.caption_size`(68) · `render.max_line_chars`(28) · `render.image_h`(그림 영역 높이 990)

## 주의
- 원문(01_raw)은 보관만 하고 대본 재료로 쓰지 않는다. 겹침·유사도 검사는 자동 장치이지 법적 판단이 아니다 — 최종 확인은 사람이.
- 화풍은 글로만 정의한다. 교과서·다른 채널 그림을 참조 이미지로 넣지 않는다.
- Codex 그림은 ChatGPT 계정 사용량을 쓴다. 바닥나면 「4 그림」이 바로 멈추고, 다시 누르면 남은 것만 이어서 그린다.
- 원고가 짧으면(예: 1,500자) 목표 길이에 못 미칠 수 있다 — 대본은 지어낸 사실 없이 원고가 허락하는 만큼만 늘린다.
