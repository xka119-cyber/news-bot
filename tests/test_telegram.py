from datetime import datetime, timezone

import pytest

from newsbot.approve.responses import process_updates
from newsbot.approve.telegram import (
    TelegramClient,
    TelegramError,
    buttons,
    callback_data,
    parse_callback,
    preview_text,
)
from newsbot.config import load_account
from newsbot.storage import drafts

NOW = datetime(2026, 11, 3, 21, 0, tzinfo=timezone.utc)  # 한국 11-04 06:00
CHAT = 123456789
FAKE_TOKEN = "1234567890" + ":" + "A" * 35


class FakeClient:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def get_updates(self, offset, timeout=0):
        self.calls.append(("getUpdates", {"offset": offset}))
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"message_id": 1}


def make_record(root, status="generated"):
    account = load_account("today_news")
    slot = drafts.slot_at(account, "07:30", NOW)
    did = drafts.draft_id("today_news", slot, account.timezone)
    record = {"id": did, "slot": slot.isoformat(), "slot_label": drafts.slot_label(slot, account.timezone), "status": status}
    drafts.save(root, record)
    return did


def callback(update_id, data, chat=CHAT, user=CHAT):
    return {"update_id": update_id, "callback_query": {
        "id": f"q{update_id}", "data": data, "from": {"id": user},
        "message": {"message_id": 50, "chat": {"id": chat}},
    }}


def test_slot_helpers():
    account = load_account("today_news")
    slot = drafts.slot_at(account, "07:30", NOW)
    assert drafts.draft_id("today_news", slot, "Asia/Seoul") == "today_news/20261104-0730"
    assert drafts.slot_label(slot, "Asia/Seoul") == "11월 4일(수) 07:30"
    assert drafts.upcoming_slot(account, NOW) == slot  # 06:00 기준 90분 뒤
    assert drafts.upcoming_slot(account, NOW.replace(hour=23)) is None  # 08:00 → 12:00은 4시간 뒤


def test_callback_data_roundtrip_and_limit():
    data = callback_data("a", "today_news/20261104-0730")
    assert parse_callback(data) == ("a", "today_news/20261104-0730")
    assert len(data.encode()) <= 64
    assert parse_callback("x|y") is None
    with pytest.raises(ValueError):
        callback_data("a", "x" * 70)
    assert buttons("today_news/20261104-0730")["inline_keyboard"][0][0]["text"] == "✅ 승인"


def test_preview_text_escapes_and_lists():
    draft = {"cover_title": "<금리>", "source_url": "https://a.kr/?a=1&b=2", "source_name": "연합뉴스", "source_title": "금리 기사"}
    text = preview_text(draft, "캡션 & 내용", "11월 4일(수) 07:30", ["원문에 없는 숫자: 7"], "오늘의 3분 뉴스")
    assert "&lt;금리&gt;" in text and "캡션 &amp; 내용" in text
    assert "⚠️ 확인할 점" in text and "07:30" in text
    numbered = preview_text(draft, "캡션", "11월 4일(수) 07:30", [], "오늘의 3분 뉴스", order="2/2")
    assert "미리보기 (2/2)" in numbered


def test_approve_records_status_and_offset(tmp_path):
    did = make_record(tmp_path)
    client = FakeClient([callback(10, f"a|{did}")])
    decisions = process_updates(client, CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert [(d.draft_id, d.status) for d in decisions] == [(did, "approved")]
    assert drafts.load(tmp_path, did)["status"] == "approved"
    answer = next(p for m, p in client.calls if m == "answerCallbackQuery")
    assert "07:30에 올라가요" in answer["text"]
    # 두 번째 실행은 offset 11부터 읽어 같은 응답을 다시 처리하지 않는다
    assert process_updates(client, CHAT, tmp_path, tmp_path / "tg.json", NOW) == []
    assert client.calls[-1] == ("getUpdates", {"offset": 11})


def test_skip(tmp_path):
    did = make_record(tmp_path)
    process_updates(FakeClient([callback(1, f"s|{did}")]), CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert drafts.load(tmp_path, did)["status"] == "skipped"


def test_other_chat_is_ignored(tmp_path):
    did = make_record(tmp_path)
    decisions = process_updates(FakeClient([callback(1, f"a|{did}", chat=999, user=999)]), CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert decisions[0].status == "ignored"
    assert drafts.load(tmp_path, did)["status"] == "generated"


def test_already_decided_is_not_changed(tmp_path):
    did = make_record(tmp_path, status="skipped")
    decisions = process_updates(FakeClient([callback(1, f"a|{did}")]), CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert decisions[0].status == "ignored"
    assert drafts.load(tmp_path, did)["status"] == "skipped"


def make_numbered(root, number, status="generated"):
    account = load_account("today_news")
    slot = drafts.slot_at(account, "07:30", NOW)
    did = drafts.draft_id("today_news", slot, account.timezone, number)
    record = {"id": did, "slot": slot.isoformat(), "slot_label": drafts.slot_label(slot, account.timezone),
              "status": status, "telegram_message_id": 100 + number}
    drafts.save(root, record)
    return did


def test_approve_all_approves_only_waiting(tmp_path):
    first, second, third = make_numbered(tmp_path, 1), make_numbered(tmp_path, 2, "skipped"), make_numbered(tmp_path, 3)
    client = FakeClient([callback(1, "A|today_news/20261104-0730")])
    decisions = process_updates(client, CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert [(d.draft_id, d.status) for d in decisions] == [(first, "approved"), (third, "approved")]
    assert drafts.load(tmp_path, second)["status"] == "skipped"
    cleared = [p["message_id"] for m, p in client.calls if m == "editMessageReplyMarkup"]
    assert cleared == [101, 103, 50]  # 각 미리보기 버튼 + 모두 승인 버튼
    answer = next(p for m, p in client.calls if m == "answerCallbackQuery")
    assert "2개 모두 승인됨" in answer["text"]


def test_last_single_decision_removes_approve_all_button(tmp_path):
    first, second = make_numbered(tmp_path, 1), make_numbered(tmp_path, 2)
    for did in (first, second):
        record = drafts.load(tmp_path, did)
        record["approve_all_message_id"] = 77
        drafts.save(tmp_path, record)
    client = FakeClient([callback(1, f"a|{first}")])
    process_updates(client, CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert 77 not in [p["message_id"] for m, p in client.calls if m == "editMessageReplyMarkup"]  # 아직 하나 남음
    client = FakeClient([callback(2, f"s|{second}")])
    process_updates(client, CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert 77 in [p["message_id"] for m, p in client.calls if m == "editMessageReplyMarkup"]


def test_approve_all_when_nothing_waiting(tmp_path):
    make_numbered(tmp_path, 1, "approved")
    decisions = process_updates(FakeClient([callback(1, "A|today_news/20261104-0730")]), CHAT, tmp_path, tmp_path / "tg.json", NOW)
    assert decisions[0].status == "ignored"


def test_client_errors_never_contain_token(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(request, timeout):
        raise urllib.error.URLError("fail " + request.full_url)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(TelegramError) as info:
        TelegramClient(FAKE_TOKEN).call("getMe")
    assert FAKE_TOKEN not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
