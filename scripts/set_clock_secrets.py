"""`.env`의 비밀값을 Cloudflare Worker(뉴스봇 시계, clock/)의 비밀값으로 옮긴다. 값은 화면에 절대 찍지 않는다.

사용:  python scripts/set_clock_secrets.py          # .env에 있는 값을 모두 옮기고, 등록된 이름 목록을 보여 준다
       python scripts/set_clock_secrets.py --check  # 옮기지 않고 .env에 있는지만 본다

옮기는 값: GH_DISPATCH_TOKEN(GitHub를 깨우는 토큰), TELEGRAM_BOT_TOKEN·TELEGRAM_CHAT_ID(깨우기 실패 알림용).
wrangler가 로그인돼 있어야 한다 (처음 한 번 clock/ 에서 `npx wrangler login`). 값은 wrangler에 표준 입력으로만 넘긴다.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
CLOCK = ROOT / "clock"
NAMES = ("GH_DISPATCH_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
NPX = "npx.cmd" if os.name == "nt" else "npx"


def mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "****")
    return text


def wrangler(args: list[str], value: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [NPX, "wrangler", *args], cwd=CLOCK, input=value, text=True, capture_output=True,
        encoding="utf-8", errors="replace",
    )


def put(name: str, value: str, secrets: list[str]) -> bool:
    proc = wrangler(["secret", "put", name], value)
    ok = proc.returncode == 0
    lines = (proc.stdout + "\n" + proc.stderr).strip().splitlines()
    tail = mask(lines[-1].strip(), secrets) if lines else ""
    print(f"{'✅' if ok else '❌'} {name}: {'Cloudflare에 저장됨' if ok else '저장 실패'} — {tail}")
    return ok


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="set_clock_secrets.py",
        description=".env의 시계용 비밀값(GH_DISPATCH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)을 Cloudflare Worker로 옮긴다. 값은 화면에 나오지 않는다.",
    )
    parser.add_argument("--check", action="store_true", help="옮기지 않고 .env에 값이 있는지만 본다")
    return parser.parse_args(argv)   # 모르는 옵션(--chek 등)은 안내문을 찍고 종료 코드 2로 끝난다


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    values = {k: (v or "").strip() for k, v in dotenv_values(ROOT / ".env", encoding="utf-8-sig").items()}
    present = [n for n in NAMES if values.get(n)]
    missing = [n for n in NAMES if not values.get(n)]
    for name in NAMES:
        print(f"{'✅' if name in present else '❌'} .env {name}: {'있음' if name in present else '없음'}")
    if args.check:
        return 0 if not missing else 1
    if not present:
        print("옮길 값이 없습니다.")
        return 1
    secrets = [values[n] for n in present]
    results = [put(name, values[name], secrets) for name in present]
    listing = wrangler(["secret", "list"])
    print("Cloudflare에 등록된 비밀값 이름:", mask(" ".join(listing.stdout.split()), secrets) or mask(listing.stderr.strip(), secrets))
    if "GH_DISPATCH_TOKEN" in missing:
        print("⚠️ GH_DISPATCH_TOKEN이 .env에 없어 시계는 아직 GitHub를 깨우지 않습니다. RUNBOOK '시계 켜기' 순서대로 넣은 뒤 다시 실행하세요.")
        return 1
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
