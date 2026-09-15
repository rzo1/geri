"""Plain data models shared between discovery, deletion and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def deletion_date(obj: Any) -> str | None:
    """``marked_for_deletion_on`` (or ``..._at``) of a GitLab object, if scheduled."""
    value = getattr(obj, "marked_for_deletion_on", None) or getattr(
        obj, "marked_for_deletion_at", None
    )
    return str(value) if value else None


@dataclass(frozen=True, slots=True)
class ProjectNode:
    """Minimal, library-independent description of a GitLab project."""

    id: int
    full_path: str
    name: str
    web_url: str | None = None
    archived: bool = False
    marked_for_deletion_on: str | None = None
    namespace_id: int | None = None

    @classmethod
    def from_gl(cls, project: Any) -> ProjectNode:
        """Create a ``ProjectNode`` from a python-gitlab ``Project``-like object."""
        namespace = getattr(project, "namespace", None) or {}
        namespace_id = namespace.get("id") if isinstance(namespace, dict) else None
        return cls(
            id=int(project.id),
            full_path=str(project.path_with_namespace),
            name=str(getattr(project, "name", None) or project.path_with_namespace),
            web_url=getattr(project, "web_url", None) or None,
            archived=bool(getattr(project, "archived", False)),
            marked_for_deletion_on=deletion_date(project),
            namespace_id=int(namespace_id) if namespace_id is not None else None,
        )


@dataclass(slots=True)
class GroupNode:
    """A GitLab group with its direct subgroups and projects."""

    id: int
    full_path: str
    name: str
    web_url: str | None = None
    marked_for_deletion_on: str | None = None
    parent_id: int | None = None
    subgroups: list[GroupNode] = field(default_factory=list)
    projects: list[ProjectNode] = field(default_factory=list)

    @classmethod
    def from_gl(cls, group: Any) -> GroupNode:
        """Create a ``GroupNode`` (without children) from a python-gitlab group object."""
        parent_id = getattr(group, "parent_id", None)
        return cls(
            id=int(group.id),
            full_path=str(group.full_path),
            name=str(getattr(group, "name", None) or group.full_path),
            web_url=getattr(group, "web_url", None) or None,
            marked_for_deletion_on=deletion_date(group),
            parent_id=int(parent_id) if parent_id is not None else None,
        )

    def walk_groups(self) -> list[GroupNode]:
        """All descendant groups (excluding ``self``), depth-first."""
        result: list[GroupNode] = []
        for sub in self.subgroups:
            result.append(sub)
            result.extend(sub.walk_groups())
        return result

    def walk_projects(self) -> list[ProjectNode]:
        """All projects of this group and every descendant group."""
        result = list(self.projects)
        for sub in self.subgroups:
            result.extend(sub.walk_projects())
        return result


@dataclass(slots=True)
class UserNamespaceNode:
    """The personal namespace of a user with its projects.

    Deliberately not a ``GroupNode``: a personal namespace cannot be deleted as a
    whole, only its projects one by one, and its namespace id must never be
    mistaken for a group id.
    """

    username: str
    projects: list[ProjectNode] = field(default_factory=list)

    @property
    def full_path(self) -> str:
        return self.username


@dataclass(frozen=True, slots=True)
class RegistryRepository:
    """A container registry repository of a project."""

    id: int
    path: str
    tags_count: int | None = None
    status: str | None = None
    """``None``, or GitLab's ``delete_scheduled`` / ``delete_ongoing`` / ``delete_failed``."""

    @property
    def deleting(self) -> bool:
        """GitLab is already removing this repository (e.g. from an earlier run)."""
        return self.status in ("delete_scheduled", "delete_ongoing")

    @classmethod
    def from_gl(cls, repository: Any) -> RegistryRepository:
        tags_count = getattr(repository, "tags_count", None)
        return cls(
            id=int(repository.id),
            path=str(getattr(repository, "path", None) or repository.id),
            tags_count=int(tags_count) if tags_count is not None else None,
            status=getattr(repository, "status", None) or None,
        )


Registries = dict[int, list[RegistryRepository]]
"""Container registry repositories keyed by project id (only projects that have any)."""
