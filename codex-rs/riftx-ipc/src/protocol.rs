use serde::Deserialize;
use serde::Serialize;

pub const IPC_PROTOCOL_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DaemonInfo {
    pub protocol_version: u32,
    pub daemon_version: String,
}

impl DaemonInfo {
    pub fn current() -> Self {
        Self {
            protocol_version: IPC_PROTOCOL_VERSION,
            daemon_version: env!("CARGO_PKG_VERSION").to_string(),
        }
    }
}
