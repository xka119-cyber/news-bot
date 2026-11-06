"""명령줄 입구: python -m newsbot <명령>"""

import argparse
import os
import sys

from dotenv import load_dotenv
from pydantic import ValidationError

from newsbot.config import ROOT, AccountConfig, load_account

PLANNED = {
    "report": ("게시물 성과 수집·주간 요약", "M8"),
}


def required_env_names(account: AccountConfig) -> list[str]:
    return [
        "ANTHROPIC_API_KEY",
        account.secrets.ig_access_token,
        account.secrets.ig_user_id,
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]


# 초안·상태 파일은 media 브랜치(.media/)에 둔다. Actions처럼 매번 새로 시작해도 이어진다(설계서 4.6).
DATA_DIR = ROOT / ".media"
DRAFTS_ROOT = DATA_DIR / "drafts"
STATE_DIR = DATA_DIR / "state"
TELEGRAM_STATE = STATE_DIR / "telegram.json"
HISTORY = STATE_DIR / "history.json"
COSTS = STATE_DIR / "costs.json"


def sync_data() -> None:
    """media 브랜치를 최신으로 받는다(없으면 만든다)."""
    from newsbot.storage.media_branch import ensure_worktree

    ensure_worktree(DATA_DIR)


def save_data(message: str) -> None:
    """바뀐 초안·상태 파일을 media 브랜치에 올린다. 실패해도 이번 작업 결과는 로컬에 남는다."""
    from newsbot.storage.media_branch import MediaError, save

    try:
        save(message, DATA_DIR)
    except MediaError as error:
        print(f"⚠️ 상태 저장(media 브랜치) 실패: {error}")


def telegram_from_env():
    from newsbot.approve.telegram import TelegramClient

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id.lstrip("-").isdigit():
        raise SystemExit("❌ .env의 TELEGRAM_CHAT_ID가 비어 있거나 숫자가 아닙니다.")
    return TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()), int(chat_id)


def run_generate(account_key: str, slot_hhmm: str | None, mock_llm: bool) -> int:
    """수집 → (게시물 수만큼) 작성 → 카드 → 초안 저장 → 텔레그램 미리보기.
    게시물마다 미리보기·버튼이 따로 가서 하나씩 승인한다. 이미 만든 번호는 건너뛴다."""
    from datetime import datetime, timezone

    from zoneinfo import ZoneInfo

    from newsbot.approve.telegram import TelegramError, preview_text, send_keyboard, send_preview, send_text
    from newsbot.collect.rss import collect, load_history
    from newsbot.design.cards import render_cards
    from newsbot.storage import drafts
    from newsbot.write.caption import build_caption
    from newsbot.write.llm import WriteError, mock_post, write_post

    account = load_account(account_key)
    load_dotenv(ROOT / ".env")
    client, chat_id = telegram_from_env()
    now = datetime.now(timezone.utc)
    slot = drafts.slot_at(account, slot_hhmm, now) if slot_hhmm else drafts.upcoming_slot(account, now)
    if slot is None:
        lo, hi = account.generate_window_minutes
        print(f"가까운 게시 시각({lo}~{hi}분 뒤)이 없어 할 일이 없습니다. 특정 시각 것을 만들려면 --slot HH:MM 을 붙입니다.")
        return 0
    total = account.posts_per_slot
    ids = [drafts.draft_id(account.account_key, slot, account.timezone, n) for n in range(1, total + 1)]
    existing = [r for r in (drafts.load(DRAFTS_ROOT, i) for i in ids) if r]
    if len(existing) == total:
        print(f"이미 초안 {total}개가 있습니다: {ids[0]} 외")
        return 0
    label = drafts.slot_label(slot, account.timezone)
    slot_id = drafts.draft_id(account.account_key, slot, account.timezone)
    chosen_links = {r["draft"]["source_url"] for r in existing}
    avoid = [r["draft"]["source_title"] for r in existing]
    # 최근 3일에 올린 기사와 오늘 다른 시각의 초안 주제도 피한다(다른 기사로 같은 소식이 또 뽑힌 2026-11-05 18:00·21:30)
    avoid += [item["title"] for item in load_history(HISTORY, now)]
    avoid += drafts.recent_titles(DRAFTS_ROOT, account.account_key, now, exclude_ids=set(ids))
    avoid = list(dict.fromkeys(avoid))

    stage = "수집"
    sent = 0
    try:
        candidates = collect(account, history_path=HISTORY).candidates
        for number, draft_id in enumerate(ids, start=1):
            if drafts.load(DRAFTS_ROOT, draft_id):
                continue
            stage = f"{number}번째 작성"
            pool = [c for c in candidates if c.link not in chosen_links]
            if mock_llm:
                result = mock_post(pool)
            else:
                result = write_post(account, pool, COSTS, avoid=avoid)
            chosen_links.add(result.draft["source_url"])
            avoid.append(result.draft["source_title"])
            caption = build_caption(result.draft, account)
            stage = f"{number}번째 카드 디자인"
            folder = drafts.draft_dir(DRAFTS_ROOT, draft_id)
            rendered = render_cards(result.draft, account, folder, slot)
            warnings = result.warnings + rendered.warnings
            reel = None
            if account.reels.enabled:
                stage = f"{number}번째 릴스 영상"
                from newsbot.design.reels import render_reel

                try:
                    reel = render_reel(result.draft, account, rendered.paths, folder)
                    warnings += reel.warnings
                except Exception as error:  # noqa: BLE001 — Chromium(Playwright Error)·ffmpeg 어떤 오류든
                    # 영상이 안 돼도 카드뉴스는 그대로 진행한다. 게시 때 한 번 더 만들어 본다.
                    warnings.append(f"릴스 영상을 만들지 못했어요(게시 때 다시 시도): {error}")
            record = {
                "id": draft_id, "account": account.account_key, "slot": slot.isoformat(), "slot_label": label,
                "number": number, "format": "carousel", "draft": result.draft, "caption": caption,
                "warnings": warnings, "cards": [p.name for p in rendered.paths], "cost_usd": result.cost_usd,
            }
            if account.reels.enabled:
                record["reel"] = {"status": "pending", "video": "reel.mp4", "cover": "reel_cover.jpg"}
            drafts.set_status(DRAFTS_ROOT, record, "generated", now)
            stage = f"{number}번째 텔레그램 전송"
            text = preview_text(result.draft, caption, label, warnings, account.brand_name, order=f"{number}/{total}",
                                reel_seconds=reel.seconds if reel else None)
            record["telegram_message_id"] = send_preview(client, chat_id, rendered.paths, text, draft_id,
                                                         reel=reel.video if reel else None)
            drafts.save(DRAFTS_ROOT, record)
            sent += 1
            print(f"✅ 미리보기 {number}/{total}: {draft_id} 「{result.draft['cover_title']}」 (${result.cost_usd:.4f})")
            for warning in warnings:
                print(f"   ⚠️ {warning}")
        waiting = [r for r in drafts.slot_records(DRAFTS_ROOT, slot_id) if r.get("status") == "generated"]
        if sent and waiting:
            stage = "승인 버튼 전송"
            local = slot.astimezone(ZoneInfo(account.timezone))
            items = [(r["number"], r["draft"]["cover_title"]) for r in waiting]
            send_keyboard(client, chat_id, items, label, f"{local:%H:%M}")
    except (WriteError, TelegramError, OSError) as error:
        print(f"❌ {stage} 단계 실패: {error}")
        try:
            send_text(client, chat_id, f"⚠️ {account.brand_name}: {stage} 단계에서 실패했어요.\n이유: {error}")
        except TelegramError:
            pass
        return 1
    print(f"미리보기 {sent}개를 보냈습니다 ({label} 게시 예정, 하나씩 따로 승인).")
    return 0


def run_todo(account_key: str, output: str | None) -> int:
    """10분마다 깨어나는 tick 작업용: 지금 미리보기·게시를 할 때인지 판단해 GitHub Actions 출력 파일에 적는다 (2026-11-06)."""
    from datetime import datetime, timezone

    from newsbot.tick import decide

    account = load_account(account_key)
    todo = decide(account, DRAFTS_ROOT, datetime.now(timezone.utc))
    for reason in todo.reasons:
        print(("▶ " if todo.generate or todo.publish else "· ") + reason)
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(todo.as_outputs())
    else:
        print(todo.as_outputs(), end="")
    return 0


def run_approvals() -> int:
    """텔레그램 버튼 응답을 읽어 초안 상태에 기록한다."""
    from datetime import datetime, timezone

    from newsbot.approve.responses import process_updates

    load_dotenv(ROOT / ".env")
    client, chat_id = telegram_from_env()
    decisions = process_updates(client, chat_id, DRAFTS_ROOT, TELEGRAM_STATE, datetime.now(timezone.utc))
    if not decisions:
        print("새 버튼 응답이 없습니다.")
    for d in decisions:
        print(f"{'✅' if d.status == 'approved' else '⏭' if d.status == 'skipped' else '·'} {d.draft_id}: {d.status} {d.note}")
    return 0


def run_publish(account_key: str, only_id: str | None) -> int:
    """버튼 응답 읽기 → 시간 지난 미승인 초안 정리 → 승인된 초안 게시 → 텔레그램 보고."""
    from datetime import datetime, timezone

    from newsbot.approve.responses import process_updates
    from newsbot.publish import flow
    from newsbot.publish.instagram import InstagramClient, InstagramError
    from newsbot.storage.media_branch import upload

    account = load_account(account_key)
    load_dotenv(ROOT / ".env")
    tg, chat_id = telegram_from_env()
    now = datetime.now(timezone.utc)
    for d in process_updates(tg, chat_id, DRAFTS_ROOT, TELEGRAM_STATE, now):
        print(f"버튼 응답: {d.draft_id} → {d.status} {d.note}")
    try:
        ig = InstagramClient(
            os.environ.get(account.secrets.ig_access_token, "").strip(),
            os.environ.get(account.secrets.ig_user_id, "").strip(),
            account.publish.graph_host, account.publish.api_version,
        )
    except InstagramError as error:
        print(f"❌ {error}")
        return 1
    outcomes = flow.run(account, DRAFTS_ROOT, ig, tg, chat_id, upload, HISTORY, now, only_id)
    if not outcomes:
        print("게시할 승인 초안이 없습니다.")
    icons = {"published": "📸", "failed": "❌", "expired": "⌛"}
    for o in outcomes:
        print(f"{icons.get(o.status, '·')} {o.draft_id}: {o.status} {o.note}")
        if o.reel_status:
            print(f"   🎬 릴스: {o.reel_status} {o.reel_note}")
    return 1 if any(o.status == "failed" or o.reel_status == "failed" for o in outcomes) else 0


def run_token_check(account_key: str) -> int:
    """인스타 토큰이 살아 있는지 확인하고, 문제가 있으면 텔레그램으로 알린다.
    시스템 사용자 토큰은 만료되지 않아 설계서의 60일 갱신 대신 점검만 한다(2026-11-04 결정)."""
    from newsbot.approve.telegram import TelegramError, send_text
    from newsbot.publish.instagram import InstagramClient, InstagramError

    account = load_account(account_key)
    load_dotenv(ROOT / ".env")
    tg, chat_id = telegram_from_env()
    try:
        ig = InstagramClient(
            os.environ.get(account.secrets.ig_access_token, "").strip(),
            os.environ.get(account.secrets.ig_user_id, "").strip(),
            account.publish.graph_host, account.publish.api_version,
        )
        name = ig.request("GET", ig.user_id, fields="username").get("username", "?")
        left = ig.quota_left()
    except InstagramError as error:
        print(f"❌ 인스타 토큰 점검 실패: {error}")
        try:
            send_text(tg, chat_id, f"⚠️ {account.brand_name}: 인스타 연결 점검에 실패했어요.\n이유: {error}\n"
                                   "설명서(RUNBOOK)의 '문제 해결'을 확인해 주세요.")
        except TelegramError:
            pass
        return 1
    print(f"✅ 인스타 연결 정상: {name} (오늘 남은 게시 한도 {left}회)")
    return 0


def run_prune(keep_days: int) -> int:
    """오래된 초안 폴더를 지우고 media 브랜치 기록을 새로 만든다(상태 파일은 유지)."""
    from datetime import datetime, timezone

    from newsbot.storage.media_branch import MediaError, prune

    try:
        removed = prune(datetime.now(timezone.utc), keep_days, DATA_DIR)
    except MediaError as error:
        print(f"❌ 정리 실패: {error}")
        return 1
    print(f"✅ 정리 완료: {len(removed)}일치 삭제 {removed}")
    return 0


def run_check(account_key: str) -> int:
    """설정 파일과 .env를 점검한다. 비밀값은 입력 여부만 표시하고 값은 출력하지 않는다."""
    try:
        account = load_account(account_key)
    except (FileNotFoundError, ValidationError) as error:
        print(f"❌ 설정 파일 오류: {error}")
        return 1
    print(f"✅ 설정 파일 정상: {account.account_key} ({account.brand_name})")

    load_dotenv(ROOT / ".env")
    missing = 0
    for name in required_env_names(account):
        if os.environ.get(name, "").strip():
            print(f"✅ {name}: 입력됨")
        else:
            print(f"❌ {name}: 비어 있음")
            missing += 1
    if missing:
        print(f"\n아직 {missing}개가 비어 있습니다. .env 파일에 값을 넣어 주세요.")
        return 1
    print("\n모든 값이 입력됐습니다.")
    return 0


def run_collect(account_key: str, show_excluded: bool) -> int:
    """RSS에서 기사 후보를 모아 표로 보여 준다."""
    from zoneinfo import ZoneInfo

    from newsbot.collect.rss import MIN_CANDIDATES, collect

    account = load_account(account_key)
    result = collect(account, history_path=HISTORY)
    tz = ZoneInfo(account.timezone)

    print(f"기사 {result.fetched}개 받음 → 최근 {account.collect.lookback_hours}시간 {result.recent}개")
    print(f"→ 중복 {result.duplicates}개, 최근 3일 사용 {result.used_before}개, 제외 {len(result.excluded)}개 빼고")
    print(f"→ 후보 {len(result.candidates)}개 (언론사별로 고르게)\n")
    print("| # | 언론사 | 시각 | 제목 |")
    print("|---|---|---|---|")
    for number, article in enumerate(result.candidates, start=1):
        when = article.published.astimezone(tz).strftime("%m-%d %H:%M")
        print(f"| {number} | {article.source} | {when} | {article.title} |")

    if show_excluded and result.excluded:
        print("\n제외된 기사:")
        for article, reason in result.excluded:
            print(f"- [{reason}] {article.source}: {article.title}")
    if result.failed_feeds:
        print(f"\n⚠️ 열리지 않은 RSS: {', '.join(result.failed_feeds)}")
    if len(result.candidates) < MIN_CANDIDATES:
        print(f"\n⚠️ 후보가 {MIN_CANDIDATES}개보다 적습니다. RSS를 더하거나 lookback_hours를 늘려야 할 수 있어요.")
        return 1
    return 0


def run_write(account_key: str, mock_llm: bool) -> int:
    """수집 → Claude 작성 → 검사 결과와 캡션을 보여 주고 out/에 저장한다."""
    import json

    from newsbot.collect.rss import collect
    from newsbot.write.caption import build_caption
    from newsbot.write.llm import WriteError, mock_post, month_key, month_spent, write_post

    account = load_account(account_key)
    load_dotenv(ROOT / ".env")
    candidates = collect(account, history_path=HISTORY).candidates
    print(f"후보 {len(candidates)}개 모음\n")
    costs_path = COSTS
    try:
        result = mock_post(candidates) if mock_llm else write_post(account, candidates, costs_path)
    except WriteError as error:
        print(f"❌ 작성 실패: {error}")
        return 1

    draft = result.draft
    caption = build_caption(draft, account)
    print(f"■ [{draft['category']}] {draft['cover_title']}  ({draft['source_name']})")
    print(f"  {draft['cover_sub']}\n")
    for point in draft["points"]:
        print(f"▸ {point['title']}")
        for line in point["lines"]:
            print(f"   · {line}")
    print(f"\n💡 {draft['why_it_matters']}\n")
    print(f"원문: {draft['source_title']}\n")
    print("— 캡션 —")
    print(caption)
    print()
    for warning in result.warnings:
        print(f"⚠️ {warning}")
    if not mock_llm:
        from datetime import datetime, timezone

        total = month_spent(costs_path, account.account_key, month_key(account, datetime.now(timezone.utc)))
        print(
            f"💰 이번 작성: ${result.cost_usd:.5f} (입력 {result.input_tokens} · 출력 {result.output_tokens} 토큰, "
            f"{result.attempts}회 호출) / 이번 달 누적 ${total:.4f} (예산 ${account.write.monthly_budget_usd:.2f})"
        )

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "draft.json").write_text(
        json.dumps({**draft, "warnings": result.warnings}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "caption.txt").write_text(caption, encoding="utf-8")
    print("\n저장: out/draft.json, out/caption.txt")
    return 0


def run_cards(account_key: str, template: str | None) -> int:
    """out/draft.json(마지막 작성 결과)으로 카드 4장을 out/cards/에 만든다."""
    import json
    from datetime import datetime, timezone

    from newsbot.design.cards import render_cards

    account = load_account(account_key)
    draft_path = ROOT / "out" / "draft.json"
    if not draft_path.exists():
        print("❌ out/draft.json이 없습니다. 먼저 write 명령을 실행하세요.")
        return 1
    draft = json.loads(draft_path.read_text(encoding="utf-8"))
    result = render_cards(draft, account, ROOT / "out" / "cards", datetime.now(timezone.utc), template=template)
    for path in result.paths:
        print(f"✅ {path.relative_to(ROOT)}")
    for warning in result.warnings:
        print(f"⚠️ {warning}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsbot", description="인스타 카드뉴스 자동화 엔진")
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")
    check = commands.add_parser("check", help="설정 파일과 .env 입력 여부 점검 (값은 표시 안 함)")
    check.add_argument("--account", default="today_news")
    gather = commands.add_parser("collect", help="RSS에서 기사 후보를 모아 표로 출력")
    gather.add_argument("--account", default="today_news")
    gather.add_argument("--show-excluded", action="store_true", help="제외된 기사와 이유도 출력")
    write = commands.add_parser("write", help="수집 후 Claude가 기사 1개를 골라 카드 문구·캡션 작성")
    write.add_argument("--account", default="today_news")
    write.add_argument("--mock-llm", action="store_true", help="Claude를 부르지 않고 샘플 문구 사용(비용 0)")
    gen = commands.add_parser("generate", help="수집 → 작성 → 카드 → 텔레그램 미리보기 전송")
    gen.add_argument("--account", default="today_news")
    gen.add_argument("--slot", default=None, help="게시 시각 HH:MM (비우면 설정의 generate_window_minutes 안의 시각을 자동 선택)")
    gen.add_argument("--mock-llm", action="store_true", help="Claude 대신 샘플 문구(비용 0)")
    todo = commands.add_parser("todo", help="(tick 작업용) 지금 미리보기·게시를 할 때인지 판단해 generate=/slot=/publish= 줄로 출력")
    todo.add_argument("--account", default="today_news")
    todo.add_argument("--output", default=None, help="결과를 덧붙여 쓸 파일 (GitHub Actions의 $GITHUB_OUTPUT). 비우면 화면에 출력")
    commands.add_parser("approvals", help="텔레그램 버튼 응답을 읽어 초안 상태에 기록")
    pub = commands.add_parser("publish", help="승인된 초안을 인스타에 게시하고 링크를 텔레그램으로 보고")
    pub.add_argument("--account", default="today_news")
    pub.add_argument("--draft", default=None, help="이 초안만 시각과 상관없이 게시 (예: today_news/20261104-2130)")
    token = commands.add_parser("token-check", help="인스타 토큰·연결 점검 (문제 있으면 텔레그램 알림)")
    token.add_argument("--account", default="today_news")
    prune = commands.add_parser("prune", help="오래된 초안 정리, media 브랜치 용량 줄이기")
    prune.add_argument("--keep-days", type=int, default=7)
    cards = commands.add_parser("cards", help="마지막 작성 결과(out/draft.json)로 카드 4장 만들기")
    cards.add_argument("--account", default="today_news")
    cards.add_argument("--template", default=None, help="시안 이름(sticker/clean/bold). 비우면 설정값")
    for name, (description, milestone) in PLANNED.items():
        commands.add_parser(name, help=f"{description} ({milestone}에서 구현)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return run_check(args.account)
    if args.command == "token-check":
        return run_token_check(args.account)
    if args.command in ("collect", "write", "generate", "approvals", "publish", "prune", "todo"):
        sync_data()  # 사용한 기사·비용·승인 상태를 media 브랜치에서 최신으로
    if args.command == "todo":
        return run_todo(args.account, args.output)  # 읽기만 하므로 저장(save_data)은 없음
    if args.command == "collect":
        return run_collect(args.account, args.show_excluded)
    if args.command == "write":
        code = run_write(args.account, args.mock_llm)
        save_data("state: 작성 비용 기록")
        return code
    if args.command in ("generate", "approvals", "publish"):
        try:
            if args.command == "generate":
                return run_generate(args.account, args.slot, args.mock_llm)
            if args.command == "approvals":
                return run_approvals()
            return run_publish(args.account, args.draft)
        finally:
            save_data(f"state: {args.command}")
    if args.command == "prune":
        return run_prune(args.keep_days)
    if args.command == "cards":
        return run_cards(args.account, args.template)
    print(f"'{args.command}' 명령은 {PLANNED[args.command][1]}에서 구현됩니다.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
