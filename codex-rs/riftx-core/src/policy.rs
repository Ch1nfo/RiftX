use crate::AuthorizationError;
use crate::AuthorizationScope;
use crate::AuthorizationWindow;
use crate::CredentialGrant;
use crate::EnvironmentClass;
use crate::ExecutionMode;
use crate::IdentitySelector;
use crate::ManagedPolicyConfig;
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
    #[error(transparent)]
    Authorization(#[from] AuthorizationError),
    #[error("target {target} is outside the effective scope: {reason}")]
    TargetOutsideScope { target: String, reason: String },
    #[error("an engagement may define at most 128 credential grants")]
    TooManyCredentialGrants,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct ApprovalGrant {
    pub capabilities: BTreeSet<String>,
    pub cidrs: BTreeSet<IpNet>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EffectivePolicy {
    pub execution_mode: ExecutionMode,
    pub environment: EnvironmentClass,
    pub authorization_window: AuthorizationWindow,
    pub allowed_identities: Vec<IdentitySelector>,
    pub allowed_capabilities: BTreeSet<String>,
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
        mode: ExecutionMode,
        authorization: &AuthorizationScope,
        approval: Option<&ApprovalGrant>,
    ) -> Result<Self, PolicyError> {
        authorization.validate_for(mode)?;
        let managed_capabilities = managed
            .allowed_capabilities
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let requested_capabilities = authorization
            .capabilities
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut allowed_capabilities = managed_capabilities
            .intersection(&requested_capabilities)
            .cloned()
            .collect::<BTreeSet<_>>();
        let mut allowed_cidrs = authorization
            .network
            .cidrs
            .iter()
            .copied()
            .collect::<BTreeSet<_>>();

        if let Some(approval) = approval {
            allowed_capabilities = allowed_capabilities
                .intersection(&approval.capabilities)
                .cloned()
                .collect();
            allowed_cidrs = allowed_cidrs
                .intersection(&approval.cidrs)
                .copied()
                .collect();
        }

        let allowed_domains = authorization
            .network
            .domains
            .iter()
            .cloned()
            .collect::<BTreeSet<_>>();
        let allowed_ports = authorization
            .network
            .ports
            .iter()
            .copied()
            .collect::<BTreeSet<_>>();
        let denied_cidrs = BUILT_IN_DENIED_CIDRS
            .iter()
            .map(|cidr| cidr.parse())
            .collect::<Result<BTreeSet<IpNet>, _>>()?
            .into_iter()
            .chain(managed.denied_cidrs.iter().copied())
            .collect();
        let denied_domains = managed.denied_domains.iter().cloned().collect();
        let mut allowed_identities = authorization.identities.clone();
        allowed_identities.sort();
        allowed_identities.dedup();
        let mut policy = Self {
            execution_mode: mode,
            environment: authorization.environment,
            authorization_window: authorization.window.clone(),
            allowed_identities,
            allowed_capabilities,
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

    pub fn allows_capability(&self, capability: &str) -> bool {
        self.allowed_capabilities.contains(capability)
    }

    pub fn bind_credential_grants(
        mut self,
        grants: &[CredentialGrant],
    ) -> Result<Self, PolicyError> {
        if grants.is_empty() {
            return Ok(self);
        }
        if grants.len() > 128 {
            return Err(PolicyError::TooManyCredentialGrants);
        }
        let mut grants = grants.to_vec();
        grants.sort_by(|left, right| left.id.cmp(&right.id));
        let encoded = serde_json::to_vec(&(&self, grants))?;
        self.revision = format!("{:x}", Sha256::digest(encoded));
        Ok(self)
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

#[cfg(test)]
#[path = "policy_tests.rs"]
mod tests;
