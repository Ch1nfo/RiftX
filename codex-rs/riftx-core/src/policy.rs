use crate::ManagedPolicyConfig;
use crate::Scope;
use crate::ToolProfileConfig;
use ipnet::IpNet;
use serde::Deserialize;
use serde::Serialize;
use sha2::Digest;
use sha2::Sha256;
use std::collections::BTreeSet;
use thiserror::Error;

const BUILT_IN_DENIED_CIDRS: [&str; 7] = [
    "0.0.0.0/8",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "224.0.0.0/4",
    "::/128",
    "::1/128",
    "fe80::/10",
];

#[derive(Debug, Error)]
pub enum PolicyError {
    #[error("invalid built-in deny CIDR: {0}")]
    InvalidBuiltInCidr(#[from] ipnet::AddrParseError),
    #[error("failed to encode effective policy: {0}")]
    Encode(#[from] serde_json::Error),
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalGrant {
    pub tools: BTreeSet<String>,
    pub cidrs: BTreeSet<IpNet>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EffectivePolicy {
    pub allowed_tools: BTreeSet<String>,
    pub allowed_cidrs: BTreeSet<IpNet>,
    pub allowed_domains: BTreeSet<String>,
    pub allowed_ports: BTreeSet<u16>,
    pub denied_cidrs: BTreeSet<IpNet>,
    pub denied_domains: BTreeSet<String>,
    pub revision: String,
}

impl EffectivePolicy {
    pub fn resolve(
        managed: &ManagedPolicyConfig,
        engagement_scope: &Scope,
        profile: &ToolProfileConfig,
        approval: Option<&ApprovalGrant>,
    ) -> Result<Self, PolicyError> {
        let managed_tools = managed
            .allowed_tools
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let profile_tools = profile
            .allowed_tools
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut allowed_tools = managed_tools
            .intersection(&profile_tools)
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut allowed_cidrs = intersect_cidrs(&engagement_scope.cidrs, &profile.scope.cidrs);

        if let Some(approval) = approval {
            allowed_tools = allowed_tools
                .intersection(&approval.tools)
                .cloned()
                .collect();
            allowed_cidrs = allowed_cidrs
                .intersection(&approval.cidrs)
                .copied()
                .collect();
        }

        let engagement_domains = engagement_scope
            .domains
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let profile_domains = profile
            .scope
            .domains
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let allowed_domains = if profile_domains.contains("*") {
            engagement_domains
        } else {
            engagement_domains
                .intersection(&profile_domains)
                .cloned()
                .collect()
        };
        let engagement_ports = engagement_scope
            .ports
            .iter()
            .copied()
            .collect::<BTreeSet<_>>();
        let profile_ports = profile.scope.ports.iter().copied().collect::<BTreeSet<_>>();
        let allowed_ports = if profile_ports.is_empty() {
            engagement_ports
        } else {
            engagement_ports
                .intersection(&profile_ports)
                .copied()
                .collect()
        };
        let denied_cidrs = BUILT_IN_DENIED_CIDRS
            .iter()
            .map(|cidr| cidr.parse())
            .collect::<Result<BTreeSet<IpNet>, _>>()?
            .into_iter()
            .chain(managed.denied_cidrs.iter().copied())
            .collect();
        let denied_domains = managed.denied_domains.iter().cloned().collect();
        let mut policy = Self {
            allowed_tools,
            allowed_cidrs,
            allowed_domains,
            allowed_ports,
            denied_cidrs,
            denied_domains,
            revision: String::new(),
        };
        let encoded = serde_json::to_vec(&policy)?;
        policy.revision = format!("{:x}", Sha256::digest(encoded));
        Ok(policy)
    }

    pub fn allows_tool(&self, tool: &str) -> bool {
        self.allowed_tools.contains(tool)
    }
}

fn intersect_cidrs(left: &[IpNet], right: &[IpNet]) -> BTreeSet<IpNet> {
    left.iter()
        .flat_map(|left_net| {
            right.iter().filter_map(move |right_net| {
                if left_net.contains(&right_net.network()) {
                    Some(*right_net)
                } else if right_net.contains(&left_net.network()) {
                    Some(*left_net)
                } else {
                    None
                }
            })
        })
        .collect()
}
