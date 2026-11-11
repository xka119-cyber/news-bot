"""Claude가 쓴 문구를 코드로 검사한다. 결과는 경고 목록(텔레그램 미리보기에 ⚠️로 표시)."""

import re

from newsbot.collect.rss import Article

LIMITS = {"cover_title": 22, "cover_sub": 30, "point_title": 14, "point_line": 35, "why_it_matters": 60, "caption_intro": 60,
          "reel_hook": 12, "reel_beat": 20, "reel_takeaway": 24,
          # v3 장면: 읽는 문장 4개가 합쳐 약 13초(1초에 6~7자) 안에 끝나야 한다
          "scene_say": 26, "scene_caption": 12}
SCENES = 4
_SEARCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 -]{2,39}")   # 영어 낱말(숫자·하이픈 허용: "4k city", "wi-fi router")
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


def _over(label: str, text: str, limit: int) -> str | None:
    return f"{label} {len(text)}자 (최대 {limit}자)" if len(text) > limit else None


def length_problems(draft) -> list[str]:
    found = [
        _over("표지 제목", draft.cover_title, LIMITS["cover_title"]),
        _over("표지 설명", draft.cover_sub, LIMITS["cover_sub"]),
        _over("한 줄 해석", draft.why_it_matters, LIMITS["why_it_matters"]),
        _over("캡션 첫 문장", draft.caption_intro, LIMITS["caption_intro"]),
    ]
    for n, point in enumerate(draft.points, start=1):
        found.append(_over(f"{n}번 소제목", point.title, LIMITS["point_title"]))
        if not 2 <= len(point.lines) <= 3:
            found.append(f"{n}번 카드가 {len(point.lines)}줄 (2~3줄이어야 함)")
        for k, line in enumerate(point.lines, start=1):
            found.append(_over(f"{n}번 카드 {k}줄", line, LIMITS["point_line"]))
    # 릴스 대본 문제는 다시 쓰게 하고, 두 번째에도 남으면 경고만 한다(릴스는 카드 문구로 대신 만든다)
    reel = getattr(draft, "reel", None)
    if reel is None:
        found.append("릴스 대본(reel)이 없음")
    else:
        if len(reel.hook) != 2:
            found.append(f"릴스 첫 화면이 {len(reel.hook)}줄 (2줄이어야 함)")
        if len(reel.beats) != 3:
            found.append(f"릴스 핵심이 {len(reel.beats)}줄 (3줄이어야 함)")
        for k, line in enumerate(reel.hook, start=1):
            found.append(_over(f"릴스 첫 화면 {k}줄", line, LIMITS["reel_hook"]))
        for k, line in enumerate(reel.beats, start=1):
            found.append(_over(f"릴스 핵심 {k}줄", line, LIMITS["reel_beat"]))
        found.append(_over("릴스 마무리", reel.takeaway, LIMITS["reel_takeaway"]))
        scenes = getattr(reel, "scenes", [])
        if len(scenes) != SCENES:
            found.append(f"릴스 장면(scenes)이 {len(scenes)}개 ({SCENES}개여야 함)")
        for n, scene in enumerate(scenes, start=1):
            found.append(_over(f"릴스 {n}번 장면 읽는 문장", scene.say, LIMITS["scene_say"]))
            if not 1 <= len(scene.caption) <= 2:
                found.append(f"릴스 {n}번 장면 자막이 {len(scene.caption)}줄 (1~2줄이어야 함)")
            for k, line in enumerate(scene.caption, start=1):
                found.append(_over(f"릴스 {n}번 장면 자막 {k}줄", line, LIMITS["scene_caption"]))
            if not _SEARCH_RE.fullmatch(scene.search.strip()):
                found.append(f"릴스 {n}번 장면 검색어는 영어 2~3단어여야 함: {scene.search!r}")
    return [p for p in found if p]


def draft_text(draft) -> str:
    parts = [draft.cover_title, draft.cover_sub, draft.why_it_matters]
    for point in draft.points:
        parts += [point.title, *point.lines]
    reel = getattr(draft, "reel", None)
    if reel is not None:
        parts += [*reel.hook, *reel.beats, reel.takeaway]
        for scene in getattr(reel, "scenes", []):
            parts += [scene.say, *scene.caption]
    return " ".join(parts)


def check_draft(draft, source: Article, exclude_keywords: list[str]) -> dict[str, list[str]]:
    """{'retry': 다시 쓰게 할 문제, 'warn': 사람이 확인할 경고}"""
    retry = length_problems(draft)
    warn: list[str] = []
    text = draft_text(draft)
    copied = longest_copy(text, source)
    if len(copied) >= COPY_RUN:
        retry.append(f"원문을 {len(copied)}자 그대로 옮김: '{copied[:25]}…'")
    missing = unknown_numbers(text, source)
    if missing:
        warn.append(f"원문에 없는 숫자: {', '.join(sorted(missing))}")
    banned = [w for w in exclude_keywords if w in text]
    if banned:
        warn.append(f"제외 단어 포함: {', '.join(banned)}")
    return {"retry": retry, "warn": warn}
