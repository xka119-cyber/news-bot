"""릴스 v3 (2026-11-11): AI 목소리 + 무료 실사 영상(Pixabay) + 큰 자막 → 1080x1920 MP4 약 13~15초.

왜: 인스타 앱의 시청 유지 그래프를 보니 v1·v2 모두 10명 중 6명이 1초 안에(글을 읽기도 전에) 넘겼다.
계정은 정상(추천 요건 통과, 조회 100%가 팔로워 아닌 사람)이라, 문구가 아니라 '멈춘 글자 화면'이라는 겉모습이 문제였다.
그래서 첫 프레임부터 실제 장면이 움직이고 목소리가 나오게 한다.

장면 4개(Claude가 쓴 reel.scenes): 장면마다 목소리 한 문장 + 자막 1~2줄 + 영상 검색어.
마지막 장면 뒤에는 같은 영상 위에 팔로우 권유 자막을 END_HOLD초 더 보여 준다.

못 만들면(V3Unavailable 등) reels.render_reel이 v2 글자 릴스로 대신 만든다:
열쇠 없음, 대본에 장면 없음, 영상 못 찾음, 목소리 서비스(edge-tts, 마이크로소프트 무료 음성의 비공식 경로) 장애.

게시 때 다시 만들어도 미리보기와 같은 영상이 나오도록, 고른 영상(clips: 주소·시작 초)을 초안 기록에 남겨 재사용한다.
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

from newsbot.config import AccountConfig
from newsbot.design.cards import FONTS_DIR
from newsbot.design.reels import AUDIO_RATE, MAX_BYTES, ReelResult, _NUMBER_RE, ffmpeg_exe

log = logging.getLogger(__name__)

STYLE = "v3"
SEARCH_URL = "https://pixabay.com/api/videos/"
CLIP_HOSTS = ("cdn.pixabay.com", "pixabay.com")   # 기록에 남긴 주소를 다시 받을 때도 이 창고의 주소만 받는다
CLIP_MAX_BYTES = 80 * 1024 * 1024
END_HOLD = 1.2                # 마지막 문장 뒤 팔로우 권유 자막을 보여 주는 시간
LONG_SECONDS = 16.0           # 이보다 길면 미리보기에 경고
CAPTION_SIZE, CAPTION_MIN, CAPTION_WIDTH, LINE_GAP = 104, 64, 900, 1.3
CAPTION_CENTER_Y = 980        # 인스타가 덮는 위·아래·오른쪽 버튼을 피한 가운데
FOLLOW_CAPTION = ["팔로우하고", "매일 3분 뉴스 받기"]
USER_AGENT = "newsbot/1.0"


class V3Unavailable(RuntimeError):
    """v3를 만들 수 없는 이유(쉬운 말). 받는 쪽이 v2로 대신 만든다."""


def scenes_of(draft: dict) -> list[dict]:
    scenes = (draft.get("reel") or {}).get("scenes") or []
    good = [s for s in scenes if (s.get("say") or "").strip() and s.get("caption") and (s.get("search") or "").strip()]
    if len(good) < 3:
        raise V3Unavailable("대본에 목소리 장면(scenes)이 없어요")
    return good[:4]


# ---------- 목소리 ----------

def media_seconds(path: Path) -> float:
    info = subprocess.run([ffmpeg_exe(), "-i", str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stderr
    found = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    if not found:
        raise V3Unavailable(f"길이를 읽지 못했어요: {path.name}")
    return int(found[1]) * 3600 + int(found[2]) * 60 + float(found[3])


def speak(text: str, voice: str, rate: str, out: Path) -> Path:
    """한 문장을 mp3로. 서비스가 잠깐 안 될 수 있어 한 번 더 해 본다."""
    import edge_tts

    last: Exception | None = None
    for _ in range(2):
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out)))
            if out.exists() and out.stat().st_size > 1000:
                return out
            last = V3Unavailable("빈 소리 파일")
        except Exception as error:  # noqa: BLE001 — 네트워크·서비스 어떤 오류든 v2로 넘어가게
            last = error
    raise V3Unavailable(f"AI 목소리를 만들지 못했어요({type(last).__name__})")


# ---------- 영상 찾기 ----------

def search_clips(query: str, key: str) -> list[dict]:
    """Pixabay 영상 검색. 열쇠는 주소에 들어가므로 오류 글에 주소를 싣지 않는다."""
    params = urllib.parse.urlencode({"key": key, "q": query, "video_type": "film", "safesearch": "true", "per_page": 30})
    request = urllib.request.Request(f"{SEARCH_URL}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response).get("hits", [])
    except urllib.error.HTTPError as error:
        raise V3Unavailable(f"영상 창고 검색 실패(HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise V3Unavailable(f"영상 창고 검색 실패({type(error).__name__})") from None


def pick_file(hit: dict) -> dict | None:
    """한 영상의 여러 크기 중 1080x1920에 쓸 만한 가장 가벼운 것(짧은 변 720 이상)."""
    # 실제 응답(2026-11-12 확인): 같은 이름이라도 영상마다 크기가 다르다(medium이 1280x720이기도 2560x1440이기도) → 크기로 고른다
    files = [f for f in (hit.get("videos") or {}).values()
             if isinstance(f, dict) and f.get("url") and min(f.get("width", 0), f.get("height", 0)) >= 720]
    return min(files, key=lambda f: f["width"] * f["height"]) if files else None


# 검색어에 딱 맞는 영상이 없을 때 쓰는, 분야별로 늘 영상이 많은 넓은 검색어 (2026-11-12 실제 검색으로 확인: 낱말 모두 일치 15~30개)
FALLBACK_SEARCH = {"경제": "money coins", "생활": "people shopping", "IT": "woman smartphone",
                   "트렌드": "people walking city", "날씨·교통": "city traffic"}
DEFAULT_FALLBACK = "city buildings"
_KOREA_TAGS = {"korea", "korean", "seoul", "busan"}


def _tokens(text: str) -> set[str]:
    """영어 낱말 묶음(3자 이상, 복수형 s는 뗀다)."""
    return {re.sub(r"s$", "", w) for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) > 2}


def rank_clips(hits: list[dict], need: float, used: set, query: str = "") -> list[dict]:
    """길이가 충분하고 아직 안 쓴 영상을 좋은 순서로 늘어놓는다. 각 항목의 match: 태그에 검색어 낱말이 "full"(모두) / "partial".

    순서: ① 태그에 든 검색어 낱말 수(많을수록) ② 한국 장면(태그에 korea·seoul 등) ③ 세로 영상 ④ 검색 순서(인기순).
    왜 낱말 '모두'를 따지나(2026-11-12 실제 검색 16개): Pixabay는 낱말 하나만 맞아도 결과로 준다(30개 중 30개).
    'calendar page'는 page만 맞은 책 영상, 'fuel pump' 2~4번째는 공사장 크레인·잠자리였다.
    세로 영상은 30개 중 0~2개뿐이라 가로 영상의 가운데를 잘라 쓴다."""
    words = _tokens(query)
    options = []
    for order, hit in enumerate(hits):
        file = pick_file(hit)
        if not file or hit.get("id") in used or hit.get("duration", 0) < need + 0.5:
            continue
        tags = _tokens(hit.get("tags", ""))
        matched = len(words & tags)
        portrait = file["height"] > file["width"]
        options.append((-matched, 0 if tags & _KOREA_TAGS else 1, 0 if portrait else 1, order, hit, file))
    ranked = []
    for score in sorted(options, key=lambda o: o[:4]):
        hit, file = score[4], score[5]
        duration = float(hit["duration"])
        start = round(max(0.0, min(duration * 0.15, duration - need - 0.3)), 2)   # 느린 도입부를 조금 건너뛴다
        ranked.append({"id": hit["id"], "url": file["url"], "page": hit.get("pageURL", ""), "start": start,
                       "match": "full" if words and -score[0] == len(words) else "partial",
                       "thumb": file.get("thumbnail", "")})
    return ranked


def _saved(clip: dict) -> dict:
    """초안 기록에 남길 모양(썸네일 주소는 고를 때만 쓴다)."""
    return {k: v for k, v in clip.items() if k != "thumb"}


def choose_clip(hits: list[dict], need: float, used: set, query: str = "") -> dict:
    """규칙만으로 고른 가장 좋은 영상 하나."""
    ranked = rank_clips(hits, need, used, query)
    if not ranked:
        raise V3Unavailable("맞는 영상을 찾지 못했어요")
    return _saved(ranked[0])


# ---------- 눈으로 고르기 (Claude가 후보 썸네일을 본다) ----------

JUDGE_QUERY_CANDIDATES, JUDGE_WIDE_CANDIDATES = 6, 3
THUMB_MAX_BYTES = 600 * 1024
NONE_FIT = -1


class ClipPick(BaseModel):
    choice: int   # 1부터 후보 번호, 0 = 어울리는 것이 없음


def _costs_path() -> Path:
    from newsbot.__main__ import COSTS

    return COSTS


def fetch_thumb(url: str) -> bytes | None:
    """후보 영상의 썸네일(JPEG). 영상 창고 주소만, 실패하면 None."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname not in CLIP_HOSTS:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(THUMB_MAX_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return data if 0 < len(data) <= THUMB_MAX_BYTES and data[:2] == b"\xff\xd8" else None


def pick_by_eye(say: str, caption: list[str], candidates: list[dict], account: AccountConfig, client=None) -> int | None:
    """Claude에게 후보 썸네일을 보여 주고 문장에 가장 어울리는 영상을 고르게 한다.

    돌려주는 값: 고른 후보의 순번(0부터) / NONE_FIT(어울리는 것이 없다고 함) / None(물어보지 못함 → 규칙으로 고른다).
    왜(2026-11-12 썸네일 비교 12개): 태그 규칙만으로는 좋아진 것 5개·나빠진 것 3개였다('money coins' 1등이 흙 묻은 보물 동전).
    태그는 영상 내용을 다 말해 주지 않아서, 사람처럼 눈으로 보는 수밖에 없다. 비용은 릴스 1개에 약 $0.002(Haiku 5.5)."""
    if not account.reels.clip_judge or len(candidates) < 2:
        return None
    if client is None and not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return None
    from datetime import datetime, timezone

    from newsbot.write.llm import call_cost, month_key, month_spent, record_cost

    try:
        costs, month = _costs_path(), month_key(account, datetime.now(timezone.utc))
        if month_spent(costs, account.account_key, month) >= account.write.monthly_budget_usd:
            return None
        shown = [(n, fetch_thumb(c.get("thumb", ""))) for n, c in enumerate(candidates)]
        shown = [(n, data) for n, data in shown if data]
        if len(shown) < 2:
            return None
        content: list[dict] = [{"type": "text", "text": (
            "짧은 뉴스 영상의 한 장면 뒤에 깔 배경 영상을 고릅니다.\n"
            f"이 장면에서 목소리가 읽는 문장: {say}\n화면 자막: {' / '.join(caption)}\n\n"
            "아래는 후보 영상의 대표 장면입니다. 실제로는 세로 화면이라 가운데 부분만 보입니다.")}]
        for k, (_, data) in enumerate(shown, start=1):
            content.append({"type": "text", "text": f"후보 {k}"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.standard_b64encode(data).decode("ascii")}})
        content.append({"type": "text", "text": (
            "문장의 내용과 가장 잘 어울리는 후보의 번호를 choice에 적으세요.\n"
            # 11-12: 처음엔 '외국 간판·외국 돈을 피하라'를 앞세웠더니 주유소 대신 밸브, 지폐 대신 흙 묻은 동전을 골랐다 → 관련성을 1순위로
            "1) 가장 중요한 기준은 관련성입니다: 처음 보는 사람이 그 장면만 보고도 문장의 주제(무엇에 관한 소식인지)를 떠올릴 수 있어야 합니다. "
            "주제의 대표적인 모습이 한눈에 보이는 장면이 좋고, 부품을 크게 찍었거나 무엇인지 알아보기 힘든 장면은 나쁩니다.\n"
            "2) 관련 있는 후보가 여럿일 때만 다음으로 가릅니다: 밝고 또렷한 장면, 한국이거나 어느 나라인지 티가 나지 않는 장면, "
            "세로로 가운데만 잘라도 중요한 것이 남는 장면을 우선합니다. 외국 간판이나 외국 돈이 보인다는 이유만으로 관련 있는 장면을 버리지는 않습니다.\n"
            "3) 주제와 직접 관련된 후보가 없으면, 문장의 분위기를 해치지 않는 무난한 일상 장면(거리, 건물, 사람들)을 고릅니다.\n"
            "- 쓸 만한 것이 하나도 없으면 0.")})
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        response = client.messages.parse(model=account.write.model, max_tokens=2000,
                                         messages=[{"role": "user", "content": content}], output_format=ClipPick)
        usage = response.usage
        record_cost(costs, account.account_key, month, call_cost(account, usage.input_tokens, usage.output_tokens),
                    usage.input_tokens, usage.output_tokens)
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            return None
        choice = response.parsed_output.choice
        if choice == 0:
            return NONE_FIT
        return shown[choice - 1][0] if 1 <= choice <= len(shown) else None
    except Exception as error:  # noqa: BLE001 — 눈으로 고르기는 덤이다: 어떤 오류든 규칙으로 고른 영상으로 계속 간다
        log.warning("영상 눈으로 고르기 실패(규칙으로 고름): %s", type(error).__name__)
        return None


def find_clip(query: str, category: str, key: str, need: float, used: set, say: str = "", caption: list[str] = (),
              account: AccountConfig | None = None) -> tuple[dict, str | None]:
    """한 장면의 배경 영상을 찾는다. (고른 영상, 사람이 확인할 경고 또는 None)

    후보 = 검색어 결과 위쪽 6개 + 분야별 넓은 검색어 결과 위쪽 3개. Claude가 썸네일을 보고 고른다(pick_by_eye).
    물어보지 못하면 규칙으로: ① 검색어 낱말이 모두 맞는 영상 → ② 넓은 검색어에서 모두 맞는 영상 → ③ 일부만 맞는 영상.
    ②③과 'Claude가 어울리는 게 없다고 한 경우'는 미리보기에서 확인하라고 경고를 남긴다."""
    wide = FALLBACK_SEARCH.get(category, DEFAULT_FALLBACK)
    ranked: list[dict] = []
    wide_ranked: list[dict] = []
    try:
        ranked = rank_clips(search_clips(query, key), need, used, query)
    except V3Unavailable:
        pass
    try:
        wide_ranked = rank_clips(search_clips(wide, key), need, used, wide)
    except V3Unavailable:
        pass
    if not ranked and not wide_ranked:
        raise V3Unavailable(f"'{query}'에 쓸 영상을 찾지 못했어요")

    doubt = None
    if account is not None:
        seen = {c["id"] for c in ranked[:JUDGE_QUERY_CANDIDATES]}
        candidates = ranked[:JUDGE_QUERY_CANDIDATES] + [c for c in wide_ranked if c["id"] not in seen][:JUDGE_WIDE_CANDIDATES]
        picked = pick_by_eye(say, list(caption), candidates, account)
        if picked is not None and picked != NONE_FIT:
            return _saved(candidates[picked]), None
        if picked == NONE_FIT:
            doubt = f"'{query}'에 어울리는 영상을 찾지 못했어요(미리보기에서 확인)"

    if ranked and ranked[0]["match"] == "full":
        return _saved(ranked[0]), doubt
    if wide_ranked and wide_ranked[0]["match"] == "full":
        return _saved(wide_ranked[0]), doubt or f"'{query}'에 딱 맞는 영상이 없어 무난한 '{wide}' 영상을 썼어요"
    return _saved((ranked or wide_ranked)[0]), doubt or f"'{query}' 영상이 내용과 다를 수 있어요(미리보기에서 확인)"


def download(url: str, dest: Path) -> Path:
    host = urllib.parse.urlparse(url).hostname or ""
    if urllib.parse.urlparse(url).scheme != "https" or host not in CLIP_HOSTS:
        raise V3Unavailable("영상 주소가 영상 창고의 것이 아니에요")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
            total = 0
            while chunk := response.read(1 << 20):
                total += len(chunk)
                if total > CLIP_MAX_BYTES:
                    raise V3Unavailable("영상 파일이 너무 커요")
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError) as error:
        raise V3Unavailable(f"영상을 내려받지 못했어요({type(error).__name__})") from None
    return dest


# ---------- 자막 ----------

def _segments(line: str, accent: str) -> list[tuple[str, str]]:
    """숫자(단위 포함)는 강조색, 나머지는 흰색."""
    out, last = [], 0
    for m in _NUMBER_RE.finditer(line):
        out += [(line[last:m.start()], "#FFFFFF"), (m.group(0), accent)]
        last = m.end()
    out.append((line[last:], "#FFFFFF"))
    return [(text, color) for text, color in out if text]


def caption_image(lines: list[str], account: AccountConfig, source: str) -> Image.Image:
    """투명 바탕에 상표 알약 + 가운데 큰 자막 + 출처. 영상 위에 덮는다."""
    width, height = account.design.reel_size
    colors = account.design.colors
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    small = ImageFont.truetype(str(FONTS_DIR / "Pretendard-Bold.otf"), 38)
    pill = draw.textlength(account.brand_name, font=small) + 70
    draw.rounded_rectangle((60, 150, 60 + pill, 220), 35, fill=colors.primary)
    draw.text((60 + pill / 2, 185), account.brand_name, font=small, fill=colors.accent, anchor="mm")
    credit = " · ".join(part for part in (f"출처 {source}" if source else "", "영상 Pixabay") if part)
    draw.text((60, height - 330), credit, font=small, fill=(255, 255, 255, 210), stroke_width=3, stroke_fill="black")

    size = CAPTION_SIZE
    while True:   # 가장 긴 줄이 화면 폭에 들어올 때까지 글자를 줄인다
        font = ImageFont.truetype(str(FONTS_DIR / "Pretendard-ExtraBold.otf"), size)
        if max(draw.textlength(line, font=font) for line in lines) <= CAPTION_WIDTH or size <= CAPTION_MIN:
            break
        size -= 6
    step = round(size * LINE_GAP)
    y = CAPTION_CENTER_Y - step * len(lines) // 2
    for line in lines:
        x = (width - draw.textlength(line, font=font)) / 2
        for text, color in _segments(line, colors.accent):
            draw.text((x, y), text, font=font, fill=color, stroke_width=max(6, size // 10), stroke_fill="black")
            x += draw.textlength(text, font=font)
        y += step
    return image


# ---------- 합치기 ----------

def _run(command: list[str], what: str) -> None:
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise OSError(f"{what}(ffmpeg) 실패: {done.stderr.strip()[-300:]}")


def make_segment(clip: Path, start: float, seconds: float, caption: Path, out: Path, size: tuple[int, int], fps: int) -> None:
    """영상의 start부터 seconds초를 화면 가득(가운데 기준으로 잘라) 채우고 조금 어둡게 한 뒤 자막을 덮는다."""
    width, height = size
    graph = (f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={fps},"
             f"eq=brightness=-0.10:saturation=1.05[v];[v][1:v]overlay=0:0,format=yuv420p[o]")
    _run([ffmpeg_exe(), "-y", "-loglevel", "error", "-stream_loop", "-1", "-ss", f"{start:.2f}", "-i", str(clip),
          "-i", str(caption), "-filter_complex", graph, "-map", "[o]", "-an", "-t", f"{seconds:.3f}", "-r", str(fps),
          "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high", "-pix_fmt", "yuv420p",
          "-g", str(fps * 2), str(out)], "장면 만들기")


def render(draft: dict, account: AccountConfig, out_dir: Path, clips: list[dict] | None = None) -> ReelResult:
    """reel.mp4와 reel_cover.jpg를 out_dir에 만든다. clips(이전에 고른 영상)가 장면 수와 맞으면 그대로 쓴다."""
    scenes = scenes_of(draft)
    r = account.reels
    if not (clips and len(clips) == len(scenes)):
        clips = None
        key = os.environ.get(r.clip_key_env, "").strip()
        if not key:
            raise V3Unavailable(f"영상 창고 열쇠({r.clip_key_env})가 없어요")
    size, fps = tuple(account.design.reel_size), r.fps
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_reel_v3"
    work.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    try:
        voices = [speak(scene["say"].strip(), r.voice, r.voice_rate, work / f"say{n}.mp3") for n, scene in enumerate(scenes)]
        lengths = [media_seconds(v) for v in voices]
        total = sum(lengths) + END_HOLD
        if total > LONG_SECONDS:
            warnings.append(f"릴스가 {total:.0f}초로 깁니다(목표 15초 이내)")

        plan, used, parts = [], set(), []
        source = draft.get("source_name", "")
        for n, scene in enumerate(scenes):
            last = n == len(scenes) - 1
            need = lengths[n] + (END_HOLD if last else 0)
            if clips:
                chosen = dict(clips[n])
            else:
                chosen, doubt = find_clip(scene["search"].strip(), draft.get("category", ""), key, need, used,
                                          say=scene["say"].strip(), caption=scene["caption"], account=account)
                if doubt:
                    warnings.append(f"릴스 {n + 1}번 장면: {doubt}")
            used.add(chosen.get("id"))
            plan.append(chosen)
            clip = download(chosen["url"], work / f"clip{n}.mp4")
            start = float(chosen.get("start", 0))
            lines = [line.strip() for line in scene["caption"] if line.strip()][:2]
            caption = work / f"cap{n}.png"
            caption_image(lines, account, source).save(caption)
            parts.append(work / f"seg{n}.mp4")
            make_segment(clip, start, lengths[n], caption, parts[-1], size, fps)
            if last:   # 같은 영상을 이어서, 팔로우 권유 자막으로
                follow = work / "cap_follow.png"
                caption_image(FOLLOW_CAPTION, account, source).save(follow)
                parts.append(work / "seg_follow.mp4")
                make_segment(clip, start + lengths[n], END_HOLD, follow, parts[-1], size, fps)

        listing = work / "list.txt"
        listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        video = out_dir / "reel.mp4"
        inputs = [arg for v in voices for arg in ("-i", str(v))]
        mix = "".join(f"[{k + 1}:a]" for k in range(len(voices)))
        mix += f"concat=n={len(voices)}:v=0:a=1,apad=pad_dur={END_HOLD + 0.5},loudnorm=I=-16:TP=-1.5[a]"
        _run([ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), *inputs,
              "-filter_complex", mix, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
              "-ar", str(AUDIO_RATE), "-ac", "2", "-t", f"{total:.3f}", "-movflags", "+faststart", str(video)], "영상 합치기")
        cover = out_dir / "reel_cover.jpg"
        _run([ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", "0.3", "-i", str(video), "-frames:v", "1",
              "-q:v", "3", str(cover)], "표지 뽑기")
    except BaseException:
        for leftover in (out_dir / "reel.mp4", out_dir / "reel_cover.jpg"):   # 반쯤 만들어진 결과물을 남기지 않는다
            leftover.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if video.stat().st_size > MAX_BYTES:
        warnings.append("릴스: 영상 파일이 100MB를 넘습니다")
    return ReelResult(video, cover, round(total, 2), warnings, style=STYLE, clips=plan)
