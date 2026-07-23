use codex_riftx_app_server_adapter::StructuredToolOutput;
use codex_riftx_app_server_adapter::StructuredToolRequest;
use codex_riftx_core::Asset;
use codex_riftx_core::Evidence;
use codex_riftx_core::Finding;
use codex_riftx_core::FindingSeverity;
use codex_riftx_core::Service;
use codex_riftx_core::StateStore;
use quick_xml::Reader;
use quick_xml::events::Event;
use serde_json::Value;
use uuid::Uuid;

pub(crate) async fn persist(
    store: &StateStore,
    engagement_id: &str,
    request: &StructuredToolRequest,
    output: &StructuredToolOutput,
) -> anyhow::Result<()> {
    match request {
        StructuredToolRequest::Nmap(_) => persist_nmap(store, engagement_id, &output.stdout).await,
        StructuredToolRequest::Httpx(_) => {
            persist_httpx(store, engagement_id, &output.stdout).await
        }
        StructuredToolRequest::Nuclei(_) => {
            persist_nuclei(store, engagement_id, &output.stdout).await
        }
        StructuredToolRequest::Ffuf(_) => persist_ffuf(store, engagement_id, &output.stdout).await,
    }
}

async fn persist_httpx(
    store: &StateStore,
    engagement_id: &str,
    output: &str,
) -> anyhow::Result<()> {
    for value in json_lines(output) {
        let target = string_at(&value, "/url")
            .or_else(|| string_at(&value, "/input"))
            .unwrap_or("unknown");
        let asset = new_asset(engagement_id, "url", target);
        store.put_asset(&asset).await?;
        let port = value
            .get("port")
            .and_then(Value::as_u64)
            .and_then(|port| u16::try_from(port).ok())
            .or_else(|| {
                url::Url::parse(target)
                    .ok()
                    .and_then(|url| url.port_or_known_default())
            });
        if let Some(port) = port {
            store
                .put_service(&Service {
                    id: Uuid::new_v4().to_string(),
                    engagement_id: engagement_id.to_string(),
                    asset_id: asset.id,
                    transport: "tcp".to_string(),
                    port,
                    name: value
                        .get("scheme")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    version: value
                        .get("webserver")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                })
                .await?;
        }
    }
    Ok(())
}

async fn persist_nuclei(
    store: &StateStore,
    engagement_id: &str,
    output: &str,
) -> anyhow::Result<()> {
    for value in json_lines(output) {
        let title = string_at(&value, "/info/name")
            .or_else(|| string_at(&value, "/template-id"))
            .unwrap_or("Nuclei finding");
        let description = string_at(&value, "/matched-at")
            .or_else(|| string_at(&value, "/host"))
            .unwrap_or("Nuclei produced a match");
        store
            .put_finding(&Finding {
                id: Uuid::new_v4().to_string(),
                engagement_id: engagement_id.to_string(),
                asset_id: None,
                title: title.to_string(),
                severity: severity(string_at(&value, "/info/severity")),
                description: description.to_string(),
                remediation: None,
            })
            .await?;
    }
    Ok(())
}

async fn persist_ffuf(store: &StateStore, engagement_id: &str, output: &str) -> anyhow::Result<()> {
    for value in json_lines(output) {
        let summary = match (
            value.get("url").and_then(Value::as_str),
            value.get("status").and_then(Value::as_u64),
        ) {
            (Some(url), Some(status)) => format!("ffuf discovered {url} with HTTP {status}"),
            (Some(url), None) => format!("ffuf discovered {url}"),
            _ => "ffuf produced a result".to_string(),
        };
        store
            .put_evidence(&Evidence {
                id: Uuid::new_v4().to_string(),
                engagement_id: engagement_id.to_string(),
                finding_id: None,
                artifact_id: None,
                summary,
                captured_at: crate::gateway_state::unix_timestamp(),
            })
            .await?;
    }
    Ok(())
}

async fn persist_nmap(store: &StateStore, engagement_id: &str, output: &str) -> anyhow::Result<()> {
    let mut reader = Reader::from_str(output);
    reader.config_mut().trim_text(true);
    let mut asset = None;
    let mut port = None;
    loop {
        match reader.read_event()? {
            Event::Start(element) | Event::Empty(element)
                if element.name().as_ref() == b"address" =>
            {
                if let Some(address) = attribute(&reader, &element, b"addr") {
                    let value = new_asset(engagement_id, "host", &address);
                    store.put_asset(&value).await?;
                    asset = Some(value);
                }
            }
            Event::Start(element) if element.name().as_ref() == b"port" => {
                port = attribute(&reader, &element, b"portid").and_then(|value| value.parse().ok());
            }
            Event::Empty(element) if element.name().as_ref() == b"service" => {
                if let (Some(asset), Some(port)) = (&asset, port) {
                    store
                        .put_service(&Service {
                            id: Uuid::new_v4().to_string(),
                            engagement_id: engagement_id.to_string(),
                            asset_id: asset.id.clone(),
                            transport: "tcp".to_string(),
                            port,
                            name: attribute(&reader, &element, b"name"),
                            version: attribute(&reader, &element, b"version"),
                        })
                        .await?;
                }
            }
            Event::End(element) if element.name().as_ref() == b"port" => port = None,
            Event::End(element) if element.name().as_ref() == b"host" => asset = None,
            Event::Eof => break,
            _ => {}
        }
    }
    Ok(())
}

fn json_lines(output: &str) -> impl Iterator<Item = Value> + '_ {
    output
        .lines()
        .filter_map(|line| serde_json::from_str(line).ok())
}

fn new_asset(engagement_id: &str, kind: &str, value: &str) -> Asset {
    Asset {
        id: Uuid::new_v4().to_string(),
        engagement_id: engagement_id.to_string(),
        kind: kind.to_string(),
        value: value.to_string(),
        discovered_at: crate::gateway_state::unix_timestamp(),
    }
}

fn string_at<'a>(value: &'a Value, pointer: &str) -> Option<&'a str> {
    value.pointer(pointer).and_then(Value::as_str)
}

fn severity(value: Option<&str>) -> FindingSeverity {
    match value {
        Some("critical") => FindingSeverity::Critical,
        Some("high") => FindingSeverity::High,
        Some("medium") => FindingSeverity::Medium,
        Some("low") => FindingSeverity::Low,
        Some("info") | Some(_) | None => FindingSeverity::Info,
    }
}

fn attribute(
    reader: &Reader<&[u8]>,
    element: &quick_xml::events::BytesStart<'_>,
    name: &[u8],
) -> Option<String> {
    element.attributes().flatten().find_map(|attribute| {
        (attribute.key.as_ref() == name)
            .then(|| {
                attribute
                    .decoded_and_normalized_value(
                        quick_xml::XmlVersion::Implicit1_0,
                        reader.decoder(),
                    )
                    .ok()
            })
            .flatten()
            .map(std::borrow::Cow::into_owned)
    })
}

#[cfg(test)]
#[path = "tool_results_tests.rs"]
mod tests;
