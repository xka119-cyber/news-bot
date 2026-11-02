"""명령줄 입구: python -m newsbot <명령>"""

import argparse
import os
import sys

from dotenv import load_dotenv
from pydantic import ValidationError

from newsbot.config import ROOT, AccountConfig, load_account

PLANNED = {
    "generate": ("수집 → 작성 → 디자인 → 텔레그램 미리보기 전송", "M1~M4"),
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
    ]


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="newsbot", description="인스타 카드뉴스 자동화 엔진")
    commands = parser.add_subparsers(dest="command", required=True, metavar="명령")
    check = commands.add_parser("check", help="설정 파일과 .env 입력 여부 점검 (값은 표시 안 함)")
    check.add_argument("--account", default="today_news")
    for name, (description, milestone) in PLANNED.items():
        commands.add_parser(name, help=f"{description} ({milestone}에서 구현)")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "check":
        return run_check(args.account)
    print(f"'{args.command}' 명령은 {PLANNED[args.command][1]}에서 구현됩니다.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
