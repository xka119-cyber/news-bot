"""텔레그램 승인: 미리보기(카드 앨범 + 내용 + 버튼) 전송, 버튼 응답 읽기.

토큰은 요청 주소에만 들어가고, 오류 메시지·로그에는 절대 넣지 않는다.
"""

import html
import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
CALLBACK_APPROVE = "a"
CALLBACK_SKIP = "s"


class TelegramError(RuntimeError):
    """메시지에 토큰이 들어가지 않는 오류."""


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30):
        if not token:
            raise TelegramError("TELEGRAM_BOT_TOKEN이 비어 있습니다.")
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, data: bytes | None = None, headers: dict | None = None) -> object:
        request = urllib.request.Request(f"{API}/bot{self._token}/{method}", data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                description = json.load(error).get("description", "")
            except ValueError:
                description = ""
            raise TelegramError(f"텔레그램 {method} 실패 (HTTP {error.code}) {description}".strip()) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise TelegramError(f"텔레그램 {method} 연결 실패 ({type(error).__name__})") from None
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
    if action not in (CALLBACK_APPROVE, CALLBACK_SKIP) or not draft_id:
        return None
    return action, draft_id


def preview_text(draft: dict, caption: str, slot_label: str, warnings: list[str], brand: str) -> str:
    e = html.escape
    lines = [f"<b>📮 {e(brand)} 미리보기</b>", f"게시 예정: <b>{e(slot_label)}</b> · 카드뉴스 4장", ""]
    lines += [f"<b>{e(draft['cover_title'])}</b>", "", "<b>캡션</b>", f"<blockquote expandable>{e(caption)}</blockquote>"]
    lines.append(f'<b>원문</b>: <a href="{e(draft["source_url"], quote=True)}">{e(draft["source_name"])} · {e(draft["source_title"])}</a>')
    if warnings:
        lines += ["", "<b>⚠️ 확인할 점</b>"] + [f"• {e(w)}" for w in warnings]
    lines += ["", "아래 버튼으로 승인하거나 건너뛰세요."]
    text = "\n".join(lines)
    return text[:4000]


def buttons(draft_id: str) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ 승인", "callback_data": callback_data(CALLBACK_APPROVE, draft_id)},
        {"text": "⏭ 건너뛰기", "callback_data": callback_data(CALLBACK_SKIP, draft_id)},
    ]]}


def send_preview(client: TelegramClient, chat_id: int, cards: list[Path], text: str, draft_id: str) -> int:
    """앨범 + 내용·버튼 메시지를 보내고, 버튼 메시지 번호를 돌려준다."""
    client.send_photos(chat_id, cards)
    message = client.call(
        "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
        link_preview_options={"is_disabled": True}, reply_markup=buttons(draft_id),
    )
    return message["message_id"]


def send_text(client: TelegramClient, chat_id: int, text: str) -> None:
    client.call("sendMessage", chat_id=chat_id, text=text, link_preview_options={"is_disabled": True})
