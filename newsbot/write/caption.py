"""인스타 캡션을 코드로 조립한다(1게시물 1뉴스)."""

from newsbot.config import AccountConfig

MAX_CAPTION = 2200
AI_NOTE = "AI의 도움을 받아 요약했어요."


def build_caption(draft: dict, account: AccountConfig) -> str:
    cap = account.caption
    lines = [draft["caption_intro"], ""]
    lines += [f"✔ {point['title']}" for point in draft["points"]]
    lines += [f"💡 {draft['why_it_matters']}", "", f"출처: {draft['source_name']}"]
    if cap.schedule_note:
        lines.append(cap.schedule_note)
    if cap.ai_disclosure:
        lines.append(AI_NOTE)
    lines += ["", " ".join(cap.hashtags[:5])]
    caption = "\n".join(lines)
    if len(caption) > MAX_CAPTION:
        raise ValueError(f"캡션이 {len(caption)}자로 {MAX_CAPTION}자를 넘습니다.")
    return caption
