from datetime import datetime, timezone

from PIL import Image

from newsbot.config import load_account
from newsbot.design.cards import build_cards, date_label, render_cards

WHEN = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)  # 한국 11월 4일 수요일 07시


def make_draft(line="요약 한 줄이에요"):
    return {
        "category": "경제", "cover_title": "전기요금 1월부터 오른다", "cover_sub": "우리 집은 얼마?",
        "points": [{"title": "무엇이 바뀌나", "lines": [line, line]}, {"title": "얼마나 오르나", "lines": [line, line, line]}],
        "why_it_matters": "관리비가 조금 늘 수 있어요", "caption_intro": "?",
        "source_name": "연합뉴스", "source_url": "https://a.kr", "source_title": "원문",
    }


def test_date_label_uses_korean_time():
    assert date_label(WHEN, "Asia/Seoul") == "11월 4일 수요일"


def test_build_cards_pages():
    cards = build_cards(make_draft(), load_account("today_news"), WHEN)
    assert [c["kind"] for c in cards] == ["cover", "point", "point", "why"]
    assert [c["page"] for c in cards] == [1, 2, 3, 4]
    assert cards[0]["emoji"] == "💰"


def test_render_four_jpegs(tmp_path):
    result = render_cards(make_draft(), load_account("today_news"), tmp_path, WHEN)
    assert len(result.paths) == 4
    for path in result.paths:
        with Image.open(path) as im:
            assert im.format == "JPEG" and im.size == (1080, 1350)
    assert result.warnings == []


def test_long_text_shrinks_or_warns(tmp_path):
    long_line = "아주 긴 요약 문장이 계속 이어져서 카드 한 장에 다 들어가지 않는 경우를 확인합니다 " * 4
    result = render_cards(make_draft(long_line), load_account("today_news"), tmp_path, WHEN)
    assert any("줄였습니다" in w or "넘칩니다" in w for w in result.warnings)
