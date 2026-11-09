"""릴스(세로 영상) v2: 화면 가득 큰 글씨 장면 5개 → 1080x1920 MP4 약 12초 (2026-11-09).

v1(2026-11-07)은 카드 이미지를 남색 바탕에 얹은 15초 영상이었는데, 릴스 9개 모두 3초 안에 넘긴 비율이 90~99%,
평균 시청 1.1~1.9초였다(인사이트 reels_skip_rate). 그래서 첫 화면부터 큰 글씨로 '나와 관련된 질문·숫자'를 보여 주고,
3초가 되기 전에 다음 장면이 나오게 바꿨다.

장면: hook(첫 화면 2줄) → beat 3개(핵심 한 줄씩) → end(마무리 + 팔로우 권유).
장면마다 HTML 1장(Chromium 스크린샷)을 만들고, Pillow로 천천히 다가가는 움직임·위로 밀려 들어오는 전환·진행 막대를 그린다.
배경음악은 아직 없다(2026-11-07 사용자 결정).
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from PIL import Image, ImageDraw

from newsbot.config import AccountConfig
from newsbot.design.cards import CATEGORY_EMOJI, FONTS_DIR, MIN_SCALE, TEMPLATES_DIR

STYLE = "v2"
# 인스타가 덮는 곳(위 버튼줄, 아래 캡션·계정명, 오른쪽 좋아요·댓글 버튼)을 피한 글자 영역
SAFE = {"left": 80, "right": 160, "top": 250, "bottom": 470}
BAR_Y, BAR_H, BAR_MARGIN = 1480, 10, 80
ZOOM_TO = 1.035                 # 장면 동안 천천히 이만큼 다가간다(정지 화면처럼 보이지 않게)
ENTER_SECONDS = 0.25            # 다음 장면이 아래에서 화면을 밀고 올라오는 시간
MAX_BYTES = 100 * 1024 * 1024   # 인스타 릴스 업로드 한도(넉넉히 아래)
AUDIO_RATE = 48000

_NUMBER_RE = re.compile(
    r"\d[\d,.]*\s*(?:%|퍼센트|배|만\s*원|억\s*원|조\s*원|천\s*원|원|만|억|조|명|곳|개|일|월|년|도|dB|kWh|세|위)?"
)


@dataclass
class ReelResult:
    video: Path
    cover: Path
    seconds: float
    warnings: list[str] = field(default_factory=list)
    style: str = STYLE


def ffmpeg_exe() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def reel_script(draft: dict) -> dict:
    """Claude가 쓴 릴스 대본. 없거나 모자라면(예전 초안) 카드 문구로 채운다."""
    reel = draft.get("reel") or {}
    hook = [s for s in reel.get("hook", []) if s.strip()][:2]
    if not hook:
        hook = [draft["cover_title"]]
    beats = [s for s in reel.get("beats", []) if s.strip()][:3]
    if len(beats) < 3:
        lines = [line for point in draft["points"] for line in point["lines"]]
        beats += [line for line in lines if line not in beats][: 3 - len(beats)]
    takeaway = (reel.get("takeaway") or "").strip() or draft["why_it_matters"]
    return {"hook": hook, "beats": beats, "takeaway": takeaway}


def highlight(text: str) -> Markup:
    """숫자(단위 포함)를 <em>으로 감싸 강조한다. 나머지 글자는 HTML 이스케이프."""
    out, last = [], 0
    for m in _NUMBER_RE.finditer(text):
        out += [escape(text[last:m.start()]), Markup("<em>"), escape(m.group(0)), Markup("</em>")]
        last = m.end()
    out.append(escape(text[last:]))
    return Markup("").join(out)


def build_scenes(draft: dict) -> list[dict]:
    script = reel_script(draft)
    context = " ".join(script["hook"])
    scenes = [{"kind": "hook", "lines": [highlight(s) for s in script["hook"]]}]
    total = len(script["beats"])
    for n, beat in enumerate(script["beats"], start=1):
        scenes.append({"kind": "beat", "line": highlight(beat), "index": n, "total": total, "context": context})
    scenes.append({"kind": "end", "line": highlight(script["takeaway"])})
    return scenes


def scene_durations(account: AccountConfig, scenes: list[dict]) -> list[float]:
    r = account.reels
    seconds = {"hook": r.hook_seconds, "beat": r.beat_seconds, "end": r.end_seconds}
    return [seconds[s["kind"]] for s in scenes]


def render_scenes(draft: dict, account: AccountConfig, scenes: list[dict], work: Path) -> tuple[list[Image.Image], list[str]]:
    """장면마다 HTML → PNG 스크린샷 → RGB 이미지. (이미지들, 경고들)"""
    from playwright.sync_api import sync_playwright

    width, height = account.design.reel_size
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR / account.account_key),
                      autoescape=select_autoescape(["html", "j2"]))
    template = env.get_template("reel.html.j2")
    images, warnings = [], []
    work.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        for n, scene in enumerate(scenes, start=1):
            html = template.render(
                scene=scene, category=draft["category"], emoji=CATEGORY_EMOJI.get(draft["category"], "📰"),
                source=draft.get("source_name", ""), brand=account.brand_name, colors=account.design.colors,
                fonts=FONTS_DIR.as_uri(), width=width, height=height, min_scale=MIN_SCALE, safe=SAFE,
            )
            html_path = (work / f"reel_scene_{n}.html").resolve()
            png_path = html_path.with_suffix(".png")
            html_path.write_text(html, encoding="utf-8")
            page.goto(html_path.as_uri())
            page.evaluate("document.fonts.ready")
            fit = page.evaluate("window.fitCard()")
            if fit["overflow"]:
                warnings.append(f"릴스 {n}번째 장면: 글이 길어 최소 크기로도 넘칩니다")
            page.screenshot(path=str(png_path), type="png", full_page=False)
            with Image.open(png_path) as im:
                images.append(im.convert("RGB"))
            html_path.unlink(missing_ok=True)
            png_path.unlink(missing_ok=True)
        browser.close()
    return images, warnings


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def zoomed(image: Image.Image, zoom: float) -> Image.Image:
    """가운데를 기준으로 zoom배 확대해 원래 크기로 자른다(1.0이면 그대로)."""
    if zoom <= 1.0001:
        return image.copy()
    w, h = image.size
    big = image.resize((round(w * zoom), round(h * zoom)), Image.BILINEAR)
    left, top = (big.width - w) // 2, (big.height - h) // 2
    return big.crop((left, top, left + w, top + h))


def draw_bar(frame: Image.Image, progress: float, colors, on_dark: bool) -> None:
    draw = ImageDraw.Draw(frame)
    left, right = BAR_MARGIN, frame.width - BAR_MARGIN
    track = "#3A4766" if on_dark else "#E0A92A"
    fill = colors.accent if on_dark else colors.primary
    draw.rounded_rectangle((left, BAR_Y, right, BAR_Y + BAR_H), radius=BAR_H // 2, fill=track)
    filled = left + round((right - left) * min(max(progress, 0.0), 1.0))
    if filled > left + BAR_H:
        draw.rounded_rectangle((left, BAR_Y, filled, BAR_Y + BAR_H), radius=BAR_H // 2, fill=fill)


def iter_frames(images: list[Image.Image], kinds: list[str], durations: list[float], fps: int, colors):
    """장면마다 1.0→ZOOM_TO로 천천히 다가가고, 다음 장면은 ENTER_SECONDS 동안 아래에서 밀고 올라온다.
    첫 프레임은 첫 장면 그대로(움직임 없이 바로 읽히게)."""
    total = sum(durations)
    total_frames = round(total * fps)
    starts = [sum(durations[:i]) for i in range(len(durations))]
    for n in range(total_frames):
        t = n / fps
        index = max(i for i, s in enumerate(starts) if s <= t + 1e-9)
        local = t - starts[index]
        frame = zoomed(images[index], 1.0 + (ZOOM_TO - 1.0) * _ease(local / durations[index]))
        if index > 0 and local < ENTER_SECONDS:
            # 다음 장면이 아래에서 화면 전체를 밀고 올라온다(색이 섞여 탁해지지 않게 겹치지 않고 덮는다)
            top = round(frame.height * (1 - _ease(local / ENTER_SECONDS)))
            if top > 0:
                pushed = zoomed(images[index - 1], ZOOM_TO)
                pushed.paste(frame.crop((0, 0, frame.width, frame.height - top)), (0, top))
                frame = pushed
        draw_bar(frame, (n + 1) / total_frames, colors, on_dark=kinds[index] == "beat")
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
    """reel.mp4와 표지 reel_cover.jpg(첫 장면)를 out_dir에 만든다. card_paths는 예전 호출 모양을 맞추려고 받기만 한다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    size = tuple(account.design.reel_size)
    fps = account.reels.fps
    scenes = build_scenes(draft)
    images, warnings = render_scenes(draft, account, scenes, out_dir)
    durations = scene_durations(account, scenes)
    kinds = [s["kind"] for s in scenes]

    cover = out_dir / "reel_cover.jpg"
    images[0].save(cover, "JPEG", quality=90)
    video = out_dir / "reel.mp4"
    encode(iter_frames(images, kinds, durations, fps, account.design.colors), size, fps, video)
    if video.stat().st_size > MAX_BYTES:
        warnings.append("릴스: 영상 파일이 100MB를 넘습니다")
    return ReelResult(video, cover, round(sum(durations), 2), warnings)
