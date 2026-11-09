import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from chirpo.core.domain import ChatRef
from chirpo.core.identity import PendingLogin
from chirpo.security import Encryptor, generate_key
from chirpo.workers.main import WorkerSettings, check_device_login
from chirpo.workers.queue import ArqLoginJobs, open_pending_login, seal_pending_login
from tests.fakes import FakeMessaging

PENDING = PendingLogin(
    workspace_id=uuid.uuid4(),
    member_id=uuid.uuid4(),
    chat=ChatRef(platform="fake", chat_id="42"),
    claim_key="fake:42",
    claim_token="abc:1762689000",
    device_code=SecretStr("dev-code-secret"),
    expires_at=datetime(2026, 11, 9, 12, 15, tzinfo=UTC),
    interval_seconds=5,
)


def test_a_pending_login_is_sealed_and_opened() -> None:
    encryptor = Encryptor([generate_key()])
    sealed = seal_pending_login(encryptor, PENDING)
    assert "dev-code-secret" not in sealed
    opened = open_pending_login(encryptor, sealed)
    assert opened == PENDING
    assert opened.device_code.get_secret_value() == "dev-code-secret"


class FakeQueue:
    """The bits of ArqRedis the login jobs use (it is a Redis client too)."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.keys: dict[str, tuple[str, int]] = {}

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((name, args, kwargs))

    async def set(self, name: str, value: str, *, nx: bool = False, ex: int) -> bool | None:
        if nx and name in self.keys:
            return None
        self.keys[name] = (value, ex)
        return True

    async def get(self, name: str) -> bytes | None:
        return self.keys[name][0].encode() if name in self.keys else None

    async def delete(self, name: str) -> int:
        return 1 if self.keys.pop(name, None) else 0

    async def expire(self, name: str, seconds: int) -> bool:
        if name not in self.keys:
            return False
        self.keys[name] = (self.keys[name][0], seconds)
        return True


async def test_claims_belong_to_one_login_at_a_time() -> None:
    queue = FakeQueue()
    jobs = ArqLoginJobs(queue, Encryptor([generate_key()]))  # type: ignore[arg-type]
    assert await jobs.claim("telegram:42", "first:1", 900)
    assert not await jobs.claim("telegram:42", "second:2", 900)
    assert await jobs.current("telegram:42") == "first:1"

    await jobs.hold("telegram:42", 600)
    assert queue.keys["chirpo:login:telegram:42"] == ("first:1", 600)

    await jobs.replace("telegram:42", "second:2", 900)
    await jobs.release("telegram:42", "first:1")  # the old login finishing changes nothing
    assert await jobs.current("telegram:42") == "second:2"
    await jobs.release("telegram:42", "second:2")
    assert await jobs.current("telegram:42") is None


async def test_the_next_check_is_deferred() -> None:
    queue = FakeQueue()
    encryptor = Encryptor([generate_key()])
    await ArqLoginJobs(queue, encryptor).check_login_later(PENDING, 5)  # type: ignore[arg-type]
    ((name, (sealed,), options),) = queue.jobs
    assert name == "check_device_login"
    assert open_pending_login(encryptor, sealed) == PENDING
    assert options["_defer_by"].total_seconds() == 5


class RecordingIdentity:
    def __init__(self) -> None:
        self.checked: list[PendingLogin] = []

    async def check(self, pending: PendingLogin, messaging: Any) -> None:
        self.checked.append(pending)


async def test_the_worker_job_runs_the_check() -> None:
    encryptor = Encryptor([generate_key()])
    identity = RecordingIdentity()
    ctx = {
        "services": SimpleNamespace(identity=identity),
        "encryptor": encryptor,
        "messaging": {"fake": FakeMessaging()},
    }
    await check_device_login(ctx, seal_pending_login(encryptor, PENDING))
    assert identity.checked == [PENDING]


async def test_the_worker_job_is_quiet_without_github_login() -> None:
    await check_device_login({"services": None}, "sealed")


def test_the_job_is_never_retried_and_keeps_no_result() -> None:
    (job,) = [f for f in WorkerSettings.functions if getattr(f, "name", "") == "check_device_login"]
    assert job.coroutine is check_device_login
    assert (job.max_tries, job.keep_result_s) == (1, 0)
