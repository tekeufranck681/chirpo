"""GitHub login (App and dev token) against saved payloads and a mocked GitHub API (no network)."""

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from githubkit import AppAuthStrategy, Response
from githubkit.exception import PrimaryRateLimitExceeded, RequestFailed

from chirpo.adapters.codehost.github.auth import GitHubAuth, GitHubSetupError, load_private_key
from chirpo.adapters.codehost.github.errors import retry_policy, translate_errors
from chirpo.config import Settings
from chirpo.core.domain import RepoRef
from chirpo.core.errors import (
    CodeHostConflictError,
    CodeHostForbiddenError,
    CodeHostNotFoundError,
    CodeHostRateLimitedError,
    CodeHostUnavailableError,
    CodeHostValidationError,
)
from chirpo.security import generate_key
from tests.integration.github_api import API, mocked_github, payload

APP_ID = 1234567
INSTALLATION_ID = 87654321
REPO = RepoRef(provider="github", owner="tekeufranck681", name="chirpo")
INSTALLATION_URL = f"{API}/repos/tekeufranck681/chirpo/installation"
TOKEN_URL = f"{API}/app/installations/{INSTALLATION_ID}/access_tokens"
REPO_URL = f"{API}/repos/tekeufranck681/chirpo"


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_pem(private_key: rsa.RSAPrivateKey) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def app_auth(private_pem: str) -> GitHubAuth:
    return GitHubAuth(app=AppAuthStrategy(APP_ID, private_pem), auto_retry=False)


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with mocked_github() as router:
        yield router


def authorization(route: respx.Route) -> str:
    return route.calls.last.request.headers["Authorization"]


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, encryption_key=generate_key(), **values)  # type: ignore[call-arg]


def token_auth() -> GitHubAuth:
    return GitHubAuth(token="ghp_devtoken", auto_retry=False)


# ------------------------------------------------------------------ App login


async def test_app_uses_a_signed_jwt_then_a_repo_scoped_installation_token(
    app_auth: GitHubAuth, github: respx.MockRouter, private_key: rsa.RSAPrivateKey
) -> None:
    lookup = github.get(INSTALLATION_URL).respond(json=payload("installation"))
    mint = github.post(TOKEN_URL).respond(201, json=payload("access_token"))
    repo = github.get(REPO_URL).respond(json=payload("repo"))

    access = await app_auth.check_repo(REPO)

    assert access.full_name == "tekeufranck681/chirpo"
    assert access.permissions["issues"] == "write"
    scheme, app_jwt = authorization(lookup).split()
    claims = jwt.decode(app_jwt, private_key.public_key(), algorithms=["RS256"])
    assert (scheme, claims["iss"]) == ("Bearer", str(APP_ID))
    assert json.loads(mint.calls.last.request.content)["repositories"] == ["chirpo"]
    assert authorization(repo) == f"token {payload('access_token')['token']}"


async def test_installation_and_token_are_reused(
    app_auth: GitHubAuth, github: respx.MockRouter
) -> None:
    lookup = github.get(INSTALLATION_URL).respond(json=payload("installation"))
    mint = github.post(TOKEN_URL).respond(201, json=payload("access_token"))
    github.get(REPO_URL).respond(json=payload("repo"))

    for text in ("tekeufranck681/chirpo", "TekeuFranck681/Chirpo"):
        client = await app_auth.for_repo(RepoRef.parse("github", text))
        await client.rest.repos.async_get(REPO.owner, REPO.name)

    assert (lookup.call_count, mint.call_count) == (1, 1)


async def test_app_not_installed_on_repo(app_auth: GitHubAuth, github: respx.MockRouter) -> None:
    github.get(INSTALLATION_URL).respond(404, json={"message": "Not Found"})
    with pytest.raises(CodeHostForbiddenError, match="not installed on tekeufranck681/chirpo"):
        await app_auth.for_repo(REPO)


async def test_token_minting_failure_is_translated(
    app_auth: GitHubAuth, github: respx.MockRouter
) -> None:
    github.get(INSTALLATION_URL).respond(json=payload("installation"))
    github.post(TOKEN_URL).respond(403, json={"message": "This installation has been suspended"})
    client = await app_auth.for_repo(REPO)
    with pytest.raises(CodeHostForbiddenError), translate_errors("get repo"):
        await client.rest.repos.async_get(REPO.owner, REPO.name)


async def test_whoami_as_app(app_auth: GitHubAuth, github: respx.MockRouter) -> None:
    route = github.get("/app").respond(json=payload("app"))
    identity = await app_auth.whoami()
    assert (identity.kind, identity.name) == ("app", "chirpo-dev-tekeu")
    assert authorization(route).startswith("Bearer ")


# ------------------------------------------------------------------ dev token


async def test_dev_token_is_used_for_every_repo(github: respx.MockRouter) -> None:
    auth = token_auth()
    github.get("/user").respond(json=payload("user"))
    repo = github.get(REPO_URL).respond(json=payload("repo"))

    identity = await auth.whoami()
    access = await auth.check_repo(REPO)

    assert (identity.kind, identity.name) == ("token", "tekeufranck681")
    assert access.permissions == {}
    assert authorization(repo) == "token ghp_devtoken"
    assert not auth.is_app


def test_exactly_one_login_method(private_pem: str) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GitHubAuth()
    with pytest.raises(ValueError, match="exactly one"):
        GitHubAuth(app=AppAuthStrategy(APP_ID, private_pem), token="ghp_x")


# ------------------------------------------------------------------ settings


def test_nothing_configured_means_no_github() -> None:
    assert GitHubAuth.from_settings(settings()) is None


def test_app_is_preferred_over_dev_token(tmp_path: Path, private_pem: str) -> None:
    key = tmp_path / "app.pem"
    key.write_text(private_pem)
    auth = GitHubAuth.from_settings(
        settings(github_app_id=APP_ID, github_app_private_key_path=key, github_dev_token="ghp_x")
    )
    assert auth is not None and auth.is_app


def test_dev_token_from_settings() -> None:
    auth = GitHubAuth.from_settings(settings(github_dev_token="ghp_x"))
    assert auth is not None and not auth.is_app


def test_missing_key_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(GitHubSetupError, match="Can't read"):
        load_private_key(tmp_path / "missing.pem")


def test_non_pem_key_is_rejected(tmp_path: Path) -> None:
    key = tmp_path / "app.pem"
    key.write_text("not a key")
    with pytest.raises(GitHubSetupError, match="not a PEM private key"):
        load_private_key(key)


# ------------------------------------------------------------------ errors and retries


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, CodeHostForbiddenError),
        (403, CodeHostForbiddenError),
        (404, CodeHostNotFoundError),
        (409, CodeHostConflictError),
        (422, CodeHostValidationError),
        (500, CodeHostUnavailableError),
        (502, CodeHostUnavailableError),
    ],
)
async def test_http_errors_become_core_errors(
    github: respx.MockRouter, status: int, error: type[Exception]
) -> None:
    github.get("/user").respond(status, json={"message": "nope"})
    with pytest.raises(error):
        await token_auth().whoami()


async def test_rate_limit_becomes_core_error_with_wait(github: respx.MockRouter) -> None:
    github.get("/user").respond(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
        json={"message": "API rate limit exceeded"},
    )
    with pytest.raises(CodeHostRateLimitedError) as caught:
        await token_auth().whoami()
    assert caught.value.retry_after_seconds > 0


async def test_network_failure_is_unavailable(github: respx.MockRouter) -> None:
    github.get("/user").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CodeHostUnavailableError):
        await token_auth().whoami()


def _response(status: int) -> Response[Any, Any]:
    return Response(httpx.Response(status, request=httpx.Request("GET", f"{API}/user")), Any)


def test_server_errors_are_retried_twice() -> None:
    assert retry_policy(RequestFailed(_response(502)), 0).do_retry
    assert retry_policy(RequestFailed(_response(502)), 1).do_retry
    assert not retry_policy(RequestFailed(_response(502)), 2).do_retry
    assert not retry_policy(RequestFailed(_response(404)), 0).do_retry


@pytest.mark.parametrize(
    ("wait", "attempt", "retried"), [(3, 0, True), (3, 1, False), (600, 0, False)]
)
def test_only_short_rate_limit_waits_are_retried_once(
    wait: int, attempt: int, retried: bool
) -> None:
    exc = PrimaryRateLimitExceeded(_response(403), timedelta(seconds=wait))
    assert retry_policy(exc, attempt).do_retry is retried


def test_writes_are_not_retried_after_a_server_error() -> None:
    request = httpx.Request("POST", f"{API}/repos/o/r/issues")
    exc = RequestFailed(Response(httpx.Response(502, request=request), Any))
    assert not retry_policy(exc, 0).do_retry


async def test_rejected_credentials_are_explained(github: respx.MockRouter) -> None:
    github.get("/user").respond(401, json={"message": "Bad credentials"})
    with pytest.raises(CodeHostForbiddenError, match="credentials"):
        await token_auth().whoami()


def test_empty_dev_token_is_not_a_login() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        GitHubAuth(token="")


async def test_github_reason_is_kept_for_refusals(github: respx.MockRouter) -> None:
    github.get("/user").respond(422, json={"message": "Validation Failed"})
    with pytest.raises(CodeHostValidationError, match="422: Validation Failed"):
        await token_auth().whoami()


async def test_server_error_bodies_are_not_repeated(github: respx.MockRouter) -> None:
    github.get("/user").respond(502, json={"message": "<html>bad gateway</html>"})
    with pytest.raises(CodeHostUnavailableError) as caught:
        await token_auth().whoami()
    assert "html" not in str(caught.value)


def test_graphql_reads_are_retried_after_a_server_error() -> None:
    request = httpx.Request("POST", f"{API}/graphql")
    exc = RequestFailed(Response(httpx.Response(502, request=request), Any))
    assert retry_policy(exc, 0).do_retry
