"""Tests for geri.discovery using fake gitlab client objects (no network)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gitlab.exceptions import GitlabGetError, GitlabListError

from geri.discovery import TreeDiscovery
from geri.models import GroupNode, ProjectNode, RegistryRepository, UserNamespaceNode

# --------------------------------------------------------------------------- fakes


def gl_project(pid, path, *, namespace_id=None, archived=False, marked_on=None):
    return SimpleNamespace(
        id=pid,
        path_with_namespace=path,
        name=path.rpartition("/")[2],
        web_url=f"https://git.example.org/{path}",
        archived=archived,
        marked_for_deletion_on=marked_on,
        namespace={"id": namespace_id, "full_path": path.rpartition("/")[0]}
        if namespace_id is not None
        else None,
    )


def gl_group(gid, full_path, *, parent_id=None, marked_on=None):
    return SimpleNamespace(
        id=gid,
        full_path=full_path,
        name=full_path.rpartition("/")[2],
        parent_id=parent_id,
        marked_for_deletion_on=marked_on,
    )


class FakeListManager:
    def __init__(self, items, *, error: Exception | None = None):
        self.items = list(items)
        self.error = error
        self.calls: list[dict] = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return iter(self.items)


class FakeGetManager:
    def __init__(self, by_key: dict):
        self.by_key = by_key
        self.calls: list[tuple] = []

    def get(self, key, **kwargs):
        self.calls.append((key, kwargs))
        try:
            return self.by_key[key]
        except KeyError:
            raise GitlabGetError("404 Not Found", response_code=404) from None


def make_gl(root, *, descendants=(), projects=(), project_error=None):
    root.descendant_groups = FakeListManager(descendants)
    root.projects = FakeListManager(projects, error=project_error)
    gl = SimpleNamespace()
    gl.groups = FakeGetManager({root.id: root, root.full_path: root})
    gl.projects = FakeGetManager(
        {p.id: p for p in projects} | {p.path_with_namespace: p for p in projects}
    )
    return gl


def _fixture():
    root = gl_group(1, "top")
    descendants = [
        gl_group(3, "top/b", parent_id=1),
        gl_group(2, "top/a", parent_id=1, marked_on="2026-09-22"),
        gl_group(4, "top/a/deep", parent_id=2),
    ]
    projects = [
        gl_project(10, "top/z-root-project", namespace_id=1),
        gl_project(11, "top/a/one", namespace_id=2, archived=True),
        gl_project(12, "top/a/deep/two", namespace_id=4),
        gl_project(13, "top/b/three", namespace_id=3),
        gl_project(14, "top/b/no-namespace-id"),  # resolved via path fallback
        gl_project(11, "top/a/one", namespace_id=2, archived=True),  # duplicate page item
    ]
    return make_gl(root, descendants=descendants, projects=projects)


# --------------------------------------------------------------------------- group tree


def test_group_tree_is_assembled_and_sorted():
    gl = _fixture()
    tree = TreeDiscovery(gl).get_group_tree("top")

    assert isinstance(tree, GroupNode)
    assert tree.full_path == "top"
    assert [g.full_path for g in tree.subgroups] == ["top/a", "top/b"]
    assert [p.full_path for p in tree.projects] == ["top/z-root-project"]

    a, b = tree.subgroups
    assert a.marked_for_deletion_on == "2026-09-22"
    assert [g.full_path for g in a.subgroups] == ["top/a/deep"]
    assert [p.full_path for p in a.projects] == ["top/a/one"]
    assert a.projects[0].archived is True
    assert [p.full_path for p in a.subgroups[0].projects] == ["top/a/deep/two"]
    assert [p.full_path for p in b.projects] == ["top/b/no-namespace-id", "top/b/three"]

    assert [g.full_path for g in tree.walk_groups()] == ["top/a", "top/a/deep", "top/b"]
    assert sorted(p.id for p in tree.walk_projects()) == [10, 11, 12, 13, 14]


def test_group_tree_listing_parameters():
    gl = _fixture()
    root = gl.groups.by_key[1]
    TreeDiscovery(gl).get_group_tree(1)

    assert gl.groups.calls == [(1, {"with_projects": False})]
    assert root.descendant_groups.calls == [{"iterator": True}]
    call = root.projects.calls[0]
    assert call["include_subgroups"] is True
    assert call["with_shared"] is False
    assert "archived" not in call  # archived projects are deleted too, so list them


def test_group_with_orphan_subgroup_is_attached_to_root():
    root = gl_group(1, "top")
    gl = make_gl(root, descendants=[gl_group(5, "top/x/orphan", parent_id=99)])
    tree = TreeDiscovery(gl).get_group_tree(1)
    assert [g.full_path for g in tree.subgroups] == ["top/x/orphan"]


def test_group_missing_raises():
    gl = make_gl(gl_group(1, "top"))
    with pytest.raises(GitlabGetError):
        TreeDiscovery(gl).get_group_tree("nope")


def test_group_listing_error_propagates():
    """An incomplete tree must never be shown for confirmation."""
    gl = make_gl(gl_group(1, "top"), project_error=GitlabListError("500", response_code=500))
    with pytest.raises(GitlabListError):
        TreeDiscovery(gl).get_group_tree(1)


# --------------------------------------------------------------------------- project


def test_get_project():
    p = gl_project(7, "top/solo", namespace_id=1, marked_on="2026-10-01")
    gl = make_gl(gl_group(1, "top"), projects=[p])
    info = TreeDiscovery(gl).get_project("top/solo")
    assert isinstance(info, ProjectNode)
    assert (info.id, info.full_path, info.name) == (7, "top/solo", "solo")
    assert info.namespace_id == 1
    assert info.marked_for_deletion_on == "2026-10-01"


def test_get_project_missing_raises():
    gl = make_gl(gl_group(1, "top"))
    with pytest.raises(GitlabGetError):
        TreeDiscovery(gl).get_project(404)


def test_models_fall_back_to_marked_for_deletion_at():
    obj = SimpleNamespace(id=1, full_path="g", marked_for_deletion_at="2026-09-20T10:00:00Z")
    assert GroupNode.from_gl(obj).marked_for_deletion_on == "2026-09-20T10:00:00Z"


# --------------------------------------------------------------------------- user namespace


class FakeUsersManager:
    def __init__(self, projects):
        self.projects = FakeListManager(projects)
        self.calls: list[tuple] = []

    def get(self, user_id, **kwargs):
        self.calls.append((user_id, kwargs))
        return SimpleNamespace(id=user_id, projects=self.projects)


def test_user_namespace_lists_only_personal_projects():
    users = FakeUsersManager(
        [
            gl_project(21, "alice/zeta", archived=True),
            gl_project(20, "alice/Alpha", marked_on="2026-09-30"),
            gl_project(22, "alice/zeta", archived=True),  # distinct id, same path is fine
            gl_project(20, "alice/Alpha"),  # duplicate page item
            gl_project(30, "some-group/not-mine"),
            gl_project(31, "alice-other/lookalike"),
        ]
    )
    gl = SimpleNamespace(user=SimpleNamespace(id=5, username="alice"), users=users)

    node = TreeDiscovery(gl).get_user_namespace()

    assert isinstance(node, UserNamespaceNode)
    assert node.full_path == "alice"
    assert [p.id for p in node.projects] == [20, 21, 22]
    assert node.projects[0].marked_for_deletion_on == "2026-09-30"
    assert users.calls == [(5, {"lazy": True})]
    assert users.projects.calls == [{"iterator": True}]  # no archived filter


def test_user_namespace_authenticates_when_needed():
    gl = SimpleNamespace(user=None, users=FakeUsersManager([gl_project(1, "bob/x")]))

    def auth():
        gl.user = SimpleNamespace(id=9, username="bob")

    gl.auth = auth
    node = TreeDiscovery(gl).get_user_namespace()
    assert node.username == "bob"
    assert [p.full_path for p in node.projects] == ["bob/x"]


# --------------------------------------------------------------------------- registries


class FakeLazyProjects:
    def __init__(self, managers):
        self.managers = managers

    def get(self, project_id, lazy=False, **kwargs):
        assert lazy is True
        return SimpleNamespace(repositories=self.managers[project_id])


def test_get_registries_collects_only_projects_with_repositories():
    managers = {
        1: FakeListManager(
            [
                SimpleNamespace(id=5, path="g/p1/zeta", tags_count=4),
                SimpleNamespace(id=6, path="g/p1/Alpha", tags_count=0),
            ]
        ),
        2: FakeListManager([]),
        3: FakeListManager([], error=GitlabListError("404 Not Found", response_code=404)),
    }
    gl = SimpleNamespace(projects=FakeLazyProjects(managers))
    nodes = [ProjectNode(id=i, full_path=f"g/p{i}", name=f"p{i}") for i in (1, 2, 3)]

    registries = TreeDiscovery(gl).get_registries(nodes)

    assert registries == {
        1: [
            RegistryRepository(id=6, path="g/p1/Alpha", tags_count=0),
            RegistryRepository(id=5, path="g/p1/zeta", tags_count=4),
        ]
    }
    assert managers[1].calls == [{"tags_count": True, "iterator": True}]


def test_get_registries_other_errors_propagate():
    managers = {1: FakeListManager([], error=GitlabListError("500", response_code=500))}
    gl = SimpleNamespace(projects=FakeLazyProjects(managers))
    with pytest.raises(GitlabListError):
        TreeDiscovery(gl).get_registries([ProjectNode(id=1, full_path="g/p", name="p")])


def test_registry_repository_status():
    queued = RegistryRepository.from_gl(SimpleNamespace(id=1, path="p", status="delete_scheduled"))
    fresh = RegistryRepository.from_gl(SimpleNamespace(id=2, path="p", status=None))
    assert queued.status == "delete_scheduled" and queued.deleting is True
    assert fresh.status is None and fresh.deleting is False
    assert RegistryRepository(id=3, path="p", status="delete_failed").deleting is False
