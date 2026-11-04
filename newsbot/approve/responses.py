"""텔레그램 버튼 응답을 읽어 초안 상태에 기록한다. 매 실행마다 호출(미확인 응답은 약 24시간만 보관됨)."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from newsbot.approve.telegram import (
    CALLBACK_APPROVE,
    CALLBACK_APPROVE_ALL,
    TelegramClient,
    TelegramError,
    parse_callback,
)
from newsbot.storage import drafts

log = logging.getLogger(__name__)


@dataclass
class Decision:
    draft_id: str
    status: str  # approved / skipped / ignored(이미 처리됨·다른 사람·없는 초안)
    note: str = ""


def load_offset(path: Path) -> int | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("offset")
    except (FileNotFoundError, ValueError):
        return None


def save_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _answer(client: TelegramClient, query_id: str, text: str) -> None:
    try:
        client.call("answerCallbackQuery", callback_query_id=query_id, text=text)
    except TelegramError as error:
        # 봇은 실행될 때만 응답을 읽으므로, 누른 지 오래된 버튼은 알림을 못 띄운다(정상). 기록은 계속한다.
        if "too old" not in str(error):
            log.warning("answerCallbackQuery 실패: %s", error)


def _close_buttons(client: TelegramClient, chat_id: int, message_id: int | None, reply: str) -> None:
    """버튼을 없애고 결과를 한 줄로 남긴다."""
    if not message_id:
        return
    try:
        client.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        client.call("sendMessage", chat_id=chat_id, text=reply,
                    reply_parameters={"message_id": message_id, "allow_sending_without_reply": True})
    except TelegramError as error:
        log.warning("버튼 정리 실패: %s", error)


def _reply_for(record: dict, status: str) -> str:
    return f"✅ 승인됨 — {record['slot_label']}에 올라가요" if status == "approved" else "⏭ 건너뛰었어요"


def _approve_all(client: TelegramClient, query: dict, chat_id: int, drafts_root: Path, slot_id: str,
                 now: datetime) -> list[Decision]:
    waiting = [r for r in drafts.slot_records(drafts_root, slot_id) if r.get("status") == "generated"]
    if not waiting:
        _answer(client, query["id"], "승인할 초안이 없어요(이미 처리됐어요).")
        return [Decision(slot_id, "ignored", "승인 대기 초안 없음")]
    decisions = []
    for record in waiting:
        drafts.set_status(drafts_root, record, "approved", now, note="텔레그램 모두 승인")
        _close_buttons(client, chat_id, record.get("telegram_message_id"), _reply_for(record, "approved"))
        decisions.append(Decision(record["id"], "approved", "모두 승인"))
    reply = f"✅ {len(waiting)}개 모두 승인됨 — {waiting[0]['slot_label']}에 올라가요"
    _answer(client, query["id"], reply)
    _close_buttons(client, chat_id, (query.get("message") or {}).get("message_id"), reply)
    return decisions


def handle_callback(client: TelegramClient, query: dict, chat_id: int, drafts_root: Path, now: datetime) -> list[Decision]:
    message = query.get("message") or {}
    from_chat = (message.get("chat") or {}).get("id")
    from_user = (query.get("from") or {}).get("id")
    parsed = parse_callback(query.get("data", ""))
    if from_chat != chat_id or from_user != chat_id:  # 보안: 내 대화방·내 계정만 인정
        _answer(client, query["id"], "이 대화방에서는 승인할 수 없어요.")
        return [Decision("?", "ignored", "다른 대화방")]
    if parsed is None:
        _answer(client, query["id"], "알 수 없는 버튼이에요.")
        return []
    action, draft_id = parsed
    if action == CALLBACK_APPROVE_ALL:
        return _approve_all(client, query, chat_id, drafts_root, draft_id, now)
    record = drafts.load(drafts_root, draft_id)
    if record is None:
        _answer(client, query["id"], "초안을 찾을 수 없어요(정리됐을 수 있어요).")
        return [Decision(draft_id, "ignored", "초안 없음")]
    if record["status"] != "generated":
        _answer(client, query["id"], f"이미 처리된 초안이에요 ({record['status']}).")
        return [Decision(draft_id, "ignored", f"이미 {record['status']}")]

    status = "approved" if action == CALLBACK_APPROVE else "skipped"
    drafts.set_status(drafts_root, record, status, now, note="텔레그램 버튼")
    reply = _reply_for(record, status)
    _answer(client, query["id"], reply)
    _close_buttons(client, chat_id, message.get("message_id"), reply)
    return [Decision(draft_id, status)]


def process_updates(client: TelegramClient, chat_id: int, drafts_root: Path, offset_path: Path, now: datetime) -> list[Decision]:
    offset = load_offset(offset_path)
    updates = client.get_updates(offset=offset)
    decisions = []
    for update in updates:
        query = update.get("callback_query")
        if query:
            decisions += handle_callback(client, query, chat_id, drafts_root, now)
        offset = update["update_id"] + 1
    if offset is not None:
        save_offset(offset_path, offset)
    return decisions
