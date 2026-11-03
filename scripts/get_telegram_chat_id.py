"""봇에게 메시지를 보낸 뒤 실행하면 내 대화방 번호(chat id)를 찾는다. 토큰은 출력하지 않는다.

python scripts/get_telegram_chat_id.py          # 찾은 번호를 보여 줌
python scripts/get_telegram_chat_id.py --write  # .env의 TELEGRAM_CHAT_ID= 줄에 바로 넣음(다른 값은 건드리지 않음)
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from newsbot.approve.telegram import TelegramClient, TelegramError  # noqa: E402

ENV = ROOT / ".env"


def write_chat_id(chat_id: int) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == "TELEGRAM_CHAT_ID":
            lines[i] = f"TELEGRAM_CHAT_ID={chat_id}"
            break
    else:
        lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    load_dotenv(ENV)
    try:
        updates = TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN", "")).get_updates(offset=None, timeout=0)
    except TelegramError as error:
        print(f"❌ {error}")
        return 1
    chats = {}
    for update in updates:
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats[chat["id"]] = chat.get("first_name", "")
    if not chats:
        print("❌ 받은 메시지가 없습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
        return 1
    if len(chats) > 1:
        print(f"⚠️ 대화방이 {len(chats)}개입니다. 본인 이름을 확인하세요: {list(chats.values())}")
        return 1
    chat_id, name = next(iter(chats.items()))
    if args.write:
        write_chat_id(chat_id)
        print(f"✅ '{name}' 대화방 번호를 .env의 TELEGRAM_CHAT_ID에 넣었습니다.")
    else:
        print(f"✅ '{name}' 대화방 번호: {chat_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
