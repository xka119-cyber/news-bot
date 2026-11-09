"""릴스·카드뉴스 성과를 인스타 인사이트에서 읽어 표로 보여 주고 out/stats/에 쌓는다 (2026-11-09).

필요 권한: instagram_manage_insights (11-09 토큰에 추가). 토큰 값은 출력하지 않는다.

    .venv/Scripts/python.exe scripts/reel_stats.py            # 최근 릴스 표
    .venv/Scripts/python.exe scripts/reel_stats.py --since 2026-11-09T20:00

머무른 비율 = 100 - reels_skip_rate (3초 안에 넘기지 않은 사람 비율). 목표 50% 이상.
릴스 디자인 판(v1/v2)은 media 브랜치의 초안 기록(reel.media_id → reel.style, 없으면 v1)에서 찾는다.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GRAPH = "https://graph.facebook.com/v26.0/"
REEL_METRICS = "views,reach,ig_reels_avg_watch_time,reels_skip_rate,saved,shares,likes,comments"


def read_env() -> dict:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def get(token: str, path: str, **params) -> dict:
    url = GRAPH + path + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return {"error": error.read().decode("utf-8", "replace")[:200]}


def reel_styles() -> dict[str, str]:
    """media_id → 디자인 판. media 브랜치를 읽기만 한다(로컬 파일은 건드리지 않음)."""
    styles: dict[str, str] = {}
    git = ["git", "-C", str(ROOT / ".media")]
    try:
        subprocess.run(git + ["fetch", "-q", "origin", "media"], check=True, capture_output=True, timeout=60)
        names = subprocess.run(git + ["ls-tree", "-r", "--name-only", "origin/media"], check=True,
                               capture_output=True, text=True, encoding="utf-8").stdout.split()
    except (subprocess.SubprocessError, OSError):
        return styles
    for name in names:
        if name.endswith("/draft.json"):
            raw = subprocess.run(git + ["show", f"origin/media:{name}"], capture_output=True, text=True,
                                 encoding="utf-8").stdout
            try:
                reel = json.loads(raw).get("reel") or {}
            except ValueError:
                continue
            if reel.get("media_id"):
                styles[reel["media_id"]] = reel.get("style", "v1")
    return styles


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="릴스 성과 표")
    parser.add_argument("--since", help="이 시각(KST, 예: 2026-11-09T20:00) 이후 게시물만")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    env = read_env()
    token, user = env["IG_ACCESS_TOKEN"], env["IG_USER_ID"]
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since + "+09:00").astimezone(timezone.utc)
    media = get(token, user + "/media", fields="id,caption,media_product_type,timestamp,permalink", limit=args.limit)
    if "error" in media:
        print("인스타 목록을 읽지 못했어요:", media["error"])
        return 1
    styles = reel_styles()
    account = get(token, user, fields="followers_count,media_count")
    rows = []
    for item in media.get("data", []):
        if item["media_product_type"] != "REELS":
            continue
        posted = datetime.fromisoformat(item["timestamp"].replace("+0000", "+00:00"))
        if since and posted < since:
            continue
        found = get(token, item["id"] + "/insights", metric=REEL_METRICS)
        values = {d["name"]: d["values"][0]["value"] for d in found.get("data", [])}
        rows.append({
            "id": item["id"], "posted": posted.astimezone().strftime("%m-%d %H:%M"), "style": styles.get(item["id"], "v1"),
            "title": (item.get("caption") or "").split("\n")[0][:24], "permalink": item.get("permalink"),
            "views": values.get("views"), "stay_pct": round(100 - values["reels_skip_rate"], 1)
            if values.get("reels_skip_rate") is not None else None,
            "avg_s": round(values["ig_reels_avg_watch_time"] / 1000, 2) if values.get("ig_reels_avg_watch_time") else None,
            "saved": values.get("saved"), "shares": values.get("shares"), "likes": values.get("likes"),
            "error": found.get("error", "")[:80],
        })
    print(f"팔로워 {account.get('followers_count')}명 · 게시물 {account.get('media_count')}개")
    print(f"{'게시':<12}{'판':<4}{'조회':>6}{'머무름%':>9}{'평균초':>7}{'저장':>5}{'공유':>5}{'좋아요':>6}  제목")
    for r in rows:
        print(f"{r['posted']:<12}{r['style']:<4}{str(r['views']):>6}{str(r['stay_pct']):>9}{str(r['avg_s']):>7}"
              f"{str(r['saved']):>5}{str(r['shares']):>5}{str(r['likes']):>6}  {r['title']} {r['error']}")
    out = ROOT / "out" / "stats"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out / f"reels_{stamp}.json").write_text(
        json.dumps({"followers": account.get("followers_count"), "reels": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
