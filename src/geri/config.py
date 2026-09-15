"""Configuration (pydantic-settings) and GitLab client construction."""

from __future__ import annotations

import gitlab
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    Values are read from (highest precedence first): explicit constructor
    kwargs (CLI flags), environment variables, a ``.env`` file in the current
    working directory. Environment variable names are exactly ``GITLAB_URL``
    and ``GITLAB_TOKEN``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    gitlab_url: str = Field(
        validation_alias="GITLAB_URL",
        description="Base URL of the GitLab instance, e.g. https://gitlab.example.com.",
    )
    gitlab_token: SecretStr = Field(
        validation_alias="GITLAB_TOKEN",
        description="Personal access token (scope: api).",
    )


def get_client(settings: Settings) -> gitlab.Gitlab:
    """Build an authenticated python-gitlab client for ``settings``.

    Calls ``.auth()`` so that an invalid URL or token fails fast with a
    ``gitlab.exceptions.GitlabAuthenticationError``.
    """
    client = gitlab.Gitlab(
        url=settings.gitlab_url.rstrip("/"),
        private_token=settings.gitlab_token.get_secret_value(),
        per_page=100,
    )
    client.auth()
    return client
