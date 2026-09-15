"""Tests for geri.deleter using fake gitlab managers (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from gitlab.exceptions import GitlabDeleteError, GitlabGetError
from requests.exceptions import ConnectionError as RequestsConnectionError

from geri.deleter import Deleter
from geri.models import GroupNode, ProjectNode


class FakeManager:
    def __init__(self, *, after=None, delete_error=None, get_error=None):
        self.after = after
        self.delete_error = delete_error
        self.get_error = get_error
        self.deleted: list = []
        self.gets: list[tuple] = []

    def delete(self, obj_id, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(obj_id)

    def get(self, obj_id, **kwargs):
        self.gets.append((obj_id, kwargs))
        if self.get_error is not None:
            raise self.get_error
        return self.after


GROUP = GroupNode(id=42, full_path="top/course", name="course")
PROJECT = ProjectNode(id=7, full_path="top/course/repo", name="repo")


def test_group_scheduled_by_numeric_id():
    groups = FakeManager(after=SimpleNamespace(marked_for_deletion_on="2026-09-22"))
    result = Deleter(SimpleNamespace(groups=groups, projects=None)).delete_group(GROUP)

    assert groups.deleted == [42]
    assert groups.gets == [(42, {"with_projects": False})]
    assert (result.kind, result.status, result.deletion_date) == (
        "group",
        "scheduled",
        "2026-09-22",
    )


def test_project_scheduled():
    projects = FakeManager(after=SimpleNamespace(marked_for_deletion_at="2026-09-22"))
    result = Deleter(SimpleNamespace(groups=None, projects=projects)).delete_project(PROJECT)

    assert projects.deleted == [7]
    assert projects.gets == [(7, {})]
    assert (result.kind, result.status, result.deletion_date) == (
        "project",
        "scheduled",
        "2026-09-22",
    )


def test_immediate_deletion_without_delayed_deletion():
    projects = FakeManager(after=SimpleNamespace(marked_for_deletion_on=None))
    result = Deleter(SimpleNamespace(projects=projects)).delete_project(PROJECT)
    assert result.status == "deleting"
    assert "no delayed deletion" in result.message


def test_gone_after_delete():
    projects = FakeManager(get_error=GitlabGetError("404 Not Found", response_code=404))
    result = Deleter(SimpleNamespace(projects=projects)).delete_project(PROJECT)
    assert result.status == "deleting"
    assert result.message == "already gone"


def test_refetch_error_is_not_a_failure():
    groups = FakeManager(get_error=GitlabGetError("502 Bad Gateway", response_code=502))
    result = Deleter(SimpleNamespace(groups=groups)).delete_group(GROUP)
    assert result.status == "deleting"
    assert "state unknown" in result.message


def test_delete_error_is_reported():
    groups = FakeManager(delete_error=GitlabDeleteError("403 Forbidden", response_code=403))
    result = Deleter(SimpleNamespace(groups=groups)).delete_group(GROUP)
    assert result.status == "failed"
    assert "403" in result.message
    assert groups.gets == []


def test_network_error_on_delete_is_reported():
    projects = FakeManager(delete_error=RequestsConnectionError("connection refused"))
    result = Deleter(SimpleNamespace(projects=projects)).delete_project(PROJECT)
    assert result.status == "failed"
    assert "connection refused" in result.message


def test_network_error_on_refetch_is_not_a_failure():
    projects = FakeManager(get_error=RequestsConnectionError("connection reset"))
    result = Deleter(SimpleNamespace(projects=projects)).delete_project(PROJECT)
    assert result.status == "deleting"
    assert "state unknown" in result.message


def test_delete_projects_continues_after_failure():
    class FlakyManager(FakeManager):
        def delete(self, obj_id, **kwargs):
            if obj_id == 2:
                raise GitlabDeleteError("403 Forbidden", response_code=403)
            super().delete(obj_id)

    projects = FlakyManager(after=SimpleNamespace(marked_for_deletion_on="2026-09-22"))
    nodes = [ProjectNode(id=i, full_path=f"alice/p{i}", name=f"p{i}") for i in (1, 2, 3)]
    results = Deleter(SimpleNamespace(projects=projects)).delete_projects(nodes)

    assert projects.deleted == [1, 3]
    assert [r.status for r in results] == ["scheduled", "failed", "scheduled"]
