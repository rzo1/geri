"""Tests for the Typer CLI (no network: client, discovery and deleter are patched)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gitlab.exceptions import GitlabAuthenticationError, GitlabGetError
from requests.exceptions import ConnectionError as RequestsConnectionError
from typer.testing import CliRunner

from geri import cli
from geri.deleter import DeletionResult
from geri.models import GroupNode, ProjectNode, UserNamespaceNode

runner = CliRunner()

TOKEN = "glpat-test-token"


def sample_tree() -> GroupNode:
    return GroupNode(
        id=1,
        full_path="top",
        name="top",
        subgroups=[
            GroupNode(
                id=2,
                full_path="top/sub",
                name="sub",
                parent_id=1,
                projects=[ProjectNode(id=11, full_path="top/sub/deep-repo", name="deep-repo")],
            )
        ],
        projects=[
            ProjectNode(id=10, full_path="top/repo", name="repo", archived=True),
        ],
    )


def sample_namespace() -> UserNamespaceNode:
    return UserNamespaceNode(
        username="alice",
        projects=[
            ProjectNode(id=20, full_path="alice/archiver", name="archiver", archived=True),
            ProjectNode(
                id=21, full_path="alice/old", name="old", marked_for_deletion_on="2026-09-20"
            ),
            ProjectNode(id=22, full_path="alice/thesis", name="thesis"),
        ],
    )


class FakeDiscovery:
    namespace: UserNamespaceNode | None = None
    tree: GroupNode | None = None
    project: ProjectNode | None = None
    error: Exception | None = None
    calls: list[tuple] = []

    def __init__(self, gl) -> None:
        self.gl = gl

    def get_group_tree(self, id_or_path):
        FakeDiscovery.calls.append(("group", id_or_path))
        if self.error is not None:
            raise self.error
        return self.tree

    def get_user_namespace(self):
        FakeDiscovery.calls.append(("user",))
        if self.error is not None:
            raise self.error
        return self.namespace

    def get_project(self, id_or_path):
        FakeDiscovery.calls.append(("project", id_or_path))
        if self.error is not None:
            raise self.error
        return self.project


class FakeDeleter:
    status: str = "scheduled"
    failing: set[int] = set()
    calls: list[tuple] = []

    def __init__(self, gl) -> None:
        self.gl = gl

    def _result(self, kind, node):
        FakeDeleter.calls.append((kind, node.id))
        status = "failed" if node.id in self.failing else self.status
        return DeletionResult(
            kind,
            node.full_path,
            status,  # type: ignore[arg-type]
            deletion_date="2026-09-22" if status == "scheduled" else None,
            message="403 Forbidden" if status == "failed" else "",
        )

    def delete_projects(self, projects):
        return [self._result("project", p) for p in projects]

    def delete_group(self, group):
        return self._result("group", group)

    def delete_project(self, project):
        return self._result("project", project)


@pytest.fixture(autouse=True)
def patched(monkeypatch, tmp_path):
    """Isolate from env/.env and swap out every network-touching collaborator."""
    monkeypatch.chdir(tmp_path)  # no .env file here
    monkeypatch.setenv("GITLAB_TOKEN", TOKEN)
    monkeypatch.setenv("GITLAB_URL", "https://git.example.org")

    client = SimpleNamespace(user=SimpleNamespace(username="alice"))
    seen_settings = []

    def fake_get_client(settings):
        seen_settings.append(settings)
        return client

    FakeDiscovery.namespace = sample_namespace()
    FakeDiscovery.tree = sample_tree()
    FakeDiscovery.project = ProjectNode(id=5, full_path="top/solo", name="solo")
    FakeDiscovery.error = None
    FakeDiscovery.calls = []
    FakeDeleter.status = "scheduled"
    FakeDeleter.calls = []
    FakeDeleter.failing = set()
    monkeypatch.setattr(cli, "get_client", fake_get_client)
    monkeypatch.setattr(cli, "TreeDiscovery", FakeDiscovery)
    monkeypatch.setattr(cli, "Deleter", FakeDeleter)
    return SimpleNamespace(settings=seen_settings, client=client)


def test_help_lists_commands():
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("group", "project", "user"):
        assert cmd in result.output


def test_group_lists_tree_and_deletes_after_confirmation():
    result = runner.invoke(cli.app, ["group", "1"], input="top\n")
    assert result.exit_code == 0, result.output
    assert "Authenticated as alice" in result.output
    assert FakeDiscovery.calls == [("group", 1)]
    for path in ("top/sub", "top/sub/deep-repo", "top/repo"):
        assert path in result.output
    assert "archived" in result.output
    assert "1 subgroup(s) and 2 project(s) (1 archived)" in result.output
    assert FakeDeleter.calls == [("group", 1)]
    assert "Scheduled" in result.output and "2026-09-22" in result.output
    assert TOKEN not in result.output


def test_group_path_argument_is_passed_as_string():
    result = runner.invoke(cli.app, ["group", "top"], input="top\n")
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.calls == [("group", "top")]


@pytest.mark.parametrize("answer", ["", "top/sub", "yes", "TOP"])
def test_wrong_confirmation_deletes_nothing(answer):
    result = runner.invoke(cli.app, ["group", "top"], input=f"{answer}\n")
    assert result.exit_code == cli.EXIT_ABORTED, result.output
    assert FakeDeleter.calls == []
    assert "Nothing was deleted" in result.output


def test_eof_on_prompt_deletes_nothing():
    result = runner.invoke(cli.app, ["group", "top"], input="")
    assert result.exit_code == cli.EXIT_ABORTED, result.output
    assert FakeDeleter.calls == []


def test_dry_run_does_not_prompt_or_delete():
    result = runner.invoke(cli.app, ["group", "top", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "top/sub/deep-repo" in result.output
    assert "Dry run" in result.output
    assert "Type the full path" not in result.output
    assert FakeDeleter.calls == []


def test_already_scheduled_target_is_left_alone():
    FakeDiscovery.tree.marked_for_deletion_on = "2026-09-20"
    result = runner.invoke(cli.app, ["group", "top"])
    assert result.exit_code == 0, result.output
    assert "already scheduled" in result.output
    assert FakeDeleter.calls == []


def test_project_command():
    result = runner.invoke(cli.app, ["project", "5"], input="top/solo\n")
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.calls == [("project", 5)]
    assert FakeDeleter.calls == [("project", 5)]


def test_failed_deletion_exit_code():
    FakeDeleter.status = "failed"
    result = runner.invoke(cli.app, ["project", "top/solo"], input="top/solo\n")
    assert result.exit_code == cli.EXIT_FAILED, result.output
    assert "Failed" in result.output and "403" in result.output


def test_not_found_exit_code():
    FakeDiscovery.error = GitlabGetError("404 Not Found", response_code=404)
    result = runner.invoke(cli.app, ["group", "nope"])
    assert result.exit_code == cli.EXIT_USAGE
    assert FakeDeleter.calls == []


def test_missing_url(monkeypatch):
    monkeypatch.delenv("GITLAB_URL")
    result = runner.invoke(cli.app, ["group", "top"])
    assert result.exit_code == cli.EXIT_USAGE


def test_auth_failure(monkeypatch):
    def boom(settings):
        raise GitlabAuthenticationError("401 Unauthorized")

    monkeypatch.setattr(cli, "get_client", boom)
    result = runner.invoke(cli.app, ["group", "top"])
    assert result.exit_code == cli.EXIT_USAGE


def test_cli_overrides_env(patched):
    result = runner.invoke(
        cli.app,
        ["group", "top", "--url", "https://gl.other.org", "--token", "other", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    settings = patched.settings[0]
    assert settings.gitlab_url == "https://gl.other.org"
    assert settings.gitlab_token.get_secret_value() == "other"


def test_unreachable_host_is_a_clean_error(monkeypatch):
    def boom(settings):
        raise RequestsConnectionError("connection refused")

    monkeypatch.setattr(cli, "get_client", boom)
    result = runner.invoke(cli.app, ["group", "top"])
    assert result.exit_code == cli.EXIT_USAGE
    assert "could not talk to" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --------------------------------------------------------------------------- user


def test_user_lists_namespace_and_deletes_pending_projects():
    result = runner.invoke(cli.app, ["user"], input="alice\n")
    assert result.exit_code == 0, result.output
    assert FakeDiscovery.calls == [("user",)]
    for path in ("alice/archiver", "alice/old", "alice/thesis"):
        assert path in result.output
    assert "3 project(s) (1 archived, 1 already scheduled)" in result.output
    assert "2 project(s)" in result.output  # in the warning: only pending ones
    assert FakeDeleter.calls == [("project", 20), ("project", 22)]  # already scheduled skipped
    assert "Summary" in result.output
    assert "2 scheduled" in result.output
    assert "1 skipped (already scheduled)" in result.output


@pytest.mark.parametrize("answer", ["", "yes", "alice/thesis"])
def test_user_wrong_confirmation_deletes_nothing(answer):
    result = runner.invoke(cli.app, ["user"], input=f"{answer}\n")
    assert result.exit_code == cli.EXIT_ABORTED, result.output
    assert FakeDeleter.calls == []


def test_user_dry_run():
    result = runner.invoke(cli.app, ["user", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "alice/thesis" in result.output
    assert "Type the full path" not in result.output
    assert FakeDeleter.calls == []


def test_user_nothing_left_to_delete():
    FakeDiscovery.namespace = UserNamespaceNode(
        username="alice",
        projects=[ProjectNode(id=1, full_path="alice/x", name="x", marked_for_deletion_on="d")],
    )
    result = runner.invoke(cli.app, ["user"])
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output
    assert FakeDeleter.calls == []


def test_user_partial_failure_exit_code():
    FakeDeleter.failing = {20}
    result = runner.invoke(cli.app, ["user"], input="alice\n")
    assert result.exit_code == cli.EXIT_FAILED, result.output
    assert FakeDeleter.calls == [("project", 20), ("project", 22)]
    assert "1 failed" in result.output and "1 scheduled" in result.output


# --------------------------------------------------------------------------- group --no-subgroups


def test_group_no_subgroups_deletes_only_direct_projects():
    result = runner.invoke(cli.app, ["group", "top", "--no-subgroups"], input="top\n")
    assert result.exit_code == 0, result.output
    assert "kept (0 subgroup(s), 1 project(s) inside)" in result.output
    assert "top/sub/deep-repo" not in result.output  # kept subgroups are shown collapsed
    assert "1 project(s) (1 archived) directly; its 1 subgroup(s) are kept" in result.output
    assert FakeDeleter.calls == [("project", 10)]  # never the group, never subgroup projects
    assert "Summary" in result.output and "1 scheduled" in result.output


def test_group_no_subgroups_dry_run():
    result = runner.invoke(cli.app, ["group", "top", "--no-subgroups", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "kept" in result.output and "top/repo" in result.output
    assert "Type the full path" not in result.output
    assert FakeDeleter.calls == []


def test_group_no_subgroups_wrong_confirmation():
    result = runner.invoke(cli.app, ["group", "top", "--no-subgroups"], input="top/sub\n")
    assert result.exit_code == cli.EXIT_ABORTED, result.output
    assert FakeDeleter.calls == []


def test_group_no_subgroups_without_direct_projects():
    FakeDiscovery.tree.projects = []
    result = runner.invoke(cli.app, ["group", "top", "--no-subgroups"])
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output
    assert FakeDeleter.calls == []


def test_group_no_subgroups_skips_already_scheduled_projects():
    FakeDiscovery.tree.projects.append(
        ProjectNode(id=12, full_path="top/gone", name="gone", marked_for_deletion_on="2026-09-20")
    )
    FakeDeleter.failing = {10}
    result = runner.invoke(cli.app, ["group", "top", "--no-subgroups"], input="top\n")
    assert result.exit_code == cli.EXIT_FAILED, result.output
    assert FakeDeleter.calls == [("project", 10)]
    assert "1 skipped (already scheduled)" in result.output
