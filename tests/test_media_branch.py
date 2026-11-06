"""media 브랜치 저장: 임시 git 저장소 두 개(원격 역할 + 내 작업본)로 실제 git 명령을 돌려 본다."""

import subprocess
from datetime import datetime, timezone

import pytest

from newsbot.storage import media_branch as mb


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    repo = tmp_path / "repo"
    git("init", "-q", "-b", "main", str(repo), cwd=tmp_path)
    for key, value in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"),
                       ("core.hooksPath", "/dev/null")):
        git("config", key, value, cwd=repo)
    (repo / "README.md").write_text("x")
    git("add", ".", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("remote", "add", "origin", str(remote), cwd=repo)
    git("push", "-q", "origin", "main", cwd=repo)
    return remote, repo, tmp_path


def test_state_saved_but_images_only_when_forced(repos):
    remote, repo, tmp = repos
    work = mb.ensure_worktree(repo / ".media", repo)
    draft = work / "drafts" / "acc" / "2026-11-05" / "0730" / "1"
    draft.mkdir(parents=True)
    (draft / "draft.json").write_text("{}")
    (draft / "card_1.jpg").write_bytes(b"jpg")
    (work / "state").mkdir()
    (work / "state" / "history.json").write_text("{}")
    assert mb.save("state", work) is True
    files = git("ls-tree", "-r", "--name-only", "media", cwd=remote).split()
    assert "state/history.json" in files and "drafts/acc/2026-11-05/0730/1/draft.json" in files
    assert not any(f.endswith(".jpg") for f in files)  # 승인 전 이미지는 올라가지 않음

    urls = mb.upload([draft / "card_1.jpg"], "drafts/acc/2026-11-05/0730/1", "o/r", "media", work)
    assert urls == ["https://raw.githubusercontent.com/o/r/media/drafts/acc/2026-11-05/0730/1/card_1.jpg"]
    assert "drafts/acc/2026-11-05/0730/1/card_1.jpg" in git("ls-tree", "-r", "--name-only", "media", cwd=remote)
    assert mb.save("nothing", work) is False  # 바뀐 게 없으면 커밋하지 않음


def test_fresh_checkout_gets_saved_state(repos):
    remote, repo, tmp = repos
    work = mb.ensure_worktree(repo / ".media", repo)
    (work / "state").mkdir()
    (work / "state" / "costs.json").write_text('{"a": 1}')
    mb.save("state", work)
    # Actions처럼 새로 받은 작업본
    clone = tmp / "clone"
    git("clone", "-q", str(remote), str(clone), cwd=tmp)
    git("config", "core.hooksPath", "/dev/null", cwd=clone)
    work2 = mb.ensure_worktree(clone / ".media", clone)
    assert (work2 / "state" / "costs.json").read_text() == '{"a": 1}'


def test_prune_removes_old_days_and_keeps_state(repos):
    remote, repo, tmp = repos
    work = mb.ensure_worktree(repo / ".media", repo)
    for day in ("2026-10-20", "2026-11-04"):
        folder = work / "drafts" / "acc" / day / "0730"
        folder.mkdir(parents=True)
        (folder / "draft.json").write_text("{}")
    (work / "state").mkdir()
    (work / "state" / "history.json").write_text("{}")
    mb.save("state", work)
    clone = tmp / "old"  # 정리 전에 받아 둔 다른 작업본
    git("clone", "-q", "-b", "main", str(remote), str(clone), cwd=tmp)
    git("config", "core.hooksPath", "/dev/null", cwd=clone)
    mb.ensure_worktree(clone / ".media", clone)
    removed = mb.prune(datetime(2026, 11, 5, tzinfo=timezone.utc), 7, work)
    assert removed == ["acc/2026-10-20"]
    files = git("ls-tree", "-r", "--name-only", "media", cwd=remote).split()
    assert "state/history.json" in files and not any("2026-10-20" in f for f in files)
    assert git("rev-list", "--count", "media", cwd=remote).strip() == "1"  # 기록이 커밋 하나로
    # 정리 전 작업본도 다음 실행 때 새 기록을 그대로 받는다(옛 기록을 다시 얹지 않음)
    work2 = mb.ensure_worktree(clone / ".media", clone)
    assert (work2 / "state" / "history.json").exists()
    assert not (work2 / "drafts" / "acc" / "2026-10-20").exists()
    assert git("rev-list", "--count", "HEAD", cwd=work2).strip() == "1"


def test_old_gitignore_gets_mp4_line(repos):
    remote, repo, tmp = repos
    work = mb.ensure_worktree(repo / ".media", repo)
    (work / ".gitignore").write_text("# 옛 설명\ndrafts/**/*.jpg\ndrafts/**/_html/\n", encoding="utf-8")
    mb.ensure_worktree(repo / ".media", repo)
    lines = (work / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "drafts/**/*.mp4" in lines and lines.count("drafts/**/*.jpg") == 1
    assert sum(line.startswith("#") for line in lines) == 1
