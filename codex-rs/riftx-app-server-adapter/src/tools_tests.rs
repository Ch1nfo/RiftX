use super::*;
use pretty_assertions::assert_eq;

#[test]
fn nmap_arguments_are_fixed_and_validated() {
    let request = StructuredToolRequest::parse(
        "rt_nmap",
        json!({"targets":["10.0.0.2"],"ports":[80,443],"serviceDetection":true}),
    )
    .expect("valid nmap arguments");

    assert_eq!(
        request.argv(),
        vec!["nmap", "-oX", "-", "-sV", "-p", "80,443", "10.0.0.2"]
    );
}

#[test]
fn target_cannot_be_interpreted_as_an_option() {
    let error = StructuredToolRequest::parse("rt_httpx", json!({"targets":["-version"]}))
        .expect_err("option-shaped target must be rejected");

    assert!(error.to_string().contains("cannot start with '-'"));
}

#[test]
fn ffuf_uses_only_managed_wordlists() {
    let request = StructuredToolRequest::parse(
        "rt_ffuf",
        json!({"url":"https://example.test/FUZZ","wordlist":"directoriesMedium"}),
    )
    .expect("valid ffuf arguments");

    assert_eq!(
        request.argv(),
        vec![
            "ffuf",
            "-s",
            "-json",
            "-u",
            "https://example.test/FUZZ",
            "-w",
            "/opt/riftx/wordlists/directory-list-2.3-medium.txt",
        ]
    );
}

#[test]
fn nuclei_uses_only_bundled_templates() {
    let request = StructuredToolRequest::parse(
        "rt_nuclei",
        json!({"targets":["https://example.test"],"tags":["riftx"]}),
    )
    .expect("valid nuclei arguments");

    assert_eq!(
        request.argv(),
        vec![
            "nuclei",
            "-silent",
            "-jsonl",
            "-duc",
            "-t",
            "/opt/riftx/nuclei-templates",
            "-u",
            "https://example.test",
            "-tags",
            "riftx",
        ]
    );
}
