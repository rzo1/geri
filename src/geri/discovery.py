"""Discovery: resolve a single project, a group with its complete subtree of
subgroups and projects (archived and already-scheduled ones included, since
deleting the group takes all of them along), or the projects in the
authenticated user's personal namespace."""

from __future__ import annotations

import logging

import gitlab

from geri.models import GroupNode, ProjectNode, UserNamespaceNode

log = logging.getLogger(__name__)


class TreeDiscovery:
    """Build deletion targets through an authenticated python-gitlab client."""

    def __init__(self, gl: gitlab.Gitlab) -> None:
        self.gl = gl

    def get_project(self, id_or_path: str | int) -> ProjectNode:
        """Return the project ``id_or_path``.

        Raises ``gitlab.exceptions.GitlabGetError`` if the project does not exist
        or is not accessible.
        """
        return ProjectNode.from_gl(self.gl.projects.get(id_or_path))

    def get_group_tree(self, id_or_path: str | int) -> GroupNode:
        """Return group ``id_or_path`` with all descendant groups and projects.

        Uses two paginated listings (``descendant_groups`` and ``projects`` with
        ``include_subgroups``) and assembles the tree locally via ``parent_id`` /
        ``namespace.id`` instead of one request per subgroup. Projects merely
        *shared* with a group are not part of it and are not listed.

        Raises ``gitlab.exceptions.GitlabGetError`` if the group cannot be fetched;
        errors while listing the contents propagate as well, because a tree that
        silently misses items must never be offered for confirmation.
        """
        group = self.gl.groups.get(id_or_path, with_projects=False)
        root = GroupNode.from_gl(group)

        by_id: dict[int, GroupNode] = {root.id: root}
        descendants = [GroupNode.from_gl(g) for g in group.descendant_groups.list(iterator=True)]
        for node in descendants:
            by_id.setdefault(node.id, node)
        for node in by_id.values():
            if node is root:
                continue
            parent = by_id.get(node.parent_id) if node.parent_id is not None else None
            if parent is None:
                log.warning("Parent of group %s not found; attaching to root", node.full_path)
                parent = root
            parent.subgroups.append(node)

        seen: set[int] = set()
        for project in group.projects.list(
            include_subgroups=True, with_shared=False, iterator=True
        ):
            info = ProjectNode.from_gl(project)
            if info.id in seen:
                continue
            seen.add(info.id)
            owner = by_id.get(info.namespace_id) if info.namespace_id is not None else None
            if owner is None:
                owner = self._owner_by_path(info, by_id) or root
            owner.projects.append(info)

        self._sort(root)
        log.info(
            "Group %s: %d subgroup(s), %d project(s)",
            root.full_path,
            len(root.walk_groups()),
            len(root.walk_projects()),
        )
        return root

    def get_user_namespace(self) -> UserNamespaceNode:
        """Return the authenticated user's personal namespace with all its projects.

        Lists ``GET /users/:id/projects`` (archived ones included) and keeps only
        projects whose namespace path is exactly the username, so a project living
        anywhere else can never end up in the deletion list.
        """
        user = getattr(self.gl, "user", None)
        if user is None:
            self.gl.auth()
            user = self.gl.user
        username = str(user.username)

        node = UserNamespaceNode(username=username)
        seen: set[int] = set()
        for project in self.gl.users.get(user.id, lazy=True).projects.list(iterator=True):
            info = ProjectNode.from_gl(project)
            if info.id in seen:
                continue
            seen.add(info.id)
            if info.full_path.rpartition("/")[0] != username:
                log.warning("Skipping %s: not in the personal namespace", info.full_path)
                continue
            node.projects.append(info)

        node.projects.sort(key=lambda p: p.full_path.lower())
        log.info("User %s: %d project(s) in personal namespace", username, len(node.projects))
        return node

    @staticmethod
    def _owner_by_path(project: ProjectNode, groups: dict[int, GroupNode]) -> GroupNode | None:
        """Fallback when ``namespace.id`` is missing: match the namespace path."""
        namespace_path = project.full_path.rpartition("/")[0]
        for node in groups.values():
            if node.full_path == namespace_path:
                return node
        return None

    @staticmethod
    def _sort(node: GroupNode) -> None:
        node.subgroups.sort(key=lambda g: g.full_path.lower())
        node.projects.sort(key=lambda p: p.full_path.lower())
        for sub in node.subgroups:
            TreeDiscovery._sort(sub)
