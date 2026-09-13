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

def test_refinement_cache_keys_encounter_then_patient():
    from epicvibe.cache import RefinementCache, refinement_cache

    now = [0.0]
    c = RefinementCache(ttl_seconds=10, clock=lambda: now[0])
    assert c.put_for("ENC1", "PAT1", "refined") == ["ENC1", "pat:PAT1"]
    assert c.get_for("ENC1", None) == "refined"
    assert c.get_for(None, "PAT1") == "refined"          # launch without encounter
    assert c.get_for("OTHER", "PAT1") == "refined"       # falls back to the patient key
    assert c.get_for("OTHER", "PAT2") is None
    now[0] = 11.0
    assert c.get_for("ENC1", "PAT1") is None             # TTL

    class _State:
        class settings:
            smart_handback_ttl_seconds = 42

    state = _State()
    cache = refinement_cache(state)
    assert cache._ttl == 42
    assert refinement_cache(state) is cache              # attached once
