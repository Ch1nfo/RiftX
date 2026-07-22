use super::*;
use pretty_assertions::assert_eq;
use tempfile::TempDir;
use tokio::io::AsyncReadExt;
use tokio::io::AsyncWriteExt;
use tokio::net::UnixListener;

#[tokio::test]
async fn create_sandbox_uses_unix_socket_contract() {
    let temp = TempDir::new().expect("temp dir");
    let socket = temp.path().join("managerd.sock");
    let listener = UnixListener::bind(&socket).expect("bind Unix socket");
    let server = tokio::spawn(async move {
        let (mut stream, _) = listener.accept().await.expect("accept request");
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        loop {
            let count = stream.read(&mut buffer).await.expect("read request");
            request.extend_from_slice(&buffer[..count]);
            let text = String::from_utf8_lossy(&request);
            let Some(header_end) = text.find("\r\n\r\n") else {
                continue;
            };
            let content_length = text[..header_end]
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|value| value.parse::<usize>().ok())
                })
                .expect("content length");
            if request.len() >= header_end + 4 + content_length {
                break;
            }
        }
        let text = String::from_utf8(request).expect("UTF-8 request");
        assert!(text.starts_with("POST /v1/sandboxes HTTP/1.1\r\n"));
        let body = text.split_once("\r\n\r\n").expect("HTTP body").1;
        let request: CreateSandboxRequest = serde_json::from_str(body).expect("request JSON");
        assert_eq!(request.engagement_id, "eng-1");
        assert_eq!(request.scope.cidrs, vec!["10.0.0.0/24"]);

        let response = r#"{"id":"sb-1","engagementId":"eng-1","status":"ready","environmentId":"env-1","execServerUrl":"ws://10.0.0.2:9800","bootstrapToken":null,"policyRevision":"rev-1","createdAt":1}"#;
        stream
            .write_all(
                format!(
                    "HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
                    response.len()
                )
                .as_bytes(),
            )
            .await
            .expect("write response");
    });

    let client = ManagerClient::new(&socket, Duration::from_secs(2)).expect("client");
    let sandbox = client
        .create_sandbox(&CreateSandboxRequest {
            engagement_id: "eng-1".to_string(),
            image: "riftx/sandbox:test".to_string(),
            profile: "recon".to_string(),
            policy_revision: "rev-1".to_string(),
            resources: SandboxResources {
                cpu_limit: 2,
                memory_mib: 1024,
                pids_limit: 256,
            },
            scope: SandboxScope {
                cidrs: vec!["10.0.0.0/24".to_string()],
                domains: Vec::new(),
                ports: vec![80],
                denied_cidrs: Vec::new(),
                denied_domains: Vec::new(),
            },
        })
        .await
        .expect("create sandbox");
    assert_eq!(sandbox.id, "sb-1");
    assert_eq!(sandbox.status, SandboxStatus::Ready);
    server.await.expect("server task");
}
