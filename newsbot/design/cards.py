"""카드뉴스 4장(표지 + 뉴스 3장)을 HTML 템플릿 → Chromium 스크린샷 → JPEG로 만든다."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from newsbot.config import ROOT, AccountConfig

TEMPLATES_DIR = Path(__file__).parent / "templates"
FONTS_DIR = ROOT / "assets" / "fonts"
JPEG_QUALITY = 90
MAX_BYTES = 8 * 1024 * 1024
MIN_SCALE = 0.8  # 글자를 이보다 더 줄여야 하면 경고

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
CATEGORY_EMOJI = {"경제": "💰", "생활": "🏠", "IT": "💻", "트렌드": "✨", "날씨·교통": "🌤️"}


@dataclass
class RenderResult:
    paths: list[Path]
    warnings: list[str] = field(default_factory=list)


def date_label(when: datetime, timezone: str) -> str:
    local = when.astimezone(ZoneInfo(timezone))
    return f"{local.month}월 {local.day}일 {WEEKDAYS[local.weekday()]}요일"


def build_cards(draft: dict, account: AccountConfig, when: datetime) -> list[dict]:
    total = len(draft["items"]) + 1
    cards = [{
        "kind": "cover",
        "hook": draft["hook"],
        "date": date_label(when, account.timezone),
        "headlines": [item["headline"] for item in draft["items"]],
        "page": 1,
        "total": total,
    }]
    for n, item in enumerate(draft["items"], start=1):
        cards.append({
            "kind": "item",
            "number": n,
            "count": len(draft["items"]),
            "emoji": CATEGORY_EMOJI.get(item["category"], "📰"),
            **item,
            "page": n + 1,
            "total": total,
        })
    return cards


def render_cards(
    draft: dict,
    account: AccountConfig,
    out_dir: Path,
    when: datetime,
    template: str | None = None,
) -> RenderResult:
    from playwright.sync_api import sync_playwright

    template = template or account.design.template
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR / account.account_key),
        autoescape=select_autoescape(["html", "j2"]),
    )
    page_template = env.get_template(f"{template}.html.j2")
    width, height = account.design.card_size
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_html"
    work.mkdir(exist_ok=True)

    result = RenderResult(paths=[])
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        for card in build_cards(draft, account, when):
            html = page_template.render(
                card=card,
                brand=account.brand_name,
                colors=account.design.colors,
                fonts=FONTS_DIR.as_uri(),
                width=width,
                height=height,
                min_scale=MIN_SCALE,
            )
            html_path = work / f"card_{card['page']}.html"
            html_path.write_text(html, encoding="utf-8")
            page.goto(html_path.as_uri())
            page.evaluate("document.fonts.ready")
            fit = page.evaluate("window.fitCard ? window.fitCard() : {scale: 1, overflow: false}")
            if fit["overflow"]:
                result.warnings.append(f"{card['page']}장: 글자가 많아 최소 크기로도 넘칩니다")
            elif fit["scale"] < 1:
                result.warnings.append(f"{card['page']}장: 글자를 {round(fit['scale'] * 100)}%로 줄였습니다")
            path = out_dir / f"card_{card['page']}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=JPEG_QUALITY, full_page=False)
            if path.stat().st_size > MAX_BYTES:
                result.warnings.append(f"{card['page']}장: 파일이 8MB를 넘습니다")
            result.paths.append(path)
        browser.close()
    return result
