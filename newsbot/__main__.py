"""명령줄 입구: python -m newsbot <명령>"""

import argparse
import os
import sys

from dotenv import load_dotenv
from pydantic import ValidationError

from newsbot.config import ROOT, AccountConfig, load_account

PLANNED = {
    "publish": ("승인된 초안을 인스타에 게시", "M5"),
    "refresh-token": ("인스타 토큰 만료일 확인·갱신", "M6"),
    "prune": ("오래된 미디어 정리", "M6"),
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


DRAFTS_ROOT = ROOT / "state" / "drafts"
TELEGRAM_STATE = ROOT / "state" / "telegram.json"


def telegram_from_env():
    from newsbot.approve.telegram import TelegramClient

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id.lstrip("-").isdigit():
        raise SystemExit("❌ .env의 TELEGRAM_CHAT_ID가 비어 있거나 숫자가 아닙니다.")
    return TelegramClient(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()), int(chat_id)


def run_generate(account_key: str, slot_hhmm: str | None, mock_llm: bool) -> int:
    """수집 → 작성 → 카드 → 초안 저장 → 텔레그램 미리보기. 같은 시각 초안이 이미 있으면 건너뛴다."""
    from datetime import datetime, timezone

    from newsbot.approve.telegram import TelegramError, preview_text, send_preview, send_text
    from newsbot.collect.rss import collect
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
        print("가까운 게시 시각(30~150분 뒤)이 없어 할 일이 없습니다.")
        return 0
    draft_id = drafts.draft_id(account.account_key, slot, account.timezone)
    if drafts.load(DRAFTS_ROOT, draft_id):
        print(f"이미 초안이 있습니다: {draft_id}")
        return 0

    stage = "수집"
    try:
        candidates = collect(account, history_path=ROOT / "state" / "history.json").candidates
        stage = "작성"
        result = mock_post(candidates) if mock_llm else write_post(account, candidates, ROOT / "state" / "costs.json")
        caption = build_caption(result.draft, account)
        stage = "카드 디자인"
        folder = drafts.draft_dir(DRAFTS_ROOT, draft_id)
        rendered = render_cards(result.draft, account, folder, slot)
        warnings = result.warnings + rendered.warnings
        label = drafts.slot_label(slot, account.timezone)
        record = {
            "id": draft_id, "account": account.account_key, "slot": slot.isoformat(), "slot_label": label,
            "format": "carousel", "draft": result.draft, "caption": caption, "warnings": warnings,
            "cards": [p.name for p in rendered.paths], "cost_usd": result.cost_usd,
        }
        drafts.set_status(DRAFTS_ROOT, record, "generated", now)
        stage = "텔레그램 전송"
        record["telegram_message_id"] = send_preview(
            client, chat_id, rendered.paths, preview_text(result.draft, caption, label, warnings, account.brand_name), draft_id
        )
        drafts.save(DRAFTS_ROOT, record)
    except (WriteError, TelegramError, OSError) as error:
        print(f"❌ {stage} 단계 실패: {error}")
        try:
            send_text(client, chat_id, f"⚠️ {account.brand_name}: {stage} 단계에서 실패했어요.\n이유: {error}")
        except TelegramError:
            pass
        return 1
    print(f"✅ 미리보기를 보냈습니다: {draft_id} ({label} 게시 예정)")
    for warning in warnings:
        print(f"⚠️ {warning}")
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
    result = collect(account, history_path=ROOT / "state" / "history.json")
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
    candidates = collect(account, history_path=ROOT / "state" / "history.json").candidates
    print(f"후보 {len(candidates)}개 모음\n")
    costs_path = ROOT / "state" / "costs.json"
    try:
        result = mock_post(candidates) if mock_llm else write_post(account, candidates, costs_path)
    except WriteError as error:
        print(f"❌ 작성 실패: {error}")
        return 1

    draft = result.draft
    caption = build_caption(draft, account)
    print(f"■ {draft['hook']}\n")
    for n, item in enumerate(draft["items"], start=1):
        print(f"{n}. [{item['category']}] {item['headline']}  ({item['source_name']})")
        for line in item["summary_lines"]:
            print(f"   · {line}")
        print(f"   → {item['why_it_matters']}\n")
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
    write = commands.add_parser("write", help="수집 후 Claude가 기사 3개를 골라 카드 문구·캡션 작성")
    write.add_argument("--account", default="today_news")
    write.add_argument("--mock-llm", action="store_true", help="Claude를 부르지 않고 샘플 문구 사용(비용 0)")
    gen = commands.add_parser("generate", help="수집 → 작성 → 카드 → 텔레그램 미리보기 전송")
    gen.add_argument("--account", default="today_news")
    gen.add_argument("--slot", default=None, help="게시 시각 HH:MM (비우면 30~150분 뒤 시각을 자동 선택)")
    gen.add_argument("--mock-llm", action="store_true", help="Claude 대신 샘플 문구(비용 0)")
    commands.add_parser("approvals", help="텔레그램 버튼 응답을 읽어 초안 상태에 기록")
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
    if args.command == "collect":
        return run_collect(args.account, args.show_excluded)
    if args.command == "write":
        return run_write(args.account, args.mock_llm)
    if args.command == "generate":
        return run_generate(args.account, args.slot, args.mock_llm)
    if args.command == "approvals":
        return run_approvals()
    if args.command == "cards":
        return run_cards(args.account, args.template)
    print(f"'{args.command}' 명령은 {PLANNED[args.command][1]}에서 구현됩니다.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
