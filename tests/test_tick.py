"""tick(10분마다 깨어나 할 일 판단)과 generate_window_minutes 설정 검사."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from newsbot.config import AccountConfig, load_account
from newsbot.storage import drafts
from newsbot.tick import decide

KST = timezone(timedelta(hours=9))


def kst(hour: int, minute: int, day: int = 6) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=KST).astimezone(timezone.utc)


@pytest.fixture
def account():
    # slots 07:30·12:00·18:00·21:30, 창 30~90분. posts_per_slot은 설정이 2로 바뀌어도 테스트가 흔들리지 않게 1로 고정
    # slots도 고정: 실제 게시 시각은 바뀔 수 있고(2026-11-11 하루 2번으로 줄임), 여기서는 tick 판단 규칙만 본다
    return load_account("today_news").model_copy(
        update={"posts_per_slot": 1, "slots": ["07:30", "12:00", "18:00", "21:30"]})


def make_record(root, account, status, slot, number=1):
    draft_id = drafts.draft_id(account.account_key, slot, account.timezone, number)
    record = {"id": draft_id, "account": account.account_key, "slot": slot.isoformat(), "slot_label": "x",
              "draft": {"cover_title": "제목", "source_title": "원문", "source_url": "https://a.kr/1"}}
    drafts.set_status(root, record, status, slot - timedelta(hours=1))
    return draft_id


def test_window_setting_is_read_from_yaml(account):
    assert account.generate_window_minutes == (30, 90)
    with pytest.raises(ValidationError, match="generate_window_minutes"):
        AccountConfig.model_validate({**account.model_dump(), "generate_window_minutes": [90, 30]})
    with pytest.raises(ValidationError):
        AccountConfig.model_validate({**account.model_dump(), "generate_lead_minutes": 60})  # 옛 이름은 거부


def test_generate_only_inside_window(account, tmp_path):
    assert decide(account, tmp_path, kst(10, 20)).generate is False   # 12:00까지 100분: 아직
    todo = decide(account, tmp_path, kst(10, 34))                       # 86분 전: 창 안
    assert todo.generate is True and todo.slot_hhmm == "12:00" and todo.publish is False
    assert "12:00" in todo.reasons[0] and "86분 전" in todo.reasons[0]
    assert decide(account, tmp_path, kst(11, 24)).generate is True     # 36분 전: 아직 창 안
    assert decide(account, tmp_path, kst(11, 30)).generate is True     # 정확히 30분 전: 경계 포함
    assert decide(account, tmp_path, kst(11, 30) + timedelta(seconds=1)).generate is False  # 1초라도 넘으면 너무 늦음
    assert decide(account, tmp_path, kst(11, 34)).generate is False    # 26분 전: 너무 늦음
    assert decide(account, tmp_path, kst(23, 0)).generate is False     # 다음 시각(내일 07:30)은 멂


def test_two_slots_in_window_picks_the_one_still_missing(account, tmp_path):
    close = account.model_copy(update={"slots": ["11:40", "12:00"]})   # 게시 간격 20분: 10:40에는 둘 다 창 안
    slot_1140 = drafts.slot_at(close, "11:40", kst(10, 40))
    todo = decide(close, tmp_path, kst(10, 40))
    assert todo.slot_hhmm == "11:40"                                     # 가까운 시각 먼저
    make_record(tmp_path, close, "generated", slot_1140)
    todo = decide(close, tmp_path, kst(10, 40))
    assert todo.generate is True and todo.slot_hhmm == "12:00"           # 11:40 것이 있으면 12:00 것을 만든다


def test_generate_skipped_when_drafts_already_exist(account, tmp_path):
    slot = drafts.slot_at(account, "12:00", kst(10, 34))
    make_record(tmp_path, account, "generated", slot)
    todo = decide(account, tmp_path, kst(10, 34))
    assert todo.generate is False and todo.publish is False
    assert todo.reasons == ["지금은 할 일이 없습니다"]


def test_generate_counts_missing_numbers_when_two_per_slot(account, tmp_path):
    two = account.model_copy(update={"posts_per_slot": 2})
    slot = drafts.slot_at(two, "12:00", kst(10, 34))
    make_record(tmp_path, two, "generated", slot, number=1)
    todo = decide(two, tmp_path, kst(10, 34))
    assert todo.generate is True and "1개 만들기" in todo.reasons[0]


def test_publish_when_a_due_draft_is_waiting_or_approved(account, tmp_path):
    slot = drafts.slot_at(account, "12:00", kst(10, 0))
    waiting = make_record(tmp_path, account, "generated", slot)
    assert decide(account, tmp_path, kst(11, 54)).publish is False      # 아직 게시 시각 전
    assert decide(account, tmp_path, kst(12, 0)).publish is True        # 정각부터 (publish 흐름의 due 조건과 같은 <=)
    todo = decide(account, tmp_path, kst(12, 4))                        # 시각 지남 → 버튼 응답을 읽어 게시/건너뜀
    assert todo.publish is True and waiting in todo.reasons[-1] and "generated" in todo.reasons[-1]
    drafts.set_status(tmp_path, drafts.load(tmp_path, waiting), "approved", kst(11, 0))
    assert decide(account, tmp_path, kst(12, 4)).publish is True


def test_publish_not_needed_for_finished_drafts(account, tmp_path):
    slot = drafts.slot_at(account, "07:30", kst(6, 0))
    for status, number in (("published", 1), ("skipped", 2), ("expired", 3), ("failed", 4)):
        make_record(tmp_path, account, status, slot, number=number)
    todo = decide(account, tmp_path, kst(9, 0))
    assert todo.publish is False and todo.generate is False


def test_generate_and_publish_can_both_be_due(account, tmp_path):
    old = drafts.slot_at(account, "07:30", kst(6, 0))
    make_record(tmp_path, account, "approved", old)
    todo = decide(account, tmp_path, kst(10, 40))   # 07:30 게시가 밀려 있고 12:00 미리보기 창 안
    assert todo.generate is True and todo.publish is True and len(todo.reasons) == 2


def test_outputs_format_for_github_actions(account, tmp_path):
    assert decide(account, tmp_path, kst(10, 34)).as_outputs() == "generate=true\nslot=12:00\npublish=false\n"
    assert decide(account, tmp_path, kst(9, 0)).as_outputs() == "generate=false\nslot=\npublish=false\n"


def test_todo_command_writes_only_key_value_lines_to_output_file(account, tmp_path, monkeypatch, capsys):
    """진짜 `python -m newsbot todo --output 파일`을 부른다. tick.yml이 $GITHUB_OUTPUT으로 받는 그 파일에는
    key=value 줄만 있어야 한다(설명 줄이 섞이면 Actions가 'Invalid format' 오류를 낸다)."""
    import newsbot.__main__ as cli

    # '지금'은 진짜 현재 시각이므로, 60분 뒤가 게시 시각인 계정과 1시간 전에 승인된 초안을 지금 기준으로 만든다
    now = datetime.now(timezone.utc)
    in_60 = (now + timedelta(minutes=60)).astimezone(ZoneInfo(account.timezone))
    live = account.model_copy(update={"slots": [f"{in_60:%H:%M}"]})
    make_record(tmp_path, live, "approved", (now - timedelta(hours=1)).replace(second=0, microsecond=0))

    calls = []
    monkeypatch.setattr(cli, "sync_data", lambda: calls.append("sync"))
    monkeypatch.setattr(cli, "save_data", lambda message: calls.append("save"))
    monkeypatch.setattr(cli, "DRAFTS_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_account", lambda key: live)

    output = tmp_path / "github_output.txt"
    output.write_text("earlier=1\n", encoding="utf-8")   # Actions가 미리 적어 둔 줄 뒤에 덧붙여야 한다
    assert cli.main(["todo", "--account", "today_news", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == f"earlier=1\ngenerate=true\nslot={in_60:%H:%M}\npublish=true\n"
    printed = capsys.readouterr().out
    assert "미리보기 1개 만들기" in printed and "게시 시각이 지난 초안 1개" in printed
    assert "generate=" not in printed   # 설명은 화면에만, key=value는 파일에만
    assert calls == ["sync"]           # 최신 상태는 받아 오고(sync), 읽기만 하므로 저장(save)은 없음


def test_upcoming_slot_uses_account_window(account):
    assert drafts.upcoming_slot(account, kst(6, 0)) == drafts.slot_at(account, "07:30", kst(6, 0))      # 90분: 경계 포함
    assert drafts.upcoming_slot(account, kst(5, 59)) is None                                            # 91분
    assert drafts.upcoming_slot(account, kst(5, 59), max_minutes=150) is not None                       # 직접 준 창 우선
