"""Claude 1회 호출로 후보 중 기사 1개를 고르고 카드 4장 문구를 쓴다. 검사·재시도·비용 기록 포함.

2026-11-04 사용자 결정: 1게시물 = 뉴스 1개 (표지 → 핵심 2장 → 나한테 의미).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from newsbot.collect.rss import Article
from newsbot.config import AccountConfig
from newsbot.write.checks import check_draft

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class LLMPoint(BaseModel):
    title: str
    lines: list[str]


class LLMDraft(BaseModel):
    candidate_id: int
    category: str
    cover_title: str
    cover_sub: str
    points: list[LLMPoint]
    why_it_matters: str
    caption_intro: str
    hashtags: list[str] = []  # 기사 주제어 해시태그 2~3개 (검색 노출용, 2026-11-05 추가)


class WriteError(RuntimeError):
    """작성 단계 실패. 메시지는 사용자에게 그대로 보여 줄 수 있는 쉬운 말."""


class BudgetExceeded(WriteError):
    pass


@dataclass
class WriteResult:
    draft: dict
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# ---------- 비용 기록 ----------

def month_key(account: AccountConfig, now: datetime) -> str:
    return now.astimezone(ZoneInfo(account.timezone)).strftime("%Y-%m")


def load_costs(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def month_spent(path: Path, account_key: str, month: str) -> float:
    return load_costs(path).get(account_key, {}).get(month, {}).get("usd", 0.0)


def record_cost(path: Path, account_key: str, month: str, usd: float, input_tokens: int, output_tokens: int) -> None:
    costs = load_costs(path)
    entry = costs.setdefault(account_key, {}).setdefault(
        month, {"usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    entry["usd"] = round(entry["usd"] + usd, 6)
    entry["calls"] += 1
    entry["input_tokens"] += input_tokens
    entry["output_tokens"] += output_tokens
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(costs, ensure_ascii=False, indent=2), encoding="utf-8")


def call_cost(account: AccountConfig, input_tokens: int, output_tokens: int) -> float:
    w = account.write
    return input_tokens / 1e6 * w.price_input_per_mtok + output_tokens / 1e6 * w.price_output_per_mtok


# ---------- 프롬프트 ----------

def format_candidates(candidates: list[Article]) -> str:
    lines = []
    for n, a in enumerate(candidates, start=1):
        lines.append(f"[{n}] ({a.source}) {a.title}\n    요약: {a.summary or '(없음)'}")
    return "\n".join(lines)


def build_prompt(account: AccountConfig, candidates: list[Article], avoid: list[str] = ()) -> str:
    """avoid: 같은 시각에 이미 고른 소식(제목). 같은 주제를 또 고르지 않게 프롬프트 끝에 붙인다."""
    template = (PROMPTS_DIR / f"{account.account_key}.md").read_text(encoding="utf-8")
    c = account.collect
    prompt = template.format(
        brand_name=account.brand_name,
        include_categories=", ".join(c.include_categories),
        exclude_categories=", ".join(c.exclude_categories),
        tone=account.write.tone,
        candidates=format_candidates(candidates),
    )
    if avoid:
        prompt += (
            "\n## 이미 고른 소식 (같은 시각에 함께 올라가거나, 최근에 이미 다룬 소식)\n"
            + "\n".join(f"- {title}" for title in avoid)
            + "\n위 소식과 같은 사건·주제는 다른 기사라도 고르지 말고, 가능하면 다른 분야의 소식을 고르세요.\n"
        )
    return prompt


# ---------- 검증 ----------

POINTS = 2


def structure_problems(draft: LLMDraft, account: AccountConfig, n_candidates: int) -> list[str]:
    problems = []
    if not 1 <= draft.candidate_id <= n_candidates:
        problems.append(f"없는 후보 번호를 고름: {draft.candidate_id}")
    if len(draft.points) != POINTS:
        problems.append(f"핵심 카드가 {len(draft.points)}개 ({POINTS}개여야 함)")
    if draft.category not in account.collect.include_categories:
        problems.append(f"정해진 분야가 아님: {draft.category}")
    return problems


MAX_TOPIC_TAGS = 3


def clean_hashtags(tags: list[str]) -> list[str]:
    """'#'을 붙이고 공백·중복·너무 긴 것을 정리한다(최대 3개)."""
    out: list[str] = []
    for tag in tags:
        word = "#" + tag.strip().lstrip("#").replace(" ", "")
        if 2 <= len(word) <= 16 and word not in out:
            out.append(word)
    return out[:MAX_TOPIC_TAGS]


def to_output(draft: LLMDraft, candidates: list[Article]) -> dict:
    """카드·캡션에 쓰는 형태로 바꾼다. 언론사·링크는 Claude가 아니라 수집 데이터에서 채운다."""
    source = candidates[draft.candidate_id - 1]
    return {
        "category": draft.category,
        "cover_title": draft.cover_title,
        "cover_sub": draft.cover_sub,
        "points": [p.model_dump() for p in draft.points],
        "why_it_matters": draft.why_it_matters,
        "caption_intro": draft.caption_intro,
        "hashtags": clean_hashtags(draft.hashtags),
        "source_name": source.source,
        "source_url": source.link,
        "source_title": source.title,
    }


# ---------- 호출 ----------

def _call_claude(client, account: AccountConfig, messages: list[dict]):
    import anthropic

    try:
        response = client.messages.parse(
            model=account.write.model,
            max_tokens=account.write.max_output_tokens,
            messages=messages,
            output_format=LLMDraft,
        )
    except anthropic.AuthenticationError as error:
        raise WriteError("Claude API 키가 맞지 않습니다. .env의 ANTHROPIC_API_KEY를 확인해 주세요.") from error
    except anthropic.PermissionDeniedError as error:
        raise WriteError("Claude API 키에 권한이 없습니다. 콘솔에서 키 상태를 확인해 주세요.") from error
    except anthropic.RateLimitError as error:
        raise WriteError("Claude API 사용량 제한에 걸렸습니다. 잠시 뒤 다시 시도합니다.") from error
    except anthropic.BadRequestError as error:
        raise WriteError(f"Claude API 요청 오류: {error.message}") from error
    except anthropic.APIStatusError as error:
        raise WriteError(f"Claude API 서버 오류({error.status_code}). 잠시 뒤 다시 시도합니다.") from error
    except anthropic.APIConnectionError as error:
        raise WriteError("Claude API에 연결하지 못했습니다(네트워크).") from error
    except ValidationError as error:  # 형식이 틀린 JSON
        return None, None, f"출력 형식 오류: {error.error_count()}곳"
    if response.stop_reason == "refusal":
        raise WriteError("Claude가 이번 후보로는 작성을 거절했습니다.")
    if response.stop_reason == "max_tokens":
        return response, None, "글이 길어 중간에 끊김"
    return response, response.parsed_output, None


def write_post(
    account: AccountConfig,
    candidates: list[Article],
    costs_path: Path,
    client=None,
    now: datetime | None = None,
    avoid: list[str] = (),
) -> WriteResult:
    now = now or datetime.now(timezone.utc)
    if not candidates:
        raise WriteError("후보 기사가 없어 작성할 수 없습니다.")
    month = month_key(account, now)
    spent = month_spent(costs_path, account.account_key, month)
    if spent >= account.write.monthly_budget_usd:
        raise BudgetExceeded(
            f"이번 달 Claude 비용 ${spent:.3f}가 예산 ${account.write.monthly_budget_usd:.2f}에 도달해 작성을 멈췄습니다."
        )
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    result = WriteResult(draft={})
    messages = [{"role": "user", "content": build_prompt(account, candidates, avoid)}]
    draft: LLMDraft | None = None
    checks: dict[str, list[str]] = {"retry": [], "warn": []}

    for attempt in (1, 2):
        result.attempts = attempt
        response, draft, error = _call_claude(client, account, messages)
        if response is not None:
            usage = response.usage
            cost = call_cost(account, usage.input_tokens, usage.output_tokens)
            record_cost(costs_path, account.account_key, month, cost, usage.input_tokens, usage.output_tokens)
            result.cost_usd += cost
            result.input_tokens += usage.input_tokens
            result.output_tokens += usage.output_tokens

        problems = [error] if error else structure_problems(draft, account, len(candidates))
        if not problems:
            checks = check_draft(draft, candidates[draft.candidate_id - 1], account.collect.exclude_keywords)
            problems = checks["retry"]
        if not problems:
            break
        log.info("작성 %d차 문제: %s", attempt, problems)
        if attempt == 2:
            if error or structure_problems(draft, account, len(candidates)):
                raise WriteError("두 번 시도했지만 올바른 형식으로 작성하지 못했습니다: " + "; ".join(problems))
            checks["warn"] = problems + checks["warn"]  # 길이·복사 문제가 남으면 경고로 넘긴다
            break
        previous = draft.model_dump_json() if draft else "(형식 오류)"
        messages += [
            {"role": "assistant", "content": previous},
            {"role": "user", "content": "아래 문제를 고쳐서 전체를 다시 써 주세요.\n- " + "\n- ".join(problems)},
        ]

    result.draft = to_output(draft, candidates)
    result.warnings = checks["warn"]
    return result


def mock_post(candidates: list[Article]) -> WriteResult:
    """디자인 작업용: Claude를 부르지 않고 고정 샘플 문구를 쓴다(비용 0)."""
    sample = LLMDraft.model_validate_json((PROMPTS_DIR / "mock_draft.json").read_text(encoding="utf-8"))
    sample.candidate_id = 1
    return WriteResult(draft=to_output(sample, candidates), warnings=["샘플 문구입니다(--mock-llm)."])
