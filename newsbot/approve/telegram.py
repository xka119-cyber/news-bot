"""텔레그램 승인: 미리보기(카드 앨범 + 내용 + 버튼) 전송, 버튼 응답 읽기.

토큰은 요청 주소에만 들어가고, 오류 메시지·로그에는 절대 넣지 않는다.
"""

import html
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
CALLBACK_APPROVE = "a"
CALLBACK_SKIP = "s"
CALLBACK_APPROVE_ALL = "A"  # 같은 시각 게시물 모두 승인 (뒤에 붙는 ID는 시각 ID, 예: today_news/20261105-0730)


class TelegramError(RuntimeError):
    """메시지에 토큰이 들어가지 않는 오류."""


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30, retries: int = 3, sleep=time.sleep):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN이 비어 있습니다.")
        self._token = token
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep

    def _request(self, method: str, data: bytes | None = None, headers: dict | None = None) -> object:
        request = urllib.request.Request(f"{API}/bot{self._token}/{method}", data=data, headers=headers or {})
        # GitHub 컴퓨터에서 텔레그램 연결이 잠깐 끊기는 일이 있어(2026-11-05 18:04 게시 실패) 연결 오류만 1→2→4초 뒤 다시 시도한다
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as error:
                try:
                    description = json.load(error).get("description", "")
                except ValueError:
                    description = ""
                raise TelegramError(f"텔레그램 {method} 실패 (HTTP {error.code}) {description}".strip()) from None
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt >= self.retries:
                    raise TelegramError(f"텔레그램 {method} 연결 실패 ({type(error).__name__}, {attempt + 1}번 시도)") from None
                self._sleep(2 ** attempt)
        if not payload.get("ok"):
            raise TelegramError(f"텔레그램 {method} 실패: {payload.get('description', '')}")
        return payload["result"]

    def call(self, method: str, **params) -> object:
        body = {k: v for k, v in params.items() if v is not None}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self._request(method, data, {"Content-Type": "application/json"})

    def send_photos(self, chat_id: int, photos: list[Path]) -> list[dict]:
        """사진 여러 장을 앨범 하나로 보낸다(sendMediaGroup, 파일 직접 업로드)."""
        boundary = uuid.uuid4().hex
        media = [{"type": "photo", "media": f"attach://p{i}"} for i in range(len(photos))]
        parts = [
            ("chat_id", None, str(chat_id).encode()),
            ("media", None, json.dumps(media).encode()),
        ] + [(f"p{i}", path.name, path.read_bytes()) for i, path in enumerate(photos)]
        body = bytearray()
        for name, filename, content in parts:
            body += f"--{boundary}\r\n".encode()
            disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
            body += f"Content-Disposition: {disposition}\r\n".encode()
            if filename:
                body += b"Content-Type: image/jpeg\r\n"
            body += b"\r\n" + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        return self._request("sendMediaGroup", bytes(body), {"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        return self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"])


# ---------- 미리보기 ----------

def callback_data(action: str, draft_id: str) -> str:
    data = f"{action}|{draft_id}"
    if len(data.encode()) > 64:
        raise ValueError("callback_data가 64바이트를 넘습니다")
    return data


def parse_callback(data: str) -> tuple[str, str] | None:
    action, _, draft_id = (data or "").partition("|")
    if action not in (CALLBACK_APPROVE, CALLBACK_SKIP, CALLBACK_APPROVE_ALL) or not draft_id:
        return None
    return action, draft_id


def preview_text(draft: dict, caption: str, slot_label: str, warnings: list[str], brand: str, order: str = "") -> str:
    """order: 같은 시각 게시물 중 몇 번째인지(예: "1/2"). 하나씩 따로 승인한다."""
    e = html.escape
    title = f"<b>📮 {e(brand)} 미리보기" + (f" ({e(order)})" if order else "") + "</b>"
    lines = [title, f"게시 예정: <b>{e(slot_label)}</b> · 카드뉴스 4장", ""]
    lines += [f"<b>{e(draft['cover_title'])}</b>", "", "<b>캡션</b>", f"<blockquote expandable>{e(caption)}</blockquote>"]
    lines.append(f'<b>원문</b>: <a href="{e(draft["source_url"], quote=True)}">{e(draft["source_name"])} · {e(draft["source_title"])}</a>')
    if warnings:
        lines += ["", "<b>⚠️ 확인할 점</b>"] + [f"• {e(w)}" for w in warnings]
    lines += ["", "맨 아래(입력칸 자리) 버튼으로 승인하세요."]
    text = "\n".join(lines)
    return text[:4000]


def buttons(draft_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ 승인", "callback_data": callback_data(CALLBACK_APPROVE, draft_id)},
        {"text": "⏭ 건너뛰기", "callback_data": callback_data(CALLBACK_SKIP, draft_id)},
    ]]}


def send_preview(client: TelegramClient, chat_id: int, cards: list[Path], text: str, draft_id: str) -> int:
    """앨범 + 내용 메시지를 보내고, 내용 메시지 번호를 돌려준다. 승인은 아래 키보드 버튼으로 받는다."""
    client.send_photos(chat_id, cards)
    message = client.call(
        "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", link_preview_options={"is_disabled": True},
    )
    return message["message_id"]


# ---------- 아래 키보드 버튼 (2026-11-05) ----------
# 메시지 안 버튼(인라인)은 봇이 2~3분 안에 대답하지 않으면 텔레그램이 누름 기록을 버린다.
# 아래 키보드 버튼은 누르면 '글 메시지'가 보내져 24시간 남으므로, 정해진 때만 깨어나는 봇도 읽을 수 있다.

def keyboard_labels(numbers: list[int], hhmm: str) -> list[list[str]]:
    """예: [["✅ 모두 승인 (07:30)", "⏭ 모두 건너뛰기 (07:30)"], ["✅ 1번 승인 (07:30)", "✅ 2번 승인 (07:30)"]]"""
    if len(numbers) == 1:
        return [[f"✅ 승인 ({hhmm})", f"⏭ 건너뛰기 ({hhmm})"]]
    return [[f"✅ 모두 승인 ({hhmm})", f"⏭ 모두 건너뛰기 ({hhmm})"],
            [f"✅ {n}번 승인 ({hhmm})" for n in numbers]]


def send_keyboard(client: TelegramClient, chat_id: int, items: list[tuple[int, str]], slot_label: str, hhmm: str) -> int:
    """미리보기들 아래에 고르는 방법 안내 + 아래 키보드 버튼을 띄운다. items: (번호, 표지 제목)"""
    lines = [f"👇 {slot_label} 게시분을 아래 버튼으로 골라 주세요."]
    if len(items) > 1:
        lines += [f"{n}번 「{title}」" for n, title in items]
        lines.append("번호 버튼은 여러 개 눌러도 돼요. 안 누른 것은 올라가지 않아요.")
    lines.append("누르면 내 메시지로 남고, 게시 시각에 봇이 읽어서 처리해요.")
    message = client.call(
        "sendMessage", chat_id=chat_id, text="\n".join(lines),
        reply_markup={
            "keyboard": [[{"text": label} for label in row] for row in keyboard_labels([n for n, _ in items], hhmm)],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "승인할 게시물을 골라 주세요",
        },
    )
    return message["message_id"]


REMOVE_KEYBOARD = {"remove_keyboard": True}


def send_text(client: TelegramClient, chat_id: int, text: str) -> None:
    client.call("sendMessage", chat_id=chat_id, text=text, link_preview_options={"is_disabled": True})
