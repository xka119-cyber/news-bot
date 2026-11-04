"""같은 저장소의 media 브랜치에 초안·상태 파일과 게시할 카드 이미지를 둔다(설계서 4.6).

- media 브랜치는 .media/ 폴더에 따로 꺼내 두고(git worktree) 거기서 읽고 쓰고 커밋·푸시한다.
  GitHub Actions처럼 매번 새로 시작하는 곳에서도 승인 상태·사용한 기사·비용이 이어진다.
- 카드 이미지(jpg)는 평소 커밋하지 않고(.gitignore), 승인된 초안만 게시 직전에 올려 공개 주소를 만든다.
"""

import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from newsbot.config import ROOT
from newsbot.util.masking import mask_text

BRANCH = "media"
WORKTREE = ROOT / ".media"
MEDIA_GITIGNORE = "# 카드 이미지는 승인된 것만 게시 직전에 git add -f로 올린다\ndrafts/**/*.jpg\ndrafts/**/_html/\n"


class MediaError(RuntimeError):
    pass


def raw_url(repo: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{BRANCH}/{path}"


def _git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise MediaError(f"git {args[0]} 실패: {mask_text(result.stderr.strip())[-300:]}")
    return result.stdout


def _remote_has_branch(repo: Path) -> bool:
    return bool(_git("ls-remote", "--heads", "origin", BRANCH, cwd=repo).strip())


def ensure_worktree(worktree: Path = WORKTREE, repo: Path = ROOT) -> Path:
    """media 브랜치를 .media/에 준비하고 원격 최신 상태로 맞춘다. 원격에 없으면 빈 브랜치로 만든다."""
    if not (worktree / ".git").exists():
        _git("worktree", "prune", cwd=repo)
        if _remote_has_branch(repo):
            _git("fetch", "origin", f"+{BRANCH}:{BRANCH}", cwd=repo)
            _git("worktree", "add", "-f", str(worktree), BRANCH, cwd=repo)
        else:
            _git("worktree", "add", "--orphan", "-b", BRANCH, str(worktree), cwd=repo)
    elif _remote_has_branch(worktree):
        _git("fetch", "origin", BRANCH, cwd=worktree)
        try:
            _git("merge-base", "HEAD", f"origin/{BRANCH}", cwd=worktree)
        except MediaError:
            # 주간 정리(prune)로 원격 기록이 새로 만들어졌다 → 원격을 그대로 받는다
            _git("reset", "-q", "--hard", f"origin/{BRANCH}", cwd=worktree)
        else:
            # 아직 못 올린 내 커밋·변경은 지키면서 원격 최신 위에 얹는다
            _git("rebase", "-q", "--autostash", f"origin/{BRANCH}", cwd=worktree)
    ignore = worktree / ".gitignore"
    if not ignore.exists():
        ignore.write_text(MEDIA_GITIGNORE, encoding="utf-8")
    return worktree


def save(message: str, worktree: Path = WORKTREE, force_paths: list[Path] = ()) -> bool:
    """바뀐 파일을 커밋·푸시한다. 다른 실행이 먼저 올렸으면 그 위에 다시 얹어 한 번 더 시도한다."""
    _git("add", "-A", cwd=worktree)
    for path in force_paths:
        _git("add", "-f", str(path.relative_to(worktree)), cwd=worktree)
    if not _git("status", "--porcelain", cwd=worktree).strip():
        return False
    _git("commit", "-q", "-m", message, cwd=worktree)
    for attempt in range(3):
        try:
            _git("push", "-q", "-u", "origin", BRANCH, cwd=worktree)
            return True
        except MediaError:
            if attempt == 2:
                raise
            _git("pull", "-q", "--rebase", "origin", BRANCH, cwd=worktree)
    return True


def upload(files: list[Path], dest: str, repo: str, message: str, worktree: Path = WORKTREE) -> list[str]:
    """files를 media 브랜치의 dest 폴더에 올리고(이미 그 자리에 있으면 그대로), 순서대로 공개 주소를 돌려준다."""
    target = worktree / dest
    target.mkdir(parents=True, exist_ok=True)
    placed = []
    for path in files:
        out = target / path.name
        if path.resolve() != out.resolve():
            shutil.copy2(path, out)
        placed.append(out)
    save(message, worktree, force_paths=placed)
    return [raw_url(repo, f"{dest}/{path.name}") for path in files]


def old_draft_days(drafts_root: Path, now: datetime, keep_days: int) -> list[Path]:
    """keep_days보다 오래된 날짜 폴더들 (drafts/{계정}/{YYYY-MM-DD})."""
    cutoff = (now - timedelta(days=keep_days)).date()
    old = []
    for day in sorted(drafts_root.glob("*/*")):
        try:
            if day.is_dir() and datetime.strptime(day.name, "%Y-%m-%d").date() < cutoff:
                old.append(day)
        except ValueError:
            continue
    return old


def prune(now: datetime, keep_days: int = 7, worktree: Path = WORKTREE) -> list[str]:
    """오래된 초안 폴더를 지우고, 브랜치 기록을 새 커밋 하나로 다시 만들어 저장소 용량을 줄인다(상태 파일은 유지)."""
    removed = old_draft_days(worktree / "drafts", now, keep_days)
    for day in removed:
        shutil.rmtree(day)
    _git("add", "-A", cwd=worktree)
    _git("checkout", "-q", "--orphan", "media-pruned", cwd=worktree)
    _git("commit", "-q", "--allow-empty", "-m", f"media: 정리 ({now:%Y-%m-%d}, {len(removed)}일치 삭제)", cwd=worktree)
    _git("branch", "-D", BRANCH, cwd=worktree)
    _git("branch", "-m", BRANCH, cwd=worktree)
    _git("push", "-q", "-f", "-u", "origin", BRANCH, cwd=worktree)
    return [f"{d.parent.name}/{d.name}" for d in removed]
