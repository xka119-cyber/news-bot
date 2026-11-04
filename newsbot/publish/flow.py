"""publish 명령의 흐름: 시간이 지난 초안 정리 → 승인된 초안만 게시 → 결과를 텔레그램으로 보고.

여러 번 실행해도 안전하다(멱등): 이미 게시·건너뜀·만료된 초안은 다시 건드리지 않는다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from newsbot.approve.telegram import REMOVE_KEYBOARD, TelegramClient, TelegramError
from newsbot.collect.rss import Article, record_used
from newsbot.config import AccountConfig
from newsbot.publish.instagram import InstagramClient, InstagramError, publish_carousel
from newsbot.storage import drafts
from newsbot.storage.media_branch import MediaError
from newsbot.write.caption import build_alt_texts

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    draft_id: str
    status: str  # published / failed / expired
    note: str = ""


def _notify(tg: TelegramClient, chat_id: int, record: dict, text: str, clear_buttons: bool = False) -> None:
    message_id = record.get("telegram_message_id")
    try:
        if clear_buttons and message_id:
            tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        reply = {"message_id": message_id, "allow_sending_without_reply": True} if message_id else None
        # 게시·건너뜀 결과를 알릴 때 아래 키보드 버튼도 내린다(그 시각 게시분은 끝났으므로)
        tg.call("sendMessage", chat_id=chat_id, text=text, reply_parameters=reply,
                link_preview_options={"is_disabled": False}, reply_markup=REMOVE_KEYBOARD)
    except TelegramError as error:
        log.warning("텔레그램 보고 실패: %s", error)


def close_approve_all(tg: TelegramClient, chat_id: int, record: dict) -> None:
    """같은 시각의 [모두 승인] 버튼을 없앤다(시간이 지났거나 이미 처리됨). 이미 없어졌으면 조용히 넘어간다."""
    message_id = record.get("approve_all_message_id")
    if not message_id:
        return
    try:
        tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    except TelegramError:
        pass


def media_dest(draft_id: str) -> str:
    return drafts.draft_dir(Path("drafts"), draft_id).as_posix()


def publish_one(record: dict, account: AccountConfig, root: Path, ig: InstagramClient, tg: TelegramClient,
                chat_id: int, upload, history_path: Path, now: datetime) -> Outcome:
    draft_id = record["id"]
    try:
        if not record.get("media_urls"):
            folder = drafts.draft_dir(root, draft_id)
            files = [folder / name for name in record["cards"]]
            if not all(f.exists() for f in files):
                # 카드 이미지는 승인 전에는 저장소에 올리지 않으므로, 새로 시작한 실행(Actions)에서는 다시 그린다
                from newsbot.design.cards import render_cards

                files = render_cards(record["draft"], account, folder, datetime.fromisoformat(record["slot"])).paths
            record["media_urls"] = upload(files, media_dest(draft_id), account.publish.media_repo, f"media: {draft_id}")
            drafts.save(root, record)
        progress = record.setdefault("publish", {})
        result = publish_carousel(ig, record["media_urls"], record["caption"], progress,
                                  save=lambda: drafts.save(root, record),
                                  alt_texts=build_alt_texts(record["draft"], account))
    except (InstagramError, MediaError, OSError) as error:
        drafts.set_status(root, record, "failed", now, note=str(error))
        _notify(tg, chat_id, record, f"❌ 게시 실패 ({record['slot_label']})\n이유: {error}")
        return Outcome(draft_id, "failed", str(error))

    record["media_id"], record["permalink"] = result.media_id, result.permalink
    drafts.set_status(root, record, "published", now)
    draft = record["draft"]
    record_used(history_path, [Article(draft["source_name"], draft["source_title"], "", draft["source_url"], now)], now)
    _notify(tg, chat_id, record, f"📸 올라갔어요! {draft['cover_title']}\n{result.permalink}")
    return Outcome(draft_id, "published", result.permalink)


def run(account: AccountConfig, root: Path, ig: InstagramClient, tg: TelegramClient, chat_id: int, upload,
        history_path: Path, now: datetime, only_id: str | None = None) -> list[Outcome]:
    """only_id를 주면 그 초안만, 게시 시각과 상관없이 처리한다(수동 실행용; 승인돼 있어야 함)."""
    outcomes = []
    for record in drafts.all_records(root, account.account_key):
        if only_id and record["id"] != only_id:
            continue
        due = datetime.fromisoformat(record["slot"]) <= now
        status = record.get("status")
        if status == "generated" and due:
            close_approve_all(tg, chat_id, record)
            if account.approval.on_timeout == "publish":
                drafts.set_status(root, record, "approved", now, note="시간 초과 자동 승인")
                status = "approved"
            else:
                drafts.set_status(root, record, "expired", now, note="승인 없이 게시 시각이 지남")
                _notify(tg, chat_id, record, f"⌛ 승인이 없어 {record['slot_label']} 게시는 건너뛰었어요.", clear_buttons=True)
                outcomes.append(Outcome(record["id"], "expired"))
                continue
        retry_failed = only_id is not None and status == "failed"
        if (status == "approved" and (due or only_id)) or retry_failed:
            outcomes.append(publish_one(record, account, root, ig, tg, chat_id, upload, history_path, now))
        elif only_id:
            outcomes.append(Outcome(record["id"], status or "?", "승인된 초안이 아니라서 게시하지 않았어요"))
    return outcomes
