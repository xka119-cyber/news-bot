from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from newsbot.collect.rss import Article
from newsbot.config import load_account
from newsbot.write.caption import build_caption
from newsbot.write.checks import check_draft, longest_copy, unknown_numbers
from newsbot.write.llm import (
    BudgetExceeded,
    LLMDraft,
    WriteError,
    build_prompt,
    load_costs,
    mock_post,
    record_cost,
    write_post,
)

NOW = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)

CANDIDATES = [
    Article("연합뉴스", "전기요금 내년 1월부터 kWh당 5원 인상", "한국전력은 가정용 전기요금을 내년 1월부터 kWh당 5원 올린다고 밝혔다.", "https://a.kr/1", NOW),
    Article("동아일보", "반도체 수출 10월 역대 최대", "10월 반도체 수출이 150억달러로 역대 최대를 기록했다.", "https://b.kr/2", NOW),
    Article("SBS", "주말 서울 첫 영하권 추위", "토요일 아침 서울 기온이 영하 2도까지 내려간다.", "https://c.kr/3", NOW),
    Article("ZDNet Korea", "AI 비서 앱 이용자 급증", "AI 비서 앱 이용자가 크게 늘었다.", "https://d.kr/4", NOW),
]


def item(cid, headline="짧은 제목", lines=("한 줄", "두 줄", "세 줄"), why="내 지갑에 영향", category="경제"):
    return {"candidate_id": cid, "headline": headline, "summary_lines": list(lines), "why_it_matters": why, "category": category}


def draft(items, hook="오늘의 뉴스 3개", intro="오늘 뉴스 뭐 있을까요?"):
    return LLMDraft.model_validate({"hook": hook, "items": items, "caption_intro": intro})


GOOD = draft([
    item(1, "전기요금 5원 오른다", ("내년 1월부터 요금이 올라요", "kWh당 5원씩 더 내요", "가정용이 대상이에요"), "관리비가 조금 늘어요", "생활"),
    item(2, "반도체 수출 최고치", ("10월 수출이 역대 최대예요", "150억달러를 기록했어요", "메모리 수요 덕분이에요"), "경기 회복 신호예요", "경제"),
    item(3, "주말 첫 영하권", ("토요일 아침 영하 2도예요", "올가을 첫 영하권이에요", "외투를 챙기세요"), "출근길 옷차림 주의", "날씨·교통"),
])


class FakeClient:
    """messages.parse 호출마다 준비한 응답을 차례로 돌려준다."""

    def __init__(self, drafts, stop_reason="end_turn"):
        self.drafts = list(drafts)
        self.calls = []
        self.messages = self
        self.stop_reason = stop_reason

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=2000, output_tokens=800),
            parsed_output=self.drafts.pop(0),
        )


@pytest.fixture
def account():
    return load_account("today_news")


def test_prompt_contains_candidates_and_rules(account):
    prompt = build_prompt(account, CANDIDATES)
    assert "[1] (연합뉴스) 전기요금" in prompt
    assert "원문에 없는 숫자" in prompt
    assert "{" not in prompt  # 빈칸이 다 채워졌는지


def test_write_success_records_cost(account, tmp_path):
    client = FakeClient([GOOD])
    costs = tmp_path / "costs.json"
    result = write_post(account, CANDIDATES, costs, client=client, now=NOW)
    assert result.attempts == 1
    assert [i["source_name"] for i in result.draft["items"]] == ["연합뉴스", "동아일보", "SBS"]
    assert result.draft["items"][1]["source_url"] == "https://b.kr/2"
    assert result.warnings == []
    expected = 2000 / 1e6 * 0.10 + 800 / 1e6 * 0.50
    assert result.cost_usd == pytest.approx(expected)
    month = load_costs(costs)["today_news"]["2026-11"]
    assert month["calls"] == 1 and month["usd"] == pytest.approx(expected, abs=1e-6)
    assert client.calls[0]["model"] == "claude-haiku-5-5"


def test_retry_on_too_long_then_ok(account, tmp_path):
    too_long = draft([item(1, "가" * 30), item(2), item(3)])
    client = FakeClient([too_long, GOOD])
    result = write_post(account, CANDIDATES, tmp_path / "c.json", client=client, now=NOW)
    assert result.attempts == 2
    assert "1번 제목 30자" in client.calls[1]["messages"][-1]["content"]
    assert load_costs(tmp_path / "c.json")["today_news"]["2026-11"]["calls"] == 2


def test_length_problem_left_after_retry_becomes_warning(account, tmp_path):
    too_long = draft([item(1, "가" * 30), item(2), item(3)])
    client = FakeClient([too_long, too_long])
    result = write_post(account, CANDIDATES, tmp_path / "c.json", client=client, now=NOW)
    assert any("1번 제목 30자" in w for w in result.warnings)


def test_bad_structure_twice_raises(account, tmp_path):
    two_items = draft([item(1), item(1)])
    client = FakeClient([two_items, two_items])
    with pytest.raises(WriteError):
        write_post(account, CANDIDATES, tmp_path / "c.json", client=client, now=NOW)


def test_budget_stops_before_calling(account, tmp_path):
    costs = tmp_path / "c.json"
    record_cost(costs, "today_news", "2026-11", 2.5, 0, 0)
    client = FakeClient([GOOD])
    with pytest.raises(BudgetExceeded):
        write_post(account, CANDIDATES, costs, client=client, now=NOW)
    assert client.calls == []


def test_refusal_raises(account, tmp_path):
    with pytest.raises(WriteError):
        write_post(account, CANDIDATES, tmp_path / "c.json", client=FakeClient([GOOD], "refusal"), now=NOW)


def test_number_check():
    assert unknown_numbers("kWh당 5원, 1월부터", CANDIDATES[0]) == set()
    assert unknown_numbers("7원 오른다", CANDIDATES[0]) == {"7"}
    assert unknown_numbers("150억달러", CANDIDATES[1]) == set()


def test_copy_check():
    copied = "가정용 전기요금을 내년 1월부터 kWh당 5원 올린다"
    assert len(longest_copy(copied, CANDIDATES[0])) >= 20
    assert len(longest_copy("요금이 조금 올라요", CANDIDATES[0])) < 20


def test_check_draft_flags_copy_and_numbers(account):
    copy_draft = draft([item(1, lines=("가정용 전기요금을 내년 1월부터 kWh당 5원 올린다고", "두 줄", "9% 오름"))])
    result = check_draft(copy_draft, CANDIDATES[:1], account.collect.exclude_keywords)
    assert any("그대로 옮김" in p for p in result["retry"])
    assert any("9" in w for w in result["warn"])


def test_caption(account):
    out = mock_post(CANDIDATES)
    caption = build_caption(out.draft, account)
    assert caption.startswith(out.draft["caption_intro"])
    assert "1️⃣" in caption and "3️⃣" in caption
    assert "출처: 연합뉴스, 동아일보, SBS" in caption
    assert "AI의 도움을 받아 요약했어요." in caption
    assert caption.count("#") <= 5
    assert "07:30" not in caption  # 게시 시간은 넣지 않음
