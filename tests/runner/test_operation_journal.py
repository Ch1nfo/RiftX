from __future__ import annotations

import asyncio
import json
import multiprocessing
from pathlib import Path

import pytest

from riftx.runner.terminal_manager import OperationJournal


def _add_in_process(path: str, operation_id: str, start) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("journal concurrency test start signal timed out")
    asyncio.run(OperationJournal(Path(path)).add(operation_id))


def _claim_in_process(path: str, operation_id: str, start, results) -> None:
    if not start.wait(timeout=10):
        raise RuntimeError("journal claim test start signal timed out")
    results.put(asyncio.run(OperationJournal(Path(path)).claim(operation_id)))


async def test_independent_journal_instances_merge_concurrent_adds(tmp_path: Path) -> None:
    path = tmp_path / "operations.json"
    operation_ids = {f"operation-{index}" for index in range(32)}

    await asyncio.gather(
        *(OperationJournal(path).add(operation_id) for operation_id in operation_ids)
    )

    assert set(json.loads(path.read_text(encoding="utf-8"))) == operation_ids


async def test_independent_journal_instances_allow_exactly_one_claim(tmp_path: Path) -> None:
    path = tmp_path / "claims.json"

    claimed = await asyncio.gather(*(OperationJournal(path).claim("delivery-1") for _ in range(32)))

    assert claimed.count(True) == 1
    assert claimed.count(False) == 31
    assert json.loads(path.read_text(encoding="utf-8")) == ["delivery-1"]


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

    assert set(json.loads(path.read_text(encoding="utf-8"))) == operation_ids


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
    assert json.loads(path.read_text(encoding="utf-8")) == ["delivery-1"]
