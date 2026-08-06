import asyncio
from epicvibe.cache import ProposalCache
from epicvibe.jobs import JobRunner

def test_cache_ttl():
    now = [0.0]
    c = ProposalCache(ttl_seconds=10, clock=lambda: now[0])
    c.put("ENC1", "value")
    assert c.get("ENC1") == "value"
    now[0] = 11.0
    assert c.get("ENC1") is None
    assert c.get("MISSING") is None

async def test_runner_dedupes_and_joins():
    runner = JobRunner()
    done = []
    async def work():
        await asyncio.sleep(0)
        done.append(1)
    assert runner.enqueue("k", work) is True
    assert runner.enqueue("k", work) is False     # deduped while running
    await runner.join()
    assert done == [1]
    assert runner.enqueue("k", work) is True      # can run again after completion

async def test_runner_swallows_exceptions():
    runner = JobRunner()
    async def boom():
        raise RuntimeError("x")
    runner.enqueue("k", boom)
    await runner.join()                           # must not raise
