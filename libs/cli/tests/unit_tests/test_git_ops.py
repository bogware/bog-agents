"""Tests for the consolidated git-command classifier (Feature #10)."""

from __future__ import annotations

import pytest

from bog_agents_cli.git_ops import GitOpType, classify_git_command


class TestNotGit:
    @pytest.mark.parametrize(
        "cmd",
        [
            "",
            "ls -la",
            "npm test",
            "python -m pytest",
            "hub push -f",  # hub is not git
            "echo git status",  # git inside a string literal, not a command
        ],
    )
    def test_non_git_returns_none(self, cmd: str) -> None:
        assert classify_git_command(cmd) is None


class TestReadOnly:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "git log --oneline -10",
            "git diff HEAD",
            "git show abc123",
            "git blame src/main.py",
            "git grep TODO src/",
            "git ls-files",
            "git ls-tree HEAD",
            "git cat-file -p HEAD",
            "git describe --tags",
            "git shortlog -sn",
            "git for-each-ref",
            "git rev-parse HEAD",
            "git count-objects",
            "git help add",
            "git version",
            "git whatchanged -1",
            "git branch -a",
            "git branch --list feature",
            "git tag",
            "git remote -v",
            "git config user.email",
            "git config --get remote.origin.url",
            "git stash list",
            "git reflog",
            "git reflog show HEAD",
            "git worktree list",
            "git clean -n",
            "git clean --dry-run",
        ],
    )
    def test_read_only(self, cmd: str) -> None:
        assert classify_git_command(cmd) is GitOpType.READ_ONLY, cmd


class TestMutating:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git add .",
            "git commit -m 'fix'",
            "git push",
            "git pull",
            "git fetch origin",
            "git merge feature/x",
            "git rebase main",
            "git cherry-pick abc123",
            "git revert abc123",
            "git checkout main",
            "git checkout -b feature/x",
            "git switch feature/x",
            "git switch -c feature/y",
            "git branch feature/z",
            "git branch -m old new",
            "git tag v1.0 -m 'release'",
            "git remote add origin https://example.com/repo.git",
            "git config user.email a@b.c",
            "git config --unset user.email",
            "git stash push",
            "git stash pop",
            "git stash apply",
            "git stash save 'wip'",
            "git clone https://example.com/repo.git",
            "git init",
            "git reset HEAD~1",
            "git reset --soft HEAD~1",
            "git restore --staged file.py",
            "git submodule update",
            "git rm file.py",
            "git mv a.py b.py",
            "git apply patch.diff",
            "git gc",
            "git prune",
            "git worktree add ../wt",
            "git archive -o out.tar HEAD",
            "git bundle create repo.bundle --all",
            "GIT_EDITOR=echo git commit -m 'x'",
            "sudo git commit -m 'x'",
            "git commit",
        ],
    )
    def test_mutating(self, cmd: str) -> None:
        assert classify_git_command(cmd) is GitOpType.MUTATING, cmd


class TestDestructive:
    @pytest.mark.parametrize(
        "cmd",
        [
            "git push -f origin main",
            "git push --force origin main",
            "git push -ff origin main",
            "git push -fff",
            "git push --force-with-lease origin main",
            "git push --force-if-includes origin main",
            # Combined short-option clusters: git accepts these and they force
            # just as hard as `-f`, but prefix matching alone missed them and
            # the auto-mode fallthrough is ALLOW.
            "git push -uf origin main",
            "git push -fq origin main",
            "git push -qfu origin main",
            # `+refspec` forces the update with no flag at all.
            "git push origin +main",
            "git push origin +HEAD:main",
            # Deleting a remote branch is destructive for everyone.
            "git push --delete origin feature",
            "git push -d origin feature",
            "git push origin :feature",
            "git reset --hard HEAD~1",
            "git reset --hard origin/main",
            "git clean -fd",
            "git clean -fdx",
            "git checkout .",
            "git checkout -- file.py",
            "git checkout -- .",
            "git checkout -f main",
            "git branch -D stale",
            "git branch -d stale",
            "git branch --delete stale",
            "git branch -Df stale",
            "git tag -d v1.0",
            "git tag -D v1.0",
            "git stash drop",
            "git stash clear",
            "git filter-branch -- --all",
            "git reflog expire --expire=now --all",
            "git reflog delete HEAD@{0}",
            "git rebase --abort",
            "git merge --abort",
            "git cherry-pick --abort",
            "git submodule update --force",
            "git submodule foreach --recursive rm -rf",
            "cd /tmp && git clean -fdx",
            "sudo git reset --hard",
        ],
    )
    def test_destructive(self, cmd: str) -> None:
        assert classify_git_command(cmd) is GitOpType.DESTRUCTIVE, cmd


class TestChainedCommands:
    def test_riskiest_segment_wins(self) -> None:
        assert (
            classify_git_command("git status && git reset --hard")
            is GitOpType.DESTRUCTIVE
        )
        assert classify_git_command("git status && git log") is GitOpType.READ_ONLY
        assert (
            classify_git_command("git push --force; echo done") is GitOpType.DESTRUCTIVE
        )

    def test_severity_ordering(self) -> None:
        assert GitOpType.READ_ONLY.value == "read_only"
        assert GitOpType.MUTATING.value == "mutating"
        assert GitOpType.DESTRUCTIVE.value == "destructive"


class TestEdgeCases:
    def test_windows_binary(self) -> None:
        assert classify_git_command("git.exe status") is GitOpType.READ_ONLY
        assert classify_git_command("git.exe push -f") is GitOpType.DESTRUCTIVE

    def test_quoted_tokens(self) -> None:
        assert classify_git_command("git commit -m 'fix --force'") is GitOpType.MUTATING
        assert (
            classify_git_command('git log --grep="stash drop"') is GitOpType.READ_ONLY
        )

    def test_force_flag_embedded_in_ref_name_is_not_force(self) -> None:
        assert classify_git_command("git push origin feat/f") is GitOpType.MUTATING
        assert classify_git_command("git push origin -f") is GitOpType.DESTRUCTIVE


class TestWrapperPeelingStaysInSyncWithExecRisk:
    """`git_ops` (CLI) and `exec_risk` (SDK) both peel wrapper prefixes.

    They parse git independently -- which is exactly how they drifted once:
    `git_ops` peeled `sudo`, `exec_risk` did not, so `sudo git -c
    core.pager=... log` hid its exec vector from the SDK analyzer while the
    CLI still classified it as a git command. A real merge is a cross-package
    refactor; until then, pin the overlap so the drift cannot come back
    silently.
    """

    def test_every_git_ops_prefix_is_an_exec_risk_wrapper(self) -> None:
        from bog_agents.exec_risk import _WRAPPERS

        from bog_agents_cli.git_ops import _PREFIX_WORDS

        missing = _PREFIX_WORDS - _WRAPPERS
        assert not missing, (
            f"exec_risk._WRAPPERS is missing {sorted(missing)}; a command behind "
            "that prefix would be classified by git_ops but skipped by the "
            "exec-risk analyzer"
        )

    @pytest.mark.parametrize(
        "prefix", sorted({"sudo", "env", "command", "nohup", "time", "timeout"})
    )
    def test_both_modules_see_through_the_same_prefix(self, prefix: str) -> None:
        from bog_agents.exec_risk import command_has_exec_risk

        # A wrapper must hide neither the risk level nor the exec vector.
        assert (
            classify_git_command(f"{prefix} git push --force") is GitOpType.DESTRUCTIVE
        )
        assert command_has_exec_risk(f"{prefix} git -c core.pager=/tmp/evil log")


class TestGitGlobalOptions:
    """Global options before the subcommand must not hide the real op (T1-1).

    `git -c x=y push --force`, `git --no-pager reset --hard`, etc. used to be
    read as if the option were the subcommand, falling through to the MUTATING
    default and getting auto-approved instead of asked.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "git --no-pager push --force",
            "git -c commit.gpgsign=false push --force",
            "git -c foo=bar reset --hard HEAD~5",
            "git --git-dir=/x --work-tree=/y clean -fd",
            "git -C /repo clean -fdx",
            "git -p push --force",
            "git --no-pager branch -D main",
            "git -c a=b -C /repo push --force",
            "cd foo && git -c a=b push --force",
        ],
    )
    def test_global_options_do_not_mask_destructive(self, cmd: str) -> None:
        assert classify_git_command(cmd) is GitOpType.DESTRUCTIVE

    @pytest.mark.parametrize(
        "cmd",
        [
            "git -C /repo status",
            "git -c x=y log",
            "git --no-pager diff",
        ],
    )
    def test_global_options_preserve_read_only(self, cmd: str) -> None:
        assert classify_git_command(cmd) is GitOpType.READ_ONLY

    def test_value_option_consumes_next_token(self) -> None:
        # `-c core.pager=x` is two argv tokens: `-c` consumes the following
        # `core.pager=x`, so the real subcommand (`reset`) is still found.
        assert classify_git_command("git -c core.pager=x reset --hard") is (
            GitOpType.DESTRUCTIVE
        )
        # `-c foo` (no `=`) consumes `foo`; the next token `status` is then the
        # subcommand — a read-only op, not masked into the mutating default.
        assert classify_git_command("git -c foo status") is GitOpType.READ_ONLY
