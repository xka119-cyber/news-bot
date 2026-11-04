import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from newsbot.config import load_account
from newsbot.publish import flow
from newsbot.publish.instagram import InstagramClient, InstagramError, publish_carousel
from newsbot.storage import drafts
from newsbot.storage.media_branch import raw_url

NOW = datetime(2026, 11, 4, 12, 40, tzinfo=timezone.utc)  # 한국 21:40
SLOT = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)  # 한국 21:30 (이미 지남)
TOKEN = "EAA" + "x" * 40


class FakeIG:
    """InstagramClient와 같은 메서드를 가진 가짜. 호출 순서를 기록한다."""

    def __init__(self, quota=100, statuses=None, fail_publish=False):
        self.calls = []
        self.quota = quota
        self.statuses = statuses or {}
        self.fail_publish = fail_publish
        self.published = False

    def quota_left(self):
        return self.quota

    def create_item(self, url):
        self.calls.append(("item", url))
        return f"child{len(self.calls)}"

    def create_carousel(self, children, caption):
        self.calls.append(("carousel", tuple(children), caption))
        return "parent"

    def status(self, cid):
        if cid == "parent" and self.published:
            return "PUBLISHED"
        return self.statuses.get(cid, "FINISHED")

    def wait_ready(self, cid):
        return self.status(cid)

    def publish(self, cid):
        self.calls.append(("publish", cid))
        self.published = True
        if self.fail_publish:
            raise InstagramError("연결 끊김")
        return "media1"

    def latest_media_id(self):
        return "media1"

    def permalink(self, media_id):
        return f"https://www.instagram.com/p/{media_id}/"


class FakeTG:
    def __init__(self):
        self.sent = []

    def call(self, method, **params):
        self.sent.append((method, params))
        return {}


URLS = [f"https://example.com/c{i}.jpg" for i in range(4)]


def test_carousel_order():
    ig = FakeIG()
    result = publish_carousel(ig, URLS, "캡션", {})
    assert [c[0] for c in ig.calls] == ["item"] * 4 + ["carousel", "publish"]
    assert ig.calls[4][2] == "캡션"
    assert result.permalink == "https://www.instagram.com/p/media1/"


def test_no_double_post_when_container_already_published():
    ig = FakeIG()
    ig.published = True
    result = publish_carousel(ig, URLS, "캡션", {"container_id": "parent"})
    assert ig.calls == []  # 새로 만들거나 게시하지 않음
    assert result.media_id == "media1"


def test_publish_error_but_actually_published_is_success():
    ig = FakeIG(fail_publish=True)
    progress = {}
    saved = []
    result = publish_carousel(ig, URLS, "캡션", progress, save=lambda: saved.append(dict(progress)))
    assert result.media_id == "media1"
    assert saved == [{"container_id": "parent"}]


def test_quota_exhausted():
    with pytest.raises(InstagramError, match="한도"):
        publish_carousel(FakeIG(quota=0), URLS, "캡션", {})


def test_carousel_needs_two_images():
    with pytest.raises(InstagramError):
        publish_carousel(FakeIG(), URLS[:1], "캡션", {})


def test_client_error_message_hides_token(monkeypatch):
    client = InstagramClient(TOKEN, "123", "https://graph.facebook.com", "v26.0", sleep=lambda s: None)
    body = json.dumps({"error": {"message": f"bad token {TOKEN}", "code": 190}}).encode()

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(InstagramError) as info:
        client.quota_left()
    assert TOKEN not in str(info.value) and "190" in str(info.value)


def test_client_retries_server_errors(monkeypatch):
    sleeps = []
    client = InstagramClient(TOKEN, "123", "https://graph.facebook.com", "v26.0", sleep=sleeps.append)

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(InstagramError):
        client.quota_left()
    assert sleeps == [1, 2, 4]


def test_numbered_draft_ids_and_folders(tmp_path):
    slot = datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc)  # 한국 11월 5일 07:30
    assert drafts.draft_id("today_news", slot, "Asia/Seoul", 2) == "today_news/20261105-0730-2"
    assert drafts.draft_id("today_news", slot, "Asia/Seoul") == "today_news/20261105-0730"
    assert drafts.draft_dir(tmp_path, "today_news/20261105-0730-2") == tmp_path / "today_news" / "2026-11-05" / "0730" / "2"
    assert drafts.draft_dir(tmp_path, "today_news/20261105-0730") == tmp_path / "today_news" / "2026-11-05" / "0730"


def test_raw_url():
    assert raw_url("o/r", "drafts/a/b.jpg") == "https://raw.githubusercontent.com/o/r/media/drafts/a/b.jpg"


# ---------- publish 흐름 ----------

@pytest.fixture
def account():
    return load_account("today_news")


def make_record(root, status, slot=SLOT, hhmm="2130", number=None):
    draft_id = f"today_news/20261104-{hhmm}" + (f"-{number}" if number else "")
    folder = drafts.draft_dir(root, draft_id)
    folder.mkdir(parents=True)
    for i in range(1, 5):
        (folder / f"card_{i}.jpg").write_bytes(b"jpg")
    record = {
        "id": draft_id, "account": "today_news", "slot": slot.isoformat(), "slot_label": "11월 4일(수) 21:30",
        "caption": "캡션", "cards": [f"card_{i}.jpg" for i in range(1, 5)], "telegram_message_id": 7,
        "draft": {"cover_title": "제목", "source_name": "연합뉴스", "source_title": "원문 제목", "source_url": "https://a.kr/1"},
    }
    drafts.set_status(root, record, status, NOW - timedelta(hours=1))
    return draft_id


def fake_upload(calls):
    def upload(files, dest, repo, message):
        calls.append((dest, [f.name for f in files]))
        return [raw_url(repo, f"{dest}/{f.name}") for f in files]
    return upload


def run(account, root, ig, tg, uploads, only_id=None, now=NOW):
    return flow.run(account, root, ig, tg, 1, fake_upload(uploads), root / "history.json", now, only_id)


def test_approved_and_due_is_published(account, tmp_path):
    draft_id = make_record(tmp_path, "approved")
    ig, tg, uploads = FakeIG(), FakeTG(), []
    outcomes = run(account, tmp_path, ig, tg, uploads)
    assert [o.status for o in outcomes] == ["published"]
    assert uploads == [("drafts/today_news/2026-11-04/2130", ["card_1.jpg", "card_2.jpg", "card_3.jpg", "card_4.jpg"])]
    record = drafts.load(tmp_path, draft_id)
    assert record["status"] == "published" and record["permalink"].endswith("/media1/")
    assert "📸" in tg.sent[-1][1]["text"]
    assert json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))["items"][0]["link"] == "https://a.kr/1"


def test_two_posts_same_slot_each_by_own_approval(account, tmp_path):
    first = make_record(tmp_path, "approved", number=1)
    second = make_record(tmp_path, "skipped", number=2)
    third = make_record(tmp_path, "approved", hhmm="2130", number=3)
    uploads = []
    outcomes = run(account, tmp_path, FakeIG(), FakeTG(), uploads)
    assert [(o.draft_id, o.status) for o in outcomes] == [(first, "published"), (third, "published")]
    assert drafts.load(tmp_path, second)["status"] == "skipped"
    assert uploads[0][0] == "drafts/today_news/2026-11-04/2130/1"


def test_second_run_does_nothing(account, tmp_path):
    make_record(tmp_path, "approved")
    run(account, tmp_path, FakeIG(), FakeTG(), [])
    ig = FakeIG()
    assert run(account, tmp_path, ig, FakeTG(), []) == []
    assert ig.calls == []


def test_skipped_is_never_published(account, tmp_path):
    make_record(tmp_path, "skipped")
    ig = FakeIG()
    assert run(account, tmp_path, ig, FakeTG(), []) == []
    assert ig.calls == []


def test_unapproved_past_slot_expires(account, tmp_path):
    draft_id = make_record(tmp_path, "generated")
    ig, tg = FakeIG(), FakeTG()
    outcomes = run(account, tmp_path, ig, tg, [])
    assert [o.status for o in outcomes] == ["expired"]
    assert drafts.load(tmp_path, draft_id)["status"] == "expired"
    assert ig.calls == []
    assert tg.sent[0][0] == "editMessageReplyMarkup"


def test_expiry_also_removes_approve_all_button(account, tmp_path):
    draft_id = make_record(tmp_path, "generated", number=1)
    record = drafts.load(tmp_path, draft_id)
    record["approve_all_message_id"] = 99
    drafts.save(tmp_path, record)
    tg = FakeTG()
    run(account, tmp_path, FakeIG(), tg, [])
    cleared = [p["message_id"] for m, p in tg.sent if m == "editMessageReplyMarkup"]
    assert 99 in cleared and 7 in cleared


def test_approved_but_not_due_waits(account, tmp_path):
    make_record(tmp_path, "approved", slot=NOW + timedelta(hours=2))
    ig = FakeIG()
    assert run(account, tmp_path, ig, FakeTG(), []) == []


def test_manual_draft_option_ignores_time_but_needs_approval(account, tmp_path):
    approved = make_record(tmp_path, "approved", slot=NOW + timedelta(hours=2), hhmm="2300")
    waiting = make_record(tmp_path, "generated", slot=NOW + timedelta(hours=3), hhmm="2330")
    assert [o.status for o in run(account, tmp_path, FakeIG(), FakeTG(), [], only_id=approved)] == ["published"]
    outcome = run(account, tmp_path, FakeIG(), FakeTG(), [], only_id=waiting)
    assert outcome[0].status == "generated" and "승인" in outcome[0].note


def test_failure_is_recorded_and_reported(account, tmp_path):
    draft_id = make_record(tmp_path, "approved")
    tg = FakeTG()
    outcomes = run(account, tmp_path, FakeIG(quota=0), tg, [])
    assert outcomes[0].status == "failed"
    assert drafts.load(tmp_path, draft_id)["status"] == "failed"
    assert "❌" in tg.sent[-1][1]["text"]
