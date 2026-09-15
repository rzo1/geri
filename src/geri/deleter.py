"""Deletion: ``DELETE /groups/:id`` or ``DELETE /projects/:id``.

On instances with delayed deletion this only *marks* the target for deletion
(restorable until the date GitLab reports); deleting a group takes all its
subgroups and projects along. Instances without delayed deletion remove the
target right away. The target is fetched again afterwards to tell both apart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import gitlab
from gitlab.exceptions import GitlabError, GitlabGetError
from requests.exceptions import RequestException

from geri.models import GroupNode, ProjectNode, deletion_date

log = logging.getLogger(__name__)

Kind = Literal["group", "project"]
Status = Literal["scheduled", "deleting", "failed"]


@dataclass
class DeletionResult:
    """Outcome of a deletion request."""

    kind: Kind
    full_path: str
    status: Status
    deletion_date: str | None = None
    message: str = ""


class Deleter:
    """Request deletion of groups and projects through a python-gitlab client."""

    def __init__(self, gl: gitlab.Gitlab) -> None:
        self.gl = gl

    def delete_group(self, group: GroupNode) -> DeletionResult:
        """Schedule ``group`` (and with it its whole subtree) for deletion."""
        return self._delete("group", self.gl.groups, group.id, group.full_path)

    def delete_project(self, project: ProjectNode) -> DeletionResult:
        """Schedule ``project`` for deletion."""
        return self._delete("project", self.gl.projects, project.id, project.full_path)

    def delete_projects(self, projects: list[ProjectNode]) -> list[DeletionResult]:
        """Schedule each of ``projects`` for deletion; a failure never stops the rest."""
        return [self.delete_project(project) for project in projects]

    def _delete(self, kind: Kind, manager: Any, obj_id: int, full_path: str) -> DeletionResult:
        # Always by numeric id as resolved during discovery: a path could point to
        # something else by now (renamed/transferred in the meantime).
        try:
            manager.delete(obj_id)
        except (GitlabError, RequestException) as exc:
            log.error("failed to delete %s %s: %s", kind, full_path, exc)
            return DeletionResult(kind, full_path, "failed", message=str(exc))

        try:
            refreshed = manager.get(obj_id, **({"with_projects": False} if kind == "group" else {}))
        except (GitlabGetError, RequestException) as exc:
            if getattr(exc, "response_code", None) == 404:
                return DeletionResult(kind, full_path, "deleting", message="already gone")
            log.warning("could not re-fetch %s %s after deletion: %s", kind, full_path, exc)
            return DeletionResult(
                kind, full_path, "deleting", message=f"deletion accepted; state unknown ({exc})"
            )

        date = deletion_date(refreshed)
        if date:
            log.info("%s %s scheduled for deletion on %s", kind, full_path, date)
            return DeletionResult(kind, full_path, "scheduled", deletion_date=date)
        return DeletionResult(
            kind,
            full_path,
            "deleting",
            message="no delayed deletion on this instance; removal is in progress",
        )
