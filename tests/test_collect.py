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
