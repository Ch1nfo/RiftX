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
    #[error("target {target} is outside the effective scope: {reason}")]
    TargetOutsideScope { target: String, reason: String },
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

    pub fn check_target(&self, target: &str) -> Result<(), PolicyError> {
        if let Ok(network) = target.parse::<IpNet>() {
            return self.check_network(target, network);
        }
        let (host, port) =
            target_host_and_port(target).ok_or_else(|| PolicyError::TargetOutsideScope {
                target: target.to_string(),
                reason: "target has no valid host".to_string(),
            })?;
        if let Ok(address) = host.parse::<std::net::IpAddr>() {
            self.check_network(target, IpNet::from(address))?;
        } else {
            let domain = host.trim_end_matches('.').to_ascii_lowercase();
            if self
                .denied_domains
                .iter()
                .any(|denied| domain_matches(&domain, denied))
            {
                return Err(outside_scope(target, "domain is explicitly denied"));
            }
            if !self
                .allowed_domains
                .iter()
                .any(|allowed| domain_matches(&domain, allowed))
            {
                return Err(outside_scope(target, "domain is not allowlisted"));
            }
        }
        if let Some(port) = port
            && !self.allowed_ports.is_empty()
            && !self.allowed_ports.contains(&port)
        {
            return Err(outside_scope(target, "port is not allowlisted"));
        }
        Ok(())
    }

    fn check_network(&self, target: &str, requested: IpNet) -> Result<(), PolicyError> {
        if self
            .denied_cidrs
            .iter()
            .any(|denied| networks_overlap(*denied, requested))
        {
            return Err(outside_scope(target, "network overlaps a denied CIDR"));
        }
        if !self
            .allowed_cidrs
            .iter()
            .any(|allowed| network_contains(*allowed, requested))
        {
            return Err(outside_scope(target, "network is not allowlisted"));
        }
        Ok(())
    }
}

fn target_host_and_port(target: &str) -> Option<(String, Option<u16>)> {
    if let Ok(parsed) = url::Url::parse(target)
        && parsed.host_str().is_some()
    {
        return Some((
            parsed.host_str()?.to_string(),
            parsed.port_or_known_default(),
        ));
    }
    let parsed = url::Url::parse(&format!("http://{target}")).ok()?;
    Some((parsed.host_str()?.to_string(), parsed.port()))
}

fn domain_matches(domain: &str, rule: &str) -> bool {
    let rule = rule.trim_end_matches('.').to_ascii_lowercase();
    if let Some(suffix) = rule.strip_prefix("*.") {
        domain != suffix && domain.ends_with(&format!(".{suffix}"))
    } else {
        domain == rule
    }
}

fn networks_overlap(left: IpNet, right: IpNet) -> bool {
    network_contains(left, right) || network_contains(right, left)
}

fn network_contains(allowed: IpNet, requested: IpNet) -> bool {
    allowed.addr().is_ipv4() == requested.addr().is_ipv4()
        && allowed.prefix_len() <= requested.prefix_len()
        && allowed.contains(&requested.network())
}

fn outside_scope(target: &str, reason: &str) -> PolicyError {
    PolicyError::TargetOutsideScope {
        target: target.to_string(),
        reason: reason.to_string(),
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

#[cfg(test)]
#[path = "policy_tests.rs"]
mod tests;
