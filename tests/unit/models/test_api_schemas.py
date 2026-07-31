from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from riftx.api.errors import _redact_validation_errors
from riftx.api.schemas.executions import ExecutionResponse
from riftx.api.schemas.models import ModelProfileUpdateRequest
from riftx.domain import Execution, ExecutionStatus, ExecutorType
from riftx.models import MAX_MODEL_TIMEOUT_SECONDS, ModelAPI, ModelProviderKind


def test_api_profile_requires_base_url_for_compatible_provider() -> None:
    with pytest.raises(ValidationError, match="explicit base_url"):
        ModelProfileUpdateRequest.model_validate(
            {
                "provider": "openai_compatible",
                "model": "local-model",
                "requires_api_key": False,
            }
        )


def test_api_profile_keeps_openai_chat_completions_compatibility() -> None:
    request = ModelProfileUpdateRequest.model_validate(
        {
            "provider": "openai",
            "model": "openai-model",
            "requires_api_key": False,
        }
    )

    assert request.provider is ModelProviderKind.OPENAI
    assert request.request_mode is ModelAPI.CHAT_COMPLETIONS
    assert request.base_url is None


@pytest.mark.parametrize(
    "timeout_seconds",
    [float("nan"), float("inf"), float("-inf"), 0, MAX_MODEL_TIMEOUT_SECONDS + 0.01],
)
def test_api_profile_timeout_matches_yaml_bounds(timeout_seconds: float) -> None:
    with pytest.raises(ValidationError):
        ModelProfileUpdateRequest.model_validate(
            {
                "provider": "openai",
                "model": "openai-model",
                "timeout_seconds": timeout_seconds,
            }
        )


def test_api_profile_rejects_key_input_when_credentials_are_disabled() -> None:
    with pytest.raises(ValidationError, match="must not be supplied"):
        ModelProfileUpdateRequest.model_validate(
            {
                "provider": "openai",
                "model": "local-model",
                "requires_api_key": False,
                "api_key": "must-never-be-used",
            }
        )


def test_pydantic_error_text_hides_secret_input_values() -> None:
    secret = "pydantic-secret-input-value"

    with pytest.raises(ValidationError) as captured:
        ModelProfileUpdateRequest.model_validate(
            {
                "provider": "openai_compatible",
                "model": "local-model",
                "api_key": secret,
            }
        )

    assert secret not in str(captured.value)


@pytest.mark.parametrize("field", ["apiKey", "accessToken", "databasePassword", "clientSecret"])
def test_http_validation_redaction_recognizes_camel_case_sensitive_fields(field: str) -> None:
    secret = "camel-case-secret-value"

    redacted = _redact_validation_errors(
        [{"loc": ["body", field], "msg": "invalid value", "input": secret}]
    )

    assert secret not in str(redacted)
    assert redacted[0]["input"] == "[redacted]"


def test_execution_response_exposes_durable_physical_stop_proof() -> None:
    confirmed_at = datetime(2026, 8, 1, tzinfo=UTC)
    execution = Execution(
        execution_key="api-stop-proof",
        run_id="run-1",
        node_id="local",
        executor_type=ExecutorType.PROCESS,
        argv=["true"],
        cwd="/tmp",
        status=ExecutionStatus.EXITED,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        physical_stop_confirmed_at=confirmed_at,
    )

    response = ExecutionResponse.from_domain(execution)

    assert response.physical_stop_confirmed_at == confirmed_at
    assert response.model_dump(mode="json")["physical_stop_confirmed_at"] == (
        "2026-08-01T00:00:00Z"
    )
