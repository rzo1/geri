# Geri

[![CI](https://github.com/rzo1/geri/actions/workflows/ci.yml/badge.svg)](https://github.com/rzo1/geri/actions/workflows/ci.yml)

<img src="docs/logo.png" alt="Geri logo" width="200" align="right">

> *Geri, "the ravenous one" — Freki's twin and Odin's other wolf. Where Freki
> devours everything to keep it, Geri tears it down.*

Schedule a GitLab **group**, a **project**, or **all projects in your
personal namespace** for deletion — safely. Geri first lists the target and
everything inside it (all subgroups and projects, at any depth) as a tree, asks
you to confirm by typing the target's full path, and only then sends the
deletion request. Works with any self-hosted GitLab or gitlab.com,
authenticated by a personal access token (PAT).

Twin of [Freki](https://github.com/rzo1/freki), Odin's other wolf: back
everything up with Freki first, then let Geri clean up.

```
$ geri group faculty/course-2025
Authenticated as alice at https://gitlab.example.com
Listing group faculty/course-2025 (incl. subgroups)...
course-2025/ faculty/course-2025 (group 1234)
├── team-1/ faculty/course-2025/team-1 (group 1240)
│   ├── backend faculty/course-2025/team-1/backend (project 5678)
│   └── frontend faculty/course-2025/team-1/frontend (project 5679) archived
├── team-2/ faculty/course-2025/team-2 (group 1241)
│   └── app faculty/course-2025/team-2/app (project 5690)
└── handout faculty/course-2025/handout (project 5601)
Group contains 2 subgroup(s) and 4 project(s) (1 archived).

WARNING: this schedules group faculty/course-2025 including all 2 subgroup(s)
and 4 project(s) (1 archived) listed above for deletion. ...
Type the full path 'faculty/course-2025' to confirm: faculty/course-2025
Scheduled group faculty/course-2025 for deletion on 2026-09-22.
```

## Requirements

- Python >= 3.14 and [uv](https://docs.astral.sh/uv/)
- A GitLab PAT with the `api` scope, belonging to a user with the **Owner**
  role on the group (or the rights to delete the project). `geri user` only
  needs the token of the namespace's own user

## Installation

```bash
git clone https://github.com/rzo1/geri.git
cd geri
uv sync
uv run geri --help
```

Also installed under the alias `dms` (*delete my shit*) — the counterpart to
Freki's `bms`.

## Configuration

Settings are read from environment variables or a `.env` file in the current
directory (see `.env.example`). CLI flags always take precedence.

| Variable       | Default      | Description                     |
|----------------|--------------|---------------------------------|
| `GITLAB_URL`   | *(required)* | Base URL of the GitLab instance |
| `GITLAB_TOKEN` | *(required)* | Personal access token (`api`)   |

## Usage

```bash
# A group, including all its subgroups and projects
geri group <GROUP_ID_OR_PATH>          # e.g. 1234 or faculty/course

# Only the projects directly in a group; the group and its subgroups are kept
geri group <GROUP_ID_OR_PATH> --no-subgroups

# A single project
geri project <PROJECT_ID_OR_PATH>      # e.g. 5678 or faculty/course/repo

# All projects in your personal namespace (<your-username>/...)
geri user

# Projects with container registry images: delete the images first
geri project faculty/course/app --purge-registry

# Only look, never prompt or delete
geri group faculty/course --dry-run
```

| Option             | Description                                                                          |
|--------------------|--------------------------------------------------------------------------------------|
| `--url URL`        | GitLab base URL (overrides `GITLAB_URL`)                                             |
| `--token TOKEN`    | Personal access token (overrides `GITLAB_TOKEN`)                                     |
| `--dry-run`        | Only list what would be deleted; no prompt, no delete                                |
| `--no-subgroups`   | `group` only: keep the group and its subgroups, delete only its direct projects      |
| `--purge-registry` | Delete the projects' container registry images first (**permanent**, see Notes)     |

Each run prints who the token authenticates as and the tree of what would be
deleted. `group` and `project` end with a one-line result; `user` and
`group --no-subgroups` end with a per-project summary table and totals (scheduled / deletion accepted / failed /
skipped).

Exit codes: `0` success (or dry run / already scheduled / nothing left to
delete), `1` a deletion request failed (for `user` / `--no-subgroups`: at least one project),
`2` configuration, authentication, connection or "not found" errors,
`3` confirmation not given — nothing was deleted.

## Notes

- **What "scheduled" means**: Geri calls `DELETE /groups/:id` or
  `DELETE /projects/:id`. With *delayed deletion* (GitLab Premium/Ultimate, and
  all tiers since GitLab 18.0) the target is only **marked** for deletion and
  can be restored from the GitLab UI until the date Geri reports. On instances
  without delayed deletion the removal starts **immediately** and cannot be
  undone — Geri tells you which of the two happened.
- **Groups go as a whole**: only the group itself is deleted; GitLab takes
  every subgroup and project along. The tree therefore includes archived
  projects and items already marked for deletion. Projects merely *shared*
  with the group are not part of it, are not listed and are not deleted.
- **Keeping subgroups** (`geri group <group> --no-subgroups`): GitLab cannot
  delete a group but spare its subgroups, so instead Geri keeps the group and
  every subgroup (shown collapsed and marked `kept`, with what is inside) and
  deletes only the projects **directly** in the group, one by one after a
  single confirmation. Like `geri user`, failures do not stop the rest and
  already-scheduled projects are skipped.
- **Personal namespaces** (`geri user`) cannot be deleted as a whole — GitLab
  only deletes groups, and your namespace belongs to your user account. Geri
  therefore lists every project under `<your-username>/` (archived ones
  included) and deletes them **one by one** after a single confirmation (type
  your username). A failing project does not stop the rest; projects already
  marked for deletion are skipped. Group projects you are merely a member of
  are never included, and your user account itself is not touched. Heads-up:
  older GitLab versions (before 18.0) delete personal-namespace projects
  **immediately**, even where group projects are only scheduled — the summary
  shows `deleting` instead of `scheduled` in that case, so try
  `geri project <your-username>/<something-unimportant>` first if unsure.
- **Container registry** (`--purge-registry`): GitLab refuses to delete a
  project whose container registry still holds tags ("Cannot rename or delete
  project because it contains container registry tags"); without the option
  Geri reports that failure and points you at `--purge-registry`. With it, Geri
  lists the registry repositories (with tag counts) of every project that is
  about to be deleted below that project in the tree, deletes them after your
  confirmation, **waits until GitLab has removed them** (up to 10 minutes), and
  only then deletes the project or group. Archived projects are read-only, so
  GitLab refuses to delete their images; Geri unarchives them for the purge and
  archives them again right afterwards (marked in the tree). A project whose
  purge fails or does not finish in time is not deleted, and a group is not
  deleted if any of its projects' purges failed. **Registry images are deleted immediately and
  permanently** — delayed deletion does not cover them, and Freki does not back
  them up; `docker pull` anything you still need first.
- **Confirmation** requires typing the exact full path of the target. Anything
  else — including an empty answer, Ctrl+C or a closed stdin — aborts with
  exit code `3`.
- **No surprises between listing and deleting**: every deletion request uses
  the numeric id resolved while listing, never the path you typed, and a
  listing error aborts the run instead of showing an incomplete tree.
- **Already scheduled** targets are reported and left untouched; Geri never
  requests permanent removal.

## Development

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Lint hooks are defined in `.pre-commit-config.yaml`; install them with
[prek](https://github.com/j178/prek) (or classic pre-commit):

```bash
uvx prek install      # or: uvx pre-commit install
```

## License

[MIT](LICENSE)

Logo generated by ChatGPT.
