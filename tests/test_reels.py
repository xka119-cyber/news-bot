"""릴스 영상 v2 (2026-11-09): 대본, 장면 길이, 프레임 합성, 실제 MP4 만들기."""

import subprocess

from PIL import Image

from newsbot.config import load_account
from newsbot.design import reels
from test_cards import make_draft

REEL = {"hook": ["우리 집 전기요금", "1월부터 오른다"], "beats": ["가정용 요금 인상", "kWh당 5원 더 내요", "다음 달 확정"],
        "takeaway": "대기전력부터 줄이기"}


def test_reel_script_uses_claude_script_or_falls_back_to_cards():
    draft = {**make_draft(), "reel": REEL}
    assert reels.reel_script(draft) == REEL
    old = make_draft()  # 2026-11-09 이전 초안: 대본 없음 → 카드 문구로
    script = reels.reel_script(old)
    assert script["hook"] == [old["cover_title"]]
    assert len(script["beats"]) == 3 and script["takeaway"] == old["why_it_matters"]


def test_scenes_and_durations_about_12_seconds():
    account = load_account("today_news")
    assert account.reels.enabled and account.reels.share_to_feed is False
    scenes = reels.build_scenes({**make_draft(), "reel": REEL})
    assert [s["kind"] for s in scenes] == ["hook", "beat", "beat", "beat", "end"]
    durations = reels.scene_durations(account, scenes)
    assert durations[0] <= 1.5, "평균 시청이 1.1~1.8초라 첫 장면이 그 안에 끝나야 다음 장면이 보인다(11-10)"
    assert round(sum(durations), 2) == 12.0


def test_highlight_numbers_and_escapes_html():
    assert str(reels.highlight("kWh당 5원 더")) == "kWh당 <em>5원</em> 더"
    assert str(reels.highlight("월세 300만 원, 5.4배")) == "월세 <em>300만 원</em>, <em>5.4배</em>"
    assert str(reels.highlight("<b>")) == "&lt;b&gt;"


def test_frames_first_frame_static_then_push_and_progress():
    account = load_account("today_news")
    images = [Image.new("RGB", (1080, 1920), c) for c in ("red", "green")]
    frames = list(reels.iter_frames(images, ["hook", "beat"], [1.0, 1.0], 10, account.design.colors))
    assert len(frames) == 20 and all(f.size == (1080, 1920) for f in frames)
    assert frames[0].getpixel((540, 300)) == (255, 0, 0)          # 첫 프레임 = 첫 장면 그대로
    middle = frames[11]                                           # 둘째 장면이 아래에서 밀고 올라오는 중
    assert middle.getpixel((540, 50))[0] > 200 and middle.getpixel((540, 1900))[1] > 100
    assert frames[-1].getpixel((540, 300))[1] > 100
    bar_end = 1080 - reels.BAR_MARGIN - 20
    assert frames[0].getpixel((bar_end, reels.BAR_Y + 5)) != frames[-1].getpixel((bar_end, reels.BAR_Y + 5))


def test_render_reel_makes_instagram_ready_mp4(tmp_path):
    account = load_account("today_news")
    # 2026-11-11부터 계정 설정은 v3(목소리+실사). 여기서는 v2 글자 릴스 자체를 본다 (v3는 test_reels_v3.py)
    account = account.model_copy(update={"reels": account.reels.model_copy(update={"style": "v2"})})
    result = reels.render_reel({**make_draft(), "reel": REEL}, account, [], tmp_path)
    assert result.seconds == 12.0 and result.warnings == [] and result.style == "v2"
    with Image.open(result.cover) as im:
        assert im.size == (1080, 1920) and im.format == "JPEG"
        assert im.getpixel((20, 20)) == (255, 200, 61)            # 첫 장면 = 노란 바탕
    info = subprocess.run([reels.ffmpeg_exe(), "-i", str(result.video)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    assert "Duration: 00:00:12.00" in info
    assert "h264" in info and "yuv420p" in info and "1080x1920" in info and "30 fps" in info
    assert "aac" in info and "48000 Hz" in info
    assert not list(tmp_path.glob("reel_scene_*")) and not (tmp_path / "reel_music.wav").exists()
    assert account.reels.music
    loud = subprocess.run([reels.ffmpeg_exe(), "-i", str(result.video), "-af", "volumedetect", "-f", "null", "-"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    mean = float(loud.split("mean_volume:")[1].split("dB")[0])
    assert -24 < mean < -14, f"배경음이 들어 있어야 함 (평균 {mean} dB)"
