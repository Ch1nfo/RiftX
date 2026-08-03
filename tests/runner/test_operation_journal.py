from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from riftx.runner.terminal_manager import (
    OperationJournal,
    OperationJournalConflict,
    OperationJournalIdentity,
)

_IDENTITY = OperationJournalIdentity(
    command_id="command-1",
    binding_digest="a" * 64,
    envelope_digest="b" * 64,
)
_DIVERGENT_IDENTITY = OperationJournalIdentity(
    command_id="command-1",
    binding_digest="c" * 64,
    envelope_digest="d" * 64,
)
_FRESH_IDENTITY = OperationJournalIdentity(
    command_id="command-2",
    binding_digest="a" * 64,
    envelope_digest="e" * 64,
)


def _add_in_process(path: str, operation_id: str, start) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("journal concurrency test start signal timed out")
    asyncio.run(
        OperationJournal(Path(path)).add(
            operation_id,
            _IDENTITY,
            outcome={"state": "completed", "operation_id": operation_id},
        )
    )


def _claim_in_process(path: str, operation_id: str, start, results) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("journal claim test start signal timed out")
    results.put(
        asyncio.run(
            OperationJournal(Path(path)).claim(
                operation_id,
                _IDENTITY,
                outcome={"state": "claimed"},
            )
        )
    )


def _stored_keys(path: Path) -> set[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == "riftx.runner-operation-journal/v1"
    return {record["operation_key"] for record in raw["records"]}


async def test_independent_journal_instances_merge_concurrent_adds(tmp_path: Path) -> None:
    path = tmp_path / "operations.json"
    operation_ids = {f"operation-{index}" for index in range(32)}

    await asyncio.gather(
        *(
            OperationJournal(path).add(
                operation_id,
                _IDENTITY,
                outcome={"state": "completed", "operation_id": operation_id},
            )
            for operation_id in operation_ids
        )
    )

    assert _stored_keys(path) == operation_ids


async def test_independent_journal_instances_allow_exactly_one_claim(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"

    claimed = await asyncio.gather(
        *(
            OperationJournal(path).claim(
                "delivery-1",
                _IDENTITY,
                outcome={"state": "claimed"},
            )
            for _ in range(32)
        )
    )

    assert claimed.count(True) == 1
    assert claimed.count(False) == 31
    assert _stored_keys(path) == {"delivery-1"}


async def test_same_key_with_divergent_digest_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "divergent.json"
    journal = OperationJournal(path)
    await journal.claim(
        "operation-1",
        _IDENTITY,
        outcome={"state": "claimed"},
    )
    before = path.read_bytes()

    with pytest.raises(OperationJournalConflict):
        await journal.claim(
            "operation-1",
            _DIVERGENT_IDENTITY,
            outcome={"state": "claimed"},
        )

    assert path.read_bytes() == before
    exact = await journal.get_exact("operation-1", _IDENTITY)
    assert exact is not None
    assert exact.outcome == {"state": "claimed"}


async def test_same_identity_requires_exact_outcome_and_supports_cas_transition(
    tmp_path: Path,
) -> None:
    journal = OperationJournal(tmp_path / "outcome.json")
    await journal.claim(
        "operation-1",
        _IDENTITY,
        outcome={"state": "claimed"},
    )

    with pytest.raises(OperationJournalConflict):
        await journal.claim(
            "operation-1",
            _IDENTITY,
            outcome={"state": "different"},
        )

    transitioned = await journal.transition(
        "operation-1",
        _IDENTITY,
        expected_outcome={"state": "claimed"},
        outcome={"state": "completed", "result": {"bytes_written": 3}},
    )
    assert transitioned.outcome == {
        "state": "completed",
        "result": {"bytes_written": 3},
    }
    replay = await journal.transition(
        "operation-1",
        _IDENTITY,
        expected_outcome={"state": "claimed"},
        outcome={"state": "completed", "result": {"bytes_written": 3}},
    )
    assert replay == transitioned


async def test_legacy_key_remains_a_monotonic_but_never_exact_tombstone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(["cancelled-resource"]), encoding="utf-8")
    journal = OperationJournal(path)

    assert await journal.contains("cancelled-resource") is True
    with pytest.raises(OperationJournalConflict):
        await journal.get_exact("cancelled-resource", _IDENTITY)

    await journal.add(
        "new-resource",
        _IDENTITY,
        outcome={"state": "cancellation_requested"},
    )
    assert _stored_keys(path) == {"cancelled-resource", "new-resource"}
    assert await journal.contains("cancelled-resource") is True


async def test_legacy_resource_namespace_cannot_collide_with_typed_resource_or_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-execution-cancellations.json"
    typed_resource_key = "execution:victim-execution"
    attempt_key = (
        f"{typed_resource_key}:command:"
        f"{hashlib.sha256(_IDENTITY.command_id.encode('utf-8')).hexdigest()}"
    )
    path.write_text(
        json.dumps([typed_resource_key, attempt_key]),
        encoding="utf-8",
    )
    journal = OperationJournal(path, legacy_list_resources=True)

    # Identical bytes in the pre-typed namespace are not modern stop facts.
    assert await journal.get_resource(typed_resource_key) is None
    legacy_resource = await journal.get_legacy_resource(typed_resource_key)
    assert legacy_resource is not None
    assert legacy_resource.outcome == {"state": "legacy_unbound"}
    assert await journal.get_resource_attempt_exact(typed_resource_key, _IDENTITY) is None

    claimed, typed_tombstone = await journal.claim_resource(
        typed_resource_key,
        _IDENTITY,
        outcome={"state": "cancellation_requested"},
    )

    assert claimed is True
    assert typed_tombstone.outcome == {"state": "cancellation_requested"}
    assert (await journal.get_resource(typed_resource_key)) == typed_tombstone
    assert (await journal.get_legacy_resource(typed_resource_key)) == legacy_resource
    # ``contains`` sees the exact command-attempt record by design. A safety
    # fence must use get_resource and therefore cannot mistake it for a stop.
    assert await journal.contains(attempt_key) is True
    assert await journal.get_resource(attempt_key) is None
    legacy_attempt = await journal.get_legacy_resource(attempt_key)
    assert legacy_attempt is not None
    assert legacy_attempt.outcome == {"state": "legacy_unbound"}

    reopened = OperationJournal(path, legacy_list_resources=True)
    assert (await reopened.get_resource(typed_resource_key)) == typed_tombstone
    assert (await reopened.get_legacy_resource(typed_resource_key)) == legacy_resource
    assert await reopened.get_resource_attempt_exact(typed_resource_key, _IDENTITY) is not None


async def test_resource_tombstone_is_monotonic_across_independent_command_attempts(
    tmp_path: Path,
) -> None:
    journal = OperationJournal(tmp_path / "resource.json")
    claimed, tombstone = await journal.claim_resource(
        "execution-key-1",
        _IDENTITY,
        outcome={"state": "cancellation_requested"},
    )
    assert claimed is True
    assert tombstone.outcome == {"state": "cancellation_requested"}
    confirmed = {
        "state": "physical_stop_confirmed",
        "result": {"execution_id": "execution-1", "physical_stop_confirmed": True},
    }
    await journal.transition_resource(
        "execution-key-1",
        _IDENTITY,
        expected_outcome={"state": "cancellation_requested"},
        outcome=confirmed,
        resource_outcome=confirmed,
    )

    fresh_claimed, fresh_tombstone = await journal.claim_resource(
        "execution-key-1",
        _FRESH_IDENTITY,
        outcome={"state": "cancellation_requested"},
    )
    assert fresh_claimed is True
    assert fresh_tombstone.outcome == confirmed
    await journal.transition_resource(
        "execution-key-1",
        _FRESH_IDENTITY,
        expected_outcome={"state": "cancellation_requested"},
        outcome=confirmed,
        resource_outcome=confirmed,
    )
    assert (await journal.get_resource("execution-key-1")) == fresh_tombstone

    with pytest.raises(OperationJournalConflict):
        await journal.get_resource_attempt_exact(
            "execution-key-1",
            _DIVERGENT_IDENTITY,
        )
    assert (await journal.get_resource("execution-key-1")) == fresh_tombstone


def test_journal_processes_merge_concurrent_adds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pytest's importlib mode can load this module without putting the project
    # root on sys.path. ``spawn`` starts a fresh interpreter which must import
    # the target by its qualified ``tests.runner...`` module name.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    path = tmp_path / "operations.json"
    operation_ids = {f"process-operation-{index}" for index in range(12)}
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    processes = [
        context.Process(
            target=_add_in_process,
            args=(str(path), operation_id, start),
        )
        for operation_id in sorted(operation_ids)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert _stored_keys(path) == operation_ids


def test_journal_processes_allow_exactly_one_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    path = tmp_path / "claims.json"
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_in_process,
            args=(str(path), "delivery-1", start, results),
        )
        for _ in range(8)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=15)
        assert all(process.exitcode == 0 for process in processes)
        claimed = [results.get(timeout=5) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()

    assert claimed.count(True) == 1
    assert claimed.count(False) == 7
    assert _stored_keys(path) == {"delivery-1"}
