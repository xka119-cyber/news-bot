"""릴스(세로 영상): 카드 4장 → 1080x1920 MP4 약 15초 (설계서 4.3 릴스, M7 2026-11-07).

구성: 위 = 제목(HTML 바탕 1장, Chromium 스크린샷), 가운데 = 카드가 살짝 커지는 움직임, 아래 = 진행 막대.
프레임은 Pillow로 그려 ffmpeg(imageio-ffmpeg에 들어 있는 것)에 넘기고 H.264 + 무음 AAC로 만든다.
배경음악은 아직 넣지 않는다(2026-11-07 사용자 결정: 조회수가 생기면 무료 음원을 직접 받아 넣기).
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image, ImageDraw

from newsbot.config import AccountConfig
from newsbot.design.cards import CATEGORY_EMOJI, FONTS_DIR, MIN_SCALE, TEMPLATES_DIR

# 인스타가 위 ~200px, 아래 ~320px에 버튼·캡션을 덮는다 → 제목과 카드는 그 사이에 둔다
TOP_Y, TOP_H = 200, 300
CARD_CENTER_Y = 1080
ZOOM_FROM, ZOOM_TO = 0.80, 0.84   # 카드 크기 (카드 원본 폭 대비). 장면마다 천천히 커진다
BAR_Y, BAR_H, BAR_MARGIN = 1680, 10, 60
FADE_SECONDS = 0.25               # 장면이 바뀔 때 겹쳐 넘어가는 시간
MAX_BYTES = 100 * 1024 * 1024     # 인스타 릴스 업로드 한도(넉넉히 아래)
AUDIO_RATE = 48000


@dataclass
class ReelResult:
    video: Path
    cover: Path
    seconds: float
    warnings: list[str] = field(default_factory=list)


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def scene_durations(account: AccountConfig, cards: int) -> list[float]:
    reels = account.reels
    return [reels.cover_seconds] + [reels.card_seconds] * (cards - 1)


def render_background(draft: dict, account: AccountConfig, path: Path) -> list[str]:
    """제목·브랜드가 들어간 바탕 1장(카드 자리는 비어 있음)을 만든다. 경고 목록을 돌려준다."""
    from playwright.sync_api import sync_playwright

    width, height = account.design.reel_size
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR / account.account_key),
                      autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("reel.html.j2").render(
        title=draft["cover_title"], category=draft["category"], emoji=CATEGORY_EMOJI.get(draft["category"], "📰"),
        brand=account.brand_name, colors=account.design.colors, fonts=FONTS_DIR.as_uri(),
        width=width, height=height, min_scale=MIN_SCALE, top_y=TOP_Y, top_h=TOP_H, bottom_y=BAR_Y,
    )
    html_path = path.resolve().with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    warnings = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        page.goto(html_path.as_uri())
        page.evaluate("document.fonts.ready")
        fit = page.evaluate("window.fitCard()")
        if fit["overflow"]:
            warnings.append("릴스: 위쪽 제목이 길어 최소 크기로도 넘칩니다")
        page.screenshot(path=str(path), type="png", full_page=False)
        browser.close()
    html_path.unlink(missing_ok=True)
    return warnings


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def compose_frame(background: Image.Image, card: Image.Image, zoom: float, progress: float, colors) -> Image.Image:
    """바탕 위에 카드(zoom 배율)를 가운데 얹고 아래에 진행 막대(0~1)를 그린다."""
    frame = background.copy()
    width = frame.width
    w = round(card.width * zoom)
    h = round(card.height * zoom)
    scaled = card.resize((w, h), Image.BILINEAR)
    frame.paste(scaled, ((width - w) // 2, CARD_CENTER_Y - h // 2))
    draw = ImageDraw.Draw(frame)
    left, right = BAR_MARGIN, width - BAR_MARGIN
    draw.rounded_rectangle((left, BAR_Y, right, BAR_Y + BAR_H), radius=BAR_H // 2, fill="#3A4766")
    filled = left + round((right - left) * min(max(progress, 0.0), 1.0))
    if filled > left + BAR_H:
        draw.rounded_rectangle((left, BAR_Y, filled, BAR_Y + BAR_H), radius=BAR_H // 2, fill=colors.accent)
    return frame


def iter_frames(background: Image.Image, cards: list[Image.Image], durations: list[float], fps: int, colors):
    """장면별로 카드가 ZOOM_FROM→ZOOM_TO로 커지고, 장면 끝 FADE_SECONDS 동안 다음 카드와 겹쳐 넘어간다."""
    total = sum(durations)
    total_frames = round(total * fps)
    starts = [sum(durations[:i]) for i in range(len(durations))]
    fade = FADE_SECONDS
    for n in range(total_frames):
        t = n / fps
        index = max(i for i, s in enumerate(starts) if s <= t)
        local = (t - starts[index]) / durations[index]
        zoom = ZOOM_FROM + (ZOOM_TO - ZOOM_FROM) * _ease(local)
        progress = (n + 1) / total_frames  # 마지막 프레임에서 막대가 끝까지 찬다
        frame = compose_frame(background, cards[index], zoom, progress, colors)
        remaining = starts[index] + durations[index] - t
        if index + 1 < len(cards) and remaining < fade:
            following = compose_frame(background, cards[index + 1], ZOOM_FROM, progress, colors)
            frame = Image.blend(frame, following, 1 - remaining / fade)
        yield frame


def encode(frames, size: tuple[int, int], fps: int, out: Path) -> None:
    """RGB 프레임들을 H.264(yuv420p) + 무음 AAC 스테레오 MP4로 만든다(인스타 릴스 권장 형식)."""
    width, height = size
    command = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={AUDIO_RATE}",
        "-shortest", "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-g", str(fps * 2), "-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_RATE),
        "-movflags", "+faststart", str(out),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames:
            process.stdin.write(frame.tobytes())
        process.stdin.close()
    except BrokenPipeError:
        pass
    error = process.stderr.read().decode("utf-8", "replace")
    if process.wait() != 0:
        raise OSError(f"영상 만들기(ffmpeg) 실패: {error.strip()[-300:]}")


def render_reel(draft: dict, account: AccountConfig, card_paths: list[Path], out_dir: Path) -> ReelResult:
    """카드 이미지들로 reel.mp4와 표지 reel_cover.jpg(첫 화면)를 out_dir에 만든다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    size = tuple(account.design.reel_size)
    fps = account.reels.fps
    background_path = out_dir / "reel_bg.png"
    warnings = render_background(draft, account, background_path)
    with Image.open(background_path) as im:
        background = im.convert("RGB")
    background_path.unlink(missing_ok=True)
    cards = []
    for path in card_paths:
        with Image.open(path) as im:
            cards.append(im.convert("RGB"))
    durations = scene_durations(account, len(cards))

    cover = out_dir / "reel_cover.jpg"
    first = compose_frame(background, cards[0], ZOOM_FROM, 0.0, account.design.colors)
    first.save(cover, "JPEG", quality=90)
    video = out_dir / "reel.mp4"
    encode(iter_frames(background, cards, durations, fps, account.design.colors), size, fps, video)
    if video.stat().st_size > MAX_BYTES:
        warnings.append("릴스: 영상 파일이 100MB를 넘습니다")
    return ReelResult(video, cover, sum(durations), warnings)
