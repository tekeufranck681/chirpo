"""The chat commands end to end against fakes: who may do what, what they see, what is audited."""

from dataclasses import replace
from datetime import timedelta

import pytest

from chirpo.core import messages
from chirpo.core.commands import Services
from chirpo.core.domain import AuditResult, ChatUser, RepoPermission, RepoRef
from chirpo.core.errors import CodeHostValidationError
from chirpo.core.permissions import PermissionChecker, Role
from chirpo.core.pipeline import handle_inbound
from chirpo.core.workspaces import CodeHostAccount, Member
from tests.fakes import (
    CHAT,
    USER,
    FakeAuditLog,
    FakeCodeHost,
    FakeMembers,
    FakeMessageBuffer,
    FakeMessaging,
    FakeWorkspaces,
    make_message,
)

SANDBOX = RepoRef.parse("github", "tekeufranck681/chirpo-sandbox")
OTHER = RepoRef.parse("github", "tekeufranck681/chirpo")
ANNIE_GITHUB = CodeHostAccount(provider="github", user_id="42", login="annie-gh")
FRANCK = ChatUser(platform="fake", user_id="77", display_name="Franck", username="franck_tg")
FRANCK_GITHUB = CodeHostAccount(provider="github", user_id="186301755", login="tekeufranck681")


class World:
    def __init__(self) -> None:
        self.members = FakeMembers()
        self.workspaces = FakeWorkspaces(self.members)
        self.audit = FakeAuditLog()
        self.codehost = FakeCodeHost()
        self.codehost.provider = "github"
        self.codehost.logins = {"42": "annie-gh", "186301755": "tekeufranck681"}
        self.messaging = FakeMessaging()
        self.messaging.chat_admins = {USER.user_id}  # Annie runs the Telegram group
        self.buffer = FakeMessageBuffer(timedelta(hours=24))
        self.services = Services(
            workspaces=self.workspaces,
            members=self.members,
            audit=self.audit,
            codehost=self.codehost,
            permissions=PermissionChecker(self.codehost),
            app_install_url="https://github.com/apps/chirpo-dev/installations/new",
            buffer=self.buffer,
            context_hours=24,
        )

    async def say(self, text: str, sender: ChatUser = USER) -> str:
        """Send a message as `sender`; returns the bot's last reply."""
        self.messaging.sent.clear()
        await handle_inbound(make_message(text, sender=sender), self.messaging, self.services)
        return self.messaging.sent[-1][1] if self.messaging.sent else ""

    def github_access(self, login: str, repo: RepoRef, permission: RepoPermission) -> None:
        self.codehost.permissions[(repo.key, login)] = permission

    async def verified(
        self, user: ChatUser = USER, account: CodeHostAccount = ANNIE_GITHUB
    ) -> Member:
        """Someone who did /iam in this chat."""
        workspace = await self.workspaces.get_or_create_for_chat(CHAT, name="Shop")
        member = await self.members.for_chat_user(workspace.id, user)
        linked = await self.members.link_codehost(workspace.id, member.id, account)
        assert linked is not None
        return linked

    def audited(self) -> list[tuple[str, AuditResult, str | None]]:
        return [(entry.action, entry.result, entry.error) for entry in self.audit.entries]


@pytest.fixture
def world() -> World:
    return World()


async def linked_by_annie(world: World) -> None:
    await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.ADMIN)
    await world.say("/link tekeufranck681/chirpo-sandbox")
    world.audit.entries.clear()


# ------------------------------------------------------------------ /link


async def test_the_first_link_makes_a_default_repo_and_an_admin(world: World) -> None:
    annie = await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.WRITE)

    reply = await world.say("/link tekeufranck681/chirpo-sandbox")

    assert reply == (
        messages.LINKED.format(repo=SANDBOX.full_name)
        + messages.LINKED_DEFAULT
        + messages.LINKED_ADMIN
        + messages.CONTEXT_NOTICE.format(hours=24)
    )
    (link,) = await world.workspaces.repos(annie.workspace_id)
    assert (link.repo, link.is_default) == (SANDBOX, True)
    (member,) = await world.members.all(annie.workspace_id)
    assert member.role == Role.ADMIN
    assert world.audited() == [("link_first_repo", AuditResult.EXECUTED, None)]


async def test_linking_needs_iam_in_this_chat_first(world: World) -> None:
    assert await world.say("/link tekeufranck681/chirpo-sandbox") == messages.SEND_IAM_HERE


async def test_unverified_people_are_sent_to_iam(world: World) -> None:
    workspace = await world.workspaces.get_or_create_for_chat(CHAT, name="Shop")
    await world.members.for_chat_user(workspace.id, USER)
    reply = await world.say("/link tekeufranck681/chirpo-sandbox")
    assert reply == messages.LINK_GITHUB_FIRST
    assert world.audited() == [("link_first_repo", AuditResult.DENIED, "not_verified")]


async def test_linking_needs_write_access_on_github(world: World) -> None:
    await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.READ)
    reply = await world.say("/link tekeufranck681/chirpo-sandbox")
    assert reply == messages.NO_REPO_ACCESS.format(
        login="annie-gh", permission="write", repo=SANDBOX.full_name
    )
    assert world.audited() == [("link_first_repo", AuditResult.DENIED, "no_repo_access")]


async def test_a_repo_without_the_app_gets_the_install_link(world: World) -> None:
    await world.verified()
    world.codehost.not_installed.add(SANDBOX.key)
    reply = await world.say("/link tekeufranck681/chirpo-sandbox")
    assert reply.startswith(messages.APP_NOT_INSTALLED.format(repo=SANDBOX.full_name))
    assert "https://github.com/apps/chirpo-dev/installations/new" in reply
    assert world.audited() == [("link_first_repo", AuditResult.DENIED, "app_not_installed")]


async def test_later_links_need_an_admin(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    world.github_access("tekeufranck681", OTHER, RepoPermission.ADMIN)

    assert await world.say("/link tekeufranck681/chirpo", FRANCK) == messages.ROLE_TOO_LOW.format(
        role="admin"
    )

    world.github_access("annie-gh", OTHER, RepoPermission.WRITE)
    reply = await world.say("/link tekeufranck681/chirpo")
    assert reply == messages.LINKED.format(repo=OTHER.full_name)  # not default, no admin note


async def test_linking_twice_says_so(world: World) -> None:
    await linked_by_annie(world)
    reply = await world.say("/link TekeuFranck681/Chirpo-Sandbox")
    assert reply == messages.ALREADY_LINKED.format(repo="TekeuFranck681/Chirpo-Sandbox")


@pytest.mark.parametrize("text", ["/link", "/link not-a-repo", "/link a/b/c"])
async def test_link_usage(world: World, text: str) -> None:
    await world.verified()
    assert await world.say(text) == messages.LINK_USAGE


async def test_losing_the_first_link_race_needs_admin(world: World) -> None:
    annie = await world.verified()
    world.github_access("annie-gh", OTHER, RepoPermission.WRITE)
    # Someone else's first link lands between Annie's check and his link.
    await world.workspaces.link_repo(annie.workspace_id, SANDBOX)
    real_repos = world.workspaces.repos

    async def repos_before_the_race(workspace_id: object) -> list[object]:
        world.workspaces.repos = real_repos  # type: ignore[method-assign]
        return []

    world.workspaces.repos = repos_before_the_race  # type: ignore[method-assign,assignment]
    reply = await world.say("/link tekeufranck681/chirpo")

    assert reply == messages.ROLE_TOO_LOW.format(role="admin")
    (member,) = await world.members.all(annie.workspace_id)
    assert member.role == Role.MEMBER


async def test_in_a_group_only_its_admins_link_first(world: World) -> None:
    world.messaging.chat_admins = set()
    annie = await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.ADMIN)

    reply = await world.say("/link tekeufranck681/chirpo-sandbox")

    assert reply == messages.FIRST_LINK_NEEDS_GROUP_ADMIN
    assert await world.workspaces.repos(annie.workspace_id) == []
    assert world.audited() == [("link_first_repo", AuditResult.DENIED, "not_group_admin")]


async def test_in_a_private_chat_you_run_it_yourself(world: World) -> None:
    world.messaging.chat_admins = set()
    annie = await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.WRITE)
    world.messaging.sent.clear()
    message = make_message("/link tekeufranck681/chirpo-sandbox", is_private_chat=True)
    await handle_inbound(message, world.messaging, world.services)
    assert len(await world.workspaces.repos(annie.workspace_id)) == 1


# ------------------------------------------------------------------ /unlink


async def test_admins_unlink(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/unlink tekeufranck681/chirpo-sandbox") == messages.UNLINKED.format(
        repo=SANDBOX.full_name
    )
    assert world.audited() == [("unlink_repo", AuditResult.EXECUTED, None)]
    assert await world.say("/unlink tekeufranck681/chirpo-sandbox") == (
        messages.NOT_LINKED_HERE.format(repo=SANDBOX.full_name)
    )


async def test_members_cannot_unlink(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    reply = await world.say("/unlink tekeufranck681/chirpo-sandbox", FRANCK)
    assert reply == messages.ROLE_TOO_LOW.format(role="admin")


# ------------------------------------------------------------------ /members and /role


async def test_members_lists_roles_and_github_accounts(world: World) -> None:
    await linked_by_annie(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.members.for_chat_user(workspace.id, FRANCK)

    reply = await world.say("/members")

    assert reply.splitlines() == [
        messages.MEMBERS_TITLE,
        "- Annie (@annie): admin, GitHub @annie-gh",
        "- Franck (@franck_tg): member, GitHub not linked yet",
    ]


@pytest.mark.parametrize("name", ["@franck_tg", "@TEKEUFRANCK681", "franck_tg"])
async def test_admins_change_roles_by_chat_or_github_name(world: World, name: str) -> None:
    await linked_by_annie(world)
    franck = await world.verified(FRANCK, FRANCK_GITHUB)

    reply = await world.say(f"/role {name} maintainer")

    assert reply == messages.ROLE_CHANGED.format(name="Franck", role="maintainer")
    by_id = {member.id: member for member in await world.members.all(franck.workspace_id)}
    assert by_id[franck.id].role == Role.MAINTAINER
    assert world.audited() == [("change_role", AuditResult.EXECUTED, None)]


async def test_the_last_admin_is_kept(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/role @annie member") == messages.LAST_ADMIN
    assert world.audited() == [("change_role", AuditResult.DENIED, "last_admin")]


async def test_members_cannot_change_roles(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    assert await world.say("/role @franck_tg admin", FRANCK) == messages.ROLE_TOO_LOW.format(
        role="admin"
    )


@pytest.mark.parametrize("text", ["/role", "/role @franck_tg", "/role @franck_tg boss"])
async def test_role_usage(world: World, text: str) -> None:
    await linked_by_annie(world)
    assert await world.say(text) == messages.ROLE_USAGE


async def test_a_name_matching_several_people_is_refused(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    # A stranger picks Franck's GitHub login as their Telegram username.
    copycat = ChatUser(
        platform="fake", user_id="99", display_name="Francky", username="tekeufranck681"
    )
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.members.for_chat_user(workspace.id, copycat)

    reply = await world.say("/role @tekeufranck681 admin")

    assert reply == messages.MEMBER_AMBIGUOUS.format(name="@tekeufranck681")
    admins = [m for m in await world.members.all(workspace.id) if m.role == Role.ADMIN]
    assert [m.display_name for m in admins] == ["Annie"]


async def test_long_member_lists_are_capped(world: World) -> None:
    await linked_by_annie(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    for n in range(60):
        person = ChatUser(platform="fake", user_id=f"5{n}", display_name=f"P{n}")
        await world.members.for_chat_user(workspace.id, person)

    lines = (await world.say("/members")).splitlines()

    assert len(lines) == 1 + 50 + 1
    assert lines[-1] == messages.MEMBERS_MORE.format(count=11)


async def test_issue_titles_are_one_line(world: World) -> None:
    await linked_by_annie(world)
    await world.say("/issue Login   button\nbroken\n## injected heading")
    (created,) = world.codehost.issues.values()
    assert created.title == "Login button broken ## injected heading"


async def test_unknown_people_are_explained(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/role @nobody maintainer") == messages.MEMBER_NOT_FOUND.format(
        name="@nobody"
    )


# ------------------------------------------------------------------ /issue


async def test_issue_is_created_in_the_default_repo_with_the_requester(world: World) -> None:
    await linked_by_annie(world)

    reply = await world.say("/issue Login button broken")

    ((repo_key, number), created) = next(iter(world.codehost.issues.items()))
    assert repo_key == SANDBOX.key
    assert created.title == "Login button broken"
    assert created.body == "Requested by @annie-gh via chirpo."
    assert reply == messages.ISSUE_CREATED.format(
        number=number, title="Login button broken", url=created.url
    )
    assert world.audited() == [("create_issue", AuditResult.EXECUTED, None)]
    assert world.audit.entries[0].payload["number"] == number


async def test_issue_needs_a_linked_repo(world: World) -> None:
    assert await world.say("/issue Login button broken") == messages.NO_REPO_LINKED
    await world.verified()
    assert await world.say("/issue Login button broken") == messages.NO_REPO_LINKED


async def test_issue_needs_read_access(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)  # no access to the sandbox on GitHub
    reply = await world.say("/issue Login button broken", FRANCK)
    assert reply == messages.NO_REPO_ACCESS.format(
        login="tekeufranck681", permission="read", repo=SANDBOX.full_name
    )
    assert world.codehost.issues == {}


async def test_issue_usage_and_title_limits(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/issue") == messages.ISSUE_USAGE
    assert await world.say("/issue " + "x" * 300) == messages.ISSUE_TITLE_TOO_LONG


async def test_a_refused_issue_is_reported_and_audited(world: World) -> None:
    await linked_by_annie(world)

    async def refuse(*_: object) -> object:
        raise CodeHostValidationError("create an issue: GitHub answered 422")

    world.codehost.create_issue = refuse  # type: ignore[method-assign,assignment]
    with pytest.raises(CodeHostValidationError):
        await world.say("/issue Login button broken")
    assert world.messaging.sent[-1][1] == messages.ISSUE_FAILED.format(
        reason="CodeHostValidationError"
    )
    assert world.audited()[-1] == (
        "create_issue",
        AuditResult.FAILED,
        "CodeHostValidationError",  # never GitHub's raw text
    )


# ------------------------------------------------------------------ dispatch


async def test_github_commands_without_github_configured(world: World) -> None:
    world.messaging.sent.clear()
    await handle_inbound(make_message("/link a/b"), world.messaging, None)
    assert world.messaging.sent[-1][1] == messages.GITHUB_NOT_CONFIGURED


async def test_basic_commands_work_without_github(world: World) -> None:
    await handle_inbound(make_message("/ping"), world.messaging, None)
    assert world.messaging.sent[-1][1] == messages.PONG


async def test_help_lists_every_command(world: World) -> None:
    from chirpo.core.commands import COMMANDS

    for name in COMMANDS:
        if name != "start":
            assert f"/{name}" in messages.HELP


# ------------------------------------------------------------------ /context


async def test_context_status_for_anyone(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    assert await world.say("/context", FRANCK) == messages.CONTEXT_IS_ON.format(hours=24)


async def test_admins_turn_context_off_and_what_was_kept_is_deleted(world: World) -> None:
    await linked_by_annie(world)
    workspace = await world.workspaces.for_chat(CHAT)
    assert workspace is not None
    await world.buffer.remember(workspace.id, make_message("checkout crashes", message_id="99"))

    assert await world.say("/context off") == messages.CONTEXT_TURNED_OFF
    assert await world.buffer.recent(workspace.id, CHAT) == []
    assert await world.say("/context") == messages.CONTEXT_IS_OFF
    assert world.audited() == [("change_settings", AuditResult.EXECUTED, None)]
    assert world.audit.entries[0].payload == {"keep_context": False}

    assert await world.say("/context on") == messages.CONTEXT_TURNED_ON.format(hours=24)
    assert (await world.workspaces.for_chat(CHAT)).keeps_context  # type: ignore[union-attr]


async def test_members_cannot_change_context(world: World) -> None:
    await linked_by_annie(world)
    await world.verified(FRANCK, FRANCK_GITHUB)
    assert await world.say("/context off", FRANCK) == messages.ROLE_TOO_LOW.format(role="admin")
    assert (await world.workspaces.for_chat(CHAT)).keeps_context  # type: ignore[union-attr]


async def test_context_usage(world: World) -> None:
    await linked_by_annie(world)
    assert await world.say("/context maybe") == messages.CONTEXT_USAGE


async def test_context_when_the_instance_keeps_nothing(world: World) -> None:
    world.services = replace(world.services, buffer=None, context_hours=0)
    await linked_by_annie(world)
    assert await world.say("/context") == messages.CONTEXT_OFF_FOR_INSTANCE
    assert await world.say("/context on") == messages.CONTEXT_OFF_FOR_INSTANCE


async def test_no_notice_on_link_when_the_instance_keeps_nothing(world: World) -> None:
    world.services = replace(world.services, buffer=None, context_hours=0)
    await world.verified()
    world.github_access("annie-gh", SANDBOX, RepoPermission.WRITE)
    reply = await world.say("/link tekeufranck681/chirpo-sandbox")
    assert "/context" not in reply


async def test_help_and_about_mention_context() -> None:
    assert "/context" in messages.HELP
    assert "/context" in messages.ABOUT
