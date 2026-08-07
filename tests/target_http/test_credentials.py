from __future__ import annotations

import pytest
from tests.integration.persistence.test_capability_repository import version

from riftx.application.errors import ApplicationConflictError
from riftx.capabilities import (
    CapabilityPermission,
    CapabilityVersionStatus,
    InMemoryCapabilitySelectionStore,
    build_technique_selection,
    capability_manifest_digest,
)
from riftx.domain import ApprovalLevel
from riftx.domain.base import utc_now
from riftx.target_http import TargetHttpRequest
from riftx.target_http.service import CapabilityCredentialReferenceAuthorizer


async def test_capability_authorizer_accepts_only_selected_technique_references() -> None:
    _capability, original = version("1.0.0")
    permission = CapabilityPermission(
        effect_class=original.manifest.permission.effect_class,
        approval_level=ApprovalLevel.SENSITIVE,
        requires_scope=True,
        credential_references=("RIFTX_FIXTURE_LOGIN", "RIFTX_FIXTURE_SESSION"),
    )
    manifest = original.manifest.model_copy(update={"permission": permission})
    selected_version = original.model_copy(
        update={
            "manifest": manifest,
            "manifest_digest": capability_manifest_digest(manifest),
        }
    )
    selected_at = utc_now()
    store = InMemoryCapabilitySelectionStore()
    await store.save_selection(
        build_technique_selection(
            selected_version,
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            reason="test",
            selected_at=selected_at,
            updated_at=selected_at,
        )
    )
    authorizer = CapabilityCredentialReferenceAuthorizer(store)

    await authorizer.require_allowed(
        run_id="run-1",
        session_id="session-1",
        references=("RIFTX_FIXTURE_LOGIN", "RIFTX_FIXTURE_SESSION"),
    )
    with pytest.raises(ApplicationConflictError) as forbidden:
        await authorizer.require_allowed(
            run_id="run-1",
            session_id="session-1",
            references=("RIFTX_UNSELECTED_SECRET",),
        )
    assert forbidden.value.code == "target_http_credential_reference_forbidden"


async def test_capability_authorizer_rejects_damaged_selection_snapshot() -> None:
    _capability, original = version("1.0.0")
    permission = original.manifest.permission.model_copy(
        update={"credential_references": ("RIFTX_FIXTURE_LOGIN",)}
    )
    manifest = original.manifest.model_copy(update={"permission": permission})
    selected_version = original.model_copy(
        update={
            "manifest": manifest,
            "manifest_digest": capability_manifest_digest(manifest),
        }
    )
    selected_at = utc_now()
    selection = build_technique_selection(
        selected_version,
        run_id="run-1",
        session_id="session-1",
        agent_id="primary",
        reason="test",
        selected_at=selected_at,
        updated_at=selected_at,
    ).model_copy(update={"snapshot": {}})
    store = InMemoryCapabilitySelectionStore()
    await store.save_selection(selection)

    with pytest.raises(ApplicationConflictError) as invalid:
        await CapabilityCredentialReferenceAuthorizer(store).require_allowed(
            run_id="run-1",
            session_id="session-1",
            references=("RIFTX_FIXTURE_LOGIN",),
        )
    assert invalid.value.code == "target_http_credential_selection_invalid"


async def test_capability_authorizer_rejects_inactive_selected_version() -> None:
    _capability, original = version("1.0.0")
    permission = original.manifest.permission.model_copy(
        update={"credential_references": ("RIFTX_FIXTURE_LOGIN",)}
    )
    manifest = original.manifest.model_copy(update={"permission": permission})
    inactive_version = original.model_copy(
        update={
            "manifest": manifest,
            "manifest_digest": capability_manifest_digest(manifest),
            "status": CapabilityVersionStatus.DISABLED,
        }
    )
    selected_at = utc_now()
    store = InMemoryCapabilitySelectionStore()
    await store.save_selection(
        build_technique_selection(
            inactive_version,
            run_id="run-1",
            session_id="session-1",
            agent_id="primary",
            reason="test",
            selected_at=selected_at,
            updated_at=selected_at,
        )
    )

    with pytest.raises(ApplicationConflictError) as invalid:
        await CapabilityCredentialReferenceAuthorizer(store).require_allowed(
            run_id="run-1",
            session_id="session-1",
            references=("RIFTX_FIXTURE_LOGIN",),
        )
    assert invalid.value.code == "target_http_credential_selection_invalid"


def test_target_http_secret_references_are_fingerprinted_without_values() -> None:
    request = TargetHttpRequest(
        execution_key="key",
        method="POST",
        url="https://target.internal/login",
        header_secret_refs={"Authorization": "RIFTX_FIXTURE_AUTH"},
        body_secret_ref="RIFTX_FIXTURE_LOGIN",
        cookie_secret_refs={"session": "RIFTX_FIXTURE_SESSION"},
    )

    payload = request.runner_payload()
    assert request.credential_references == (
        "RIFTX_FIXTURE_AUTH",
        "RIFTX_FIXTURE_LOGIN",
        "RIFTX_FIXTURE_SESSION",
    )
    assert payload["header_secret_refs"] == {
        "Authorization": "RIFTX_FIXTURE_AUTH"
    }
    assert payload["body_secret_ref"] == "RIFTX_FIXTURE_LOGIN"
    assert payload["cookie_secret_refs"] == {"session": "RIFTX_FIXTURE_SESSION"}
    assert "secret-value" not in str(payload)


def test_target_http_rejects_public_and_secret_value_collisions() -> None:
    with pytest.raises(ValueError, match="Header"):
        TargetHttpRequest(
            execution_key="key",
            method="GET",
            url="https://target.internal/",
            headers={"Authorization": "public"},
            header_secret_refs={"authorization": "RIFTX_FIXTURE_AUTH"},
        )
    with pytest.raises(ValueError, match="Cookie"):
        TargetHttpRequest(
            execution_key="key",
            method="GET",
            url="https://target.internal/",
            cookies={"session": "public"},
            cookie_secret_refs={"session": "RIFTX_FIXTURE_SESSION"},
        )
