"""publish 명령의 흐름: 시간이 지난 초안 정리 → 승인된 초안만 게시(카드뉴스, 이어서 릴스) → 결과를 텔레그램으로 보고.

릴스(M7, 2026-11-07)는 같은 승인으로 카드뉴스 다음에 올린다. 릴스가 실패해도 카드뉴스는 그대로 두고,
`publish --draft ID`로 다시 실행하면 이미 올라간 카드는 건너뛰고 릴스만 다시 시도한다.

여러 번 실행해도 안전하다(멱등): 이미 게시·건너뜀·만료된 초안은 다시 건드리지 않는다.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from newsbot.approve.telegram import REMOVE_KEYBOARD, TelegramClient, TelegramError
from newsbot.collect.rss import Article, record_used
from newsbot.config import AccountConfig
from newsbot.publish.instagram import InstagramClient, InstagramError, publish_carousel, publish_reel
from newsbot.storage import drafts
from newsbot.storage.media_branch import MediaError
from newsbot.write.caption import build_alt_texts

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    draft_id: str
    status: str  # published / failed / expired
    note: str = ""
    reel_status: str = ""  # 릴스를 시도했을 때만: published / failed
    reel_note: str = ""


def _notify(tg: TelegramClient, chat_id: int, record: dict, text: str, clear_buttons: bool = False) -> None:
    message_id = record.get("telegram_message_id")
    try:
        if clear_buttons and message_id:
            tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
        reply = {"message_id": message_id, "allow_sending_without_reply": True} if message_id else None
        # 게시·건너뜀 결과를 알릴 때 아래 키보드 버튼도 내린다(그 시각 게시분은 끝났으므로)
        tg.call("sendMessage", chat_id=chat_id, text=text, reply_parameters=reply,
                link_preview_options={"is_disabled": False}, reply_markup=REMOVE_KEYBOARD)
    except TelegramError as error:
        log.warning("텔레그램 보고 실패: %s", error)


def close_approve_all(tg: TelegramClient, chat_id: int, record: dict) -> None:
    """같은 시각의 [모두 승인] 버튼을 없앤다(시간이 지났거나 이미 처리됨). 이미 없어졌으면 조용히 넘어간다."""
    message_id = record.get("approve_all_message_id")
    if not message_id:
        return
    try:
        tg.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    except TelegramError:
        pass


def media_dest(draft_id: str) -> str:
    return drafts.draft_dir(Path("drafts"), draft_id).as_posix()


def ensure_cards(record: dict, account: AccountConfig, root: Path) -> list[Path]:
    """카드 이미지는 승인 전에는 저장소에 올리지 않으므로, 새로 시작한 실행(Actions)에서는 다시 그린다."""
    folder = drafts.draft_dir(root, record["id"])
    files = [folder / name for name in record["cards"]]
    if not all(f.exists() for f in files):
        from newsbot.design.cards import render_cards

        files = render_cards(record["draft"], account, folder, datetime.fromisoformat(record["slot"])).paths
    return files


def wants_reel(record: dict, account: AccountConfig) -> bool:
    """릴스를 (아직) 올려야 하는 초안인가. 릴스 기능을 켜기 전에 만든 초안은 기록에 reel이 없어 올리지 않는다."""
    reel = record.get("reel")
    return account.reels.enabled and bool(reel) and reel.get("status") != "published"


def publish_reel_part(record: dict, account: AccountConfig, root: Path, ig: InstagramClient, upload) -> tuple[str, str]:
    """릴스를 만들고(없으면) 올린다. (상태, 링크 또는 실패 이유)를 돌려주고 기록에 남긴다."""
    reel = record["reel"]
    draft_id = record["id"]
    try:
        if not reel.get("video_url"):
            folder = drafts.draft_dir(root, draft_id)
            video, cover = folder / reel.get("video", "reel.mp4"), folder / reel.get("cover", "reel_cover.jpg")
            if not (video.exists() and cover.exists()):
                from newsbot.design.reels import render_reel

                made = render_reel(record["draft"], account, ensure_cards(record, account, root), folder)
                reel["style"] = made.style  # 성과 비교용: 게시 때 새로 만든 영상은 지금 판(2026-11-09)
                video, cover = made.video, made.cover
            reel["video_url"], reel["cover_url"] = upload([video, cover], media_dest(draft_id),
                                                          account.publish.media_repo, f"media: {draft_id} 릴스")
            drafts.save(root, record)
        progress = reel.setdefault("publish", {})
        result = publish_reel(ig, reel["video_url"], record["caption"], progress, save=lambda: drafts.save(root, record),
                              cover_url=reel.get("cover_url"), share_to_feed=account.reels.share_to_feed)
    except Exception as error:  # noqa: BLE001 — 카드뉴스는 이미 올라갔다: 영상(Chromium·ffmpeg) 어떤 오류든 릴스 실패로 기록하고 보고까지 간다
        log.warning("릴스 실패 %s: %s", draft_id, error)
        reel["status"], reel["note"] = "failed", f"{type(error).__name__}: {error}"[:500]
        drafts.save(root, record)
        return "failed", reel["note"]
    reel.update(status="published", media_id=result.media_id, permalink=result.permalink, note="", recovered=result.recovered)
    drafts.save(root, record)
    return "published", result.permalink


def publish_one(record: dict, account: AccountConfig, root: Path, ig: InstagramClient, tg: TelegramClient,
                chat_id: int, upload, history_path: Path, now: datetime) -> Outcome:
    draft_id = record["id"]
    if record.get("status") != "published":
        try:
            if not record.get("media_urls"):
                files = ensure_cards(record, account, root)
                record["media_urls"] = upload(files, media_dest(draft_id), account.publish.media_repo, f"media: {draft_id}")
                drafts.save(root, record)
            progress = record.setdefault("publish", {})
            result = publish_carousel(ig, record["media_urls"], record["caption"], progress,
                                      save=lambda: drafts.save(root, record),
                                      alt_texts=build_alt_texts(record["draft"], account))
        except (InstagramError, MediaError, OSError) as error:
            drafts.set_status(root, record, "failed", now, note=str(error))
            hint = ""
            if "코드 4)" in str(error) or "코드 4-" in str(error):  # 인스타의 '활동 제한'(자동 게시를 스팸으로 의심). 2026-11-07 21:30에 겪음. 463(토큰 만료) 등은 제외
                hint = ("\n인스타가 자동 게시를 잠깐 의심한 것일 수 있어요. 인스타 앱에 안내가 떠 있으면 '실수예요'를 누르고, "
                        "몇 시간 뒤 Claude에게 '다시 올려줘'라고 하면 돼요(이미 올라가 있으면 다시 올리지 않아요).")
            _notify(tg, chat_id, record, f"❌ 게시 실패 ({record['slot_label']})\n이유: {error}{hint}")
            return Outcome(draft_id, "failed", str(error))

        record["media_id"], record["permalink"] = result.media_id, result.permalink
        progress["recovered"] = result.recovered  # 오류 응답 뒤 되찾은 게시인지는 기록에만 남긴다
        drafts.set_status(root, record, "published", now)
        draft = record["draft"]
        record_used(history_path, [Article(draft["source_name"], draft["source_title"], "", draft["source_url"], now)], now)
        reel_next = "\n🎬 릴스는 이어서 올리는 중이에요" if wants_reel(record, account) else ""
        # 11-08: 카드뉴스는 인스타가 매번 '오류'라고 답하고도 정상 게시돼서, 되찾은 경우에도 평소 문구를 쓴다(사용자가 이상하다고 함)
        # 카드 결과는 바로 알린다(릴스 영상 처리 대기가 길어도 먼저 알 수 있게)
        _notify(tg, chat_id, record, f"📸 올라갔어요! {draft['cover_title']}\n{result.permalink}{reel_next}")

    outcome = Outcome(draft_id, "published", record.get("permalink", ""))
    if wants_reel(record, account):
        outcome.reel_status, outcome.reel_note = publish_reel_part(record, account, root, ig, upload)
        if outcome.reel_status == "published":
            _notify(tg, chat_id, record, f"🎬 릴스도 올라갔어요\n{outcome.reel_note}")
        else:
            _notify(tg, chat_id, record, f"⚠️ 릴스는 실패했어요(카드뉴스는 올라감)\n이유: {outcome.reel_note}\n"
                                         "Claude에게 '릴스 다시 올려줘'라고 하면 릴스만 다시 올려요.")
    return outcome


def run(account: AccountConfig, root: Path, ig: InstagramClient, tg: TelegramClient, chat_id: int, upload,
        history_path: Path, now: datetime, only_id: str | None = None) -> list[Outcome]:
    """only_id를 주면 그 초안만, 게시 시각과 상관없이 처리한다(수동 실행용; 승인돼 있어야 함)."""
    outcomes = []
    for record in drafts.all_records(root, account.account_key):
        if only_id and record["id"] != only_id:
            continue
        due = datetime.fromisoformat(record["slot"]) <= now
        status = record.get("status")
        if status == "generated" and due:
            close_approve_all(tg, chat_id, record)
            if account.approval.on_timeout == "publish":
                drafts.set_status(root, record, "approved", now, note="시간 초과 자동 승인")
                status = "approved"
            else:
                drafts.set_status(root, record, "expired", now, note="승인 없이 게시 시각이 지남")
                _notify(tg, chat_id, record, f"⌛ 승인이 없어 {record['slot_label']} 게시는 건너뛰었어요.", clear_buttons=True)
                outcomes.append(Outcome(record["id"], "expired"))
                continue
        retry_failed = only_id is not None and status == "failed"
        # 카드는 올라갔는데 릴스만 실패한 초안: 직접 실행(publish --draft)에서만 릴스를 다시 시도한다
        retry_reel = only_id is not None and status == "published" and wants_reel(record, account)
        if (status == "approved" and (due or only_id)) or retry_failed or retry_reel:
            outcomes.append(publish_one(record, account, root, ig, tg, chat_id, upload, history_path, now))
        elif only_id:
            outcomes.append(Outcome(record["id"], status or "?", "승인된 초안이 아니라서 게시하지 않았어요"))
    return outcomes
