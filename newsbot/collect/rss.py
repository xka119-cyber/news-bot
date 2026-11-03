"""RSS에서 기사 후보를 모은다. 본문은 가져오지 않고 제목·요약·링크·언론사·발행시각만 쓴다."""

import calendar
import html
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from newsbot.config import AccountConfig, Feed

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; newsbot/1.0)"
SUMMARY_MAX_CHARS = 300
TITLE_SIMILARITY = 0.5
HISTORY_DAYS = 3
MAX_CANDIDATES = 30
MIN_CANDIDATES = 15

# 사진·영상·운세·인사 같은 카드뉴스로 쓰기 어려운 기사
JUNK_KEYWORDS = [
    "[영상]", "[포토]", "[사진]", "[인사]", "[부고]", "부음", "[게시판]", "[표]",
    "[사설]", "[칼럼]", "[기고]", "[오피니언]", "운세", "[알립니다]", "[모집]", "초대석",
]

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】")
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]")
_TRACKING_PARAMS = {"plink", "cooper", "ref", "from", "fbclid", "gclid"}


@dataclass
class Article:
    source: str
    title: str
    summary: str
    link: str
    published: datetime  # UTC

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "summary": self.summary,
            "link": self.link,
            "published": self.published.isoformat(),
        }


@dataclass
class CollectResult:
    candidates: list[Article]
    fetched: int = 0
    recent: int = 0
    duplicates: int = 0
    used_before: int = 0
    excluded: list[tuple[Article, str]] = field(default_factory=list)
    failed_feeds: list[str] = field(default_factory=list)


def clean_text(raw: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", raw or ""))
    return _SPACE_RE.sub(" ", text).strip()


def shorten(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def normalize_link(link: str) -> str:
    parts = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def _bigrams(title: str) -> set[str]:
    text = _NON_WORD_RE.sub("", _BRACKET_RE.sub("", title)).lower()
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


def title_similarity(a: str, b: str) -> float:
    """제목 글자 두 개씩 묶음의 겹침 비율(자카드). 1이면 같은 제목."""
    x, y = _bigrams(a), _bigrams(b)
    return len(x & y) / len(x | y) if x | y else 0.0


def fetch_feed(url: str, timeout: int = 15) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_feed(raw: bytes, feed: Feed) -> list[Article]:
    articles = []
    for entry in feedparser.parse(raw).entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        title = clean_text(entry.get("title", ""))
        link = entry.get("link", "")
        if not (stamp and title and link):
            continue
        articles.append(
            Article(
                source=feed.source,
                title=title,
                summary=shorten(clean_text(entry.get("summary", ""))),
                link=link,
                published=datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc),
            )
        )
    return articles


def exclusion_reason(article: Article, exclude_keywords: list[str]) -> str | None:
    for word in JUNK_KEYWORDS:
        if word in article.title:
            return f"카드로 쓰기 어려움({word})"
    text = f"{article.title} {article.summary}"
    for word in exclude_keywords:
        if word in text:
            return f"제외 키워드({word})"
    return None


def load_history(path: Path, now: datetime) -> list[dict]:
    """최근 HISTORY_DAYS일 안에 쓴 기사 목록. 파일이 없거나 깨졌으면 빈 목록."""
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    except (FileNotFoundError, ValueError):
        return []
    cutoff = now - timedelta(days=HISTORY_DAYS)
    recent = []
    for item in items:
        try:
            if datetime.fromisoformat(item["used_at"]) >= cutoff:
                recent.append(item)
        except (KeyError, ValueError):
            continue
    return recent


def record_used(path: Path, articles: list[Article], now: datetime) -> None:
    """게시에 쓴 기사를 기록한다(M5 게시 후 호출). 오래된 기록은 정리한다."""
    items = load_history(path, now)
    items += [{"link": a.link, "title": a.title, "used_at": now.isoformat()} for a in articles]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def _balance(articles: list[Article], limit: int) -> list[Article]:
    """언론사별로 최신순 줄을 세우고 번갈아 한 개씩 뽑아 한 곳에 쏠리지 않게 한다."""
    by_source: dict[str, list[Article]] = {}
    for article in sorted(articles, key=lambda a: a.published, reverse=True):
        by_source.setdefault(article.source, []).append(article)
    picked: list[Article] = []
    queues = list(by_source.values())
    while queues and len(picked) < limit:
        for queue in list(queues):
            if len(picked) >= limit:
                break
            picked.append(queue.pop(0))
            if not queue:
                queues.remove(queue)
    return sorted(picked, key=lambda a: a.published, reverse=True)


def collect(account: AccountConfig, history_path: Path, now: datetime | None = None, fetch=fetch_feed) -> CollectResult:
    now = now or datetime.now(timezone.utc)
    cfg = account.collect
    result = CollectResult(candidates=[])

    articles: list[Article] = []
    for feed in cfg.rss_feeds:
        try:
            parsed = parse_feed(fetch(feed.url), feed)
        except Exception as error:  # 한 곳이 막혀도 나머지로 계속
            log.warning("RSS 실패: %s %s (%s)", feed.source, feed.section, type(error).__name__)
            result.failed_feeds.append(f"{feed.source} {feed.section}".strip())
            continue
        result.fetched += len(parsed)
        articles += parsed

    cutoff = now - timedelta(hours=cfg.lookback_hours)
    articles = [a for a in articles if cutoff <= a.published <= now + timedelta(minutes=10)]
    result.recent = len(articles)

    history = load_history(history_path, now)
    used_links = {normalize_link(item["link"]) for item in history}
    used_titles = [item["title"] for item in history]

    kept: list[Article] = []
    seen_links: set[str] = set()
    # 요약이 긴 기사를 먼저 보게 해서, 중복이면 정보가 많은 쪽이 남게 한다
    for article in sorted(articles, key=lambda a: len(a.summary), reverse=True):
        link = normalize_link(article.link)
        if link in used_links or any(title_similarity(article.title, t) >= TITLE_SIMILARITY for t in used_titles):
            result.used_before += 1
            continue
        if link in seen_links or any(title_similarity(article.title, k.title) >= TITLE_SIMILARITY for k in kept):
            result.duplicates += 1
            continue
        seen_links.add(link)
        reason = exclusion_reason(article, cfg.exclude_keywords)
        if reason:
            result.excluded.append((article, reason))
            continue
        kept.append(article)

    result.candidates = _balance(kept, MAX_CANDIDATES)
    return result
