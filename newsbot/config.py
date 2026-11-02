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


class Collect(_Strict):
    rss_feeds: list[str] = Field(default_factory=list)
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


class Caption(_Strict):
    hashtags: list[str] = Field(max_length=5)
    ai_disclosure: bool
    schedule_note: str

    @field_validator("hashtags")
    @classmethod
    def _starts_with_hash(cls, value: list[str]) -> list[str]:
        if any(not tag.startswith("#") for tag in value):
            raise ValueError("해시태그는 #으로 시작해야 합니다")
        return value


class Approval(_Strict):
    required: bool
    on_timeout: Literal["skip", "publish"]


class AccountConfig(_Strict):
    account_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    enabled: bool
    brand_name: str = Field(min_length=1)
    timezone: str
    secrets: Secrets
    slots: list[str] = Field(min_length=1)
    generate_lead_minutes: int = Field(gt=0)
    format_plan: FormatPlan
    reels_enabled: bool
    collect: Collect
    write: Write
    design: Design
    caption: Caption
    approval: Approval

    @field_validator("timezone")
    @classmethod
    def _is_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"알 수 없는 시간대입니다: {value}") from error
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
        slots = set(self.slots)
        if set(self.format_plan.week1) != slots or set(self.format_plan.week2) != slots:
            raise ValueError("format_plan의 시각이 slots와 일치하지 않습니다")
        return self


def load_account(account_key: str, accounts_dir: Path = ACCOUNTS_DIR) -> AccountConfig:
    path = accounts_dir / f"{account_key}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"계정 설정 파일이 없습니다: {path.name}")
    return AccountConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
