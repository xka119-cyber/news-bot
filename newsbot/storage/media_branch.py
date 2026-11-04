"""카드 이미지를 같은 저장소의 media 브랜치에 올려 공개 주소(raw URL)를 만든다.

인스타는 게시할 때 이미지를 공개 주소에서 내려받으므로 필요하다(설계서 4.6).
승인된 초안만 게시 직전에 올린다(승인 전 초안은 공개하지 않음).
media 브랜치는 .media/ 폴더에 따로 꺼내 두고(git worktree) 거기서 커밋·푸시한다.
"""

import shutil
import subprocess
from pathlib import Path

from newsbot.config import ROOT
from newsbot.util.masking import mask_text

BRANCH = "media"
WORKTREE = ROOT / ".media"


class MediaError(RuntimeError):
    pass


def raw_url(repo: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{BRANCH}/{path}"


def _git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise MediaError(f"git {args[0]} 실패: {mask_text(result.stderr.strip())[-300:]}")
    return result.stdout


def _remote_has_branch() -> bool:
    return bool(_git("ls-remote", "--heads", "origin", BRANCH).strip())


def ensure_worktree(worktree: Path = WORKTREE) -> Path:
    """media 브랜치를 .media/에 준비하고 원격 최신 상태로 맞춘다. 원격에 없으면 빈 브랜치로 만든다."""
    if not (worktree / ".git").exists():
        _git("worktree", "prune")
        if _remote_has_branch():
            _git("fetch", "origin", f"{BRANCH}:{BRANCH}")
            _git("worktree", "add", str(worktree), BRANCH)
        else:
            _git("worktree", "add", "--orphan", "-b", BRANCH, str(worktree))
    elif _remote_has_branch():
        _git("pull", "--ff-only", "origin", BRANCH, cwd=worktree)
    return worktree


def upload(files: list[Path], dest: str, repo: str, message: str, worktree: Path = WORKTREE) -> list[str]:
    """files를 media 브랜치의 dest 폴더에 올리고, 순서대로 공개 주소를 돌려준다."""
    ensure_worktree(worktree)
    target = worktree / dest
    target.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, target / path.name)
    _git("add", dest, cwd=worktree)
    if _git("status", "--porcelain", cwd=worktree).strip():
        _git("commit", "-m", message, cwd=worktree)
    _git("push", "-u", "origin", BRANCH, cwd=worktree)
    return [raw_url(repo, f"{dest}/{path.name}") for path in files]
