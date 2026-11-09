from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from chirpo.core import messages
from chirpo.core.commands import Services
from chirpo.core.domain import AuditResult, ChatRef
from chirpo.core.errors import CodeHostForbiddenError, CodeHostUnavailableError
from chirpo.core.identity import (
    DeviceLogin,
    IdentityLinking,
    LoginCheck,
    LoginStatus,
    PendingLogin,
)
from chirpo.core.permissions import PermissionChecker
from chirpo.core.pipeline import handle_inbound
from chirpo.core.workspaces import CodeHostAccount
from tests.fakes import (
    CHAT,
    USER,
    FakeAuditLog,
    FakeCodeHost,
    FakeMembers,
    FakeMessaging,
    FakeWorkspaces,
    make_message,
)

FRANCK = CodeHostAccount(provider="github", user_id="186301755", login="tekeufranck681")
PRIVATE = ChatRef(platform="fake", chat_id=USER.user_id)


class FakeLogin:
    provider = "github"

    def __init__(self) -> None:
        self.answers: list[LoginCheck | Exception] = []
        self.started = 0
        self.fail_start = False

    async def start(self) -> DeviceLogin:
        self.started += 1
        if self.fail_start:
            raise CodeHostForbiddenError("Tick 'Enable Device Flow' in the GitHub App settings")
        return DeviceLogin(
            device_code=SecretStr("dev-code-secret"),
            user_code="WDJB-MJHT",
            verification_url="https://github.com/login/device",
            expires_in_seconds=899,  # what GitHub really sends: must still read "15 minutes"
            interval_seconds=5,
        )

    async def check(self, device_code: SecretStr) -> LoginCheck:
        assert device_code.get_secret_value() == "dev-code-secret"
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeJobs:
    def __init__(self) -> None:
        self.scheduled: list[tuple[PendingLogin, int]] = []
        self.claims: dict[str, str] = {}
        self.held: dict[str, int] = {}

    async def check_login_later(self, pending: PendingLogin, delay_seconds: int) -> None:
        self.scheduled.append((pending, delay_seconds))

    async def claim(self, key: str, token: str, seconds: int) -> bool:
        if key in self.claims:
            return False
        self.claims[key] = token
        return True

    async def current(self, key: str) -> str | None:
        return self.claims.get(key)

    async def replace(self, key: str, token: str, seconds: int) -> None:
        self.claims[key] = token

    async def release(self, key: str, token: str) -> None:
        if self.claims.get(key) == token:
            del self.claims[key]

    async def hold(self, key: str, seconds: int) -> None:
        self.held[key] = seconds


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 9, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class World:
    def __init__(self) -> None:
        self.login = FakeLogin()
        self.jobs = FakeJobs()
        self.workspaces = FakeWorkspaces()
        self.members = FakeMembers()
        self.audit = FakeAuditLog()
        self.messaging = FakeMessaging()
        self.clock = Clock()
        self.identity = IdentityLinking(
            login=self.login,
            workspaces=self.workspaces,
            members=self.members,
            audit=self.audit,
            jobs=self.jobs,
            now=self.clock,
        )

    @property
    def services(self) -> Services:
        codehost = FakeCodeHost()
        return Services(
            workspaces=self.workspaces,
            members=self.members,
            audit=self.audit,
            codehost=codehost,
            permissions=PermissionChecker(codehost),
            identity=self.identity,
        )

    def texts(self, chat: ChatRef) -> list[str]:
        return [text for to, text, _ in self.messaging.sent if to == chat]

    async def started(self) -> PendingLogin:
        await self.identity.start(make_message("/iam"), self.messaging)
        ((pending, _),) = self.jobs.scheduled
        self.jobs.scheduled.clear()
        self.messaging.sent.clear()
        return pending


@pytest.fixture
def world() -> World:
    return World()


# ------------------------------------------------------------------ /iam


async def test_iam_in_a_group_sends_the_code_privately(world: World) -> None:
    await world.identity.start(make_message("/iam"), world.messaging)

    (private_text,) = world.texts(PRIVATE)
    assert private_text.startswith("Paul,")
    assert "Only approve if you sent /iam yourself" in private_text
    assert "https://github.com/login/device" in private_text
    assert "WDJB-MJHT" in private_text
    assert "15 minutes" in private_text
    assert "dev-code-secret" not in private_text
    assert world.texts(CHAT) == [messages.IAM_SENT_PRIVATELY]

    ((pending, delay),) = world.jobs.scheduled
    assert delay == 5
    assert pending.chat == PRIVATE
    assert pending.expires_at == world.clock.now + timedelta(seconds=899)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None and pending.workspace_id == workspace.id
    (member,) = await world.members.all(workspace.id)
    assert pending.member_id == member.id


async def test_iam_in_a_private_chat_answers_only_there(world: World) -> None:
    message = make_message("/iam", chat=PRIVATE, is_private_chat=True)
    await world.identity.start(message, world.messaging)
    assert len(world.texts(PRIVATE)) == 1
    assert len(world.jobs.scheduled) == 1


async def test_without_a_private_chat_the_person_gets_a_link(world: World) -> None:
    world.messaging.unreachable_chats.add(PRIVATE.chat_id)

    await world.identity.start(make_message("/iam"), world.messaging)

    (reply,) = world.texts(CHAT)
    assert "https://chat.example/chirpo?start=iam" in reply
    assert world.jobs.scheduled == []
    assert world.jobs.claims == {}  # they can try again straight away
    assert world.workspaces.by_chat == {}  # nothing stored for an undelivered code


async def test_anonymous_senders_are_refused(world: World) -> None:
    anonymous = USER.model_copy(update={"is_anonymous": True})
    await world.identity.start(make_message("/iam", sender=anonymous), world.messaging)
    assert world.texts(CHAT) == [messages.IAM_ANONYMOUS]
    assert world.login.started == 0


async def test_a_setup_problem_is_explained_and_raised(world: World) -> None:
    world.login.fail_start = True
    with pytest.raises(CodeHostForbiddenError):
        await world.identity.start(make_message("/iam"), world.messaging)
    assert world.texts(CHAT) == [messages.GITHUB_LOGIN_UNAVAILABLE]
    assert world.jobs.claims == {}
    assert world.workspaces.by_chat == {}


async def test_a_second_request_within_a_minute_gets_no_new_code(world: World) -> None:
    await world.identity.start(make_message("/iam"), world.messaging)
    world.clock.now += timedelta(seconds=59)
    await world.identity.start(make_message("/iam", message_id="11"), world.messaging)

    assert world.login.started == 1
    assert len(world.jobs.scheduled) == 1
    assert world.jobs.held == {"fake:42": 899}  # the claim lasts as long as GitHub's code
    assert world.texts(CHAT)[-1] == messages.IAM_JUST_SENT


async def test_a_later_request_replaces_the_pending_code(world: World) -> None:
    old = await world.started()
    world.clock.now += timedelta(seconds=60)
    await world.identity.start(make_message("/iam", message_id="11"), world.messaging)

    assert world.login.started == 2
    ((new, _),) = world.jobs.scheduled
    assert world.jobs.claims == {"fake:42": new.claim_token}
    world.jobs.scheduled.clear()
    world.messaging.sent.clear()

    await world.identity.check(old, world.messaging)  # the old code's next check
    assert (world.jobs.scheduled, world.messaging.sent) == ([], [])  # stopped, silently


async def test_finishing_an_old_login_keeps_the_new_claim(world: World) -> None:
    old = await world.started()
    world.jobs.claims["fake:42"] = "newer:0"
    await world.jobs.release(old.claim_key, old.claim_token)
    assert world.jobs.claims == {"fake:42": "newer:0"}


# ------------------------------------------------------------------ waiting for approval


async def test_pending_checks_again_after_the_interval(world: World) -> None:
    pending = await world.started()
    world.login.answers = [LoginCheck(status=LoginStatus.PENDING)]
    await world.identity.check(pending, world.messaging)
    assert world.jobs.scheduled == [(pending, 5)]
    assert world.messaging.sent == []
    assert world.jobs.claims == {"fake:42": pending.claim_token}  # still in progress


async def test_slow_down_waits_longer(world: World) -> None:
    pending = await world.started()
    world.login.answers = [
        LoginCheck(status=LoginStatus.SLOW_DOWN, interval_seconds=10),
        LoginCheck(status=LoginStatus.SLOW_DOWN),
    ]
    await world.identity.check(pending, world.messaging)
    ((slower, delay),) = world.jobs.scheduled
    assert (slower.interval_seconds, delay) == (10, 10)

    await world.identity.check(slower, world.messaging)
    assert world.jobs.scheduled[-1][1] == 15


async def test_a_hiccup_is_retried(world: World) -> None:
    pending = await world.started()
    world.login.answers = [CodeHostUnavailableError("down")]
    await world.identity.check(pending, world.messaging)
    assert world.jobs.scheduled == [(pending, 5)]


async def test_approval_links_the_account(world: World) -> None:
    pending = await world.started()
    world.login.answers = [LoginCheck(status=LoginStatus.APPROVED, account=FRANCK)]

    await world.identity.check(pending, world.messaging)

    (member,) = await world.members.all(pending.workspace_id)
    assert member.codehost == FRANCK
    assert world.texts(PRIVATE) == [messages.IAM_DONE.format(login="tekeufranck681")]
    assert world.jobs.claims == {}
    (entry,) = world.audit.entries
    assert (entry.action, entry.result, entry.actor_member_id) == (
        "verify_identity",
        AuditResult.EXECUTED,
        member.id,
    )
    assert entry.payload["user_id"] == "186301755"
    assert world.jobs.scheduled == []


@pytest.mark.parametrize(
    ("status", "text"),
    [(LoginStatus.DENIED, messages.IAM_DENIED), (LoginStatus.EXPIRED, messages.IAM_EXPIRED)],
)
async def test_denied_or_expired_ends_with_a_message(
    world: World, status: LoginStatus, text: str
) -> None:
    pending = await world.started()
    world.login.answers = [LoginCheck(status=status)]
    await world.identity.check(pending, world.messaging)
    assert world.texts(PRIVATE) == [text]
    assert world.jobs.scheduled == []
    assert world.jobs.claims == {}


async def test_past_the_deadline_github_is_not_asked(world: World) -> None:
    pending = await world.started()
    world.clock.now = pending.expires_at
    await world.identity.check(pending, world.messaging)
    assert world.texts(PRIVATE) == [messages.IAM_EXPIRED]
    assert world.jobs.claims == {}


async def test_a_member_removed_meanwhile_is_not_linked(world: World) -> None:
    pending = await world.started()
    world.members.members.clear()
    world.login.answers = [LoginCheck(status=LoginStatus.APPROVED, account=FRANCK)]
    await world.identity.check(pending, world.messaging)
    assert world.messaging.sent == []
    assert world.audit.entries == []


# ------------------------------------------------------------------ routing


@pytest.mark.parametrize("text", ["/iam", "/start iam"])
async def test_iam_is_routed_to_identity_linking(world: World, text: str) -> None:
    await handle_inbound(make_message(text), world.messaging, world.services)
    assert world.login.started == 1


async def test_plain_start_still_shows_help(world: World) -> None:
    await handle_inbound(make_message("/start"), world.messaging, world.services)
    assert world.login.started == 0
    assert world.texts(CHAT) == [messages.HELP]


async def test_iam_without_github_login_configured(world: World) -> None:
    await handle_inbound(make_message("/iam"), world.messaging, None)
    assert world.texts(CHAT) == [messages.GITHUB_LOGIN_UNAVAILABLE]


# ------------------------------------------------------------------ already linked


async def linked(world: World) -> None:
    pending = await world.started()
    world.login.answers = [LoginCheck(status=LoginStatus.APPROVED, account=FRANCK)]
    await world.identity.check(pending, world.messaging)
    world.messaging.sent.clear()


async def test_iam_when_already_linked_sends_no_code(world: World) -> None:
    await linked(world)
    await handle_inbound(make_message("/iam", message_id="20"), world.messaging, world.services)
    assert world.texts(CHAT) == [messages.IAM_ALREADY_LINKED.format(login="tekeufranck681")]
    assert world.login.started == 1  # only the first, original login
    assert world.jobs.scheduled == []


@pytest.mark.parametrize("text", ["/iam switch", "/iam SWITCH"])
async def test_iam_switch_starts_a_new_login(world: World, text: str) -> None:
    await linked(world)
    await handle_inbound(make_message(text, message_id="20"), world.messaging, world.services)
    assert world.login.started == 2
    assert len(world.jobs.scheduled) == 1


async def test_switching_replaces_the_linked_account(world: World) -> None:
    await linked(world)
    await world.identity.start(make_message("/iam switch"), world.messaging, switch=True)
    ((pending, _),) = world.jobs.scheduled
    other = CodeHostAccount(provider="github", user_id="7", login="franck-work")
    world.login.answers = [LoginCheck(status=LoginStatus.APPROVED, account=other)]

    await world.identity.check(pending, world.messaging)

    (member,) = await world.members.all(pending.workspace_id)
    assert member.codehost == other


def test_the_switch_command_is_one_tappable_word() -> None:
    assert "/iam_switch" in messages.IAM_ALREADY_LINKED
    assert "/iam switch" not in messages.IAM_ALREADY_LINKED


async def test_a_claim_in_an_old_format_is_replaced(world: World) -> None:
    world.jobs.claims["fake:42"] = "1"
    await world.identity.start(make_message("/iam"), world.messaging)
    assert world.login.started == 1
