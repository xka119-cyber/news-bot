import logging

from newsbot.util.masking import MaskingFilter, mask_text

# 검사 스크립트에 걸리지 않도록 가짜 토큰은 조각을 이어 붙여 만든다.
FAKE_ANTHROPIC = "sk-" + "ant-" + "a" * 30
FAKE_TELEGRAM = "123456789" + ":" + "b" * 35


def test_masks_token_shapes_completely():
    result = mask_text(f"key={FAKE_ANTHROPIC} bot={FAKE_TELEGRAM}")
    assert result == "key=**** bot=****"


def test_masks_known_values():
    assert mask_text("id is 1789", ["1789"]) == "id is ****"


def test_logging_filter_masks_arguments():
    record = logging.LogRecord("t", logging.INFO, "", 0, "token %s", (FAKE_ANTHROPIC,), None)
    assert MaskingFilter().filter(record)
    assert record.getMessage() == "token ****"
