"""Claude 1회 호출로 후보 중 기사를 고르고 카드 문구를 쓴다. 검사·재시도·비용 기록 포함."""

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


class LLMItem(BaseModel):
    candidate_id: int
    headline: str
    summary_lines: list[str]
    why_it_matters: str
    category: str


class LLMDraft(BaseModel):
    hook: str
    items: list[LLMItem]
    caption_intro: str


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


def build_prompt(account: AccountConfig, candidates: list[Article]) -> str:
    template = (PROMPTS_DIR / f"{account.account_key}.md").read_text(encoding="utf-8")
    c = account.collect
    return template.format(
        brand_name=account.brand_name,
        items_per_post=c.items_per_post,
        include_categories=", ".join(c.include_categories),
        exclude_categories=", ".join(c.exclude_categories),
        tone=account.write.tone,
        candidates=format_candidates(candidates),
    )


# ---------- 검증 ----------

def structure_problems(draft: LLMDraft, account: AccountConfig, n_candidates: int) -> list[str]:
    want = account.collect.items_per_post
    ids = [item.candidate_id for item in draft.items]
    problems = []
    if len(draft.items) != want:
        problems.append(f"기사를 {len(draft.items)}개 골랐음 ({want}개여야 함)")
    if any(not 1 <= i <= n_candidates for i in ids):
        problems.append(f"없는 후보 번호를 고름: {ids}")
    if len(set(ids)) != len(ids):
        problems.append(f"같은 후보를 두 번 고름: {ids}")
    allowed = set(account.collect.include_categories)
    bad = [item.category for item in draft.items if item.category not in allowed]
    if bad:
        problems.append(f"정해진 분야가 아님: {bad}")
    return problems


def to_output(draft: LLMDraft, candidates: list[Article]) -> dict:
    """스키마(설계서 4.2) 형태로 바꾼다. 언론사·링크는 Claude가 아니라 수집 데이터에서 채운다."""
    items = []
    for item in draft.items:
        source = candidates[item.candidate_id - 1]
        items.append({
            "headline": item.headline,
            "summary_lines": item.summary_lines,
            "why_it_matters": item.why_it_matters,
            "category": item.category,
            "source_name": source.source,
            "source_url": source.link,
        })
    return {"hook": draft.hook, "items": items, "caption_intro": draft.caption_intro}


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
) -> WriteResult:
    now = now or datetime.now(timezone.utc)
    if len(candidates) < account.collect.items_per_post:
        raise WriteError(f"후보 기사가 {len(candidates)}개뿐이라 작성할 수 없습니다.")
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
    messages = [{"role": "user", "content": build_prompt(account, candidates)}]
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
            sources = [candidates[item.candidate_id - 1] for item in draft.items]
            checks = check_draft(draft, sources, account.collect.exclude_keywords)
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
    for n, item in enumerate(sample.items, start=1):
        item.candidate_id = n
    return WriteResult(draft=to_output(sample, candidates), warnings=["샘플 문구입니다(--mock-llm)."])
