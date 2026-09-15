"""Deletion: ``DELETE /groups/:id`` or ``DELETE /projects/:id``.

On instances with delayed deletion this only *marks* the target for deletion
(restorable until the date GitLab reports); deleting a group takes all its
subgroups and projects along. Instances without delayed deletion remove the
target right away. The target is fetched again afterwards to tell both apart.

GitLab refuses to delete (i.e. rename) projects whose container registry still
holds tags. With a purge, the given registry repositories are deleted first
(``DELETE /projects/:id/registry/repositories/:repository_id``, permanent and
asynchronous), Geri waits until GitLab has removed them, and only then deletes;
a project whose purge did not finish is not deleted.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import gitlab
from gitlab.exceptions import GitlabError, GitlabGetError, GitlabListError
from requests.exceptions import RequestException

from geri.models import GroupNode, ProjectNode, Registries, deletion_date

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


REGISTRY_TIMEOUT = 600.0  # seconds to wait for GitLab to remove purged registry repositories
REGISTRY_POLL_INTERVAL = 5.0


class Deleter:
    """Request deletion of groups and projects through a python-gitlab client."""

    def __init__(
        self,
        gl: gitlab.Gitlab,
        *,
        registry_timeout: float = REGISTRY_TIMEOUT,
        poll_interval: float = REGISTRY_POLL_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.gl = gl
        self.registry_timeout = registry_timeout
        self.poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    def delete_group(
        self, group: GroupNode, registries: Registries | None = None
    ) -> DeletionResult:
        """Schedule ``group`` (and with it its whole subtree) for deletion.

        ``registries`` of projects inside the group are purged first; if any purge
        fails, the group is not deleted.
        """
        if registries:
            errors = self.purge_registries(group.walk_projects(), registries)
            if errors:
                message = "; ".join(f"{path}: {error}" for path, error in errors.items())
                log.error("not deleting group %s: %s", group.full_path, message)
                return DeletionResult("group", group.full_path, "failed", message=message)
        return self._delete("group", self.gl.groups, group.id, group.full_path)

    def delete_project(
        self, project: ProjectNode, registries: Registries | None = None
    ) -> DeletionResult:
        """Schedule ``project`` for deletion (purging its registry first, if given)."""
        return self.delete_projects([project], registries)[0]

    def delete_projects(
        self, projects: list[ProjectNode], registries: Registries | None = None
    ) -> list[DeletionResult]:
        """Schedule each of ``projects`` for deletion; a failure never stops the rest.

        All given ``registries`` are purged up front (waiting for them together);
        projects whose purge failed are reported as failed and not deleted.
        """
        errors = self.purge_registries(projects, registries) if registries else {}
        results = []
        for project in projects:
            if project.full_path in errors:
                results.append(
                    DeletionResult(
                        "project", project.full_path, "failed", message=errors[project.full_path]
                    )
                )
                continue
            results.append(self._delete("project", self.gl.projects, project.id, project.full_path))
        return results

    # ------------------------------------------------------------------ registry

    def _lazy_project(self, project_id: int) -> Any:
        return self.gl.projects.get(project_id, lazy=True)

    def purge_registries(
        self, projects: list[ProjectNode], registries: Registries
    ) -> dict[str, str]:
        """Delete the registry repositories of ``projects`` and wait until they are gone.

        Archived projects are read-only, so GitLab refuses to delete their images
        (403). They are unarchived for the purge and archived again afterwards,
        whatever the outcome, before anything is deleted.

        Returns an error message per project path for every purge that failed or
        did not finish within ``registry_timeout``; purged projects are absent.
        """
        errors: dict[str, str] = {}
        unarchived: list[ProjectNode] = []
        try:
            waiting = self._request_purges(projects, registries, errors, unarchived)
            self._wait_for_purges(waiting, errors)
        finally:
            for project in unarchived:
                self._set_archived(project, archived=True)
        return errors

    def _request_purges(
        self,
        projects: list[ProjectNode],
        registries: Registries,
        errors: dict[str, str],
        unarchived: list[ProjectNode],
    ) -> dict[int, tuple[ProjectNode, set[int]]]:
        waiting: dict[int, tuple[ProjectNode, set[int]]] = {}
        for project in projects:
            repos = registries.get(project.id) or []
            if not repos:
                continue
            if project.archived:
                error = self._set_archived(project, archived=False)
                if error:
                    errors[project.full_path] = f"registry purge failed: {error}"
                    continue
                unarchived.append(project)
            manager = self._lazy_project(project.id).repositories
            try:
                for repo in repos:
                    manager.delete(repo.id)
                    log.info(
                        "%s: deleting container registry repository %s",
                        project.full_path,
                        repo.path,
                    )
            except (GitlabError, RequestException) as exc:
                log.error("registry purge of %s failed: %s", project.full_path, exc)
                errors[project.full_path] = f"registry purge failed: {exc}"
                continue
            waiting[project.id] = (project, {repo.id for repo in repos})
        return waiting

    def _wait_for_purges(
        self, waiting: dict[int, tuple[ProjectNode, set[int]]], errors: dict[str, str]
    ) -> None:
        deadline = self._clock() + self.registry_timeout
        while waiting:
            for project_id, (project, repo_ids) in list(waiting.items()):
                error = self._registry_state(project_id, repo_ids)
                if error is None:
                    continue  # still being removed (or transient error): poll again
                if error:
                    errors[project.full_path] = error
                else:
                    log.info("%s: container registry purged", project.full_path)
                del waiting[project_id]
            if not waiting:
                break
            if self._clock() >= deadline:
                for project, _ in waiting.values():
                    errors[project.full_path] = (
                        f"registry purge still running after {self.registry_timeout:.0f}s; "
                        "project not deleted, re-run later"
                    )
                break
            log.info(
                "waiting for GitLab to remove registry images of %d project(s)...", len(waiting)
            )
            self._sleep(self.poll_interval)

    def _set_archived(self, project: ProjectNode, archived: bool) -> str | None:
        """(Un)archive ``project``; return an error message or ``None``."""
        action = "archive" if archived else "unarchive"
        try:
            getattr(self._lazy_project(project.id), action)()
        except (GitlabError, RequestException) as exc:
            log.error("could not %s %s: %s", action, project.full_path, exc)
            return f"could not {action} project ({exc})"
        log.info("%s: %sd", project.full_path, action)
        return None

    def _registry_state(self, project_id: int, repo_ids: set[int]) -> str | None:
        """``""`` when all ``repo_ids`` are gone, an error message when GitLab gave up
        on one, ``None`` while removal is still in progress."""
        try:
            current = list(self._lazy_project(project_id).repositories.list(iterator=True))
        except GitlabListError as exc:
            if exc.response_code == 404:
                return ""
            log.warning("could not check registry of project %s: %s", project_id, exc)
            return None
        except (GitlabError, RequestException) as exc:
            log.warning("could not check registry of project %s: %s", project_id, exc)
            return None
        remaining = [r for r in current if int(r.id) in repo_ids]
        if not remaining:
            return ""
        failed = [r for r in remaining if getattr(r, "status", None) == "delete_failed"]
        if failed:
            paths = ", ".join(str(getattr(r, "path", r.id)) for r in failed)
            return f"GitLab failed to delete registry repository {paths}"
        return None

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
