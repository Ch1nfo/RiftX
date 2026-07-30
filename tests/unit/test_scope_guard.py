from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from riftx.domain import Scope
from riftx.scope import ScopeGuard, ScopeViolationError


def test_scope_guard_matches_network_domain_and_url_targets() -> None:
    guard = ScopeGuard(
        Scope(
            cidrs=["192.0.2.0/24"],
            ips=["198.51.100.7"],
            domains=["example.test"],
            url_prefixes=["https://other.test/allowed/"],
        )
    )

    assert guard.require("192.0.2.8").allowed
    assert guard.require("192.0.2.128/25").allowed
    assert guard.require("198.51.100.7").allowed
    assert guard.require("api.example.test").allowed
    assert guard.require("https://example.test/app").allowed
    assert guard.require("https://other.test/allowed/page").allowed


@pytest.mark.parametrize(
    "target",
    [
        "203.0.113.10",
        "192.0.3.0/24",
        "example.invalid",
        "https://other.test/denied",
    ],
)
def test_scope_guard_rejects_out_of_scope_targets(target: str) -> None:
    guard = ScopeGuard(
        Scope(
            cidrs=["192.0.2.0/24"],
            domains=["example.test"],
            url_prefixes=["https://other.test/allowed/"],
        )
    )

    with pytest.raises(ScopeViolationError, match="outside authorized scope"):
        guard.require(target)


def test_scope_guard_exclusions_override_positive_scope() -> None:
    guard = ScopeGuard(
        Scope(
            cidrs=["192.0.2.0/24"],
            domains=["example.test"],
            exclusions=["192.0.2.8", "admin.example.test"],
        )
    )

    with pytest.raises(ScopeViolationError, match="scope exclusion"):
        guard.require("192.0.2.8")
    with pytest.raises(ScopeViolationError, match="scope exclusion"):
        guard.require("api.admin.example.test")


def test_scope_guard_enforces_time_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    guard = ScopeGuard(
        Scope(
            ips=["192.0.2.8"],
            starts_at=now + timedelta(minutes=1),
            ends_at=now + timedelta(minutes=2),
        )
    )

    assert not guard.check("192.0.2.8", at=now).allowed
    assert guard.require("192.0.2.8", at=now + timedelta(seconds=90)).allowed
    assert not guard.check("192.0.2.8", at=now + timedelta(minutes=2)).allowed


def test_empty_positive_scope_allows_targets_but_honors_exclusions() -> None:
    guard = ScopeGuard(Scope(exclusions=["blocked.example.test"]))

    assert guard.require("free.example.test").allowed
    with pytest.raises(ScopeViolationError, match="scope exclusion"):
        guard.require("blocked.example.test")
