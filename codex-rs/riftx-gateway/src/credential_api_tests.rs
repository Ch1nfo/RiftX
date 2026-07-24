use super::*;
use crate::api::build_router;
use crate::api::tests::native_engagement;
use crate::api::tests::test_state;
use axum::body::Body;
use axum::http::Request;
use codex_riftx_core::EngagementStatus;
use http_body_util::BodyExt;
use pretty_assertions::assert_eq;
use tempfile::TempDir;
use tower::ServiceExt;

#[tokio::test]
async fn credential_grants_are_scoped_audited_and_policy_bound() {
    let temp = TempDir::new().expect("temp dir");
    let state = test_state(&temp).await;
    let mut engagement = native_engagement(&state, "eng-credentials", EngagementStatus::Draft);
    engagement.authorization.capabilities = vec!["network.discovery".to_string()];
    state
        .store
        .put_engagement(&engagement)
        .await
        .expect("engagement");
    let app = build_router(state.clone());

    let response = post_json(
        app.clone(),
        "/v1/engagements/eng-credentials/credentials",
        r#"{"label":"Authorized lab user","kind":"password","username":"lab.user","domain":"LAB"}"#,
    )
    .await;
    assert_eq!(response.status(), StatusCode::CREATED);
    let reference: CredentialReference = response_json(response).await;
    assert_eq!(reference.engagement_id, engagement.id);
    assert_eq!(
        reference.storage_key,
        format!("engagement/{}/credential/{}", engagement.id, reference.id)
    );
    let reference_json = serde_json::to_value(&reference).expect("reference JSON");
    assert!(reference_json.get("secret").is_none());

    let response = post_json(
        app.clone(),
        "/v1/engagements/eng-credentials/credential-grants",
        &serde_json::json!({
            "credentialId": reference.id,
            "allowedTargets": {
                "cidrs": ["10.10.0.10/32"],
                "domains": [],
                "ports": [],
            },
            "allowedCapabilities": ["network.discovery"],
            "maxUses": 5,
            "maxFailuresPerIdentity": 2,
            "startsAt": null,
            "expiresAt": 2_000_000_000_i64,
        })
        .to_string(),
    )
    .await;
    assert_eq!(response.status(), StatusCode::CREATED);
    let grant: CredentialGrant = response_json(response).await;
    let with_grant = state
        .store
        .engagement(&engagement.id)
        .await
        .expect("engagement with grant");
    assert_ne!(with_grant.policy_revision, engagement.policy_revision);
    let agent_input = crate::api::operational_agent_input(
        &state,
        &engagement.id,
        "Use the authorized credential reference".to_string(),
    )
    .await
    .expect("agent input");
    assert!(agent_input.contains(&format!("credential://{}", reference.id)));
    assert!(!agent_input.contains(&reference.storage_key));

    let response = post_json(
        app.clone(),
        "/v1/engagements/eng-credentials/credential-grants",
        &serde_json::json!({
            "credentialId": reference.id,
            "allowedTargets": {
                "cidrs": ["10.20.0.1/32"],
                "domains": [],
                "ports": [],
            },
            "allowedCapabilities": ["network.discovery"],
            "maxUses": 1,
            "maxFailuresPerIdentity": 1,
            "startsAt": null,
            "expiresAt": 2_000_000_000_i64,
        })
        .to_string(),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let response = post_json(
        app.clone(),
        &format!(
            "/v1/engagements/eng-credentials/credentials/{}/delete",
            reference.id
        ),
        "{}",
    )
    .await;
    assert_eq!(response.status(), StatusCode::CONFLICT);

    let response = post_json(
        app,
        &format!(
            "/v1/engagements/eng-credentials/credential-grants/{}/revoke",
            grant.id
        ),
        "{}",
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    let revoked: CredentialGrant = response_json(response).await;
    assert!(revoked.revoked_at.is_some());
    let after_revoke = state
        .store
        .engagement(&engagement.id)
        .await
        .expect("engagement after revoke");
    assert_ne!(after_revoke.policy_revision, with_grant.policy_revision);

    let audit = tokio::fs::read_to_string(temp.path().join("audit.jsonl"))
        .await
        .expect("credential audit");
    assert!(audit.contains("credential/referenceCreated"));
    assert!(audit.contains("credential/grantCreated"));
    assert!(audit.contains("credential/grantRevoked"));
    assert!(!audit.contains("lab.user"));
    assert!(!audit.contains(&reference.storage_key));
}

async fn post_json(app: axum::Router, uri: &str, body: &str) -> axum::response::Response {
    app.oneshot(
        Request::builder()
            .method("POST")
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .expect("request"),
    )
    .await
    .expect("response")
}

async fn response_json<T: serde::de::DeserializeOwned>(response: axum::response::Response) -> T {
    serde_json::from_slice(
        &response
            .into_body()
            .collect()
            .await
            .expect("response body")
            .to_bytes(),
    )
    .expect("response JSON")
}
