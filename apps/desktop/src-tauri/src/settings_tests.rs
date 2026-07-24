use super::*;
use pretty_assertions::assert_eq;

#[test]
fn settings_config_distinguishes_keyring_and_environment_sources() {
    let keyring: SettingsConfig = toml::from_str(
        r#"
[llm]
model = "model-a"
base_url = "https://llm.example.test/v1"
api_key = { source = "keyring", profile = "default" }
"#,
    )
    .expect("keyring config");
    let environment: SettingsConfig = toml::from_str(
        r#"
[llm]
model = "model-b"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
"#,
    )
    .expect("environment config");

    assert_eq!(
        (keyring.llm.model, keyring.llm.api_key),
        (
            "model-a".to_string(),
            SettingsApiKeySource::Keyring {
                profile: "default".to_string(),
            },
        )
    );
    assert_eq!(
        (environment.llm.model, environment.llm.api_key),
        (
            "model-b".to_string(),
            SettingsApiKeySource::Environment {
                variable: "RIFTX_TEST_API_KEY".to_string(),
            },
        )
    );
}
