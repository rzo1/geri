"""Tests for geri.deleter using fake gitlab managers (no network)."""

from __future__ import annotations

from types import SimpleNamespace

from gitlab.exceptions import GitlabDeleteError, GitlabGetError, GitlabListError
from requests.exceptions import ConnectionError as RequestsConnectionError

from geri.deleter import Deleter
from geri.models import GroupNode, ProjectNode, RegistryRepository


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


# --------------------------------------------------------------------------- registry purge


class FakeRepoManager:
    """Registry repositories of one project; deleted ones vanish after ``lag`` polls."""

    def __init__(self, repos, *, lag=1, delete_error=None, list_errors=(), status=None):
        self.repos = {r.id: r for r in repos}
        self.lag = lag
        self.delete_error = delete_error
        self.list_errors = list(list_errors)
        self.status = status
        self.deleted: list[int] = []
        self.polls = 0

    def delete(self, repo_id, **kwargs):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(repo_id)

    def list(self, **kwargs):
        self.polls += 1
        if self.list_errors:
            raise self.list_errors.pop(0)
        if self.polls > self.lag:
            for repo_id in self.deleted:
                self.repos.pop(repo_id, None)
        return iter(
            SimpleNamespace(
                id=r.id, path=r.path, status=self.status if r.id in self.deleted else None
            )
            for r in self.repos.values()
        )


class FakeProjects(FakeManager):
    """gl.projects with per-project registry managers for lazy gets."""

    def __init__(self, repo_managers, **kwargs):
        super().__init__(**kwargs)
        self.repo_managers = repo_managers

        self.events: list[tuple] = []
        self.unarchive_error: Exception | None = None

    def get(self, obj_id, lazy=False, **kwargs):
        if lazy:
            return SimpleNamespace(
                repositories=self.repo_managers[obj_id],
                archive=lambda: self.events.append(("archive", obj_id)),
                unarchive=lambda: self._unarchive(obj_id),
            )
        return super().get(obj_id, **kwargs)

    def _unarchive(self, obj_id):
        if self.unarchive_error is not None:
            raise self.unarchive_error
        self.events.append(("unarchive", obj_id))

    def delete(self, obj_id, **kwargs):
        self.events.append(("delete", obj_id))
        super().delete(obj_id, **kwargs)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def repo(rid, path="img", tags=2):
    return RegistryRepository(id=rid, path=path, tags_count=tags)


def make_deleter(projects, clock, timeout=60.0):
    return Deleter(
        SimpleNamespace(projects=projects, groups=projects),
        registry_timeout=timeout,
        poll_interval=5.0,
        sleep=clock.sleep,
        clock=clock,
    )


SCHEDULED = SimpleNamespace(marked_for_deletion_on="2026-09-22")


def test_purge_waits_until_repositories_are_gone_then_deletes():
    repos = FakeRepoManager([repo(1, "a"), repo(2, "b")], lag=2)
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    clock = FakeClock()

    result = make_deleter(projects, clock).delete_project(PROJECT, {7: [repo(1), repo(2)]})

    assert repos.deleted == [1, 2]
    assert clock.sleeps == [5.0, 5.0]
    assert projects.deleted == [7]
    assert result.status == "scheduled"


def test_purge_delete_error_keeps_project():
    repos = FakeRepoManager(
        [repo(1)], delete_error=GitlabDeleteError("403 Forbidden", response_code=403)
    )
    projects = FakeProjects({7: repos}, after=SCHEDULED)

    result = make_deleter(projects, FakeClock()).delete_project(PROJECT, {7: [repo(1)]})

    assert result.status == "failed"
    assert "registry purge failed" in result.message and "403" in result.message
    assert projects.deleted == []


def test_purge_timeout_keeps_project():
    repos = FakeRepoManager([repo(1)], lag=10_000)
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    clock = FakeClock()

    result = make_deleter(projects, clock, timeout=12).delete_project(PROJECT, {7: [repo(1)]})

    assert result.status == "failed"
    assert "still removing the registry images (waited 12s)" in result.message
    assert "re-run later" in result.message
    assert projects.deleted == []
    assert clock.now >= 12


def test_purge_delete_failed_status_is_reported():
    repos = FakeRepoManager([repo(1, "broken")], lag=10_000, status="delete_failed")
    projects = FakeProjects({7: repos}, after=SCHEDULED)

    result = make_deleter(projects, FakeClock()).delete_project(PROJECT, {7: [repo(1)]})

    assert result.status == "failed"
    assert "GitLab failed to delete registry repository broken" in result.message
    assert projects.deleted == []


def test_purge_transient_list_errors_are_retried():
    repos = FakeRepoManager(
        [repo(1)],
        lag=0,
        list_errors=[RequestsConnectionError("reset"), GitlabListError("502", response_code=502)],
    )
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    clock = FakeClock()

    result = make_deleter(projects, clock).delete_project(PROJECT, {7: [repo(1)]})

    assert result.status == "scheduled"
    assert len(clock.sleeps) == 2


def test_purge_registry_404_counts_as_gone():
    repos = FakeRepoManager([repo(1)], list_errors=[GitlabListError("404", response_code=404)])
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    result = make_deleter(projects, FakeClock()).delete_project(PROJECT, {7: [repo(1)]})
    assert result.status == "scheduled"


def test_delete_projects_skips_only_projects_whose_purge_failed():
    ok = FakeRepoManager([repo(1)], lag=0)
    bad = FakeRepoManager([repo(2)], delete_error=GitlabDeleteError("400", response_code=400))
    projects = FakeProjects({1: ok, 2: bad}, after=SCHEDULED)
    nodes = [ProjectNode(id=i, full_path=f"alice/p{i}", name=f"p{i}") for i in (1, 2, 3)]

    results = make_deleter(projects, FakeClock()).delete_projects(
        nodes, {1: [repo(1)], 2: [repo(2)]}
    )

    assert [r.status for r in results] == ["scheduled", "failed", "scheduled"]
    assert projects.deleted == [1, 3]


def test_group_is_not_deleted_when_a_purge_fails():
    bad = FakeRepoManager([repo(2)], delete_error=GitlabDeleteError("400", response_code=400))
    projects = FakeProjects({11: bad}, after=SCHEDULED)
    group = GroupNode(
        id=42,
        full_path="top",
        name="top",
        projects=[ProjectNode(id=11, full_path="top/app", name="app")],
    )

    result = make_deleter(projects, FakeClock()).delete_group(group, {11: [repo(2)]})

    assert result.status == "failed"
    assert result.message.startswith("top/app: registry purge failed")
    assert projects.deleted == []


def test_group_is_deleted_after_successful_purge():
    ok = FakeRepoManager([repo(2)], lag=0)
    projects = FakeProjects({11: ok}, after=SCHEDULED)
    group = GroupNode(
        id=42,
        full_path="top",
        name="top",
        subgroups=[
            GroupNode(
                id=43,
                full_path="top/sub",
                name="sub",
                projects=[ProjectNode(id=11, full_path="top/sub/app", name="app")],
            )
        ],
    )

    result = make_deleter(projects, FakeClock()).delete_group(group, {11: [repo(2)]})

    assert ok.deleted == [2]
    assert projects.deleted == [42]  # groups and projects share the fake manager
    assert result.status == "scheduled"


ARCHIVED = ProjectNode(id=7, full_path="top/course/old", name="old", archived=True)


def test_archived_project_is_unarchived_for_purge_and_archived_again():
    repos = FakeRepoManager([repo(1)], lag=0)
    projects = FakeProjects({7: repos}, after=SCHEDULED)

    result = make_deleter(projects, FakeClock()).delete_project(ARCHIVED, {7: [repo(1)]})

    assert result.status == "scheduled"
    assert repos.deleted == [1]
    assert projects.events == [("unarchive", 7), ("archive", 7), ("delete", 7)]


def test_archived_project_is_archived_again_when_purge_fails():
    repos = FakeRepoManager([repo(1)], delete_error=GitlabDeleteError("400", response_code=400))
    projects = FakeProjects({7: repos}, after=SCHEDULED)

    result = make_deleter(projects, FakeClock()).delete_project(ARCHIVED, {7: [repo(1)]})

    assert result.status == "failed"
    assert projects.events == [("unarchive", 7), ("archive", 7)]


def test_unarchive_failure_skips_purge_and_project():
    repos = FakeRepoManager([repo(1)], lag=0)
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    projects.unarchive_error = GitlabDeleteError("403 Forbidden", response_code=403)

    result = make_deleter(projects, FakeClock()).delete_project(ARCHIVED, {7: [repo(1)]})

    assert result.status == "failed"
    assert "could not unarchive project" in result.message
    assert repos.deleted == []
    assert projects.events == []


def test_active_project_is_not_unarchived():
    repos = FakeRepoManager([repo(1)], lag=0)
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    make_deleter(projects, FakeClock()).delete_project(PROJECT, {7: [repo(1)]})
    assert projects.events == [("delete", 7)]


def test_repositories_already_being_deleted_are_only_waited_for():
    repos = FakeRepoManager([repo(1), repo(2)], lag=0)
    repos.deleted = [1]  # requested by an earlier run
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    registries = {
        7: [
            RegistryRepository(id=1, path="a", tags_count=2, status="delete_scheduled"),
            RegistryRepository(id=2, path="b", tags_count=2, status="delete_failed"),
        ]
    }

    result = make_deleter(projects, FakeClock()).delete_project(PROJECT, registries)

    assert repos.deleted == [1, 2]  # only the failed one was requested again
    assert result.status == "scheduled"


def test_zero_timeout_checks_once_and_keeps_project():
    repos = FakeRepoManager([repo(1)], lag=10_000)
    projects = FakeProjects({7: repos}, after=SCHEDULED)
    clock = FakeClock()

    result = make_deleter(projects, clock, timeout=0).delete_project(PROJECT, {7: [repo(1)]})

    assert repos.deleted == [1]
    assert repos.polls == 1
    assert clock.sleeps == []
    assert result.status == "failed" and "re-run later" in result.message
    assert projects.deleted == []
