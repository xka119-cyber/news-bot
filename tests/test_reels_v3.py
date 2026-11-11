"""릴스 v3 (2026-11-11): 목소리 + 실사 영상 + 자막 합성, 영상 고르기, 안 될 때 v2로 대신 만들기.

진짜 목소리 서비스·영상 창고는 부르지 않는다: ffmpeg로 만든 가짜 소리·가짜 영상으로 바꿔 끼운다."""

import shutil
import subprocess

import pytest
from PIL import Image

from newsbot.config import load_account
from newsbot.design import reels, reels_v3
from test_cards import make_draft

SCENES = [
    {"say": "우리 집 전기요금, 1월부터 올라요.", "caption": ["우리 집 전기요금", "1월부터 오른다"], "search": "electricity meter"},
    {"say": "가정용 전기요금이 인상돼요.", "caption": ["가정용 요금 인상"], "search": "apartment lights night"},
    {"say": "kWh당 5원씩 더 내게 돼요.", "caption": ["kWh당 5원 더"], "search": "paying bills"},
    {"say": "겨울엔 대기전력부터 줄여 보세요.", "caption": ["대기전력부터", "줄여 보세요"], "search": "unplugging power cord"},
]
REEL = {"hook": ["우리 집 전기요금", "1월부터 오른다"], "beats": ["가정용 요금 인상", "kWh당 5원 더 내요", "다음 달 확정"],
        "takeaway": "대기전력부터 줄이기", "scenes": SCENES}


def ffmpeg(*args):
    subprocess.run([reels.ffmpeg_exe(), "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """가짜 재료: 세로 영상 6초 1개, 1.5초짜리 소리 1개."""
    folder = tmp_path_factory.mktemp("v3media")
    clip, voice = folder / "clip.mp4", folder / "voice.mp3"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=6", "-pix_fmt", "yuv420p", str(clip))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", str(voice))
    return clip, voice


@pytest.fixture
def account():
    return load_account("today_news")


@pytest.fixture
def fakes(monkeypatch, media):
    """목소리·검색·내려받기를 가짜로. 부른 기록을 돌려준다."""
    clip, voice = media
    calls = {"speak": [], "search": [], "download": []}

    def speak(text, voice_name, rate, out):
        calls["speak"].append((text, voice_name, rate))
        shutil.copy(voice, out)
        return out

    def search(query, key):
        calls["search"].append((query, key))
        return [{"id": 100 + n + 10 * len(calls["search"]), "duration": 6, "tags": query, "pageURL": f"https://pixabay.com/videos/id-{100 + n}/",
                 "videos": {"medium": {"url": f"https://cdn.pixabay.com/video/{100 + n}_medium.mp4", "width": 720, "height": 1280}}}
                for n in range(6)]

    def download(url, dest):
        calls["download"].append(url)
        shutil.copy(clip, dest)
        return dest

    monkeypatch.setattr(reels_v3, "speak", speak)
    monkeypatch.setattr(reels_v3, "search_clips", search)
    monkeypatch.setattr(reels_v3, "download", download)
    monkeypatch.setattr(reels_v3, "pick_by_eye", lambda *args, **kwargs: None)   # 진짜 Claude를 부르지 않는다
    return calls


def test_account_uses_v3_with_fallback_settings(account):
    assert account.reels.style == "v3" and account.reels.clip_key_env == "PIXABAY_API_KEY"
    assert account.reels.voice.startswith("ko-KR-")


def test_render_v3_makes_instagram_ready_mp4(account, fakes, monkeypatch, tmp_path):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    result = reels.render_reel({**make_draft(), "reel": REEL}, account, [], tmp_path)
    assert result.style == "v3" and result.warnings == []
    assert 7.0 < result.seconds < 7.6                       # 소리 1.5초 x 4 + 팔로우 권유 1.2초
    assert [text for text, _, _ in fakes["speak"]] == [s["say"] for s in SCENES]
    assert [q for q, k in fakes["search"]][::2] == [s["search"] for s in SCENES]      # 장면마다 검색어 + 넓은 검색어
    wide = set(reels_v3.FALLBACK_SEARCH.values()) | {reels_v3.DEFAULT_FALLBACK}
    assert all(q in wide for q, k in fakes["search"][1::2]) and all(k == "test-key" for q, k in fakes["search"])
    assert len({c["id"] for c in result.clips}) == 4, "장면마다 다른 영상"
    assert all(c["url"].startswith("https://cdn.pixabay.com/") and "start" in c for c in result.clips)
    info = subprocess.run([reels.ffmpeg_exe(), "-i", str(result.video)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    assert "h264" in info and "yuv420p" in info and "1080x1920" in info and "30 fps" in info
    assert "aac" in info and "48000 Hz" in info and "stereo" in info
    assert abs(reels_v3.media_seconds(result.video) - result.seconds) < 0.2
    with Image.open(result.cover) as im:
        assert im.size == (1080, 1920) and im.format == "JPEG"
    assert not (tmp_path / "_reel_v3").exists(), "작업 폴더는 지운다"


def test_saved_clips_are_reused_without_key_or_search(account, fakes, monkeypatch, tmp_path):
    """게시 때 다시 만들 때: 기록해 둔 영상을 그대로 쓰므로 열쇠도 검색도 필요 없다(미리보기와 같은 영상)."""
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    saved = [{"id": n, "url": f"https://cdn.pixabay.com/video/saved{n}.mp4", "page": "", "start": 1.0} for n in range(4)]
    result = reels.render_reel({**make_draft(), "reel": REEL}, account, [], tmp_path, clips=saved)
    assert result.style == "v3" and result.clips == saved
    assert fakes["search"] == [] and fakes["download"] == [c["url"] for c in saved]


def test_falls_back_to_v2_text_reel_and_says_why(account, fakes, monkeypatch, tmp_path):
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    result = reels.render_reel({**make_draft(), "reel": REEL}, account, [], tmp_path)
    assert result.style == "v2" and result.clips == [] and result.seconds == 12.0
    assert result.warnings[0].startswith("목소리+실사 릴스를 못 만들어 글자 릴스로 대신했어요") and "PIXABAY_API_KEY" in result.warnings[0]
    assert result.video.exists() and result.cover.exists()


def test_v3_needs_scenes_and_cleans_up_on_failure(account, fakes, monkeypatch, tmp_path):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    with pytest.raises(reels_v3.V3Unavailable, match="장면"):
        reels_v3.render({**make_draft(), "reel": {**REEL, "scenes": []}}, account, tmp_path)
    monkeypatch.setattr(reels_v3, "search_clips", lambda query, key: [])
    with pytest.raises(reels_v3.V3Unavailable, match="쓸 영상을 찾지 못했어요"):
        reels_v3.render({**make_draft(), "reel": REEL}, account, tmp_path)
    assert not (tmp_path / "_reel_v3").exists()


def test_choose_clip_prefers_portrait_and_skips_used_or_short():
    def hit(i, w, h, duration=10):
        return {"id": i, "duration": duration, "videos": {"medium": {"url": f"https://cdn.pixabay.com/{i}.mp4", "width": w, "height": h}}}

    hits = [hit(1, 1280, 720), hit(2, 720, 1280, duration=2), hit(3, 720, 1280), hit(4, 720, 1280)]
    assert reels_v3.choose_clip(hits, 3.0, set())["id"] == 3          # 세로 먼저, 짧은 2번은 제외
    assert reels_v3.choose_clip(hits, 3.0, {3})["id"] == 4            # 이미 쓴 것 제외
    assert reels_v3.choose_clip(hits, 3.0, {3, 4})["id"] == 1         # 세로가 없으면 가로(가운데를 잘라 씀)
    chosen = reels_v3.choose_clip(hits, 3.0, set())
    assert 0 <= chosen["start"] <= 10 - 3.0 - 0.3
    with pytest.raises(reels_v3.V3Unavailable):
        reels_v3.choose_clip([hit(9, 320, 180)], 3.0, set())         # 너무 작은 영상뿐


def test_download_only_from_clip_store(tmp_path):
    for url in ("http://cdn.pixabay.com/a.mp4", "https://evil.example/a.mp4", "file:///etc/passwd"):
        with pytest.raises(reels_v3.V3Unavailable, match="영상 창고"):
            reels_v3.download(url, tmp_path / "x.mp4")


def test_search_error_never_shows_the_key(monkeypatch):
    import urllib.error

    def boom(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, None)

    monkeypatch.setattr(reels_v3.urllib.request, "urlopen", boom)
    with pytest.raises(reels_v3.V3Unavailable) as caught:
        reels_v3.search_clips("chocolate bar", "SECRET-KEY-123")
    assert "429" in str(caught.value) and "SECRET-KEY-123" not in str(caught.value)
    assert caught.value.__cause__ is None, "원래 오류(주소에 열쇠가 들어 있음)를 달고 다니지 않는다"


def test_caption_image_fits_long_lines_and_highlights_numbers(account):
    image = reels_v3.caption_image(["kWh당 5원 더"], account, "연합뉴스")
    assert image.size == (1080, 1920) and image.mode == "RGBA"
    box = image.crop((0, 800, 1080, 1200)).getbbox()
    assert box and box[0] > 60 and box[2] < 1020, "자막이 화면 폭 안에"
    colors = {image.getpixel((x, y))[:3] for x in range(90, 990, 3) for y in range(900, 1060, 3) if image.getpixel((x, y))[3] == 255}
    assert (255, 200, 61) in colors and (255, 255, 255) in colors   # 숫자는 노랑, 나머지는 흰색
    long = reels_v3.caption_image(["아주아주아주아주아주아주 긴 자막 한 줄입니다 정말로"], account, "")
    box = long.crop((0, 800, 1080, 1200)).getbbox()
    assert box[0] >= 0 and box[2] <= 1080


def test_style_override_keeps_the_approved_version(account, fakes, monkeypatch, tmp_path):
    """게시 때: 설정이 v3여도 style="v2"를 넘기면 v3를 시도하지 않는다."""
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    result = reels.render_reel({**make_draft(), "reel": REEL}, account, [], tmp_path, style="v2")
    assert result.style == "v2" and result.warnings == [] and fakes["speak"] == [] and fakes["search"] == []


def test_failed_v3_leaves_no_half_made_video(account, fakes, monkeypatch, tmp_path):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-key")
    (tmp_path / "reel.mp4").write_bytes(b"old")
    (tmp_path / "reel_cover.jpg").write_bytes(b"old")

    def broken(*args, **kwargs):
        raise OSError("장면 만들기(ffmpeg) 실패")

    monkeypatch.setattr(reels_v3, "make_segment", broken)
    with pytest.raises(OSError):
        reels_v3.render({**make_draft(), "reel": REEL}, account, tmp_path)
    assert not (tmp_path / "reel.mp4").exists() and not (tmp_path / "reel_cover.jpg").exists()
    assert not (tmp_path / "_reel_v3").exists()


def test_choose_clip_prefers_on_topic_tags_and_lightest_usable_file():
    """2026-11-12 실제 검색에서 본 모양: 'fuel pump' 2~4번째가 엉뚱한 영상, 같은 이름의 파일도 영상마다 크기가 다름."""
    def hit(i, tags, videos):
        return {"id": i, "duration": 10, "tags": tags, "videos": videos}

    big = {"large": {"url": "https://cdn.pixabay.com/l.mp4", "width": 3840, "height": 2160},
           "medium": {"url": "https://cdn.pixabay.com/m.mp4", "width": 2560, "height": 1440},
           "small": {"url": "https://cdn.pixabay.com/s.mp4", "width": 1920, "height": 1080},
           "tiny": {"url": "https://cdn.pixabay.com/t.mp4", "width": 1280, "height": 720}}
    assert reels_v3.pick_file({"videos": big})["url"].endswith("/t.mp4")      # 짧은 변 720 이상 중 가장 가벼운 것
    low = {"small": {"url": "https://cdn.pixabay.com/s.mp4", "width": 960, "height": 540}}
    assert reels_v3.pick_file({"videos": low}) is None
    hits = [hit(1, "construction, crane, truck", big), hit(2, "dragonfly, insect", big), hit(3, "road, fuel station, petrol pump", big)]
    assert reels_v3.choose_clip(hits, 3.0, set(), "fuel pump")["id"] == 3     # 태그에 검색어 낱말이 있는 영상 먼저
    assert reels_v3.choose_clip(hits, 3.0, {3}, "fuel pump")["id"] == 1       # 없으면 검색 순서대로


# ---------- 눈으로 고르기 (2026-11-12) ----------

def _ranked(prefix, count, match="full"):
    return [{"id": f"{prefix}{n}", "url": f"https://cdn.pixabay.com/{prefix}{n}.mp4", "page": "", "start": 0.0,
             "match": match, "thumb": f"https://cdn.pixabay.com/{prefix}{n}.jpg"} for n in range(count)]


def test_find_clip_uses_the_clip_claude_picked_and_never_saves_thumbs(account, monkeypatch):
    monkeypatch.setattr(reels_v3, "search_clips", lambda query, key: [])
    monkeypatch.setattr(reels_v3, "rank_clips", lambda hits, need, used, query: _ranked("q" if query == "fuel pump" else "w", 8))
    shown = {}

    def pick(say, caption, candidates, acct):
        shown.update(say=say, ids=[c["id"] for c in candidates])
        return 7                                             # 넓은 검색어 후보 중 두 번째

    monkeypatch.setattr(reels_v3, "pick_by_eye", pick)
    clip, doubt = reels_v3.find_clip("fuel pump", "경제", "k", 3.0, set(), say="기름값이 내려요.", caption=["기름값"], account=account)
    assert shown["ids"] == [f"q{n}" for n in range(6)] + ["w0", "w1", "w2"] and shown["say"] == "기름값이 내려요."
    assert clip["id"] == "w1" and doubt is None and "thumb" not in clip


def test_find_clip_falls_back_to_rules_and_warns(account, monkeypatch):
    monkeypatch.setattr(reels_v3, "search_clips", lambda query, key: [])
    monkeypatch.setattr(reels_v3, "rank_clips",
                        lambda hits, need, used, query: _ranked("q", 3, "partial") if query == "calendar page" else _ranked("w", 3))
    monkeypatch.setattr(reels_v3, "pick_by_eye", lambda *a, **k: None)             # 물어보지 못함 → 규칙
    clip, doubt = reels_v3.find_clip("calendar page", "경제", "k", 3.0, set(), account=account)
    assert clip["id"] == "w0" and "money coins" in doubt
    monkeypatch.setattr(reels_v3, "pick_by_eye", lambda *a, **k: reels_v3.NONE_FIT)  # 어울리는 게 없다고 함 → 규칙 + 경고
    clip, doubt = reels_v3.find_clip("calendar page", "경제", "k", 3.0, set(), account=account)
    assert clip["id"] == "w0" and "어울리는 영상을 찾지 못했어요" in doubt


class _FakeClaude:
    def __init__(self, choice):
        self.choice, self.calls, self.messages = choice, [], self

    def parse(self, **kwargs):
        from types import SimpleNamespace

        self.calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=4000, output_tokens=200), stop_reason="end_turn",
                               parsed_output=reels_v3.ClipPick(choice=self.choice))


def test_pick_by_eye_sends_thumbnails_and_records_cost(account, monkeypatch, tmp_path):
    costs = tmp_path / "costs.json"
    monkeypatch.setattr(reels_v3, "_costs_path", lambda: costs)
    # 두 번째 후보는 썸네일을 못 받음 → 보여 주는 건 0·2번, Claude의 '2번'은 원래 순번 2
    monkeypatch.setattr(reels_v3, "fetch_thumb", lambda url: None if url.endswith("q1.jpg") else b"\xff\xd8jpeg")
    claude = _FakeClaude(choice=2)
    picked = reels_v3.pick_by_eye("기름값이 내려요.", ["기름값"], _ranked("q", 3), account, client=claude)
    assert picked == 2
    content = claude.calls[0]["messages"][0]["content"]
    assert sum(1 for block in content if block["type"] == "image") == 2 and "기름값이 내려요." in content[0]["text"]
    assert claude.calls[0]["model"] == account.write.model and claude.calls[0]["output_format"] is reels_v3.ClipPick
    import json as _json

    spent = _json.loads(costs.read_text(encoding="utf-8"))["today_news"]
    assert next(iter(spent.values()))["calls"] == 1, "비용은 글 쓰기와 같은 장부에 적는다"
    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), account, client=_FakeClaude(0)) == reels_v3.NONE_FIT
    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), account, client=_FakeClaude(9)) is None      # 없는 번호


def test_pick_by_eye_is_skipped_when_off_no_key_or_over_budget(account, monkeypatch, tmp_path):
    monkeypatch.setattr(reels_v3, "_costs_path", lambda: tmp_path / "costs.json")
    monkeypatch.setattr(reels_v3, "fetch_thumb", lambda url: b"\xff\xd8jpeg")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), account) is None                              # 열쇠 없음
    off = account.model_copy(update={"reels": account.reels.model_copy(update={"clip_judge": False})})
    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), off, client=_FakeClaude(1)) is None           # 꺼 둠
    broke = account.model_copy(update={"write": account.write.model_copy(update={"monthly_budget_usd": 0.0})})
    claude = _FakeClaude(1)
    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), broke, client=claude) is None and claude.calls == []  # 예산 소진

    class Boom:
        messages = None

    assert reels_v3.pick_by_eye("문장", [], _ranked("q", 3), account, client=Boom()) is None               # 어떤 오류든 규칙으로


def test_fetch_thumb_only_from_clip_store():
    assert reels_v3.fetch_thumb("https://evil.example/a.jpg") is None and reels_v3.fetch_thumb("") is None
