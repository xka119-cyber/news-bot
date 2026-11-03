"""초안 상태 파일: state/drafts/{account}/{YYYY-MM-DD}/{HHMM}/draft.json

상태 흐름: generated → approved / skipped / expired → published / failed
(M6에서 이 폴더를 media 브랜치로 옮긴다.)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from newsbot.config import AccountConfig

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def slot_label(slot: datetime, timezone: str) -> str:
    local = slot.astimezone(ZoneInfo(timezone))
    return f"{local.month}월 {local.day}일({WEEKDAYS[local.weekday()]}) {local:%H:%M}"


def draft_id(account_key: str, slot: datetime, timezone: str) -> str:
    local = slot.astimezone(ZoneInfo(timezone))
    return f"{account_key}/{local:%Y%m%d-%H%M}"


def draft_dir(root: Path, draft_id_: str) -> Path:
    account, stamp = draft_id_.split("/")
    day, hhmm = stamp.split("-")
    return root / account / f"{day[:4]}-{day[4:6]}-{day[6:]}" / hhmm


def slot_at(account: AccountConfig, hhmm: str, now: datetime) -> datetime:
    """오늘 그 시각(이미 지났으면 내일)."""
    tz = ZoneInfo(account.timezone)
    local_now = now.astimezone(tz)
    hour, minute = map(int, hhmm.split(":"))
    slot = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot if slot > local_now else slot + timedelta(days=1)


def upcoming_slot(account: AccountConfig, now: datetime, min_minutes: int = 30, max_minutes: int = 150) -> datetime | None:
    """앞으로 30~150분 안의 게시 시각(설계서 4.7). 없으면 None."""
    for hhmm in account.slots:
        slot = slot_at(account, hhmm, now)
        if timedelta(minutes=min_minutes) <= slot - now <= timedelta(minutes=max_minutes):
            return slot
    return None


def save(root: Path, record: dict) -> Path:
    folder = draft_dir(root, record["id"])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "draft.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load(root: Path, draft_id_: str) -> dict | None:
    path = draft_dir(root, draft_id_) / "draft.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def set_status(root: Path, record: dict, status: str, now: datetime, note: str = "") -> dict:
    record["status"] = status
    record.setdefault("history", []).append({"status": status, "at": now.isoformat(), "note": note})
    save(root, record)
    return record
