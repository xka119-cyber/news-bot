"""커밋 직전 검사: .env 파일이나 토큰 문자열이 올라가려 하면 커밋을 막는다.

찾은 값은 출력하지 않고 파일 이름과 줄 번호만 알려준다.
"""

import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newsbot.util.masking import SECRET_PATTERNS  # noqa: E402


def read_env_values(env_path: Path) -> list[tuple[str, str]]:
    """.env의 실제 값 목록. 짧은 값은 일반 글자와 우연히 겹칠 수 있어 제외한다."""
    if not env_path.exists():
        return []
    values = []
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if len(value) >= 8:
                values.append((name.strip(), value))
    return values


def is_forbidden_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name != ".env.example" and (name == ".env" or name.startswith(".env."))


def find_problems(path: str, text: str, env_values: list[tuple[str, str]]) -> list[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(line):
                problems.append(f"{path}:{number} — {label} 형태의 문자열")
        for name, value in env_values:
            if value in line:
                problems.append(f"{path}:{number} — .env의 {name} 값과 같은 문자열")
    return problems


def staged_files() -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    return [name.decode("utf-8") for name in output.split(b"\0") if name]


def staged_text(path: str) -> str | None:
    data = subprocess.run(
        ["git", "show", f":{path}"], cwd=ROOT, capture_output=True, check=True,
    ).stdout
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    env_values = read_env_values(ROOT / ".env")
    problems = []
    for path in staged_files():
        if is_forbidden_file(path):
            problems.append(f"{path} — 비밀값 파일은 커밋할 수 없음")
            continue
        text = staged_text(path)
        if text is not None:
            problems.extend(find_problems(path, text, env_values))
    if problems:
        print("🚫 비밀값이 포함될 수 있어 커밋을 막았습니다.")
        print("\n".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
