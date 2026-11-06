"""릴스 영상 (M7): 장면 길이, 프레임 합성, 실제 MP4 만들기."""

import subprocess
from datetime import datetime, timezone

from PIL import Image

from newsbot.config import load_account
from newsbot.design import reels
from newsbot.design.cards import render_cards
from test_cards import make_draft

WHEN = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)


def test_scene_durations_about_15_seconds():
    account = load_account("today_news")
    assert account.reels.enabled and account.reels.share_to_feed is False
    assert reels.scene_durations(account, 4) == [3, 4, 4, 4]


def test_frames_count_zoom_and_progress():
    account = load_account("today_news")
    bg = Image.new("RGB", (1080, 1920), "#1F2A44")
    cards = [Image.new("RGB", (1080, 1350), c) for c in ("red", "green")]
    frames = list(reels.iter_frames(bg, cards, [1.0, 1.0], 10, account.design.colors))
    assert len(frames) == 20 and all(f.size == (1080, 1920) for f in frames)
    # 첫 장면은 빨간 카드, 마지막은 초록 카드. 진행 막대는 끝으로 갈수록 찬다
    assert frames[0].getpixel((540, 1080)) == (255, 0, 0)
    assert frames[-1].getpixel((540, 1080))[1] > 100
    bar_end = 1080 - reels.BAR_MARGIN - 20
    assert frames[0].getpixel((bar_end, reels.BAR_Y + 5)) != frames[-1].getpixel((bar_end, reels.BAR_Y + 5))


def test_render_reel_makes_instagram_ready_mp4(tmp_path):
    account = load_account("today_news")
    cards = render_cards(make_draft(), account, tmp_path, WHEN).paths
    result = reels.render_reel(make_draft(), account, cards, tmp_path)
    assert result.seconds == 15 and result.warnings == []
    with Image.open(result.cover) as im:
        assert im.size == (1080, 1920) and im.format == "JPEG"
    info = subprocess.run([reels.ffmpeg_exe(), "-i", str(result.video)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    assert "Duration: 00:00:15.00" in info
    assert "h264" in info and "yuv420p" in info and "1080x1920" in info and "30 fps" in info
    assert "aac" in info and "48000 Hz" in info
    assert not (tmp_path / "reel_bg.png").exists()
