"""Typer CLI entry point (``geri group|project|user``, alias ``dms``)."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Annotated

import typer
from gitlab.exceptions import GitlabAuthenticationError, GitlabError, GitlabGetError
from pydantic import ValidationError
from requests.exceptions import RequestException
from rich import get_console
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.table import Table
from rich.tree import Tree

from geri import __version__
from geri.config import Settings, get_client
from geri.deleter import Deleter, DeletionResult
from geri.discovery import TreeDiscovery
from geri.models import GroupNode, ProjectNode, Registries, UserNamespaceNode

EXIT_OK = 0
EXIT_FAILED = 1  # a deletion request failed
EXIT_USAGE = 2  # configuration / authentication / not-found errors
EXIT_ABORTED = 3  # confirmation not given; nothing was deleted

console = get_console()
err_console = Console(stderr=True)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
    help=(
        "Schedule a GitLab group, a project, or all projects in your personal namespace for "
        "deletion.\n\n"
        "The target and everything inside it (subgroups, projects) is listed as a tree and "
        "must be confirmed by typing its full path. Settings are read from environment "
        "variables or a [bold].env[/bold] file (GITLAB_URL, GITLAB_TOKEN); command-line "
        "options take precedence."
    ),
)

# --------------------------------------------------------------------------- options

UrlOpt = Annotated[
    str | None,
    typer.Option(
        "--url",
        help="Base URL of the GitLab instance.",
        rich_help_panel="Connection",
        show_default="$GITLAB_URL",
    ),
]
TokenOpt = Annotated[
    str | None,
    typer.Option(
        "--token",
        envvar="GITLAB_TOKEN",
        prompt=False,
        hide_input=True,
        show_default=False,
        help="Personal access token (scope: api).",
        rich_help_panel="Connection",
    ),
]
DryRunOpt = Annotated[
    bool,
    typer.Option("--dry-run", help="Only list what would be deleted; do not prompt or delete."),
]
NoSubgroupsOpt = Annotated[
    bool,
    typer.Option(
        "--no-subgroups",
        help=(
            "Keep the group and all its subgroups (with their projects); only schedule the "
            "projects directly in the group for deletion, one by one."
        ),
    ),
]
PurgeRegistryOpt = Annotated[
    bool,
    typer.Option(
        "--purge-registry",
        help=(
            "Delete the container registry images of the projects first (GitLab refuses to "
            "delete projects that have any). Permanent: images are not restorable."
        ),
    ),
]


@dataclass(frozen=True)
class CommonOptions:
    """Command-line values shared by all commands (``None`` = not given)."""

    url: str | None = None
    token: str | None = None
    dry_run: bool = False
    no_subgroups: bool = False
    purge_registry: bool = False

    def to_settings(self) -> Settings:
        """Build ``Settings`` from env/.env, overridden by the given CLI values."""
        overrides = {"gitlab_url": self.url, "gitlab_token": self.token}
        return Settings(**{k: v for k, v in overrides.items() if v is not None})


# --------------------------------------------------------------------------- helpers


def coerce_id(value: str) -> int | str:
    """Numeric ids become ``int`` so python-gitlab does not URL-encode them as paths."""
    value = value.strip()
    return int(value) if value.isdigit() else value


def _setup_logging() -> None:
    logger = logging.getLogger("geri")
    if any(isinstance(h, RichHandler) for h in logger.handlers):
        return
    handler = RichHandler(
        console=console, show_path=False, show_time=False, rich_tracebacks=False, markup=False
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _fail(message: str, code: int = EXIT_USAGE) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {escape(message)}")
    raise typer.Exit(code)


def _flags(archived: bool, marked_on: str | None) -> str:
    flags = []
    if archived:
        flags.append("[yellow]archived[/yellow]")
    if marked_on:
        flags.append(f"[red]scheduled for deletion on {escape(marked_on)}[/red]")
    return f" {' '.join(flags)}" if flags else ""


def _group_label(group: GroupNode) -> str:
    return (
        f"[bold blue]{escape(group.name)}/[/bold blue] "
        f"[dim]{escape(group.full_path)} (group {group.id})[/dim]"
        f"{_flags(False, group.marked_for_deletion_on)}"
    )


def _project_label(project: ProjectNode) -> str:
    return (
        f"{escape(project.name)} "
        f"[dim]{escape(project.full_path)} (project {project.id})[/dim]"
        f"{_flags(project.archived, project.marked_for_deletion_on)}"
    )


def _user_label(namespace: UserNamespaceNode) -> str:
    return (
        f"[bold magenta]{escape(namespace.username)}/[/bold magenta] "
        f"[dim]personal namespace of {escape(namespace.username)}[/dim]"
    )


def _kept_label(group: GroupNode) -> str:
    inside = f"{len(group.walk_groups())} subgroup(s), {len(group.walk_projects())} project(s)"
    return f"{_group_label(group)} [green]kept ({inside} inside)[/green]"


def _tags(count: int | None) -> str:
    return "? tags" if count is None else f"{count} tag(s)"


def _add_registries(branch: Tree, project: ProjectNode, registries: Registries) -> None:
    if project.archived and registries.get(project.id):
        branch.add(
            "[yellow]unarchived temporarily for the purge, archived again afterwards[/yellow]"
        )
    for repo in registries.get(project.id, []):
        branch.add(
            f"[magenta]registry[/magenta] {escape(repo.path)} [dim]({_tags(repo.tags_count)})"
            "[/dim] [red]purged permanently[/red]"
        )


Target = GroupNode | ProjectNode | UserNamespaceNode


def build_tree(
    target: Target, keep_subgroups: bool = False, registries: Registries | None = None
) -> Tree:
    """Rich tree of ``target``: subgroups first, then projects, both sorted by path.

    With ``keep_subgroups`` the subgroups of a group are shown collapsed and marked
    as kept instead of being expanded. Container ``registries`` to purge are shown
    below their project.
    """
    registries = registries or {}

    def add_project(branch: Tree, project: ProjectNode) -> None:
        _add_registries(branch.add(_project_label(project)), project, registries)

    if isinstance(target, ProjectNode):
        tree = Tree(_project_label(target))
        _add_registries(tree, target, registries)
        return tree
    if isinstance(target, UserNamespaceNode):
        tree = Tree(_user_label(target))
        for project in target.projects:
            add_project(tree, project)
        return tree

    def add(branch: Tree, group: GroupNode) -> None:
        for sub in group.subgroups:
            add(branch.add(_group_label(sub)), sub)
        for project in group.projects:
            add_project(branch, project)

    tree = Tree(_group_label(target))
    if keep_subgroups:
        for sub in target.subgroups:
            tree.add(_kept_label(sub))
        for project in target.projects:
            add_project(tree, project)
        return tree
    add(tree, target)
    return tree


def _describe_registries(registries: Registries) -> str:
    repos = [repo for repo_list in registries.values() for repo in repo_list]
    tags = sum(repo.tags_count or 0 for repo in repos)
    return (
        f"{len(repos)} container registry repository(ies) with {tags} tag(s) in "
        f"{len(registries)} project(s)"
    )


_REGISTRY_ERROR_MARKER = "container registry"


def _with_hint(message: str, purge_registry: bool) -> str:
    """Point at --purge-registry when GitLab refused because of registry tags."""
    if not purge_registry and _REGISTRY_ERROR_MARKER in message.lower():
        return f"{message} (re-run with --purge-registry to delete the images first)"
    return message


def _describe_projects(projects: list[ProjectNode], extra_scheduled: int = 0) -> str:
    extras = []
    archived = sum(1 for p in projects if p.archived)
    scheduled = sum(1 for p in projects if p.marked_for_deletion_on) + extra_scheduled
    if archived:
        extras.append(f"{archived} archived")
    if scheduled:
        extras.append(f"{scheduled} already scheduled")
    suffix = f" ({', '.join(extras)})" if extras else ""
    return f"{len(projects)} project(s){suffix}"


def _describe_contents(target: GroupNode | UserNamespaceNode) -> str:
    if isinstance(target, UserNamespaceNode):
        return _describe_projects(target.projects)
    subgroups = target.walk_groups()
    scheduled_groups = sum(1 for g in subgroups if g.marked_for_deletion_on)
    return f"{len(subgroups)} subgroup(s) and " + _describe_projects(
        target.walk_projects(), scheduled_groups
    )


def _confirm(
    kind: str,
    target: Target,
    pending: list[ProjectNode],
    keep_subgroups: bool,
    registries: Registries,
) -> bool:
    """Ask the user to type the target's full path; return whether it matched."""
    if isinstance(target, UserNamespaceNode):
        what = (
            f"[bold]{len(pending)} project(s)[/bold] in the personal namespace "
            f"[bold]{escape(target.username)}[/bold] for deletion, one by one (all listed "
            "above except those already scheduled). Your user account itself is not touched"
        )
    elif isinstance(target, GroupNode) and keep_subgroups:
        what = (
            f"[bold]{len(pending)} project(s)[/bold] directly in group "
            f"[bold]{escape(target.full_path)}[/bold] for deletion, one by one (all listed "
            "above except those already scheduled). The group itself and its "
            f"{len(target.subgroups)} subgroup(s) marked as kept are not touched"
        )
    else:
        what = f"{kind} [bold]{escape(target.full_path)}[/bold]"
        if isinstance(target, GroupNode):
            what += f" including all {_describe_contents(target)} listed above"
        what += " for deletion"
    purge = ""
    if registries:
        purge = (
            f"\n[bold red]First, {_describe_registries(registries)} are deleted "
            "permanently; container images are never restorable.[/bold red]"
        )
    console.print(
        f"\n[bold red]WARNING:[/bold red] this schedules {what}.{purge}\n"
        "GitLab keeps deleted items restorable until the deletion date if delayed deletion "
        "applies; [bold]otherwise they are removed immediately and cannot be "
        "undone[/bold].",
    )
    try:
        answer = typer.prompt(
            f"Type the full path '{target.full_path}' to confirm",
            default="",
            show_default=False,
        )
    except typer.Abort:
        console.print()
        return False
    return answer.strip() == target.full_path


def _print_result(result: DeletionResult, purge_registry: bool = False) -> None:
    name = f"{result.kind} [bold]{escape(result.full_path)}[/bold]"
    if result.status == "scheduled":
        console.print(
            f"[green]Scheduled[/green] {name} for deletion on "
            f"[bold]{escape(result.deletion_date or '?')}[/bold]."
        )
    elif result.status == "deleting":
        console.print(f"[cyan]Deletion accepted[/cyan] for {name}: {escape(result.message)}.")
    else:
        message = _with_hint(result.message, purge_registry)
        console.print(f"[bold red]Failed[/bold red] to delete {name}: {escape(message)}")


_STATUS_STYLE = {"scheduled": "green", "deleting": "cyan", "failed": "bold red"}


def _summary_table(results: list[DeletionResult], purge_registry: bool = False) -> Table:
    table = Table(title="Summary")
    table.add_column("Project", overflow="fold")
    table.add_column("Status")
    table.add_column("Deletion date / message", overflow="fold")
    for r in results:
        style = _STATUS_STYLE[r.status]
        table.add_row(
            escape(r.full_path),
            f"[{style}]{r.status}[/{style}]",
            escape(r.deletion_date or _with_hint(r.message, purge_registry)),
        )
    return table


def _print_counts(results: list[DeletionResult], skipped: int) -> int:
    """Print the totals line; return the number of failed deletions."""
    counts = Counter(r.status for r in results)
    console.print(f"Total: {len(results) + skipped} project(s)", soft_wrap=True)
    for status, label in (
        ("scheduled", "scheduled"),
        ("deleting", "deletion accepted (immediate)"),
        ("failed", "failed"),
    ):
        n = counts.get(status, 0)
        style = _STATUS_STYLE[status] if n else "dim"
        console.print(f"  [{style}]{n:>5} {label}[/{style}]", soft_wrap=True)
    style = "yellow" if skipped else "dim"
    console.print(f"  [{style}]{skipped:>5} skipped (already scheduled)[/{style}]", soft_wrap=True)
    return counts.get("failed", 0)


def _discover(kind: str, target_ref: str | None, discovery: TreeDiscovery) -> Target:
    if kind == "group":
        assert target_ref is not None
        console.print(f"Listing group [bold]{escape(target_ref)}[/bold] (incl. subgroups)...")
        return discovery.get_group_tree(coerce_id(target_ref))
    if kind == "project":
        assert target_ref is not None
        console.print(f"Looking up project [bold]{escape(target_ref)}[/bold]...")
        return discovery.get_project(coerce_id(target_ref))
    console.print("Listing the projects in your personal namespace...")
    return discovery.get_user_namespace()


def _execute(kind: str, target_ref: str | None, opts: CommonOptions) -> None:
    """Shared workflow: settings -> auth -> discover -> tree -> confirm -> delete."""
    _setup_logging()

    try:
        settings = opts.to_settings()
    except ValidationError as exc:
        missing = [".".join(str(p) for p in e["loc"]) for e in exc.errors()]
        if any("GITLAB_URL" in m or "gitlab_url" in m for m in missing):
            _fail("no GitLab URL given. Use --url, set GITLAB_URL, or add it to .env.")
        if any("GITLAB_TOKEN" in m or "gitlab_token" in m for m in missing):
            _fail("no token given. Use --token, set GITLAB_TOKEN, or add it to .env.")
        _fail(f"invalid configuration: {exc}")

    try:
        client = get_client(settings)
    except GitlabAuthenticationError as exc:
        _fail(f"authentication against {settings.gitlab_url} failed: {exc}")
    except (GitlabError, RequestException) as exc:
        _fail(f"could not talk to {settings.gitlab_url}: {exc}")

    username = getattr(getattr(client, "user", None), "username", None) or "<unknown>"
    console.print(
        f"Authenticated as [bold]{escape(str(username))}[/bold] at {escape(settings.gitlab_url)}"
    )

    label = kind if target_ref is None else f"{kind} '{target_ref}'"
    try:
        target = _discover(kind, target_ref, TreeDiscovery(client))
    except GitlabGetError as exc:
        _fail(f"{label} not found or not accessible: {exc}")
    except (GitlabError, RequestException) as exc:
        _fail(f"listing {label} failed: {exc}")

    keep_subgroups = opts.no_subgroups and isinstance(target, GroupNode)
    # Project-by-project mode: personal namespaces, and groups whose subgroups are kept.
    bulk = keep_subgroups or isinstance(target, UserNamespaceNode)
    pending: list[ProjectNode] = (
        [p for p in target.projects if not p.marked_for_deletion_on] if bulk else []
    )

    registries: Registries = {}
    if opts.purge_registry:
        if bulk:
            affected = pending
        elif isinstance(target, GroupNode):
            affected = [] if target.marked_for_deletion_on else target.walk_projects()
        else:
            affected = [] if target.marked_for_deletion_on else [target]
        if affected:
            console.print(f"Checking container registries of {len(affected)} project(s)...")
        try:
            registries = TreeDiscovery(client).get_registries(affected)
        except (GitlabError, RequestException) as exc:
            _fail(f"listing container registries failed: {exc}")

    console.print(build_tree(target, keep_subgroups=keep_subgroups, registries=registries))
    if keep_subgroups:
        console.print(
            f"Group contains {_describe_projects(target.projects)} directly; its "
            f"{len(target.subgroups)} subgroup(s) are kept."
        )
    elif isinstance(target, GroupNode):
        console.print(f"Group contains {_describe_contents(target)}.")
    elif isinstance(target, UserNamespaceNode):
        console.print(f"Personal namespace contains {_describe_contents(target)}.")
    if opts.purge_registry:
        if registries:
            console.print(f"[red]To purge: {_describe_registries(registries)}.[/red]")
        else:
            console.print("No container registry images to purge.")

    if bulk:
        if not pending:
            console.print("[yellow]No projects left to delete; nothing to do.[/yellow]")
            raise typer.Exit(EXIT_OK)
    elif target.marked_for_deletion_on:
        console.print(
            f"[yellow]{kind.capitalize()} {escape(target.full_path)} is already scheduled for "
            f"deletion on {escape(target.marked_for_deletion_on)}; nothing to do.[/yellow]"
        )
        raise typer.Exit(EXIT_OK)

    if opts.dry_run:
        console.print("[yellow]Dry run:[/yellow] nothing will be deleted.")
        raise typer.Exit(EXIT_OK)

    if not _confirm(kind, target, pending, keep_subgroups, registries):
        console.print("[yellow]Confirmation did not match. Nothing was deleted.[/yellow]")
        raise typer.Exit(EXIT_ABORTED)

    deleter = Deleter(client)
    if bulk:
        results = deleter.delete_projects(pending, registries)
        console.print(_summary_table(results, opts.purge_registry))
        if _print_counts(results, skipped=len(target.projects) - len(pending)):
            raise typer.Exit(EXIT_FAILED)
        return

    if isinstance(target, GroupNode):
        result = deleter.delete_group(target, registries)
    else:
        result = deleter.delete_project(target, registries)

    _print_result(result, opts.purge_registry)
    if result.status == "failed":
        raise typer.Exit(EXIT_FAILED)


# --------------------------------------------------------------------------- commands


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"geri {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Schedule GitLab groups and projects for deletion."""


@app.command()
def group(
    group_id_or_path: Annotated[
        str, typer.Argument(help="Numeric group id or full path, e.g. 'faculty/course'.")
    ],
    url: UrlOpt = None,
    token: TokenOpt = None,
    dry_run: DryRunOpt = False,
    no_subgroups: NoSubgroupsOpt = False,
    purge_registry: PurgeRegistryOpt = False,
) -> None:
    """List a group with all subgroups and projects, then schedule it for deletion.

    With --no-subgroups the group and its subgroups are kept and only the projects
    directly in the group are scheduled for deletion.
    """
    _execute(
        "group",
        group_id_or_path,
        CommonOptions(
            url=url,
            token=token,
            dry_run=dry_run,
            no_subgroups=no_subgroups,
            purge_registry=purge_registry,
        ),
    )


@app.command()
def project(
    project_id_or_path: Annotated[
        str, typer.Argument(help="Numeric project id or full path, e.g. 'group/sub/project'.")
    ],
    url: UrlOpt = None,
    token: TokenOpt = None,
    dry_run: DryRunOpt = False,
    purge_registry: PurgeRegistryOpt = False,
) -> None:
    """Show a project, then schedule it for deletion."""
    _execute(
        "project",
        project_id_or_path,
        CommonOptions(url=url, token=token, dry_run=dry_run, purge_registry=purge_registry),
    )


@app.command()
def user(
    url: UrlOpt = None,
    token: TokenOpt = None,
    dry_run: DryRunOpt = False,
    purge_registry: PurgeRegistryOpt = False,
) -> None:
    """List all projects in your personal namespace, then schedule each for deletion.

    Covers only projects under <your-username>/ (not group projects you are a member
    of); your user account itself is not touched.
    """
    _execute(
        "user",
        None,
        CommonOptions(url=url, token=token, dry_run=dry_run, purge_registry=purge_registry),
    )


if __name__ == "__main__":  # pragma: no cover
    app()
