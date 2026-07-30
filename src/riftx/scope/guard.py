"""Lightweight structured-target checks for authorized Run scope."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from riftx.domain import Scope
from riftx.domain.base import utc_now


class ScopeTargetKind(StrEnum):
    IP = "ip"
    CIDR = "cidr"
    DOMAIN = "domain"
    URL = "url"


class ScopeViolationError(ValueError):
    """Raised when a structured Skill target is outside the authorized scope."""


@dataclass(frozen=True, slots=True)
class ScopeDecision:
    allowed: bool
    target: str
    kind: ScopeTargetKind
    reason: str


class ScopeGuard:
    """Check explicit IP, CIDR, domain, and URL targets without claiming sandboxing."""

    def __init__(self, scope: Scope) -> None:
        self._scope = scope

    def check(
        self,
        target: str,
        *,
        kind: ScopeTargetKind | None = None,
        at: datetime | None = None,
    ) -> ScopeDecision:
        value = target.strip()
        if not value:
            raise ScopeViolationError("scope target must not be empty")
        resolved_kind = kind or infer_scope_target_kind(value)
        moment = at or utc_now()

        if self._scope.starts_at and moment < self._scope.starts_at:
            return ScopeDecision(False, value, resolved_kind, "scope time window has not started")
        if self._scope.ends_at and moment >= self._scope.ends_at:
            return ScopeDecision(False, value, resolved_kind, "scope time window has ended")
        if self._is_excluded(value, resolved_kind):
            return ScopeDecision(False, value, resolved_kind, "target matches a scope exclusion")
        if not self._has_positive_constraints():
            return ScopeDecision(
                True, value, resolved_kind, "scope has no positive target constraint"
            )

        allowed = self._is_allowed(value, resolved_kind)
        return ScopeDecision(
            allowed,
            value,
            resolved_kind,
            "target matches authorized scope" if allowed else "target is outside authorized scope",
        )

    def require(
        self,
        target: str,
        *,
        kind: ScopeTargetKind | None = None,
        at: datetime | None = None,
    ) -> ScopeDecision:
        decision = self.check(target, kind=kind, at=at)
        if not decision.allowed:
            raise ScopeViolationError(
                f"{decision.kind.value} target {decision.target!r} rejected: {decision.reason}"
            )
        return decision

    def _has_positive_constraints(self) -> bool:
        scope = self._scope
        return bool(scope.cidrs or scope.ips or scope.domains or scope.url_prefixes)

    def _is_allowed(self, value: str, kind: ScopeTargetKind) -> bool:
        scope = self._scope
        if kind is ScopeTargetKind.IP:
            address = ipaddress.ip_address(value)
            return value in scope.ips or any(
                address in ipaddress.ip_network(item, strict=False) for item in scope.cidrs
            )
        if kind is ScopeTargetKind.CIDR:
            network = ipaddress.ip_network(value, strict=False)
            return any(
                network.subnet_of(ipaddress.ip_network(item, strict=False)) for item in scope.cidrs
            )
        if kind is ScopeTargetKind.DOMAIN:
            domain = _normalize_domain(value)
            return any(_domain_matches(domain, item) for item in scope.domains)
        url = _normalize_url(value)
        if any(_url_prefix_matches(url, prefix) for prefix in scope.url_prefixes):
            return True
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return any(_domain_matches(hostname, item) for item in scope.domains)
        return hostname in scope.ips or any(
            address in ipaddress.ip_network(item, strict=False) for item in scope.cidrs
        )

    def _is_excluded(self, value: str, kind: ScopeTargetKind) -> bool:
        for exclusion in self._scope.exclusions:
            try:
                exclusion_kind = infer_scope_target_kind(exclusion)
            except ValueError:
                if value == exclusion:
                    return True
                continue
            if kind in {ScopeTargetKind.IP, ScopeTargetKind.CIDR} and exclusion_kind in {
                ScopeTargetKind.IP,
                ScopeTargetKind.CIDR,
            }:
                if _network_overlap(value, kind, exclusion, exclusion_kind):
                    return True
            elif kind is ScopeTargetKind.DOMAIN and exclusion_kind is ScopeTargetKind.DOMAIN:
                if _domain_matches(_normalize_domain(value), exclusion):
                    return True
            elif kind is ScopeTargetKind.URL and exclusion_kind is ScopeTargetKind.URL:
                if _url_prefix_matches(_normalize_url(value), exclusion):
                    return True
            elif kind is ScopeTargetKind.URL and exclusion_kind is ScopeTargetKind.DOMAIN:
                hostname = urlsplit(_normalize_url(value)).hostname
                if hostname and _domain_matches(hostname, exclusion):
                    return True
            elif kind is ScopeTargetKind.URL and exclusion_kind in {
                ScopeTargetKind.IP,
                ScopeTargetKind.CIDR,
            }:
                hostname = urlsplit(_normalize_url(value)).hostname
                try:
                    ipaddress.ip_address(hostname or "")
                except ValueError:
                    continue
                if _network_overlap(hostname or "", ScopeTargetKind.IP, exclusion, exclusion_kind):
                    return True
        return False


def infer_scope_target_kind(target: str) -> ScopeTargetKind:
    value = target.strip()
    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError(f"invalid URL scope target: {target!r}")
        return ScopeTargetKind.URL
    try:
        parsed_network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        if not _normalize_domain(value):
            raise ValueError(f"invalid scope target: {target!r}") from None
        return ScopeTargetKind.DOMAIN
    if parsed_network.prefixlen != parsed_network.max_prefixlen:
        return ScopeTargetKind.CIDR
    return ScopeTargetKind.IP


def _network_overlap(
    value: str,
    kind: ScopeTargetKind,
    exclusion: str,
    exclusion_kind: ScopeTargetKind,
) -> bool:
    target_network = (
        ipaddress.ip_network(value, strict=False)
        if kind is ScopeTargetKind.CIDR
        else ipaddress.ip_network(value)
    )
    excluded_network = (
        ipaddress.ip_network(exclusion, strict=False)
        if exclusion_kind is ScopeTargetKind.CIDR
        else ipaddress.ip_network(exclusion)
    )
    return target_network.overlaps(excluded_network)


def _normalize_domain(value: str) -> str:
    return value.strip().rstrip(".").lower()


def _domain_matches(domain: str, configured: str) -> bool:
    expected = _normalize_domain(configured)
    return domain == expected or domain.endswith(f".{expected}")


def _normalize_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"invalid URL scope target: {value!r}")
    hostname = parsed.hostname.lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{hostname}{port}{path}{query}"


def _url_prefix_matches(normalized_url: str, configured: str) -> bool:
    prefix = _normalize_url(configured)
    if prefix.endswith("/"):
        return normalized_url.startswith(prefix)
    return normalized_url == prefix or normalized_url.startswith((prefix + "/", prefix + "?"))
