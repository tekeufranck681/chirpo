"""GitHub device flow against mocked github.com endpoints (payloads as in GitHub's docs)."""

from collections.abc import Iterator
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from pydantic import SecretStr

from chirpo.adapters.codehost.github.device import (
    DEVICE_CODE_URL,
    GRANT_TYPE,
    TOKEN_URL,
    USER_URL,
    GitHubDeviceLogin,
)
from chirpo.core.errors import CodeHostForbiddenError, CodeHostUnavailableError
from chirpo.core.identity import LoginStatus
from tests.integration.github_api import payload

CLIENT_ID = "Iv23liEXAMPLE0000000"
DEVICE_CODE = SecretStr("3584d83530557fdd1f46af8289938c8ef79f9dc5")


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def login() -> GitHubDeviceLogin:
    return GitHubDeviceLogin(CLIENT_ID)


def form(route: respx.Route) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in parse_qs(route.calls.last.request.content.decode()).items()
    }


async def test_start(login: GitHubDeviceLogin, github: respx.MockRouter) -> None:
    route = github.post(DEVICE_CODE_URL).respond(
        json={
            "device_code": DEVICE_CODE.get_secret_value(),
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
    )
    device = await login.start()
    assert form(route) == {"client_id": CLIENT_ID}
    assert route.calls.last.request.headers["Accept"] == "application/json"
    assert (device.user_code, device.verification_url) == (
        "WDJB-MJHT",
        "https://github.com/login/device",
    )
    assert (device.expires_in_seconds, device.interval_seconds) == (900, 5)
    assert device.device_code == DEVICE_CODE
    assert "3584d835" not in repr(device)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("authorization_pending", LoginStatus.PENDING),
        ("access_denied", LoginStatus.DENIED),
        ("expired_token", LoginStatus.EXPIRED),
        ("incorrect_device_code", LoginStatus.EXPIRED),
    ],
)
async def test_waiting_answers(
    login: GitHubDeviceLogin, github: respx.MockRouter, error: str, status: LoginStatus
) -> None:
    route = github.post(TOKEN_URL).respond(json={"error": error})
    check = await login.check(DEVICE_CODE)
    assert (check.status, check.account) == (status, None)
    assert form(route) == {
        "client_id": CLIENT_ID,
        "device_code": DEVICE_CODE.get_secret_value(),
        "grant_type": GRANT_TYPE,
    }


async def test_slow_down_carries_the_new_interval(
    login: GitHubDeviceLogin, github: respx.MockRouter
) -> None:
    github.post(TOKEN_URL).respond(json={"error": "slow_down", "interval": 10})
    check = await login.check(DEVICE_CODE)
    assert (check.status, check.interval_seconds) == (LoginStatus.SLOW_DOWN, 10)


async def test_approval_asks_who_with_the_one_time_token(
    login: GitHubDeviceLogin, github: respx.MockRouter
) -> None:
    github.post(TOKEN_URL).respond(
        json={"access_token": "ghu_onetime", "token_type": "bearer", "expires_in": 28800}
    )
    who = github.get(USER_URL).respond(json=payload("user"))

    check = await login.check(DEVICE_CODE)

    assert who.calls.last.request.headers["Authorization"] == "Bearer ghu_onetime"
    assert check.status == LoginStatus.APPROVED
    assert check.account is not None
    assert (check.account.user_id, check.account.login) == ("186301755", "tekeufranck681")
    assert "ghu_onetime" not in repr(check)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("device_flow_disabled", "Enable Device Flow"),
        ("incorrect_client_credentials", "CHIRPO_GITHUB_APP_CLIENT_ID"),
    ],
)
async def test_setup_problems_say_what_to_fix(
    login: GitHubDeviceLogin, github: respx.MockRouter, error: str, hint: str
) -> None:
    github.post(DEVICE_CODE_URL).respond(json={"error": error})
    with pytest.raises(CodeHostForbiddenError, match=hint):
        await login.start()


async def test_github_down_is_temporary(login: GitHubDeviceLogin, github: respx.MockRouter) -> None:
    github.post(TOKEN_URL).respond(502)
    with pytest.raises(CodeHostUnavailableError):
        await login.check(DEVICE_CODE)
    github.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CodeHostUnavailableError):
        await login.check(DEVICE_CODE)
