from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from newsbot.collect.rss import Article
from newsbot.config import load_account
from newsbot.write.caption import build_alt_texts, build_caption
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
]


def make(cid=1, cover_title="전기요금 1월부터 오른다", points=None, why="관리비가 조금 늘 수 있어요", category="생활"):
    points = points or [
        {"title": "무엇이 바뀌나", "lines": ["가정용 요금이 올라요", "1월부터 적용돼요"]},
        {"title": "얼마나 오르나", "lines": ["kWh당 5원 더 내요", "겨울에 체감이 커요"]},
    ]
    return LLMDraft.model_validate({
        "candidate_id": cid, "category": category, "cover_title": cover_title, "cover_sub": "우리 집은 얼마?",
        "points": points, "why_it_matters": why, "caption_intro": "전기요금 얼마나 오를까요?",
    })


GOOD = make()


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
    assert "기사 **1개**" in prompt
    assert "{" not in prompt  # 빈칸이 다 채워졌는지


def test_prompt_avoids_already_chosen(account):
    assert "이미 고른 소식" not in build_prompt(account, CANDIDATES)
    prompt = build_prompt(account, CANDIDATES, avoid=["반도체 수출 10월 역대 최대"])
    assert "이미 고른 소식" in prompt and "- 반도체 수출 10월 역대 최대" in prompt


def test_write_success_records_cost(account, tmp_path):
    client = FakeClient([GOOD])
    costs = tmp_path / "costs.json"
    result = write_post(account, CANDIDATES, costs, client=client, now=NOW)
    assert result.attempts == 1
    assert result.draft["source_name"] == "연합뉴스" and result.draft["source_url"] == "https://a.kr/1"
    assert len(result.draft["points"]) == 2
    assert result.warnings == []
    expected = 2000 / 1e6 * 0.10 + 800 / 1e6 * 0.50
    assert result.cost_usd == pytest.approx(expected)
    month = load_costs(costs)["today_news"]["2026-11"]
    assert month["calls"] == 1 and month["usd"] == pytest.approx(expected, abs=1e-6)
    assert client.calls[0]["model"] == "claude-haiku-5-5"


def test_retry_on_too_long_then_ok(account, tmp_path):
    client = FakeClient([make(cover_title="가" * 30), GOOD])
    result = write_post(account, CANDIDATES, tmp_path / "c.json", client=client, now=NOW)
    assert result.attempts == 2
    assert "표지 제목 30자" in client.calls[1]["messages"][-1]["content"]
    assert load_costs(tmp_path / "c.json")["today_news"]["2026-11"]["calls"] == 2


def test_length_problem_left_after_retry_becomes_warning(account, tmp_path):
    long = make(cover_title="가" * 30)
    result = write_post(account, CANDIDATES, tmp_path / "c.json", client=FakeClient([long, long]), now=NOW)
    assert any("표지 제목 30자" in w for w in result.warnings)


def test_bad_structure_twice_raises(account, tmp_path):
    bad = make(cid=9)
    with pytest.raises(WriteError):
        write_post(account, CANDIDATES, tmp_path / "c.json", client=FakeClient([bad, bad]), now=NOW)


def test_wrong_point_count_is_retried(account, tmp_path):
    one_point = make(points=[{"title": "무엇이", "lines": ["한 줄", "두 줄"]}])
    client = FakeClient([one_point, GOOD])
    result = write_post(account, CANDIDATES, tmp_path / "c.json", client=client, now=NOW)
    assert result.attempts == 2


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
    assert len(longest_copy("가정용 전기요금을 내년 1월부터 kWh당 5원 올린다", CANDIDATES[0])) >= 20
    assert len(longest_copy("요금이 조금 올라요", CANDIDATES[0])) < 20


def test_check_draft_flags_copy_and_numbers(account):
    copy_draft = make(points=[
        {"title": "무엇이", "lines": ["가정용 전기요금을 내년 1월부터 kWh당 5원 올린다고", "9% 오름"]},
        {"title": "얼마나", "lines": ["한 줄", "두 줄"]},
    ])
    result = check_draft(copy_draft, CANDIDATES[0], account.collect.exclude_keywords)
    assert any("그대로 옮김" in p for p in result["retry"])
    assert any("9" in w for w in result["warn"])


def test_caption(account):
    out = mock_post(CANDIDATES)
    caption = build_caption(out.draft, account)
    assert caption.startswith(out.draft["cover_title"] + "\n" + out.draft["caption_intro"])  # 핵심 단어가 맨 앞
    assert "✔ 무엇이 바뀌나" in caption and "💡" in caption
    assert "📌 저장해 두면" in caption  # 저장·공유 권유
    assert "출처: 연합뉴스" in caption
    assert "AI의 도움을 받아 요약했어요." in caption
    assert caption.count("#") <= 5
    assert caption.splitlines()[-1] == "#전기요금 #전기요금인상 #관리비 #3분뉴스 #오늘의뉴스"  # 주제 태그 먼저, 고정 태그로 채움
    assert "07:30" not in caption  # 게시 시간은 넣지 않음


def test_hashtags_are_cleaned():
    from newsbot.write.llm import clean_hashtags

    assert clean_hashtags(["#퇴직연금", "DC 형", "퇴직연금", "", "아주아주아주아주긴해시태그열여섯자넘음", "공공분양"]) == ["#퇴직연금", "#DC형", "#공공분양"]


def test_alt_texts(account):
    out = mock_post(CANDIDATES)
    alts = build_alt_texts(out.draft, account)
    assert len(alts) == 4 and all(len(a) <= 1000 for a in alts)
    assert "전기요금 내년 1월부터 오른다" in alts[0] and alts[1].startswith("무엇이 바뀌나:")
    assert "출처: 연합뉴스" in alts[3]
