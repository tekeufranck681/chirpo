"""GitHub's device flow: identify a person with no callback URL (works behind NAT, polling mode).

https://docs.github.com/apps/creating-github-apps/writing-code-for-a-github-app/building-a-cli-with-a-github-app
The user token GitHub returns is used once, to ask who approved, and then dropped: chirpo keeps no
user tokens. They also expire on their own (8 hours, with "Expire user authorization tokens").
"""

from typing import Any

import httpx
from pydantic import SecretStr

from chirpo.core.errors import (
    CodeHostError,
    CodeHostForbiddenError,
    CodeHostUnavailableError,
)
from chirpo.core.identity import DeviceLogin, LoginCheck, LoginStatus
from chirpo.core.workspaces import CodeHostAccount

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 (a URL, not a secret)
USER_URL = "https://api.github.com/user"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
TIMEOUT = httpx.Timeout(10.0)

# GitHub answers 200 with an "error" field while the person hasn't approved yet.
_STATUS = {
    "authorization_pending": LoginStatus.PENDING,
    "slow_down": LoginStatus.SLOW_DOWN,
    "access_denied": LoginStatus.DENIED,
    "expired_token": LoginStatus.EXPIRED,
    "incorrect_device_code": LoginStatus.EXPIRED,
}
_SETUP_HINTS = {
    "device_flow_disabled": "Tick 'Enable Device Flow' in the GitHub App settings",
    "incorrect_client_credentials": "CHIRPO_GITHUB_APP_CLIENT_ID is not this App's Client ID",
    "unsupported_grant_type": "GitHub rejected the device login request",
}


class GitHubDeviceLogin:
    provider = "github"

    def __init__(self, client_id: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._client_id = client_id
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT)

    async def start(self) -> DeviceLogin:
        data = await self._post(DEVICE_CODE_URL, {"client_id": self._client_id})
        try:
            return DeviceLogin(
                device_code=SecretStr(data["device_code"]),
                user_code=data["user_code"],
                verification_url=data["verification_uri"],
                expires_in_seconds=int(data["expires_in"]),
                interval_seconds=int(data["interval"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeHostError("GitHub device login: unexpected answer") from exc

    async def check(self, device_code: SecretStr) -> LoginCheck:
        data = await self._post(
            TOKEN_URL,
            {
                "client_id": self._client_id,
                "device_code": device_code.get_secret_value(),
                "grant_type": GRANT_TYPE,
            },
        )
        if token := data.get("access_token"):
            return LoginCheck(status=LoginStatus.APPROVED, account=await self._who(token))
        status = _STATUS.get(data.get("error", ""), LoginStatus.EXPIRED)
        interval = data.get("interval") if status == LoginStatus.SLOW_DOWN else None
        return LoginCheck(status=status, interval_seconds=int(interval) if interval else None)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _who(self, token: str) -> CodeHostAccount:
        response = await self._send(
            "GET",
            USER_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        try:
            user = response.json()
            return CodeHostAccount(
                provider=self.provider, user_id=str(user["id"]), login=user["login"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CodeHostError("GitHub device login: unexpected answer from /user") from exc

    async def _post(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        response = await self._send("POST", url, data=form, headers={"Accept": "application/json"})
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise CodeHostError("GitHub device login: answer was not JSON") from exc
        if hint := _SETUP_HINTS.get(data.get("error", "")):
            raise CodeHostForbiddenError(f"GitHub device login: {hint}")
        return data

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise CodeHostUnavailableError("GitHub device login: GitHub did not answer") from exc
        if response.status_code >= 500:
            raise CodeHostUnavailableError(
                f"GitHub device login: GitHub answered {response.status_code}"
            )
        if response.status_code >= 400:
            raise CodeHostError(f"GitHub device login: GitHub answered {response.status_code}")
        return response
