# 인스타 자동화 엔진 v1 설계서 — '오늘의 뉴스' 계정

작성일: 2026-11-02 · 대상: Claude Code · 언어: 한국어(코드·식별자는 영어)

> **Claude Code에게:** 이 문서를 끝까지 읽은 뒤 「0. 진행 방식」을 따라 **M0부터** 진행해줘.
> 외부 API 스펙은 바뀔 수 있으니, 구현 전에 최신 공식 문서로 확인하고 이 문서와 다르면 사용자에게 알린 뒤 공식 문서 기준으로 맞춰줘.

---

## 0. 진행 방식 (Claude Code 필독)

### 사용자에 대해
- CS 전공이지만 5년 넘게 개발을 하지 않았고, **코드를 직접 읽거나 고치지 않는다.**
- 목적은 두 가지: ① 봇을 완성해서 2주간 반응 테스트, ② 자동화가 어떻게 돌아가는지 **흐름을 배우기**.
- 시간이 많지 않다. 운영은 하루 5~10분(텔레그램 승인)이 목표.

### 진행 규칙
1. **마일스톤(M0~M8) 단위로 진행**하고, 하나가 끝날 때마다 멈춘다. 그때 다음 세 가지를 쉬운 한국어로 알려주고 사용자 확인을 받은 뒤 넘어간다.
   - 무엇을 만들었는지 (3~5줄, 비유 환영)
   - 사용자가 직접 할 일 (있다면 단계별로)
   - 잘 됐는지 확인하는 방법
2. 사용자가 해야 하는 클릭 작업(GitHub, Meta, 텔레그램, Anthropic Console)은 **메뉴 이름까지 단계별로** 안내한다.
3. 결정이 필요하면 **선택지 2~3개 + 추천안**으로 묻는다.
4. 마일스톤마다 `docs/RUNBOOK.md`(사용자용 운영 설명서)를 갱신한다.
5. 테스트를 같이 만든다(`pytest`). 실패하면 고친 뒤 보고한다.

### 비밀값 규칙 (절대 어기지 말 것)
- API 키·토큰을 **코드, 로그, 커밋, 터미널 출력, 채팅 답변에 절대 노출하지 않는다.**
- 저장 위치는 **로컬 `.env`**(gitignore)와 **GitHub Secrets** 두 곳뿐. `.env.example`에는 키 이름만 적는다.
- 사용자에게 비밀값을 채팅에 붙여넣으라고 하지 말고, **`.env` 파일에 직접 붙여넣도록** 안내한다.
- 로그에 출력할 때는 마스킹(`sk-...abcd` 형태도 금지, `****`만).
- 커밋 전 `.env`·토큰 문자열이 포함되지 않았는지 확인한다(간단한 pre-commit 검사 스크립트 추가).

---

## 1. 목표와 범위

- 인스타그램 '오늘의 뉴스' 계정에 **하루 4회 자동 게시**한다.
  - v1: 카드뉴스(캐러셀)
  - v1.1: 같은 카드로 만든 릴스 추가
- 사람은 텔레그램으로 미리보기를 받고 **[승인]/[건너뛰기] 버튼만** 누른다.
- 2주 반응 테스트 후, 같은 엔진에 **계정 설정 파일만 추가**해서 다른 계정(경제·재테크, 심리테스트 카드, 전시·축제 캘린더)을 운영할 예정이다. → **처음부터 다계정 구조로 설계**한다.
- 월 운영비 목표: **1달러 미만**(Claude API Haiku만 유료).

### 이번 범위에서 하지 않는 것
- X·스레드 게시, 댓글·DM 자동 응답, 광고 집행, AI 음성(TTS) — 나중에 필요하면 추가.

---

## 2. 전체 구조

```
[수집] → [작성] → [디자인] → [승인] → [게시]
 RSS     Claude    카드 JPEG   텔레그램   인스타
         Haiku     (+릴스 MP4)  버튼       Graph API
```

### 두 단계 실행 (서버 없이 GitHub Actions로)
| 작업 | 실행 시점 | 하는 일 |
|---|---|---|
| `generate` | 각 게시 시각 약 1시간 전 | 수집 → 작성 → 디자인 → 미디어를 공개 URL로 업로드 → 텔레그램 미리보기 + 버튼 전송 → 초안 상태 저장 |
| `publish` | 각 게시 시각 | 텔레그램 버튼 응답 읽기 → 승인된 초안 게시 → 결과(게시물 링크)를 텔레그램으로 보고 |
| `token-refresh` | 주 1회 | 인스타 토큰 만료일 확인·갱신 |
| `prune` | 주 1회 | 오래된 미디어 정리 |
| `report` (M8, 선택) | 매일 1회 | 게시물 성과 수집, 주간 요약 |

- `publish`는 **멱등**하게 만든다: "예정 시각이 지났고, 승인됐고, 아직 게시 안 된 초안"을 모두 처리. (GitHub 예약 실행은 몇 분~수십 분 늦을 수 있음)
- 승인 없이 게시 시각이 지나면 설정값(`on_timeout`)에 따라 처리. 학습 기간 기본값은 `skip`.

### 기술 스택
| 영역 | 선택 | 비고 |
|---|---|---|
| 언어 | Python 3.12 | |
| LLM | Anthropic Python SDK, 모델 `claude-haiku-5-5` | 설정에서 변경 가능 |
| 카드 렌더링 | Jinja2 HTML/CSS → Playwright(Chromium) 스크린샷 → JPEG | 인스타 이미지 게시는 JPEG |
| 영상(v1.1) | ffmpeg | GitHub ubuntu 러너에 없으면 설치 |
| 폰트 | Pretendard (SIL OFL 1.1) | `assets/fonts/`에 포함 |
| 설정 검증 | pydantic | |
| 실행·스케줄 | GitHub Actions (cron + 수동 실행) | |
| 미디어 호스팅·상태 저장 | 같은 저장소의 `media` 브랜치 (raw URL) | 4.6 참고 |
| 승인 | Telegram Bot API (getUpdates 폴링) | 웹훅 사용 안 함 |
| 테스트 | pytest | |

### 저장소 구조 (제안)
```
newsbot/
├─ accounts/
│  └─ today_news.yaml        # 계정별 설정 (계정 추가 = 파일 추가)
├─ newsbot/
│  ├─ __main__.py            # CLI: generate / publish / refresh-token / prune / report
│  ├─ config.py              # 설정 로드·검증(pydantic)
│  ├─ collect/rss.py         # 수집
│  ├─ write/llm.py           # Claude 호출, 스키마 검증, 비용 기록
│  ├─ write/prompts/         # 프롬프트 템플릿
│  ├─ write/checks.py        # 숫자 검증, 원문 복사 검사, 금지 주제 필터
│  ├─ design/cards.py        # 카드 렌더링
│  ├─ design/reels.py        # 릴스 렌더링 (v1.1)
│  ├─ design/templates/      # HTML/CSS
│  ├─ approve/telegram.py    # 미리보기 전송, 응답 읽기
│  ├─ publish/instagram.py   # 인스타 게시
│  ├─ storage/media_branch.py# 미디어 업로드·상태 저장
│  └─ util/ (time, logging, retry, masking)
├─ assets/fonts/  assets/music/(v1.1, 라이선스 파일 포함)
├─ scripts/get_telegram_chat_id.py
├─ tests/
├─ .github/workflows/ (generate.yml, publish.yml, maintenance.yml)
├─ docs/RUNBOOK.md
├─ .env.example  .gitignore  README.md
```

---

## 3. 계정 설정 파일 예시 — `accounts/today_news.yaml`

```yaml
account_key: today_news          # 내부 이름
enabled: true
brand_name: "오늘의 3분 뉴스"     # M0에서 사용자와 확정
timezone: Asia/Seoul

secrets:                          # 어떤 환경변수를 쓸지 '이름'만 적는다
  ig_user_id: IG_USER_ID
  ig_access_token: IG_ACCESS_TOKEN

slots:                            # 게시 시각(KST)
  - "07:30"
  - "12:00"
  - "18:00"
  - "21:30"
generate_lead_minutes: 60         # 게시 몇 분 전에 만들지

format_plan:                      # 2주 A/B 테스트
  test_start_date: null           # 릴스 켜는 날 설정 (YYYY-MM-DD)
  week1: { "07:30": reel, "12:00": carousel, "18:00": reel, "21:30": carousel }
  week2: { "07:30": carousel, "12:00": reel, "18:00": carousel, "21:30": reel }
reels_enabled: false              # v1.1 전까지 false → 전부 carousel

collect:
  rss_feeds: []                   # M1에서 동작 확인된 주소로 채움
  lookback_hours: 12
  include_categories: [경제, 생활, IT, 트렌드, 날씨·교통]
  exclude_categories: [정치, 선거, 사건사고, 범죄, 연예가십]
  exclude_keywords: []            # M1에서 함께 정리
  items_per_post: 3

write:
  model: claude-haiku-5-5
  max_output_tokens: 2000
  tone: "친근한 존댓말, 짧고 쉬운 문장"
  monthly_budget_usd: 2.0         # 넘으면 생성 중단 + 텔레그램 알림

design:
  card_size: [1080, 1350]
  reel_size: [1080, 1920]
  colors: { bg: "#FFFFFF", primary: "#1F2A44", accent: "#FFC83D", text: "#111111" }
  font: Pretendard

caption:
  hashtags: ["#오늘의뉴스", "#뉴스요약", "#출근길뉴스", "#경제뉴스", "#3분뉴스"]  # 최대 5개
  ai_disclosure: true             # "AI의 도움을 받아 요약했어요" 한 줄
  schedule_note: "매일 07:30 · 12:00 · 18:00 · 21:30 업데이트"

approval:
  required: true
  on_timeout: skip                # skip | publish
```

- 설정 로드 시 pydantic으로 검증한다(해시태그 5개 초과, 잘못된 시각 형식 등은 에러).

---

## 4. 단계별 상세 설계

### 4.1 수집 (`collect/rss.py`)
- 설정의 RSS 목록에서 최근 `lookback_hours` 이내 기사의 **제목·요약(description)·링크·언론사명·발행시각**만 가져온다.
- **M1에서 Claude Code가 실제로 동작하는 국내 언론사 RSS 주소를 찾아** 후보를 보여주고, 사용자 확인 후 설정에 넣는다. (경제·생활·IT 위주, 여러 언론사 섞기)
- 기사 본문을 긁어오지 않는다(사이트 약관·저작권). 제목+요약만 사용.
- 중복 제거: 같은 링크, 그리고 제목이 매우 비슷한 기사(간단한 유사도). 
- 최근 3일 동안 이미 쓴 기사·주제는 제외(상태 파일 `state/history.json`).
- 제외 키워드·제외 카테고리 1차 필터 → 후보 15~30개를 작성 단계로 넘긴다.

### 4.2 작성 (`write/llm.py`)
- Claude API(`claude-haiku-5-5`) 1회 호출로 **후보 중 3개 선정 + 문구 작성 + 정치 등 제외 판단**까지 한다.
- 출력은 **JSON 하나**로 받고 pydantic으로 검증한다. 형식이 틀리면 1회 재시도, 그래도 실패하면 텔레그램으로 오류 알림.

**출력 스키마**
```json
{
  "hook": "출근길 3분, 오늘 꼭 알아둘 뉴스 3개",
  "items": [
    {
      "headline": "20자 이내 제목",
      "summary_lines": ["35자 이내", "35자 이내", "35자 이내"],
      "why_it_matters": "40자 이내, '내 생활·지갑에 어떤 영향?' 한 줄",
      "category": "경제 | 생활 | IT | 트렌드 | 날씨·교통",
      "source_name": "언론사명",
      "source_url": "원문 링크"
    }
  ],
  "caption_intro": "캡션 첫 문장 (검색될 법한 질문형 문장)"
}
```

**프롬프트 핵심 규칙** (`write/prompts/today_news.md`로 분리)
- 원문(제목·요약)에 있는 사실만 쓴다. 원문에 없는 숫자·이름·날짜·인과관계를 만들지 않는다. 정보가 부족한 기사는 고르지 않는다.
- 원문 문장을 그대로 베끼지 않고 **자기 문장으로** 다시 쓴다.
- 정치·선거·정당·특정 정치인 관련, 사건사고·범죄·참사·피해자 관련, 연예 가십은 고르지 않는다.
- 특정 종목 매수·매도 추천, 수익 보장 같은 **투자 조언 금지**.
- 개인(일반인)의 실명·신상은 쓰지 않는다.
- `why_it_matters`는 "그래서 나한테 무슨 의미인지"를 쉬운 말로.

**코드 레벨 검사** (`write/checks.py`) — 결과는 텔레그램 미리보기에 ⚠️로 표시
- **숫자 검증:** 출력에 나온 숫자·퍼센트가 해당 원문 제목/요약에 있는지 확인. 없으면 경고.
- **복사 검사:** 출력과 원문 사이에 공백 제외 20자 이상 똑같은 구간이 있으면 경고(1회 재작성 시도).
- **길이 검사:** 스키마 글자 수 제한 초과 시 재시도.
- **금지어 2차 필터:** 제외 키워드가 출력에 있으면 해당 항목 교체 시도 또는 경고.

**비용 기록**
- 응답의 토큰 사용량으로 비용을 계산해 `state/costs.json`에 월별 누적. `monthly_budget_usd`를 넘으면 생성 중단 + 텔레그램 알림.
- 단가는 설정값으로 두고(Haiku 5.5: 입력 $0.10/MTok, 출력 $0.50/MTok — 10만 토큰 이하 요청 기준), 최신 단가는 공식 가격 문서로 확인.
- 디자인 작업 중 비용을 아끼기 위해 `--mock-llm` 옵션(고정 샘플 JSON 사용)을 만든다.

**캡션 조립** (코드로)
```
{caption_intro}

1️⃣ {headline 1}
2️⃣ {headline 2}
3️⃣ {headline 3}

출처: {언론사명들}
{schedule_note}
AI의 도움을 받아 요약했어요.

{hashtags ≤ 5}
```
- 캡션 2,200자 이하, 해시태그 최대 5개.

### 4.3 디자인
**카드뉴스 (`design/cards.py`)**
- 4장: ① 표지(hook + 날짜 + 시간대) ② ~ ④ 뉴스 1개씩(제목, 요약 3줄, 💡왜 중요한가, 하단 출처·장 번호).
- 1080×1350, **JPEG**(품질 90, 8MB 이하). 모든 장 같은 비율.
- 기사 사진·언론사 로고 사용 금지. 색상·도형·이모지·간단한 아이콘으로만 디자인.
- 안전 여백 80px, 제목 64~72px, 본문 38~42px, 출처 24px 기준. **글자가 넘치면 자동으로 줄여서 맞추기**(최소 크기 이하로 줄어야 하면 경고).
- 템플릿은 계정별로 교체 가능하게(`design/templates/{account_key}/`).
- M3에서 시안 2~3개를 만들어 사용자에게 고르게 한다.

**릴스 (`design/reels.py`, v1.1)**
- 1080×1920, 30fps, H.264(yuv420p), AAC. 길이 25~35초 (API 업로드 제한 고려해 60초를 넘기지 않기).
- 구성: 훅 장면 2.5초(큰 글씨) → 카드 3장 각 7초(살짝 확대되는 움직임, 상단에 훅 문구, 하단에 진행 바) → 마무리 2.5초("매일 4번 업데이트").
- 4:5 카드를 세로 화면 가운데에 두고 위아래는 단색 배경 + 텍스트.
- **자막 필수**(카드 텍스트가 곧 자막 역할). 소리 없이 봐도 이해되게.
- 음악: `assets/music/`의 **상업적 이용 가능한 로열티 프리 음원**(라이선스 파일 함께 보관)을 무작위로 골라 작게(-18dB, 페이드 인·아웃). 폴더가 비어 있으면 **무음 오디오 트랙**을 넣는다.
- 인스타 음악 라이브러리는 'Instagram 로그인' 방식 API로 붙일 수 없으므로 사용하지 않는다.
- 커버 이미지: 표지 카드.

### 4.4 승인 (`approve/telegram.py`)
- `generate` 끝에 텔레그램으로 전송:
  1. 카드 이미지 앨범(`sendMediaGroup`) 또는 릴스 영상(`sendVideo`)
  2. 메시지: 게시 예정 시각, 형식, 캡션 전문, 원문 링크 목록, ⚠️경고 목록
  3. 인라인 버튼: **[✅ 승인] [⏭ 건너뛰기]** — `callback_data`에 초안 ID 포함(64바이트 이하)
- `publish`(그리고 다른 작업이 돌 때도) `getUpdates`로 버튼 응답을 읽어 상태 파일에 기록하고, 처리한 업데이트는 offset으로 확인 처리. 마지막 offset은 `state/telegram.json`에 저장.
  - 텔레그램은 미확인 업데이트를 약 24시간만 보관하므로 매 실행마다 읽어서 기록한다.
- **보안:** 설정된 `TELEGRAM_CHAT_ID`에서 온 응답만 인정한다.
- 버튼을 누르면 `answerCallbackQuery`로 "승인됨 — 07:30에 올라가요" 같은 응답을 준다(가능할 때).
- 게시 후 결과(성공 시 게시물 링크, 실패 시 이유)를 같은 대화방에 보고한다.
- `scripts/get_telegram_chat_id.py`: 사용자가 봇에게 메시지를 보낸 뒤 실행하면 chat id를 알려주는 도구(토큰은 출력하지 않음).

### 4.5 게시 (`publish/instagram.py`)
- **Instagram API with Instagram Login** 방식 사용(호스트 `graph.instagram.com`, 페이스북 페이지 불필요). API 버전은 설정값으로 두고 최신 버전 확인.
- 필요 권한: `instagram_business_basic`, `instagram_business_content_publish`.
- 게시 전 `/{ig-user-id}/content_publishing_limit`로 남은 한도 확인(24시간 50건 제한, 캐러셀은 1건으로 계산).
- **캐러셀:** 이미지마다 `is_carousel_item=true` 컨테이너 생성(`image_url` = 공개 URL) → `media_type=CAROUSEL` 부모 컨테이너(`children`, `caption`) → `media_publish`.
- **릴스:** `media_type=REELS`, `video_url`(공개 URL), `caption`, `cover_url`, `share_to_feed=true` → `status_code`가 `FINISHED`가 될 때까지 1분 간격으로 최대 5분 폴링 → `media_publish`.
- 게시 후 `permalink` 조회해 상태 파일과 텔레그램에 기록.
- 네트워크 오류는 지수 백오프로 최대 3회 재시도. 같은 초안이 두 번 게시되지 않도록 상태 확인 후 진행.

### 4.6 미디어 호스팅과 상태 저장 (`storage/media_branch.py`)
- 인스타 API는 게시 시점에 이미지·영상을 **공개 URL에서 내려받으므로** 공개 주소가 필요하다.
- **기본안:** 저장소를 **public**으로 만들고, `media`라는 별도 브랜치에 생성물을 올려 `https://raw.githubusercontent.com/{owner}/{repo}/media/...` 주소를 사용한다.
  - 장점: 추가 서비스·카드 등록 없이 무료, public 저장소는 Actions 실행 시간도 넉넉함.
  - 주의: 코드와 생성물이 공개된다(비밀값은 Secrets에 있으므로 노출 안 됨). 승인 전 초안도 공개 주소에 존재.
  - M0에서 이 내용을 사용자에게 설명하고 public/private 중 선택받는다. private을 고르면 대안(예: Cloudflare R2 공개 버킷)을 제안한다.
  - 인스타가 raw 주소를 거부하면 GitHub Pages 주소로 전환을 검토한다.
- 같은 브랜치에 상태 파일도 저장:
  - `drafts/{account}/{YYYY-MM-DD}/{HHMM}/` — 이미지, 영상, `draft.json`(상태: generated → approved/skipped/expired → published/failed)
  - `state/history.json`(사용한 기사), `state/costs.json`, `state/telegram.json`, `state/token.json`(만료 예정일만, 토큰 자체는 저장 금지)
- 모든 워크플로에 같은 `concurrency` 그룹을 걸어 브랜치 동시 수정을 막는다.
- `prune`: 주 1회, 7일 지난 drafts 삭제 후 `media` 브랜치를 새 단일 커밋으로 다시 만들어 저장소 용량을 유지(상태 파일은 보존).

### 4.7 스케줄 (GitHub Actions)
- GitHub cron은 **UTC 기준**(KST−9시간). 정각은 혼잡해 지연이 잦으니 몇 분 비켜서 설정.
- 예약 실행은 늦어질 수 있으므로, `generate`는 "앞으로 30~150분 안에 있는 슬롯 중 아직 초안이 없는 것"을 찾아 만들고, `publish`는 "예정 시각이 지난 승인 초안"을 처리한다.

| KST | 작업 | cron (UTC) |
|---|---|---|
| 06:25 | generate (07:30 슬롯) | `25 21 * * *` (전날) |
| 07:30 | publish | `30 22 * * *` (전날) |
| 10:55 | generate (12:00 슬롯) | `55 1 * * *` |
| 12:02 | publish | `2 3 * * *` |
| 16:55 | generate (18:00 슬롯) | `55 7 * * *` |
| 18:02 | publish | `2 9 * * *` |
| 20:25 | generate (21:30 슬롯) | `25 11 * * *` |
| 21:30 | publish | `30 12 * * *` |
| 월 04:10 | token-refresh, prune | `10 19 * * 0` |

- 모든 워크플로에 `workflow_dispatch`(수동 실행 버튼) 추가: 입력값으로 `account`, `slot`, `dry_run`.
- 권한: `contents: write`(media 브랜치 푸시용). Playwright Chromium·ffmpeg 설치 단계 포함, pip 캐시 사용.
- 참고: public 저장소는 활동이 60일 없으면 예약 실행이 꺼질 수 있다. media 브랜치에 매일 커밋이 생기므로 보통 문제없지만 RUNBOOK에 적어둔다.

### 4.8 인스타 토큰 관리
- 대시보드에서 발급한 토큰은 약 60일짜리. `token-refresh`가 주 1회 남은 기간을 확인하고, Instagram의 장기 토큰 갱신 엔드포인트로 갱신한다(최신 문서 확인).
- 갱신된 토큰을 GitHub Secret에 다시 저장하려면 권한 있는 토큰이 필요하다:
  - 선택 비밀값 `GH_PAT`(이 저장소 한정, Secrets 읽기/쓰기 권한의 fine-grained 토큰)이 있으면 자동으로 Secret 갱신.
  - 없으면 만료 10일 전부터 텔레그램으로 "토큰 재발급 필요" 알림 + RUNBOOK의 재발급 절차 링크.
- 매 실행 시작 시 `/me` 호출로 토큰 유효성 확인. 실패하면 즉시 텔레그램 알림.

### 4.9 오류 처리·로그
- 어떤 단계든 실패하면 텔레그램으로 **"어느 단계에서, 무엇 때문에"**를 쉬운 말로 보고(비밀값 제외).
- 로그는 Actions 로그에 남기되 마스킹 유틸을 거친다.
- 한 슬롯이 실패해도 다음 슬롯은 정상 동작해야 한다.

---

## 5. 콘텐츠 규칙 (프롬프트·코드 양쪽에 반영)

1. 정치·선거·정당·정치인 이슈 제외 (메타가 팔로우하지 않은 계정의 정치·사회 이슈 콘텐츠 추천을 기본 제한 → 테스트 왜곡 방지)
2. 사건사고·범죄·참사·피해자 관련, 연예 가십 제외
3. 원문 문장 복사 금지, 자기 문장으로 요약 + 해석 한 줄
4. 원문에 없는 사실·숫자 생성 금지
5. 언론사 사진·로고·기사 캡처 사용 금지
6. 출처는 언론사명으로 카드 하단과 캡션에 표기
7. 특정 종목 추천·수익 보장 등 투자 조언 금지
8. 일반인 실명·신상 금지
9. 해시태그 최대 5개, "좋아요 눌러주세요" 같은 반응 구걸 문구 금지
10. AI 요약임을 캡션에 한 줄로 밝힘(설정으로 끌 수 있음)

---

## 6. 마일스톤과 완료 기준

| # | 내용 | 완료 기준 |
|---|---|---|
| **M0** | 준비: 저장소 구조, 가상환경, `.gitignore`, `.env.example`, pre-commit 비밀값 검사, GitHub 저장소 생성(public/private 결정), 브랜드 이름·색상 확정 | `python -m newsbot --help` 동작, `.env`가 git에 안 올라감을 확인, 사용자가 `.env`에 값 4개를 넣음(없으면 부록 A로 안내) |
| **M1** | 수집: RSS 후보 찾기·확정, 중복 제거, 1차 필터 | 명령 한 번으로 후보 15~30개가 표로 출력되고, 정치 키워드 기사가 빠져 있음 |
| **M2** | 작성: Claude 호출, JSON 검증, 검사(숫자·복사·길이), 비용 기록, `--mock-llm` | 실제 호출 1회로 3개 항목 JSON 생성, 비용이 `state/costs.json`에 기록, 테스트 통과 |
| **M3** | 카드 디자인: 템플릿 시안 2~3개 → 사용자 선택 → 확정 | `out/` 폴더에 JPEG 4장, 글자 넘침 없음, 한글 폰트 정상, 사용자가 디자인 승인 |
| **M4** | 텔레그램 승인: chat id 확보, 미리보기 + 버튼, 응답 기록 | 사용자가 폰에서 미리보기를 받고, 버튼을 누르면 상태 파일에 승인/건너뛰기가 기록됨 |
| **M5** | 인스타 게시: media 브랜치 업로드 → 캐러셀 게시 | 수동 명령으로 실제 계정에 카드뉴스 1건 게시, 링크가 텔레그램으로 옴 |
| **M6** | 자동화: GitHub Actions 워크플로, Secrets 등록 안내, 수동 실행 테스트, token-refresh·prune | Actions 탭에서 수동 실행으로 generate → 승인 → publish 한 바퀴 성공, 다음 예약 슬롯이 자동 실행됨, RUNBOOK 완성 |
| **M7** | v1.1 릴스: 영상 렌더링, 릴스 게시, `format_plan` 적용, `reels_enabled: true` | 릴스 1건 실제 게시 성공, 2주 A/B 일정이 설정대로 동작 |
| **M8** (선택) | 성과 리포트: 게시 24·72시간 후 지표 수집(도달, 저장, 공유, 프로필 방문, 팔로우 등 가능한 항목), 주간 요약을 텔레그램으로 | 2주 테스트 결과를 형식별·시간대별로 비교한 표가 나옴. 필요 권한이 추가로 필요하면 사용자에게 토큰 재발급 안내 |

- M0~M6이 **엔진 v1**(카드뉴스만), M7이 **v1.1**이다. M6까지 끝나면 2~3일 운영해 안정성을 확인한 뒤 M7로 간다.
- 각 마일스톤에서 사용자에게 보여줄 **확인 방법**을 꼭 함께 알려준다.

---

## 7. 비밀값 목록

| 이름 | 용도 | 어디서 받나 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 글 작성 | Claude Console(platform.claude.com) → API keys |
| `IG_ACCESS_TOKEN` | 인스타 게시 | Meta 개발자 대시보드 → API setup with Instagram login → Generate token |
| `IG_USER_ID` | 인스타 계정 식별 | 같은 화면의 계정 숫자 ID |
| `TELEGRAM_BOT_TOKEN` | 승인 봇 | 텔레그램 @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | 미리보기 받을 대화방 | `scripts/get_telegram_chat_id.py`로 확인 |
| `GH_PAT` (선택) | 인스타 토큰 자동 갱신 시 Secret 업데이트 | GitHub → Settings → Developer settings → Fine-grained tokens |

- 로컬: `.env` / 서버: GitHub 저장소 → Settings → Secrets and variables → Actions.
- 계정을 추가할 때는 `IG_ACCESS_TOKEN_ECONOMY`처럼 접미사를 붙이고 계정 설정 파일의 `secrets`에 이름을 적는다.

---

## 부록 A. 사전 준비 (값을 아직 못 받았다면 Claude Code가 이 순서로 안내)

1. **인스타 새 계정** (폰): 프로필 → 계정 이름 → 계정 추가 → 새 계정 만들기 → ≡ → 계정 유형 및 도구 → 프로페셔널 계정으로 전환 → **비즈니스**. 자동 게시 전에 직접 게시물 1~2개 올려두기.
2. **메타 개발자 앱** (PC): developers.facebook.com에 페이스북 계정으로 개발자 등록 → Create App → 사용 사례 'Manage messaging & content on Instagram' → App roles → Roles → Add People → Instagram Tester로 새 계정 추가 → 인스타 웹(설정 → 웹사이트 권한 → 앱 및 웹사이트 → 테스터 초대)에서 수락 → Instagram → API setup with Instagram login → Generate access tokens → Add account → Generate token. 토큰은 한 번만 보여주므로 바로 `.env`에 저장. 권한 목록에 콘텐츠 게시 권한이 있는지 확인.
3. **텔레그램 봇** (폰): @BotFather(공식 계정) → /newbot → 표시 이름 → 사용자명(bot으로 끝나게) → 토큰을 `.env`에 저장 → 만든 봇에게 '시작' 후 아무 메시지 1개 보내기.
4. **Claude API** (PC): platform.claude.com 가입(claude.ai 구독과 별개) → Billing에서 5달러 충전 + 월 한도 2~3달러 → API keys → Create key → `.env`에 저장.
5. **GitHub**: 계정이 없으면 가입. 저장소 생성과 Secrets 등록은 M0·M6에서 Claude Code가 안내.

## 부록 B. RUNBOOK.md에 꼭 들어갈 내용 (사용자용, 쉬운 말)
- 매일 할 일: 텔레그램 미리보기 보고 [승인]/[건너뛰기]
- 잠깐 멈추기: GitHub → Actions → 워크플로 → Disable / 다시 켜기
- 게시 시간·형식 바꾸기: 어느 파일의 어느 줄을 바꾸는지 (또는 Claude Code에 요청하는 문장 예시)
- 토큰 만료 알림이 오면: 재발급 절차
- 비용 확인: Claude Console → Usage
- 문제가 생기면: 텔레그램 오류 메시지를 복사해 Claude Code에 "RUNBOOK 보고 고쳐줘"라고 요청
- 2주 테스트 지표 보는 법(릴스: 비팔로워 도달·공유·프로필 방문 / 카드: 저장·끝까지 넘김 / 공통: 팔로우 수)
