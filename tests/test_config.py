import copy

import pytest
import yaml
from pydantic import ValidationError

from newsbot.config import ACCOUNTS_DIR, AccountConfig, load_account


@pytest.fixture
def raw():
    return yaml.safe_load((ACCOUNTS_DIR / "today_news.yaml").read_text(encoding="utf-8"))


def test_today_news_loads():
    account = load_account("today_news")
    assert account.account_key == "today_news"
    assert account.slots == ["07:30", "12:00", "18:00", "21:30"]
    assert account.design.card_size == (1080, 1350)


def test_missing_account_file():
    with pytest.raises(FileNotFoundError):
        load_account("no_such_account")


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["caption"].update(hashtags=[f"#t{i}" for i in range(6)]),
        lambda c: c["caption"].update(hashtags=["태그"]),
        lambda c: c.update(slots=["25:00", "12:00", "18:00", "21:30"]),
        lambda c: c.update(slots=["07:30", "07:30", "18:00", "21:30"]),
        lambda c: c["approval"].update(on_timeout="later"),
        lambda c: c["design"]["colors"].update(bg="white"),
        lambda c: c["secrets"].update(ig_access_token="real-token-value"),
        lambda c: c.update(format_plan={"week1": {"07:30": "reel"}, "week2": {"07:30": "reel"}}),
        lambda c: c["reels"].update(beat_seconds=0.5),
        lambda c: c.update(unknown_setting=True),
        lambda c: c.update(timezone="Asia/Nowhere"),
    ],
)
def test_invalid_settings_are_rejected(raw, change):
    broken = copy.deepcopy(raw)
    change(broken)
    with pytest.raises(ValidationError):
        AccountConfig.model_validate(broken)
