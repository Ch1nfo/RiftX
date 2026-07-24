use super::*;
use pretty_assertions::assert_eq;

#[test]
fn settings_config_distinguishes_keyring_and_environment_sources() {
    let keyring: SettingsConfig = toml::from_str(
        r#"
[llm]
default_profile = "openai"

[llm.profiles.openai]
model = "model-a"
base_url = "https://llm.example.test/v1"
api_key = { source = "keyring", credential = "openai" }
timeout_seconds = 300
reasoning_level = "high"
context_budget = 200000
"#,
    )
    .expect("keyring config");
    let environment: SettingsConfig = toml::from_str(
        r#"
[llm]
default_profile = "local"

[llm.profiles.local]
model = "model-b"
base_url = "http://127.0.0.1:8766/v1"
api_key = { source = "environment", variable = "RIFTX_TEST_API_KEY" }
timeout_seconds = 60
reasoning_level = "medium"
context_budget = 64000
"#,
    )
    .expect("environment config");

    assert_eq!(
        (
            keyring.llm.default_profile,
            keyring.llm.profiles["openai"].clone(),
        ),
        (
            "openai".to_string(),
            SettingsLlmProfileConfig {
                model: "model-a".to_string(),
                base_url: "https://llm.example.test/v1".to_string(),
                api_key: SettingsApiKeySource::Keyring {
                    credential: "openai".to_string(),
                },
                timeout_seconds: 300,
                reasoning_level: "high".to_string(),
                context_budget: 200_000,
            },
        )
    );
    assert_eq!(
        (
            environment.llm.default_profile,
            environment.llm.profiles["local"].clone(),
        ),
        (
            "local".to_string(),
            SettingsLlmProfileConfig {
                model: "model-b".to_string(),
                base_url: "http://127.0.0.1:8766/v1".to_string(),
                api_key: SettingsApiKeySource::Environment {
                    variable: "RIFTX_TEST_API_KEY".to_string(),
                },
                timeout_seconds: 60,
                reasoning_level: "medium".to_string(),
                context_budget: 64_000,
            },
        )
    );
}
