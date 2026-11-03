"""Claude가 쓴 문구를 코드로 검사한다. 결과는 경고 목록(텔레그램 미리보기에 ⚠️로 표시)."""

import re

from newsbot.collect.rss import Article

LIMITS = {"hook": 25, "headline": 20, "summary_line": 35, "why_it_matters": 40, "caption_intro": 60}
COPY_RUN = 20  # 공백 빼고 이만큼 똑같이 이어지면 베낀 것으로 본다

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER_RE.findall(text)}


def unknown_numbers(written: str, source: Article) -> set[str]:
    """출력에 있는데 원문 제목·요약에는 없는 숫자."""
    return _numbers(written) - _numbers(f"{source.title} {source.summary}")


def longest_copy(written: str, source: Article) -> str:
    """출력과 원문 사이 가장 긴 공통 구간(공백 제외)."""
    a = re.sub(r"\s", "", written)
    b = re.sub(r"\s", "", f"{source.title}{source.summary}")
    best = ""
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > len(best):
                    best = a[i - cur[j] : i]
        prev = cur
    return best


def length_problems(draft) -> list[str]:
    problems = []
    if len(draft.hook) > LIMITS["hook"]:
        problems.append(f"hook {len(draft.hook)}자 (최대 {LIMITS['hook']}자)")
    if len(draft.caption_intro) > LIMITS["caption_intro"]:
        problems.append(f"캡션 첫 문장 {len(draft.caption_intro)}자 (최대 {LIMITS['caption_intro']}자)")
    for n, item in enumerate(draft.items, start=1):
        if len(item.headline) > LIMITS["headline"]:
            problems.append(f"{n}번 제목 {len(item.headline)}자 (최대 {LIMITS['headline']}자)")
        if len(item.summary_lines) != 3:
            problems.append(f"{n}번 요약이 {len(item.summary_lines)}줄 (3줄이어야 함)")
        for k, line in enumerate(item.summary_lines, start=1):
            if len(line) > LIMITS["summary_line"]:
                problems.append(f"{n}번 요약 {k}줄 {len(line)}자 (최대 {LIMITS['summary_line']}자)")
        if len(item.why_it_matters) > LIMITS["why_it_matters"]:
            problems.append(f"{n}번 한 줄 해석 {len(item.why_it_matters)}자 (최대 {LIMITS['why_it_matters']}자)")
    return problems


def item_text(item) -> str:
    return " ".join([item.headline, *item.summary_lines, item.why_it_matters])


def check_draft(draft, sources: list[Article], exclude_keywords: list[str]) -> dict[str, list[str]]:
    """{'retry': 다시 쓰게 할 문제, 'warn': 사람이 확인할 경고}"""
    retry = length_problems(draft)
    warn: list[str] = []
    for n, (item, source) in enumerate(zip(draft.items, sources), start=1):
        text = item_text(item)
        copied = longest_copy(text, source)
        if len(copied) >= COPY_RUN:
            retry.append(f"{n}번이 원문을 {len(copied)}자 그대로 옮김: '{copied[:25]}…'")
        missing = unknown_numbers(text, source)
        if missing:
            warn.append(f"{n}번 원문에 없는 숫자: {', '.join(sorted(missing))}")
        banned = [w for w in exclude_keywords if w in text]
        if banned:
            warn.append(f"{n}번 제외 단어 포함: {', '.join(banned)}")
    return {"retry": retry, "warn": warn}
