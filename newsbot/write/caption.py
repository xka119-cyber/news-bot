"""인스타 캡션과 이미지 대체 텍스트를 코드로 조립한다(1게시물 1뉴스).

2026-11-05 노출 개선:
- 캡션 첫 줄에 표지 제목(핵심 단어)을 둔다. 인스타 검색과 구글 검색이 캡션 앞부분을 더 본다.
- 저장·공유를 부드럽게 권하는 한 줄을 넣는다(탐색 탭 노출 신호는 저장·보내기).
- 해시태그는 기사 주제어 2~3개(Claude) + 고정 태그로 채워 최대 5개.
- 카드마다 대체 텍스트(alt text)를 넣어 검색·접근성에 도움이 되게 한다.
"""

from newsbot.config import AccountConfig

MAX_CAPTION = 2200
MAX_HASHTAGS = 5
MAX_ALT = 1000
AI_NOTE = "AI의 도움을 받아 요약했어요."
CTA = "📌 저장해 두면 나중에 다시 볼 수 있어요. 궁금해할 친구에게도 보내 주세요."


def merge_hashtags(topic: list[str], fixed: list[str], limit: int = MAX_HASHTAGS) -> list[str]:
    """주제 태그를 앞에, 고정 태그로 나머지를 채운다(중복 제외)."""
    out: list[str] = []
    for tag in list(topic) + list(fixed):
        if tag not in out:
            out.append(tag)
    return out[:limit]


def build_caption(draft: dict, account: AccountConfig) -> str:
    cap = account.caption
    lines = [draft["cover_title"], draft["caption_intro"], ""]
    lines += [f"✔ {point['title']}" for point in draft["points"]]
    lines += [f"💡 {draft['why_it_matters']}", "", CTA, "", f"출처: {draft['source_name']}"]
    if cap.schedule_note:
        lines.append(cap.schedule_note)
    if cap.ai_disclosure:
        lines.append(AI_NOTE)
    lines += ["", " ".join(merge_hashtags(draft.get("hashtags", []), cap.hashtags))]
    caption = "\n".join(lines)
    if len(caption) > MAX_CAPTION:
        raise ValueError(f"캡션이 {len(caption)}자로 {MAX_CAPTION}자를 넘습니다.")
    return caption


def build_alt_texts(draft: dict, account: AccountConfig) -> list[str]:
    """카드 4장 순서대로 대체 텍스트. 화면에 보이는 글을 그대로 옮긴다(숨은 키워드 넣지 않음)."""
    brand = account.brand_name
    texts = [f"{brand} 카드뉴스 표지. {draft['category']}: {draft['cover_title']}. {draft['cover_sub']}"]
    for point in draft["points"]:
        texts.append(f"{point['title']}: " + " ".join(point["lines"]))
    texts.append(f"그래서 나한테는? {draft['why_it_matters']} (출처: {draft['source_name']})")
    return [t[:MAX_ALT] for t in texts]
