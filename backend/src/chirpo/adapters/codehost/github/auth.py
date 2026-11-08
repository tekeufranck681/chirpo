"""Logging in to GitHub: as the GitHub App (production) or with a personal token (development only).

As an App, chirpo first signs a short JWT with the App's private key, asks GitHub which installation
covers a repo, then exchanges the JWT for an installation token (valid 1 hour). githubkit caches the
JWT and the tokens and renews them before they expire; this class remembers each installation.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from githubkit import AppAuthStrategy, GitHub, TokenAuthStrategy
from githubkit.cache import MemCacheStrategy
from githubkit.retry import RetryDecisionFunc
from githubkit_schemas.latest.models import Installation

from chirpo.adapters.codehost.github.errors import retry_policy, translate_errors
from chirpo.config import Settings
from chirpo.core.domain import RepoRef
from chirpo.core.errors import CodeHostForbiddenError, CodeHostNotFoundError

USER_AGENT = "chirpo"


class GitHubSetupError(Exception):
    """The GitHub login settings are unusable, e.g. the App key file is missing."""


@dataclass(frozen=True, slots=True)
class Identity:
    kind: Literal["app", "token"]
    name: str


@dataclass(frozen=True, slots=True)
class RepoAccess:
    full_name: str
    private: bool
    # What the App's installation may do (e.g. issues=write); empty when using a token.
    permissions: Mapping[str, str]


def load_private_key(path: Path) -> str:
    try:
        key = path.read_text()
    except OSError as exc:
        raise GitHubSetupError(f"Can't read the GitHub App private key at {path}") from exc
    if "-----BEGIN" not in key or "PRIVATE KEY-----" not in key:
        raise GitHubSetupError(f"{path} is not a PEM private key (the .pem file from GitHub)")
    return key


class GitHubAuth:
    def __init__(
        self,
        *,
        app: AppAuthStrategy | None = None,
        token: str | None = None,
        auto_retry: bool | RetryDecisionFunc = retry_policy,
    ) -> None:
        if (app is None) == (not token):
            raise ValueError("pass exactly one of app or token")
        self._app = app
        self._github: GitHub[Any] = GitHub(
            app or TokenAuthStrategy(token or ""),
            user_agent=USER_AGENT,
            cache_strategy=MemCacheStrategy(),
            http_cache=False,
            auto_retry=auto_retry,
        )
        self._installations: dict[str, int] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "GitHubAuth | None":
        """The App when configured, else the dev token, else None (GitHub not set up)."""
        if settings.github_app_id is not None and settings.github_app_private_key_path:
            key = load_private_key(settings.github_app_private_key_path)
            return cls(app=AppAuthStrategy(settings.github_app_id, key))
        if settings.github_dev_token and settings.github_dev_token.get_secret_value():
            return cls(token=settings.github_dev_token.get_secret_value())
        return None

    @property
    def is_app(self) -> bool:
        return self._app is not None

    async def for_repo(self, repo: RepoRef) -> GitHub[Any]:
        """A client allowed to act on `repo` only (an installation token scoped to that repo)."""
        if self._app is None:
            return self._github
        installation_id = self._installations.get(repo.key)
        if installation_id is None:
            installation_id = (await self._installation(repo)).id
        return self._github.with_auth(
            self._app.as_installation(installation_id, repositories=[repo.name.lower()])
        )

    def forget(self, repo: RepoRef) -> None:
        """Drop the cached installation, e.g. after the App was reinstalled (its id changes)."""
        self._installations.pop(repo.key, None)

    async def whoami(self) -> Identity:
        if self._app is not None:
            with translate_errors("get the GitHub App"):
                app = (await self._github.rest.apps.async_get_authenticated()).parsed_data
            if app is None:
                raise CodeHostNotFoundError("GitHub returned no App for these credentials")
            return Identity("app", app.slug or app.name)
        with translate_errors("get the token's user"):
            user = (await self._github.rest.users.async_get_authenticated()).parsed_data
        return Identity("token", user.login)

    async def check_repo(self, repo: RepoRef) -> RepoAccess:
        permissions: dict[str, str] = {}
        if self._app is not None:
            installation = await self._installation(repo)
            permissions = installation.permissions.model_dump(exclude_unset=True)
        github = await self.for_repo(repo)
        with translate_errors(f"get {repo.full_name}"):
            found = (await github.rest.repos.async_get(repo.owner, repo.name)).parsed_data
        return RepoAccess(found.full_name, found.private, permissions)

    async def _installation(self, repo: RepoRef) -> Installation:
        try:
            with translate_errors(f"find the App installation for {repo.full_name}"):
                response = await self._github.rest.apps.async_get_repo_installation(
                    repo.owner, repo.name
                )
        except CodeHostNotFoundError as exc:
            raise CodeHostForbiddenError(
                f"The GitHub App is not installed on {repo.full_name}, or the repo doesn't exist"
            ) from exc
        self._installations[repo.key] = response.parsed_data.id
        return response.parsed_data
