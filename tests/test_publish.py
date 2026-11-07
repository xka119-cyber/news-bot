import io
import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from newsbot.config import load_account
from newsbot.publish import flow
from newsbot.publish.instagram import InstagramClient, InstagramError, find_posted, publish_carousel, publish_reel
from newsbot.storage import drafts
from newsbot.storage.media_branch import raw_url

NOW = datetime(2026, 11, 4, 12, 40, tzinfo=timezone.utc)  # 한국 21:40
SLOT = datetime(2026, 11, 4, 12, 30, tzinfo=timezone.utc)  # 한국 21:30 (이미 지남)
TOKEN = "EAA" + "x" * 40


class FakeIG:
    """InstagramClient와 같은 메서드를 가진 가짜. 호출 순서를 기록한다(읽기만 하는 status·recent_media는 기록하지 않음)."""

    def __init__(self, quota=100, statuses=None, fail_publish=False, fail_reel=False, recent=None):
        self.calls = []
        self.fail_reel = fail_reel
        self.quota = quota
        self.statuses = statuses or {}
        self.fail_publish = fail_publish
        self.published = False
        self.recent = list(recent or [])  # recent_media()가 돌려줄 최근 글 (최신순)
        self.captions = {}                # 컨테이너 → 캡션
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def quota_left(self):
        return self.quota

    def recent_media(self, limit=5):
        return self.recent[:limit]

    def create_item(self, url, alt_text=None):
        self.calls.append(("item", url, alt_text))
        return f"child{len(self.calls)}"

    def create_carousel(self, children, caption):
        self.calls.append(("carousel", tuple(children), caption))
        self.captions["parent"] = caption
        return "parent"

    def create_reel(self, video_url, caption, cover_url=None, share_to_feed=False):
        self.calls.append(("reel", video_url, cover_url, share_to_feed))
        if self.fail_reel:
            raise InstagramError("영상 형식 오류")
        self.captions["reel1"] = caption
        return "reel1"

    def status(self, cid):
        if cid == "parent" and self.published:
            return "PUBLISHED"
        return self.statuses.get(cid, "FINISHED")

    def wait_ready(self, cid, interval=5, timeout=300):
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


def test_alt_texts_go_to_each_image():
    ig = FakeIG()
    publish_carousel(ig, URLS, "캡션", {}, alt_texts=["표지", "핵심1", "핵심2", "정리"])
    assert [c[2] for c in ig.calls if c[0] == "item"] == ["표지", "핵심1", "핵심2", "정리"]


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


def test_recent_titles_cover_today_but_not_skipped_or_old(tmp_path):
    published = make_record(tmp_path, "published", number=1)
    make_record(tmp_path, "skipped", number=2)
    waiting = make_record(tmp_path, "approved", slot=NOW + timedelta(hours=1), hhmm="2300", number=1)
    make_record(tmp_path, "published", slot=NOW - timedelta(days=2), hhmm="0730", number=1)
    titles = drafts.recent_titles(tmp_path, "today_news", NOW)
    assert titles == ["제목", "제목"]  # 오늘 게시 1건 + 승인 대기 1건
    assert drafts.recent_titles(tmp_path, "today_news", NOW, exclude_ids={published, waiting}) == []


def test_raw_url():
    assert raw_url("o/r", "drafts/a/b.jpg") == "https://raw.githubusercontent.com/o/r/media/drafts/a/b.jpg"


# ---------- publish 흐름 ----------

@pytest.fixture
def account():
    return load_account("today_news")


def make_record(root, status, slot=SLOT, hhmm="2130", number=None, reel=False):
    draft_id = f"today_news/20261104-{hhmm}" + (f"-{number}" if number else "")
    folder = drafts.draft_dir(root, draft_id)
    folder.mkdir(parents=True)
    for i in range(1, 5):
        (folder / f"card_{i}.jpg").write_bytes(b"jpg")
    record = {
        "id": draft_id, "account": "today_news", "slot": slot.isoformat(), "slot_label": "11월 4일(수) 21:30",
        "caption": "캡션", "cards": [f"card_{i}.jpg" for i in range(1, 5)], "telegram_message_id": 7,
        "draft": {"category": "경제", "cover_title": "제목", "cover_sub": "설명", "why_it_matters": "의미",
                  "points": [{"title": "하나", "lines": ["가", "나"]}, {"title": "둘", "lines": ["다"]}],
                  "source_name": "연합뉴스", "source_title": "원문 제목", "source_url": "https://a.kr/1"},
    }
    if reel:
        (folder / "reel.mp4").write_bytes(b"mp4")
        (folder / "reel_cover.jpg").write_bytes(b"jpg")
        record["reel"] = {"status": "pending", "video": "reel.mp4", "cover": "reel_cover.jpg"}
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
    assert ig.calls[0][2].startswith("오늘의 3분 뉴스 카드뉴스 표지")  # 대체 텍스트가 붙음
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


# ---------- 릴스 (M7, 2026-11-07) ----------

def test_publish_reel_creates_waits_and_publishes():
    ig, progress = FakeIG(), {}
    result = publish_reel(ig, "https://x/reel.mp4", "캡션", progress, cover_url="https://x/c.jpg")
    assert ig.calls[0] == ("reel", "https://x/reel.mp4", "https://x/c.jpg", False)
    assert ("publish", "reel1") in ig.calls and progress["container_id"] == "reel1"
    assert result.permalink.endswith("/media1/")


def test_publish_reel_not_twice_when_already_published():
    ig = FakeIG(statuses={"reel1": "PUBLISHED"})
    publish_reel(ig, "https://x/reel.mp4", "캡션", {"container_id": "reel1"})
    assert ig.calls == []


def test_publish_reel_reuses_container_still_processing_and_remakes_expired():
    ig = FakeIG(statuses={"old": "IN_PROGRESS"})
    publish_reel(ig, "u", "c", {"container_id": "old"})
    assert ig.calls == [("publish", "old")]
    ig = FakeIG(statuses={"old": "EXPIRED"})
    progress = {"container_id": "old"}
    publish_reel(ig, "u", "c", progress)
    assert ig.calls[0][0] == "reel" and progress["container_id"] == "reel1"


def test_reel_follows_carousel_with_same_approval(account, tmp_path):
    draft_id = make_record(tmp_path, "approved", reel=True)
    ig, tg, uploads = FakeIG(), FakeTG(), []
    outcomes = run(account, tmp_path, ig, tg, uploads)
    assert [(o.status, o.reel_status) for o in outcomes] == [("published", "published")]
    assert uploads[1] == ("drafts/today_news/2026-11-04/2130", ["reel.mp4", "reel_cover.jpg"])
    reel_call = next(c for c in ig.calls if c[0] == "reel")
    assert reel_call[1].endswith("/reel.mp4") and reel_call[2].endswith("/reel_cover.jpg") and reel_call[3] is False
    record = drafts.load(tmp_path, draft_id)
    assert record["status"] == "published" and record["reel"]["status"] == "published"
    texts = [params["text"] for method, params in tg.sent if method == "sendMessage"]
    assert "📸" in texts[0] and "이어서" in texts[0] and "🎬 릴스도" in texts[1]  # 카드 결과를 먼저 알림
    assert run(account, tmp_path, FakeIG(), FakeTG(), []) == []  # 두 번째 실행은 아무것도 안 함


def test_reel_failure_keeps_carousel_and_can_be_retried(account, tmp_path):
    draft_id = make_record(tmp_path, "approved", reel=True)
    tg = FakeTG()
    outcome = run(account, tmp_path, FakeIG(fail_reel=True), tg, [])[0]
    assert (outcome.status, outcome.reel_status) == ("published", "failed")
    record = drafts.load(tmp_path, draft_id)
    assert record["status"] == "published" and record["reel"]["status"] == "failed"
    assert "릴스는 실패" in tg.sent[-1][1]["text"]
    # 자동 실행은 실패한 릴스를 계속 다시 시도하지 않는다
    ig = FakeIG()
    assert run(account, tmp_path, ig, FakeTG(), []) == []
    # 직접 실행(publish --draft)은 카드는 건너뛰고 릴스만 올린다
    ig = FakeIG()
    outcome = run(account, tmp_path, ig, FakeTG(), [], only_id=draft_id)[0]
    assert outcome.reel_status == "published"
    assert [c[0] for c in ig.calls] == ["reel", "publish"]


def test_old_draft_without_reel_posts_carousel_only(account, tmp_path):
    make_record(tmp_path, "approved")
    ig = FakeIG()
    outcome = run(account, tmp_path, ig, FakeTG(), [])[0]
    assert outcome.reel_status == "" and not any(c[0] == "reel" for c in ig.calls)


def test_any_reel_error_is_recorded_not_crashing(account, tmp_path):
    """영상 쪽(Chromium·ffmpeg) 오류가 어떤 종류든 카드는 올라간 상태로 남고 실패가 기록·보고된다 (QA 11-07)."""
    draft_id = make_record(tmp_path, "approved", reel=True)

    class BrowserError(Exception):
        pass

    def upload(files, dest, repo, message):
        if any(f.name == "reel.mp4" for f in files):
            raise BrowserError("chromium crashed")
        return [raw_url(repo, f"{dest}/{f.name}") for f in files]

    tg = FakeTG()
    outcome = flow.run(account, tmp_path, FakeIG(), tg, 1, upload, tmp_path / "history.json", NOW)[0]
    assert (outcome.status, outcome.reel_status) == ("published", "failed")
    assert "BrowserError" in drafts.load(tmp_path, draft_id)["reel"]["note"]
    assert "릴스는 실패" in tg.sent[-1][1]["text"]


def test_publish_reel_remakes_container_it_cannot_read():
    class Gone(FakeIG):
        def status(self, cid):
            if cid == "gone":
                raise InstagramError("없는 컨테이너")
            return super().status(cid)

    ig, progress = Gone(), {"container_id": "gone"}
    publish_reel(ig, "u", "c", progress)
    assert ig.calls[0][0] == "reel" and progress["container_id"] == "reel1"


# ---------- 인스타가 '오류'라고 답했지만 실제로는 올라간 경우 (2026-11-07 21:30) ----------

class LyingIG(FakeIG):
    """게시 요청에 403 '활동 제한'이라 답하지만 실제로는 글을 올리고, 그 뒤 컨테이너 상태를 ERROR라고 하는 인스타."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lied = set()

    def publish(self, cid):
        self.calls.append(("publish", cid))
        self.lied.add(cid)
        product = "REELS" if cid == "reel1" else "FEED"
        self.recent.insert(0, {"id": f"real-{cid}", "caption": self.captions.get(cid, ""), "media_product_type": product})
        raise InstagramError("인스타 media_publish 실패 (HTTP 403, 코드 4-2207051) 활동을 제한합니다")

    def status(self, cid):
        return "ERROR" if cid in self.lied else super().status(cid)


def test_false_error_response_is_detected_from_recent_posts():
    ig = LyingIG()
    result = publish_carousel(ig, URLS, "캡션", {})
    assert result.media_id == "real-parent" and result.recovered is True
    kinds = [c[0] for c in ig.calls]
    assert kinds.count("carousel") == 1 and kinds.count("publish") == 1  # 두 번 올리지 않음
    assert ig.sleeps == [3]


def test_retry_after_false_error_finds_post_instead_of_posting_again():
    """실패로 기록된 초안을 '다시 올려줘'로 돌릴 때: 컨테이너는 ERROR지만 최근 글에 같은 캡션이 있으면 새로 만들지 않는다."""
    ig = FakeIG(statuses={"parent": "ERROR"}, recent=[{"id": "real1", "caption": "캡션", "media_product_type": "FEED"}])
    result = publish_carousel(ig, URLS, "캡션", {"container_id": "parent"})
    assert result.media_id == "real1" and result.recovered is True
    assert ig.calls == []


def test_retry_with_error_container_and_no_post_makes_a_new_one():
    ig = FakeIG(statuses={"parent": "ERROR"}, recent=[{"id": "other", "caption": "다른 글", "media_product_type": "FEED"}])
    result = publish_carousel(ig, URLS, "캡션", {"container_id": "parent"})
    assert [c[0] for c in ig.calls] == ["item"] * 4 + ["carousel", "publish"] and result.recovered is False


def test_recent_post_match_ignores_whitespace_but_not_type():
    ig = FakeIG(recent=[{"id": "r", "caption": "첫 줄\r\n\r\n#태그", "media_product_type": "REELS"},
                        {"id": "f", "caption": "첫 줄\n\n#태그 ", "media_product_type": "FEED"}])
    assert find_posted(ig, "첫 줄\n#태그", "FEED") == "f"
    assert find_posted(ig, "첫 줄\n#태그", "REELS") == "r"
    assert find_posted(ig, "없는 글", "FEED") is None


def test_normal_publish_never_looks_at_recent_posts():
    class NoPeeking(FakeIG):
        def recent_media(self, limit=5):
            raise AssertionError("정상 게시에는 최근 글을 보지 않는다 (추가 요청 0번)")

    result = publish_carousel(NoPeeking(), URLS, "캡션", {})
    assert result.media_id == "media1" and result.recovered is False
    assert publish_reel(NoPeeking(), "u", "캡션", {}).recovered is False


def test_reel_false_error_is_detected_too():
    result = publish_reel(LyingIG(), "u", "캡션", {})
    assert result.media_id == "real-reel1" and result.recovered is True
    ig = FakeIG(statuses={"reel1": "ERROR"}, recent=[{"id": "rr", "caption": "캡션", "media_product_type": "REELS"}])
    assert publish_reel(ig, "u", "캡션", {"container_id": "reel1"}).media_id == "rr"
    assert ig.calls == []


def test_lookup_failure_after_error_reports_the_original_error():
    class Broken(LyingIG):
        def recent_media(self, limit=5):
            raise InstagramError("인스타 media 실패 (HTTP 500)")

    with pytest.raises(InstagramError, match="media_publish"):
        publish_carousel(Broken(), URLS, "캡션", {})


def test_flow_false_error_marks_published_and_goes_on_to_reel(account, tmp_path):
    draft_id = make_record(tmp_path, "approved", reel=True)
    ig, tg = LyingIG(), FakeTG()
    outcome = run(account, tmp_path, ig, tg, [])[0]
    assert (outcome.status, outcome.reel_status) == ("published", "published")
    record = drafts.load(tmp_path, draft_id)
    assert record["status"] == "published" and record["media_id"] == "real-parent"
    assert record["reel"]["status"] == "published" and record["reel"]["media_id"] == "real-reel1"
    texts = [params["text"] for method, params in tg.sent if method == "sendMessage"]
    assert "📸 올라가 있었어요" in texts[0] and "🎬 릴스도 올라가 있었어요" in texts[1]
    assert run(account, tmp_path, FakeIG(), FakeTG(), []) == []  # 두 번째 실행은 아무것도 안 함


def test_failure_with_code_4_gets_a_hint(account, tmp_path):
    class Blocked(FakeIG):
        def publish(self, cid):
            raise InstagramError("인스타 media_publish 실패 (HTTP 403, 코드 4-2207051) 활동을 제한합니다")

    make_record(tmp_path, "approved")
    tg = FakeTG()
    assert run(account, tmp_path, Blocked(), tg, [])[0].status == "failed"
    assert "다시 올려줘" in tg.sent[-1][1]["text"]


def test_client_error_message_includes_subcode(monkeypatch):
    client = InstagramClient(TOKEN, "123", "https://graph.facebook.com", "v26.0", sleep=lambda s: None)
    body = json.dumps({"error": {"message": "limit", "error_user_msg": "활동을 제한합니다",
                                 "code": 4, "error_subcode": 2207051}}).encode()

    def boom(*args, **kwargs):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(InstagramError, match="코드 4-2207051"):
        client.quota_left()


def test_retry_lookback_is_wider_than_the_quick_check():
    """재시도는 최근 25개를 본다(5개면 반나절 뒤 재시도에서 같은 글이 밀려나 두 번 올라감 — QA 11-07)."""
    others = [{"id": f"o{i}", "caption": f"다른 글 {i}", "media_product_type": "FEED"} for i in range(7)]
    mine = {"id": "mine", "caption": "캡션", "media_product_type": "FEED"}
    ig = FakeIG(statuses={"parent": "ERROR"}, recent=others + [mine])
    assert find_posted(ig, "캡션", "FEED") is None  # 오류 직후의 빠른 확인은 5개만 본다
    assert publish_carousel(ig, URLS, "캡션", {"container_id": "parent"}).media_id == "mine"
    assert ig.calls == []


def test_published_container_without_caption_match_takes_newest_of_same_kind():
    ig = FakeIG(statuses={"parent": "PUBLISHED"},
                recent=[{"id": "someone-reel", "caption": "x", "media_product_type": "REELS"},
                        {"id": "feed-new", "caption": "y", "media_product_type": "FEED"}])
    assert publish_carousel(ig, URLS, "캡션", {"container_id": "parent"}).media_id == "feed-new"
    assert publish_reel(ig, "u", "캡션", {"container_id": "parent"}).media_id == "someone-reel"


def test_container_published_while_waiting_is_not_published_again():
    ig = FakeIG(statuses={"parent": "PUBLISHED"}, recent=[{"id": "f1", "caption": "캡션", "media_product_type": "FEED"}])
    result = publish_carousel(ig, URLS, "캡션", {})
    assert result.media_id == "f1" and not any(c[0] == "publish" for c in ig.calls)


def test_reel_error_container_without_post_is_remade():
    ig = FakeIG(statuses={"reel1": "ERROR"})
    publish_reel(ig, "u", "캡션", {"container_id": "reel1"})
    assert ig.calls[0][0] == "reel" and ("publish", "reel1") in ig.calls


def test_hint_only_for_code_4_not_other_4xx(account, tmp_path):
    class Expired(FakeIG):
        def publish(self, cid):
            raise InstagramError("인스타 media_publish 실패 (HTTP 400, 코드 463) 토큰 만료")

    make_record(tmp_path, "approved")
    tg = FakeTG()
    assert run(account, tmp_path, Expired(), tg, [])[0].status == "failed"
    assert "다시 올려줘" not in tg.sent[-1][1]["text"]


def test_client_wraps_dropped_connections_and_retries(monkeypatch):
    sleeps = []
    client = InstagramClient(TOKEN, "123", "https://graph.facebook.com", "v26.0", sleep=sleeps.append)

    def drop(*args, **kwargs):
        raise ConnectionResetError("reset")

    monkeypatch.setattr("urllib.request.urlopen", drop)
    with pytest.raises(InstagramError, match=r"연결 실패 \(ConnectionResetError\)"):
        client.quota_left()
    assert sleeps == [1, 2, 4]
