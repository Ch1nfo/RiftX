import pytest
from pydantic import ValidationError

from riftx.domain import BrowserMode, BrowserSession, BrowserSessionStatus


def test_persistent_and_cdp_modes_require_their_runner_local_configuration() -> None:
    common = {
        "run_id": "run-1",
        "agent_session_id": "session-1",
        "node_id": "local",
    }
    with pytest.raises(ValidationError, match="profile_id"):
        BrowserSession(mode=BrowserMode.MANAGED_PERSISTENT, **common)
    with pytest.raises(ValidationError, match="cdp_endpoint"):
        BrowserSession(mode=BrowserMode.ATTACHED_CDP, **common)

    persistent = BrowserSession(
        mode=BrowserMode.MANAGED_PERSISTENT,
        profile_id="engagement-profile",
        **common,
    )
    attached = BrowserSession(
        mode=BrowserMode.ATTACHED_CDP,
        cdp_endpoint="http://127.0.0.1:9222",
        **common,
    )
    assert persistent.profile_id == "engagement-profile"
    assert attached.cdp_endpoint.endswith("9222")


def test_agent_tool_result_does_not_expose_runner_profile_or_cdp_secrets() -> None:
    from riftx.browser.service import BrowserView
    from riftx.browser.tools import BrowserToolResult
    from riftx.domain import BrowserPage, BrowserSessionStatus

    session = BrowserSession(
        run_id="run-1",
        agent_session_id="session-1",
        node_id="local",
        mode=BrowserMode.ATTACHED_CDP,
        status=BrowserSessionStatus.ACTIVE,
        cdp_endpoint="http://127.0.0.1:9222/private",
        profile_path="/runner/private/profile",
    )
    result = BrowserToolResult.from_view(
        BrowserView(
            session=session,
            pages=[
                BrowserPage(
                    browser_session_id=session.id,
                    url="https://example.com/",
                )
            ],
        )
    ).model_dump(mode="json")
    assert "profile_path" not in result["session"]
    assert "cdp_endpoint" not in result["session"]


def test_lost_session_requires_later_closed_acknowledgement_for_confirmation() -> None:
    session = BrowserSession(
        run_id="run-1",
        agent_session_id="session-1",
        node_id="local",
        mode=BrowserMode.MANAGED_EPHEMERAL,
        status=BrowserSessionStatus.LOST,
    )

    assert session.may_still_be_running is True
    assert session.stop_confirmed is False
    session.transition_to(BrowserSessionStatus.CLOSED)
    assert session.may_still_be_running is False
    assert session.stop_confirmed is True
