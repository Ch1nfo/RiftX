use super::*;
use pretty_assertions::assert_eq;

#[test]
fn tool_names_are_truncated_on_utf8_boundaries() {
    let value = "工具-timeout";

    assert_eq!(truncate_utf8(value, 4), "工");
    assert_eq!(truncate_utf8(value, value.len()), value);
}
