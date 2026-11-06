"""10분마다 깨어나는 tick 실행이 '지금 할 일이 있는지' 판단한다 (2026-11-06).

왜 이렇게 하나: GitHub Actions의 예약(cron)은 정해진 시각에 오지 않을 수 있다. 2026-11-05~06에는 매번 4~11시간 늦게 왔다.
그래서 정해진 시각 하나에 기대지 않고 10분마다 깨어나 '현재 시각' 기준으로 판단한다. 늦게 와도 그때 맞는 일을 하면 되고,
한 번 빠져도 다음 tick이 이어받는다. generate·publish는 여러 번 돌아도 안전(멱등)하므로 겹쳐도 문제없다.

판단 규칙
- 미리보기(generate): 다음 게시 시각이 설정의 generate_window_minutes(기본 30~90분) 안에 있고, 그 시각 초안이 posts_per_slot보다 적을 때
- 게시(publish): 게시 시각이 지난 초안 중 generated(승인 대기)·approved(승인됨)가 하나라도 있을 때
  (publish가 버튼 응답을 읽어 승인된 것은 올리고, 안 누른 것은 '건너뜀'으로 정리한다)
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from newsbot.config import AccountConfig
from newsbot.storage import drafts

PENDING = ("generated", "approved")


@dataclass
class Todo:
    generate: bool = False
    slot_hhmm: str = ""          # generate가 True일 때 만들 게시 시각(현지 HH:MM). generate --slot 에 그대로 넘긴다
    publish: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_outputs(self) -> str:
        """GitHub Actions의 $GITHUB_OUTPUT 형식(key=value 줄). 다음 단계의 if: 조건이 읽는다."""
        flag = lambda v: "true" if v else "false"  # noqa: E731
        return f"generate={flag(self.generate)}\nslot={self.slot_hhmm}\npublish={flag(self.publish)}\n"


def decide(account: AccountConfig, root: Path, now: datetime) -> Todo:
    todo = Todo()
    # 창 안의 게시 시각을 가까운 순으로 보고, 초안이 아직 모자란 첫 시각을 고른다 (한 번에 한 시각만; 다음 시각은 다음 tick이)
    for slot in drafts.upcoming_slots(account, now):
        ids = [drafts.draft_id(account.account_key, slot, account.timezone, n) for n in range(1, account.posts_per_slot + 1)]
        missing = [i for i in ids if drafts.load(root, i) is None]
        if missing:
            minutes = int((slot - now).total_seconds() // 60)
            todo.generate = True
            todo.slot_hhmm = f"{slot.astimezone(ZoneInfo(account.timezone)):%H:%M}"
            todo.reasons.append(f"{drafts.slot_label(slot, account.timezone)} 게시분 미리보기 {len(missing)}개 만들기 (게시 {minutes}분 전)")
            break

    due = []
    for record in drafts.all_records(root, account.account_key):
        if record.get("status") not in PENDING:
            continue
        try:
            slot_time = datetime.fromisoformat(record["slot"])
        except (KeyError, ValueError):
            continue
        if slot_time <= now:
            due.append(record)
    if due:
        todo.publish = True
        names = ", ".join(f"{r['id']}({r['status']})" for r in due)
        todo.reasons.append(f"게시 시각이 지난 초안 {len(due)}개 처리 (승인된 것은 게시, 아니면 건너뜀): {names}")

    if not todo.reasons:
        todo.reasons.append("지금은 할 일이 없습니다")
    return todo
