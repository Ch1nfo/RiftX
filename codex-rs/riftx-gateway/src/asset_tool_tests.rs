use super::*;
use pretty_assertions::assert_eq;

#[test]
fn asset_values_are_normalized_by_kind() {
    assert_eq!(
        normalize_asset(AssetKind::Host, " 10.10.0.20 "),
        Ok("10.10.0.20".to_string())
    );
    assert_eq!(
        normalize_asset(AssetKind::Domain, "API.Example.Test."),
        Ok("api.example.test".to_string())
    );
    assert_eq!(
        normalize_asset(AssetKind::Url, "https://API.Example.Test/path#fragment"),
        Ok("https://api.example.test/path".to_string())
    );
}

#[test]
fn asset_kind_rejects_ambiguous_or_secret_bearing_values() {
    assert!(normalize_asset(AssetKind::Host, "api.example.test").is_err());
    assert!(normalize_asset(AssetKind::Domain, "10.10.0.20").is_err());
    assert!(normalize_asset(AssetKind::Url, "ftp://api.example.test/file").is_err());
    assert!(normalize_asset(AssetKind::Url, "https://user:secret@example.test/").is_err());
}
