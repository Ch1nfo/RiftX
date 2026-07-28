use super::*;
use codex_riftx_core::AssessmentObjective;
use codex_riftx_core::AuthorizationScope;
use codex_riftx_core::Engagement;
use codex_riftx_core::EngagementStatus;
use codex_riftx_core::ExecutionMode;
use codex_riftx_core::Scope;
use pretty_assertions::assert_eq;

#[test]
fn reload_impact_names_sorts_and_deduplicates_affected_engagements() {
    let engagement = Engagement {
        id: "engagement-a".to_string(),
        name: "Authorized lab".to_string(),
        status: EngagementStatus::Active,
        objective: AssessmentObjective {
            summary: "Assess the authorized lab".to_string(),
            success_criteria: Vec::new(),
            structured_criteria: Vec::new(),
        },
        entry_points: Vec::new(),
        mode: ExecutionMode::Pentest,
        llm_profile: "profile-a".to_string(),
        auto_limits: None,
        authorization: AuthorizationScope {
            network: Scope {
                cidrs: Vec::new(),
                domains: Vec::new(),
                ports: Vec::new(),
            },
            identities: Vec::new(),
            capabilities: Vec::new(),
            environment: codex_riftx_core::EnvironmentClass::Lab,
            window: codex_riftx_core::AuthorizationWindow {
                starts_at: None,
                expires_at: None,
            },
        },
        policy_revision: "policy-a".to_string(),
        thread_id: None,
        created_at: 1,
        updated_at: 1,
    };
    assert_eq!(
        settings_reload_impact_view(
            vec![
                ActiveTurnStatus {
                    engagement_id: "engagement-b".to_string(),
                    profile_name: "profile-b".to_string(),
                },
                ActiveTurnStatus {
                    engagement_id: "engagement-a".to_string(),
                    profile_name: "profile-a".to_string(),
                },
                ActiveTurnStatus {
                    engagement_id: "engagement-a".to_string(),
                    profile_name: "profile-a".to_string(),
                },
            ],
            vec![engagement],
        ),
        SettingsReloadImpactView {
            active_turns: vec![
                SettingsAffectedEngagementView {
                    engagement_id: "engagement-a".to_string(),
                    engagement_name: "Authorized lab".to_string(),
                    profile_name: "profile-a".to_string(),
                },
                SettingsAffectedEngagementView {
                    engagement_id: "engagement-b".to_string(),
                    engagement_name: "engagement-b".to_string(),
                    profile_name: "profile-b".to_string(),
                },
            ],
        }
    );
}

#[test]
fn reload_preparation_rejects_a_changed_impact_set() {
    assert_eq!(
        validate_expected_engagements(
            vec!["engagement-b".to_string(), "engagement-a".to_string()],
            &["engagement-a".to_string(), "engagement-b".to_string()],
        ),
        Ok(())
    );
    assert_eq!(
        validate_expected_engagements(
            vec!["engagement-a".to_string()],
            &["engagement-a".to_string(), "engagement-b".to_string()],
        ),
        Err(DesktopError::new(
            "settings_impact_changed",
            "Affected active tasks changed before confirmation; review the updated list and retry",
        ))
    );
}
