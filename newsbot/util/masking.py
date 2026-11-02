"""비밀값 가리기. 표준 라이브러리만 사용한다(pre-commit 검사에서도 쓰기 때문)."""

import logging
import re
from collections.abc import Iterable

MASK = "****"

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "Anthropic 키": re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    "텔레그램 봇 토큰": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}"),
    "인스타 토큰": re.compile(r"\b(?:IGAA|IGQV)[A-Za-z0-9_-]{30,}"),
    "메타 토큰": re.compile(r"\bEAA[A-Za-z0-9]{30,}"),
    "GitHub 토큰": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
}


def mask_text(text: str, known_values: Iterable[str] = ()) -> str:
    """알려진 비밀값과 토큰 형태의 문자열을 모두 ****로 바꾼다."""
    for value in sorted((v for v in known_values if v), key=len, reverse=True):
        text = text.replace(value, MASK)
    for pattern in SECRET_PATTERNS.values():
        text = pattern.sub(MASK, text)
    return text


class MaskingFilter(logging.Filter):
    """로그 메시지가 출력되기 전에 비밀값을 가린다."""

    def __init__(self, known_values: Iterable[str] = ()) -> None:
        super().__init__()
        self.known_values = tuple(known_values)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_text(record.getMessage(), self.known_values)
        record.args = ()
        return True
