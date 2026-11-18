from datetime import datetime, timedelta, timezone

import pytest

from newsbot.collect.rss import (
    Article,
    clean_text,
    collect,
    load_history,
    normalize_link,
    record_used,
    title_similarity,
)
from newsbot.config import load_account

NOW = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)


def rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        f"<pubDate>{p.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
        for t, link, d, p in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


@pytest.fixture
def account():
    acc = load_account("today_news")
    acc.collect.rss_feeds = acc.collect.rss_feeds[:2]  # 연합 경제, 연합 산업
    return acc


def make_fetch(feeds):
    def fetch(url):
        if url not in feeds:
            raise OSError("down")
        return feeds[url]
    return fetch


def test_config_has_feeds():
    feeds = load_account("today_news").collect.rss_feeds
    assert len(feeds) >= 6
    assert len({f.source for f in feeds}) >= 5


def test_clean_text_strips_tags_and_entities():
    assert clean_text("<p>물가&nbsp;상승 &amp; <b>금리</b></p>") == "물가 상승 & 금리"


def test_normalize_link_drops_tracking():
    a = normalize_link("https://News.sbs.co.kr/a?news_id=1&plink=RSSREADER&utm_source=x#top")
    assert a == normalize_link("https://news.sbs.co.kr/a?news_id=1")


def test_title_similarity():
    assert title_similarity("[단독] 금리 0.25%p 인하 결정", "금리 0.25%p 인하 결정…시장 환영") >= 0.5
    assert title_similarity("금리 인하 결정", "애플 새 아이폰 공개") < 0.2


def test_collect_filters_old_duplicate_excluded(account, tmp_path):
    u1, u2 = account.collect.rss_feeds[0].url, account.collect.rss_feeds[1].url
    recent = NOW - timedelta(hours=1)
    feeds = {
        u1: rss([
            ("전기요금 내년부터 인상", "https://a.kr/1", "가정용 전기요금이 오른다는 소식입니다", recent),
            ("李대통령 국채발언 놓고 공방", "https://a.kr/2", "정치권 공방", recent),
            ("오래된 기사", "https://a.kr/3", "어제 기사", NOW - timedelta(hours=30)),
            ("[포토] 가을 단풍", "https://a.kr/4", "사진", recent),
        ]),
        u2: rss([
            ("전기요금 내년부터 인상…가구당 부담", "https://b.kr/1", "짧음", recent),
            ("반도체 수출 역대 최대", "https://b.kr/2", "수출이 늘었습니다", recent),
        ]),
    }
    result = collect(account, tmp_path / "history.json", now=NOW, fetch=make_fetch(feeds))
    titles = [a.title for a in result.candidates]
    assert titles.count("전기요금 내년부터 인상") == 1
    assert "전기요금 내년부터 인상…가구당 부담" not in titles  # 비슷한 제목은 하나만
    assert "반도체 수출 역대 최대" in titles
    assert not any("대통령" in t for t in titles)
    assert "오래된 기사" not in titles
    assert not any("[포토]" in t for t in titles)
    assert result.duplicates == 1
    reasons = {a.title: r for a, r in result.excluded}
    assert "제외 키워드" in reasons["李대통령 국채발언 놓고 공방"]
    assert all(a.source == "연합뉴스" for a in result.candidates)


def test_failed_feed_does_not_stop(account, tmp_path):
    u1 = account.collect.rss_feeds[0].url
    feeds = {u1: rss([("반도체 수출 역대 최대", "https://b.kr/2", "수출", NOW - timedelta(hours=1))])}
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch(feeds))
    assert len(result.candidates) == 1
    assert result.failed_feeds == ["연합뉴스 산업"]


def test_history_excludes_recently_used(account, tmp_path):
    path = tmp_path / "state" / "history.json"
    used = Article("연합뉴스", "반도체 수출 역대 최대", "", "https://b.kr/2", NOW)
    record_used(path, [used], NOW - timedelta(days=1))
    u1 = account.collect.rss_feeds[0].url
    feeds = {u1: rss([("반도체 수출 역대 최대", "https://b.kr/2?utm_source=x", "수출", NOW - timedelta(hours=1))])}
    result = collect(account, path, now=NOW, fetch=make_fetch(feeds))
    assert result.candidates == []
    assert result.used_before == 1


def test_history_forgets_after_three_days(tmp_path):
    path = tmp_path / "h.json"
    record_used(path, [Article("x", "t", "", "https://x.kr", NOW)], NOW - timedelta(days=4))
    assert load_history(path, NOW) == []
    assert load_history(tmp_path / "missing.json", NOW) == []


def test_balance_mixes_sources(account, tmp_path):
    u1, u2 = account.collect.rss_feeds[0].url, account.collect.rss_feeds[1].url
    account.collect.rss_feeds[1].source = "동아일보"
    def unique_title(i):  # 서로 겹치지 않는 한글 제목
        return "".join(chr(0xAC00 + (i * 53 + k * 389) % 11172) for k in range(8))

    many = [(unique_title(i), f"https://a.kr/{i}", "요약", NOW - timedelta(minutes=i)) for i in range(40)]
    feeds = {u1: rss(many), u2: rss([("반도체 수출 역대 최대", "https://b.kr/2", "수출", NOW - timedelta(hours=5))])}
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch(feeds))
    assert len(result.candidates) == 30
    assert any(a.source == "동아일보" for a in result.candidates)


def test_priority_keywords_go_first_and_are_capped(account, tmp_path):
    """11-19 실험: 제목에 우선 낱말(내 돈·할 일)이 든 기사가 30개 안에 먼저 들어가고, 최대 20개까지만."""
    u1, u2 = account.collect.rss_feeds[0].url, account.collect.rss_feeds[1].url
    account.collect.rss_feeds[1].source = "동아일보"
    account.collect.priority_keywords = ["환급"]

    def unique_title(i, prefix=""):
        return prefix + "".join(chr(0xAC00 + (i * 53 + k * 389) % 11172) for k in range(8))

    # 최신 기사 40개(우선 낱말 없음)보다 오래된 '환급' 기사 25개 — 예전 방식이면 오래된 쪽은 잘려 나간다
    plain = [(unique_title(i), f"https://a.kr/{i}", "요약", NOW - timedelta(minutes=i)) for i in range(40)]
    refunds = [(unique_title(100 + i, f"환급{i} "), f"https://b.kr/{i}", "요약", NOW - timedelta(hours=5, minutes=i))
               for i in range(25)]
    feeds = {u1: rss(plain), u2: rss(refunds)}
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch(feeds))
    assert len(result.candidates) == 30
    assert result.prioritized == 20
    assert all("환급" in a.title for a in result.candidates[:20])
    # 나머지 10개는 남은 기사 전체에서 언론사를 번갈아 최신순 — 가장 최신 일반 기사가 반드시 들어간다
    rest_titles = [a.title for a in result.candidates[20:]]
    assert plain[0][0] in rest_titles and sum("환급" not in t for t in rest_titles) >= 5


def test_no_priority_keywords_keeps_old_behavior(account, tmp_path):
    account.collect.priority_keywords = []
    u1 = account.collect.rss_feeds[0].url
    feeds = {u1: rss([("전기요금 환급 신청 시작", "https://a.kr/1", "요약", NOW - timedelta(hours=1))])}
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch(feeds))
    assert result.prioritized == 0 and len(result.candidates) == 1


def test_priority_matches_title_not_summary(account, tmp_path):
    account.collect.priority_keywords = ["마감"]
    u1 = account.collect.rss_feeds[0].url
    feeds = {u1: rss([("반도체 수출 역대 최대", "https://a.kr/1", "코스피 상승 마감", NOW - timedelta(hours=1))])}
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch(feeds))
    assert result.prioritized == 0


def test_few_priority_articles_fill_rest_without_duplicates(account, tmp_path):
    """우선 기사가 20개보다 적으면 남은 칸을 일반 기사로 채우고, 같은 기사가 두 번 들어가지 않는다."""
    u1, u2 = account.collect.rss_feeds[0].url, account.collect.rss_feeds[1].url
    account.collect.rss_feeds[1].source = "동아일보"
    account.collect.priority_keywords = ["환급"]

    def unique_title(i, prefix=""):
        return prefix + "".join(chr(0xAC00 + (i * 53 + k * 389) % 11172) for k in range(8))

    plain = [(unique_title(i), f"https://a.kr/{i}", "요약", NOW - timedelta(minutes=i)) for i in range(40)]
    refunds = [(unique_title(100 + i, f"환급{i} "), f"https://b.kr/{i}", "요약", NOW - timedelta(hours=5, minutes=i))
               for i in range(3)]
    result = collect(account, tmp_path / "h.json", now=NOW, fetch=make_fetch({u1: rss(plain), u2: rss(refunds)}))
    links = [a.link for a in result.candidates]
    assert result.prioritized == 3 and len(result.candidates) == 30 and len(set(links)) == 30
    assert all("환급" in a.title for a in result.candidates[:3])


def test_config_keywords_skip_known_false_positives():
    """11-19 검토: '%포인트'·'인하대'·'사기 혐의'·매시간 날씨 기사가 우선 낱말에 걸리지 않게."""
    from newsbot.collect.rss import is_priority
    keywords = load_account("today_news").collect.priority_keywords
    for title in ["기준금리 0.25%포인트 인상", "인하대, 신입생 모집", "사기 혐의 30대 구속영장 신청",
                  "[날씨] 내일 맑음", "국고채 금리 대체로 상승", "코스피 50포인트 하락"]:
        assert not is_priority(Article("x", title, "", "https://x.kr", NOW), keywords), title
    for title in ["가스요금 인상 검토", "카드 포인트 내년 5월 소멸", "금감원 보험사기 주의보", "내일 아침 한파"]:
        assert is_priority(Article("x", title, "", "https://x.kr", NOW), keywords), title
