"""계정 설정 파일을 읽고 검증한다. 계정 추가 = accounts/ 에 YAML 파일 추가."""

import re
from datetime import date
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS_DIR = ROOT / "accounts"
_SLOT_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

PostFormat = Literal["carousel", "reel"]
Size = tuple[int, int]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Secrets(_Strict):
    """실제 값이 아니라 환경변수 '이름'만 담는다."""

    ig_user_id: str
    ig_access_token: str

    @field_validator("ig_user_id", "ig_access_token")
    @classmethod
    def _is_env_name(cls, value: str) -> str:
        if not _ENV_NAME_RE.fullmatch(value):
            raise ValueError("환경변수 이름만 적을 수 있습니다 (예: IG_ACCESS_TOKEN)")
        return value


class FormatPlan(_Strict):
    test_start_date: date | None = None
    week1: dict[str, PostFormat]
    week2: dict[str, PostFormat]


class Reels(_Strict):
    """같은 뉴스를 카드뉴스와 함께 릴스(세로 영상)로도 올린다 (M7, 2026-11-07 사용자 결정: 둘 다, A/B 안 함).

    2026-11-09 v2: 카드를 얹은 15초 영상은 90~99%가 3초 안에 넘겨서, 화면 가득 큰 글씨 장면
    (첫 화면 hook → 핵심 3줄 → 마무리·팔로우)으로 바꿨다. 약 12초."""

    enabled: bool = False
    hook_seconds: float = Field(default=1.5, ge=1, le=6)   # 첫 화면 (평균 시청 1.1~1.8초 안에 다음 장면이 나와야 한다, 11-10)
    beat_seconds: float = Field(default=2.4, ge=1.5, le=6)  # 핵심 한 줄씩
    end_seconds: float = Field(default=3.3, ge=1.5, le=6)   # 마무리 + 팔로우 권유
    music: bool = False  # 코드로 만든 잔잔한 배경음(저작권 걱정 없음). false = 무음 (2026-11-09 사용자 선택: 켬)
    fps: int = Field(default=30, ge=24, le=60)
    # 2026-11-11 v3: AI 목소리 + 무료 실사 영상(Pixabay) + 큰 자막. 시청 유지 그래프에서 10명 중 6명이 1초 안에
    # (글을 읽기도 전에) 넘겨서, 글자 화면이라는 형식 자체를 바꾼다. 열쇠가 없거나 목소리·영상을 못 구하면 v2로 대신 만든다.
    style: Literal["v2", "v3"] = "v2"
    voice: str = "ko-KR-SunHiNeural"   # edge-tts 목소리 이름 (남성: ko-KR-InJoonNeural)
    voice_rate: str = Field(default="+15%", pattern=r"^[+-]\d{1,2}%$")  # 읽는 빠르기
    clip_key_env: str = Field(default="PIXABAY_API_KEY", pattern=r"^[A-Z][A-Z0-9_]*$")  # 영상 창고 열쇠가 든 환경변수 '이름'
    # true = Claude가 후보 영상 썸네일을 보고 문장에 어울리는 것을 고른다(릴스 1개에 약 $0.002). false = 태그 규칙으로만 (2026-11-12)
    clip_judge: bool = True
    # false = 릴스 탭에만(프로필 격자에 같은 뉴스가 카드·릴스로 두 번 보이지 않게)
    share_to_feed: bool = False


class Feed(_Strict):
    source: str = Field(min_length=1)  # 카드·캡션에 적을 언론사명
    section: str = ""
    url: str = Field(pattern=r"^https?://")


class Collect(_Strict):
    rss_feeds: list[Feed] = Field(default_factory=list)
    lookback_hours: int = Field(gt=0)
    include_categories: list[str]
    exclude_categories: list[str]
    exclude_keywords: list[str] = Field(default_factory=list)
    items_per_post: int = Field(gt=0)


class Write(_Strict):
    model: str
    max_output_tokens: int = Field(gt=0)
    tone: str
    monthly_budget_usd: float = Field(gt=0)
    # 1백만 토큰당 달러. 공식 가격표가 바뀌면 여기만 고친다(Haiku 5.5, 10만 토큰 이하 요청 기준).
    price_input_per_mtok: float = Field(default=0.10, ge=0)
    price_output_per_mtok: float = Field(default=0.50, ge=0)


class Colors(_Strict):
    bg: str
    primary: str
    accent: str
    text: str

    @field_validator("bg", "primary", "accent", "text")
    @classmethod
    def _is_hex(cls, value: str) -> str:
        if not _COLOR_RE.fullmatch(value):
            raise ValueError("색상은 #RRGGBB 형식이어야 합니다")
        return value


class Design(_Strict):
    card_size: Size
    reel_size: Size
    colors: Colors
    font: str
    template: str = "sticker"  # design/templates/{account_key}/{template}.html.j2


class Caption(_Strict):
    hashtags: list[str] = Field(max_length=5)
    ai_disclosure: bool
    schedule_note: str = ""  # 비우면 캡션에 게시 시간 줄을 넣지 않는다

    @field_validator("hashtags")
    @classmethod
    def _starts_with_hash(cls, value: list[str]) -> list[str]:
        if any(not tag.startswith("#") for tag in value):
            raise ValueError("해시태그는 #으로 시작해야 합니다")
        return value


class Approval(_Strict):
    required: bool
    on_timeout: Literal["skip", "publish"]


class Publish(_Strict):
    # 시스템 사용자 토큰은 graph.facebook.com에서만 된다(설계서의 graph.instagram.com과 다름, 2026-11-04 확인).
    graph_host: str = "https://graph.facebook.com"
    api_version: str = Field(default="v26.0", pattern=r"^v\d+\.\d+$")
    media_repo: str = "xka119-cyber/news-bot"  # 카드 이미지를 올릴 공개 저장소(media 브랜치)


class AccountConfig(_Strict):
    account_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    enabled: bool
    brand_name: str = Field(min_length=1)
    timezone: str
    secrets: Secrets
    slots: list[str] = Field(min_length=1)
    # 미리보기를 게시 몇 분 전에 만들지: [가장 늦어도, 가장 일러도]. 10분마다 깨어나는 tick이 이 창 안에 들어오면 만든다.
    # (설계서의 generate_lead_minutes: 60 대신. GitHub 예약이 몇 시간씩 늦어 창으로 바꿈, 2026-11-06)
    generate_window_minutes: tuple[int, int] = (30, 90)
    posts_per_slot: int = Field(default=1, ge=1, le=5)  # 게시 시각마다 만들 게시물 수(하나씩 따로 승인)
    # 설계서의 2주 A/B(시각별로 카드·릴스 번갈아) 계획. 2026-11-07 사용자가 '둘 다 올리기'를 골라 지금은 쓰지 않는다(비워 둠)
    format_plan: FormatPlan | None = None
    reels: Reels = Field(default_factory=Reels)
    collect: Collect
    write: Write
    design: Design
    caption: Caption
    approval: Approval
    publish: Publish = Field(default_factory=Publish)

    @field_validator("timezone")
    @classmethod
    def _is_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"알 수 없는 시간대입니다: {value}") from error
        return value

    @field_validator("generate_window_minutes")
    @classmethod
    def _valid_window(cls, value: tuple[int, int]) -> tuple[int, int]:
        earliest_end, latest_start = value
        if not 0 < earliest_end < latest_start:
            raise ValueError("generate_window_minutes는 [가장 늦어도, 가장 일러도] 순서의 분이어야 합니다 (예: [30, 90])")
        return value

    @field_validator("slots")
    @classmethod
    def _valid_slots(cls, value: list[str]) -> list[str]:
        for slot in value:
            if not _SLOT_RE.fullmatch(slot):
                raise ValueError(f"게시 시각 형식이 잘못됐습니다: {slot} (예: 07:30)")
        if len(set(value)) != len(value):
            raise ValueError("같은 게시 시각이 두 번 있습니다")
        return value

    @model_validator(mode="after")
    def _plan_matches_slots(self) -> "AccountConfig":
        if self.format_plan is None:
            return self
        slots = set(self.slots)
        if set(self.format_plan.week1) != slots or set(self.format_plan.week2) != slots:
            raise ValueError("format_plan의 시각이 slots와 일치하지 않습니다")
        return self


def load_account(account_key: str, accounts_dir: Path = ACCOUNTS_DIR) -> AccountConfig:
    path = accounts_dir / f"{account_key}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"계정 설정 파일이 없습니다: {path.name}")
    return AccountConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
