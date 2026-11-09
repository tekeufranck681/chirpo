"""Workspaces, members and audit: the same tests run on the in-memory fakes and on PostgreSQL,
so core tests that use the fakes see the real behaviour."""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from chirpo.core.domain import AuditResult, ChatRef, ChatUser, RepoRef
from chirpo.core.errors import LastAdminError
from chirpo.core.permissions import Role
from chirpo.core.ports import AuditLog, Members, Workspaces
from chirpo.core.workspaces import AuditEntry, CodeHostAccount
from chirpo.db import models as db
from chirpo.db.repositories import SqlAuditLog, SqlMembers, SqlWorkspaces
from chirpo.db.session import create_session_factory
from tests.fakes import FakeAuditLog, FakeMembers, FakeWorkspaces


@dataclass
class Stores:
    workspaces: Workspaces
    members: Members
    audit: AuditLog


@pytest.fixture(params=["fake", "postgres"])
def stores(request: pytest.FixtureRequest) -> Iterator[Stores]:
    if request.param == "fake":
        members = FakeMembers()
        yield Stores(FakeWorkspaces(members), members, FakeAuditLog())
        return
    engine: AsyncEngine = request.getfixturevalue("engine")
    sessions = create_session_factory(engine)
    yield Stores(SqlWorkspaces(sessions), SqlMembers(sessions), SqlAuditLog(sessions))


def new_chat() -> ChatRef:
    return ChatRef(platform="telegram", chat_id=f"-100{uuid.uuid4().int % 10**10}")


def chat_user(
    user_id: str = "11", name: str = "Franck", username: str | None = "franck"
) -> ChatUser:
    return ChatUser(platform="telegram", user_id=user_id, display_name=name, username=username)


def repo(text: str) -> RepoRef:
    return RepoRef.parse("github", text)


GITHUB_FRANCK = CodeHostAccount(provider="github", user_id="186301755", login="tekeufranck681")


# ------------------------------------------------------------------ workspaces


async def test_a_chat_gets_one_workspace(stores: Stores) -> None:
    chat = new_chat()
    assert await stores.workspaces.for_chat(chat) is None

    created = await stores.workspaces.get_or_create_for_chat(chat, name="Shop devs")
    again = await stores.workspaces.get_or_create_for_chat(chat, name="renamed")

    assert again == created == await stores.workspaces.for_chat(chat)
    assert created.name == "Shop devs"
    other = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Other")
    assert other.id != created.id


async def test_concurrent_creation_gives_one_workspace(stores: Stores) -> None:
    chat = new_chat()
    created = await asyncio.gather(
        *(stores.workspaces.get_or_create_for_chat(chat, name="Shop") for _ in range(5))
    )
    assert len({workspace.id for workspace in created}) == 1


async def test_first_linked_repo_is_the_default(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    first = await stores.workspaces.link_repo(
        workspace.id, repo("acme/shop-app"), installation_id=7
    )
    second = await stores.workspaces.link_repo(workspace.id, repo("acme/shop-api"))

    assert (first.is_default, first.installation_id, second.is_default) == (True, 7, False)
    assert [link.repo.full_name for link in await stores.workspaces.repos(workspace.id)] == [
        "acme/shop-app",
        "acme/shop-api",
    ]


async def test_linking_again_in_other_casing_keeps_one_link(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    await stores.workspaces.link_repo(workspace.id, repo("acme/shop-app"))
    again = await stores.workspaces.link_repo(workspace.id, repo("ACME/Shop-App"))
    assert again.repo.full_name == "acme/shop-app"
    assert len(await stores.workspaces.repos(workspace.id)) == 1


async def test_unlinking_the_default_promotes_the_oldest_other(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    for name in ("acme/a", "acme/b", "acme/c"):
        await stores.workspaces.link_repo(workspace.id, repo(name))

    assert await stores.workspaces.unlink_repo(workspace.id, repo("ACME/A"))
    assert not await stores.workspaces.unlink_repo(workspace.id, repo("acme/a"))

    links = await stores.workspaces.repos(workspace.id)
    assert [(link.repo.full_name, link.is_default) for link in links] == [
        ("acme/b", True),
        ("acme/c", False),
    ]


async def test_repos_are_scoped_to_their_workspace(stores: Stores) -> None:
    shop = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    mobile = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Mobile")
    await stores.workspaces.link_repo(shop.id, repo("acme/shop-app"))

    assert await stores.workspaces.repos(mobile.id) == []
    assert not await stores.workspaces.unlink_repo(mobile.id, repo("acme/shop-app"))
    linked_in_mobile = await stores.workspaces.link_repo(mobile.id, repo("acme/shop-app"))
    assert linked_in_mobile.is_default  # each workspace has its own default


async def test_relinking_updates_the_installation(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    await stores.workspaces.link_repo(workspace.id, repo("acme/shop-app"), installation_id=7)
    unchanged = await stores.workspaces.link_repo(workspace.id, repo("acme/shop-app"))
    reinstalled = await stores.workspaces.link_repo(
        workspace.id, repo("acme/shop-app"), installation_id=8
    )
    assert (unchanged.installation_id, reinstalled.installation_id) == (7, 8)
    assert (await stores.workspaces.repos(workspace.id))[0].installation_id == 8


# ------------------------------------------------------------------ members


async def test_members_are_created_on_first_contact(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    franck = await stores.members.for_chat_user(workspace.id, chat_user())
    again = await stores.members.for_chat_user(workspace.id, chat_user())
    kevin = await stores.members.for_chat_user(workspace.id, chat_user("22", "Kevin", None))

    assert again == franck
    assert (franck.role, franck.display_name, franck.chat_username) == (
        Role.MEMBER,
        "Franck",
        "franck",
    )
    assert not franck.is_verified
    assert kevin.id != franck.id
    assert {member.id for member in await stores.members.all(workspace.id)} == {
        franck.id,
        kevin.id,
    }


async def test_concurrent_first_contact_gives_one_member(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    created = await asyncio.gather(
        *(stores.members.for_chat_user(workspace.id, chat_user()) for _ in range(5))
    )
    assert len({member.id for member in created}) == 1


async def test_chat_name_changes_are_picked_up(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    before = await stores.members.for_chat_user(workspace.id, chat_user())
    after = await stores.members.for_chat_user(
        workspace.id, chat_user(name="Franck T.", username="franck_t")
    )
    assert after.id == before.id
    assert (after.display_name, after.chat_username) == ("Franck T.", "franck_t")


async def test_the_same_person_is_a_separate_member_per_workspace(stores: Stores) -> None:
    shop = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    mobile = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Mobile")
    in_shop = await stores.members.for_chat_user(shop.id, chat_user())
    in_mobile = await stores.members.for_chat_user(mobile.id, chat_user())
    assert in_shop.id != in_mobile.id
    assert [member.id for member in await stores.members.all(mobile.id)] == [in_mobile.id]


async def test_roles_change_only_inside_the_workspace(stores: Stores) -> None:
    shop = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    mobile = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Mobile")
    franck = await stores.members.for_chat_user(shop.id, chat_user())

    promoted = await stores.members.set_role(shop.id, franck.id, Role.ADMIN)

    assert promoted is not None and promoted.role == Role.ADMIN
    assert await stores.members.set_role(mobile.id, franck.id, Role.MEMBER) is None
    assert await stores.members.set_role(shop.id, uuid.uuid4(), Role.ADMIN) is None


async def test_linking_a_github_account_verifies_the_member(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    franck = await stores.members.for_chat_user(workspace.id, chat_user())

    verified = await stores.members.link_codehost(workspace.id, franck.id, GITHUB_FRANCK)

    assert verified is not None and verified.codehost == GITHUB_FRANCK
    assert verified.is_verified
    renamed = GITHUB_FRANCK.model_copy(update={"login": "franck-new"})
    relinked = await stores.members.link_codehost(workspace.id, franck.id, renamed)
    assert relinked is not None and relinked.codehost == renamed
    assert await stores.members.link_codehost(workspace.id, uuid.uuid4(), GITHUB_FRANCK) is None


async def test_a_github_account_moves_to_the_member_who_proves_it(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    old_account = await stores.members.for_chat_user(workspace.id, chat_user("11"))
    new_account = await stores.members.for_chat_user(workspace.id, chat_user("99"))
    await stores.members.link_codehost(workspace.id, old_account.id, GITHUB_FRANCK)

    await stores.members.link_codehost(workspace.id, new_account.id, GITHUB_FRANCK)

    by_id = {member.id: member for member in await stores.members.all(workspace.id)}
    assert by_id[new_account.id].codehost == GITHUB_FRANCK
    assert by_id[old_account.id].codehost is None


async def test_one_account_claimed_twice_at_once_ends_with_one_member(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    first = await stores.members.for_chat_user(workspace.id, chat_user("11"))
    second = await stores.members.for_chat_user(workspace.id, chat_user("22"))

    await asyncio.gather(
        stores.members.link_codehost(workspace.id, first.id, GITHUB_FRANCK),
        stores.members.link_codehost(workspace.id, second.id, GITHUB_FRANCK),
    )

    owners = [m for m in await stores.members.all(workspace.id) if m.codehost == GITHUB_FRANCK]
    assert len(owners) == 1


# ------------------------------------------------------------------ first admin, last admin


async def test_the_first_link_makes_its_linker_admin(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    franck = await stores.members.for_chat_user(workspace.id, chat_user("11"))
    kevin = await stores.members.for_chat_user(workspace.id, chat_user("22", "Kevin"))

    first = await stores.workspaces.link_repo(workspace.id, repo("acme/a"), first_admin=franck.id)
    late = await stores.workspaces.link_repo(workspace.id, repo("acme/b"), first_admin=kevin.id)

    assert first is not None and first.is_default
    assert late is None  # not first any more: nothing linked, nobody promoted
    roles = {m.id: m.role for m in await stores.members.all(workspace.id)}
    assert roles == {franck.id: Role.ADMIN, kevin.id: Role.MEMBER}
    assert len(await stores.workspaces.repos(workspace.id)) == 1


async def test_simultaneous_first_links_make_one_admin(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    people = [
        await stores.members.for_chat_user(workspace.id, chat_user(str(n), f"P{n}"))
        for n in range(4)
    ]
    results = await asyncio.gather(
        *(
            stores.workspaces.link_repo(workspace.id, repo(f"acme/r{n}"), first_admin=p.id)
            for n, p in enumerate(people)
        )
    )
    assert sum(result is not None for result in results) == 1
    admins = [m for m in await stores.members.all(workspace.id) if m.role == Role.ADMIN]
    assert len(admins) == 1


async def test_the_last_admin_cannot_be_demoted(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    franck = await stores.members.for_chat_user(workspace.id, chat_user("11"))
    kevin = await stores.members.for_chat_user(workspace.id, chat_user("22", "Kevin"))
    await stores.members.set_role(workspace.id, franck.id, Role.ADMIN)

    with pytest.raises(LastAdminError):
        await stores.members.set_role(workspace.id, franck.id, Role.MEMBER)

    await stores.members.set_role(workspace.id, kevin.id, Role.ADMIN)
    demoted = await stores.members.set_role(workspace.id, franck.id, Role.MAINTAINER)
    assert demoted is not None and demoted.role == Role.MAINTAINER


async def test_two_admins_demoting_each_other_keep_one(stores: Stores) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    a = await stores.members.for_chat_user(workspace.id, chat_user("11"))
    b = await stores.members.for_chat_user(workspace.id, chat_user("22", "Kevin"))
    for member in (a, b):
        await stores.members.set_role(workspace.id, member.id, Role.ADMIN)

    results = await asyncio.gather(
        stores.members.set_role(workspace.id, a.id, Role.MEMBER),
        stores.members.set_role(workspace.id, b.id, Role.MEMBER),
        return_exceptions=True,
    )

    assert sum(isinstance(result, LastAdminError) for result in results) == 1
    admins = [m for m in await stores.members.all(workspace.id) if m.role == Role.ADMIN]
    assert len(admins) == 1


# ------------------------------------------------------------------ audit


async def test_audit_entries_are_recorded(stores: Stores, request: pytest.FixtureRequest) -> None:
    workspace = await stores.workspaces.get_or_create_for_chat(new_chat(), name="Shop")
    franck = await stores.members.for_chat_user(workspace.id, chat_user())
    entry = AuditEntry(
        workspace_id=workspace.id,
        actor_member_id=franck.id,
        action="create_issue",
        result=AuditResult.EXECUTED,
        payload={"repo": "acme/shop-app", "number": 12},
    )

    await stores.audit.record(entry)

    if isinstance(stores.audit, FakeAuditLog):
        assert stores.audit.entries == [entry]
        return
    engine: AsyncEngine = request.getfixturevalue("engine")
    async with create_session_factory(engine)() as session:
        row = await session.scalar(
            select(db.AuditEvent).where(db.AuditEvent.workspace_id == workspace.id)
        )
    assert row is not None
    assert (row.action, row.result, row.payload) == ("create_issue", "executed", entry.payload)
    assert row.actor_member_id == franck.id


async def test_context_is_kept_by_default_and_can_be_turned_off(stores: Stores) -> None:
    chat = new_chat()
    workspace = await stores.workspaces.get_or_create_for_chat(chat, name="Team")
    assert workspace.keeps_context
    await stores.workspaces.set_keeps_context(workspace.id, keep=False)
    found = await stores.workspaces.for_chat(chat)
    assert found is not None
    assert not found.keeps_context
    await stores.workspaces.set_keeps_context(workspace.id, keep=True)
    assert (await stores.workspaces.get_or_create_for_chat(chat, name="Team")).keeps_context
