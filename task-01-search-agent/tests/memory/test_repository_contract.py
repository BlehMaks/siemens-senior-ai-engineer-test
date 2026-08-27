from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from search_agent.memory import (
    InMemoryReflectionRepository,
    ReflectionInputError,
    ReflectionRepository,
    SQLiteReflectionRepository,
)

from .helpers import reflection


@pytest.fixture(params=("memory", "sqlite"))
def repository(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[ReflectionRepository]:
    if request.param == "memory":
        yield InMemoryReflectionRepository()
        return
    adapter = SQLiteReflectionRepository(tmp_path / "contract.sqlite3")
    try:
        yield adapter
    finally:
        adapter.close()


def test_contract_replaces_and_returns_strict_reflections(
    repository: ReflectionRepository,
) -> None:
    original = reflection(request="Find the original Siemens report.")
    replacement = reflection(request="Find the replacement Siemens report.")

    repository.put(original)
    repository.put(replacement)

    stored = repository.get(
        tenant_id="tenant-one", session_id="session-one", run_id="run-000001"
    )
    assert stored == replacement
    assert stored is not replacement


def test_contract_never_crosses_tenant_or_session(
    repository: ReflectionRepository,
) -> None:
    repository.put(reflection())

    assert (
        repository.get(
            tenant_id="tenant-two",
            session_id="session-one",
            run_id="run-000001",
        )
        is None
    )
    assert (
        repository.get(
            tenant_id="tenant-one",
            session_id="session-two",
            run_id="run-000001",
        )
        is None
    )
    assert (
        repository.list_session(tenant_id="tenant-two", session_id="session-one") == ()
    )
    assert (
        repository.delete_run(
            tenant_id="tenant-two",
            session_id="session-one",
            run_id="run-000001",
        )
        is False
    )
    assert (
        repository.delete_session(tenant_id="tenant-one", session_id="session-two") == 0
    )


def test_contract_list_is_ordered_and_bounded(
    repository: ReflectionRepository,
) -> None:
    for run_id in ("run-000003", "run-000001", "run-000002"):
        repository.put(reflection(run_id=run_id))

    listed = repository.list_session(
        tenant_id="tenant-one", session_id="session-one", limit=2
    )

    assert tuple(item.run_id for item in listed) == ("run-000001", "run-000002")
    for invalid_limit in (0, 101, True, -1):
        with pytest.raises(ReflectionInputError, match="list limit"):
            repository.list_session(
                tenant_id="tenant-one",
                session_id="session-one",
                limit=invalid_limit,
            )


def test_contract_deletes_by_run_session_and_tenant(
    repository: ReflectionRepository,
) -> None:
    repository.put(reflection(run_id="run-000001"))
    repository.put(reflection(run_id="run-000002"))
    repository.put(reflection(session_id="session-two", run_id="run-000003"))
    repository.put(
        reflection(
            tenant_id="tenant-two",
            session_id="session-one",
            run_id="run-000004",
        )
    )

    assert repository.delete_run(
        tenant_id="tenant-one", session_id="session-one", run_id="run-000001"
    )
    assert (
        repository.delete_session(tenant_id="tenant-one", session_id="session-one") == 1
    )
    assert repository.delete_tenant(tenant_id="tenant-one") == 1
    assert (
        repository.list_session(tenant_id="tenant-one", session_id="session-two") == ()
    )
    assert (
        repository.get(
            tenant_id="tenant-two", session_id="session-one", run_id="run-000004"
        )
        is not None
    )
    assert repository.delete_tenant(tenant_id="tenant-two") == 1


def test_contract_rejects_invalid_scope_and_hostile_reflection_container(
    repository: ReflectionRepository,
) -> None:
    with pytest.raises(ReflectionInputError, match="scope id"):
        repository.get(
            tenant_id="tenant-one' OR 1=1 --",
            session_id="session-one",
            run_id="run-000001",
        )

    hostile = reflection(run_id="run-hostile-memory")
    object.__setattr__(hostile, "actions", list(hostile.actions))
    with pytest.raises(ReflectionInputError, match="strict validation"):
        repository.put(hostile)
