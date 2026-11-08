from chirpo.workers.main import WorkerSettings, noop


async def test_noop_job_returns_ok() -> None:
    assert await noop({"job_id": "job-1"}) == "ok"


def test_worker_registers_the_noop_job() -> None:
    assert noop in WorkerSettings.functions
