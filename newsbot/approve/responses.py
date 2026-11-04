"""텔레그램 응답을 읽어 초안 상태에 기록한다. 매 실행마다 호출(미확인 글 메시지는 약 24시간 보관됨).

승인은 아래 키보드 버튼이 보낸 글 메시지("✅ 1번 승인 (07:30)" 등)로 받는다.
예전 메시지 안 버튼(콜백)도 읽지만, 텔레그램이 2~3분 뒤 버려서 정해진 때만 깨어나는 봇에는 잘 안 닿는다.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from newsbot.approve.telegram import (
    CALLBACK_APPROVE,
    CALLBACK_APPROVE_ALL,
    REMOVE_KEYBOARD,
    TelegramClient,
    TelegramError,
    parse_callback,
)
from newsbot.storage import drafts

log = logging.getLogger(__name__)

# "✅ 모두 승인 (07:30)", "⏭ 모두 건너뛰기 (07:30)", "✅ 2번 승인 (07:30)", "✅ 승인 (07:30)"
_KEYBOARD_RE = re.compile(r"^\s*(✅|⏭)\s*(?:(모두)|(\d+)번)?\s*(승인|건너뛰기)\s*(?:\((\d{1,2}:\d{2})\))?\s*$")


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


def _waiting_records(drafts_root: Path, hhmm: str | None) -> list[dict]:
    """승인을 기다리는 초안 중 가장 이른 게시 시각의 것들. hhmm이 있으면 그 시각만."""
    accounts = sorted(p.name for p in drafts_root.iterdir() if p.is_dir()) if drafts_root.exists() else []
    time_suffix = f" {int(hhmm.split(':')[0]):02d}:{hhmm.split(':')[1]}" if hhmm else ""
    waiting = [
        record
        for account in accounts
        for record in drafts.all_records(drafts_root, account)
        if record.get("status") == "generated" and record.get("slot_label", "").endswith(time_suffix)
    ]
    if not waiting:
        return []
    first = min(r["slot"] for r in waiting)
    return [r for r in waiting if r["slot"] == first]


def handle_text(client: TelegramClient, message: dict, chat_id: int, drafts_root: Path, now: datetime) -> list[Decision]:
    """아래 키보드 버튼이 보낸 글 메시지를 처리한다. 내 대화방·내 계정이 보낸 것만 인정."""
    if (message.get("chat") or {}).get("id") != chat_id or (message.get("from") or {}).get("id") != chat_id:
        return []
    match = _KEYBOARD_RE.match(message.get("text") or "")
    if not match:
        return []  # 버튼 글이 아닌 일반 메시지는 무시
    _mark, _every, number, verb, hhmm = match.groups()
    status = "approved" if verb == "승인" else "skipped"
    waiting = _waiting_records(drafts_root, hhmm)
    if number:
        waiting = [r for r in waiting if r["id"].count("-") == 2 and r["id"].rsplit("-", 1)[1] == number]
    if not waiting:
        _reply(client, chat_id, message, "이미 처리됐거나 게시 시각이 지난 버튼이에요.", remove_keyboard=True)
        return [Decision(message.get("text", ""), "ignored", "기다리는 초안 없음")]
    decisions = []
    for record in waiting:
        drafts.set_status(drafts_root, record, status, now, note="키보드 버튼")
        decisions.append(Decision(record["id"], status, "키보드 버튼"))
    label = waiting[0]["slot_label"]
    names = f"{number}번" if number else (f"{len(waiting)}개 모두" if len(waiting) > 1 else "게시물")
    text = f"✅ {names} 승인됨 — {label}에 올라가요" if status == "approved" else f"⏭ {names} 건너뛰었어요"
    slot_left = [r for r in drafts.slot_records(drafts_root, waiting[0]["id"].rsplit("-", 1)[0])
                 if r.get("status") == "generated"] if waiting[0]["id"].count("-") == 2 else []
    _reply(client, chat_id, message, text, remove_keyboard=not slot_left)
    return decisions


def _reply(client: TelegramClient, chat_id: int, message: dict, text: str, remove_keyboard: bool = False) -> None:
    try:
        client.call("sendMessage", chat_id=chat_id, text=text,
                    reply_parameters={"message_id": message.get("message_id"), "allow_sending_without_reply": True},
                    reply_markup=REMOVE_KEYBOARD if remove_keyboard else None)
    except TelegramError as error:
        log.warning("답장 실패: %s", error)


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
    slot_id = draft_id.rsplit("-", 1)[0]
    others = drafts.slot_records(drafts_root, slot_id) if draft_id.count("-") == 2 else []
    if record.get("approve_all_message_id") and not any(r.get("status") == "generated" for r in others):
        try:  # 하나씩 다 정했으면 [모두 승인] 버튼은 필요 없다
            client.call("editMessageReplyMarkup", chat_id=chat_id, message_id=record["approve_all_message_id"],
                        reply_markup={"inline_keyboard": []})
        except TelegramError:
            pass
    return [Decision(draft_id, status)]


def process_updates(client: TelegramClient, chat_id: int, drafts_root: Path, offset_path: Path, now: datetime) -> list[Decision]:
    offset = load_offset(offset_path)
    updates = client.get_updates(offset=offset)
    decisions = []
    for update in updates:
        query = update.get("callback_query")
        if query:
            decisions += handle_callback(client, query, chat_id, drafts_root, now)
        elif update.get("message"):
            decisions += handle_text(client, update["message"], chat_id, drafts_root, now)
        offset = update["update_id"] + 1
    if offset is not None:
        save_offset(offset_path, offset)
    return decisions
