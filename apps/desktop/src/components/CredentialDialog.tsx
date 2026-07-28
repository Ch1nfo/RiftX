import {
  Ban,
  Eye,
  EyeOff,
  KeyRound,
  LoaderCircle,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  bridgeError,
  createAssessmentCredential,
  createCredentialGrant,
  deleteAssessmentCredential,
  revokeCredentialGrant,
} from "../bridge";
import type {
  CredentialGrant,
  CredentialKind,
  CredentialReference,
  DesktopBridgeError,
  Engagement,
} from "../models";

interface CredentialDialogProps {
  open: boolean;
  engagement: Engagement | null;
  references: CredentialReference[];
  grants: CredentialGrant[];
  mutable: boolean;
  onChanged: () => void;
  onClose: () => void;
  onError: (error: DesktopBridgeError) => void;
}

type CredentialTab = "references" | "grants";

const KINDS: { value: CredentialKind; label: string }[] = [
  { value: "password", label: "Password" },
  { value: "apiToken", label: "API token" },
  { value: "sshKey", label: "SSH key" },
  { value: "certificate", label: "Certificate" },
  { value: "other", label: "Other" },
];

export function CredentialDialog({
  open,
  engagement,
  references,
  grants,
  mutable,
  onChanged,
  onClose,
  onError,
}: CredentialDialogProps) {
  const [tab, setTab] = useState<CredentialTab>("references");
  const [busy, setBusy] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState<CredentialKind>("password");
  const [username, setUsername] = useState("");
  const [domain, setDomain] = useState("");
  const [secret, setSecret] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [cidrs, setCidrs] = useState("");
  const [domains, setDomains] = useState("");
  const [ports, setPorts] = useState("");
  const [capabilities, setCapabilities] = useState<string[]>([]);
  const [maxUses, setMaxUses] = useState(1);
  const [maxFailures, setMaxFailures] = useState(3);
  const [expiresAt, setExpiresAt] = useState("");

  const grantHistory = useMemo(
    () => new Set(grants.map((grant) => grant.credentialId)),
    [grants],
  );
  const labels = useMemo(
    () => new Map(references.map((reference) => [reference.id, reference.label])),
    [references],
  );

  useEffect(() => {
    if (!open || !engagement) {
      setBusy(false);
      setShowSecret(false);
      setSecret("");
      return;
    }
    setCredentialId((current) =>
      references.some((reference) => reference.id === current)
        ? current
        : (references[0]?.id ?? ""),
    );
    setCidrs(engagement.authorization.network.cidrs.join("\n"));
    setDomains(engagement.authorization.network.domains.join("\n"));
    setPorts(engagement.authorization.network.ports.join(", "));
    setCapabilities(engagement.authorization.capabilities);
    setExpiresAt(
      toLocalDateTime(
        engagement.authorization.window.expiresAt ??
          Math.floor(Date.now() / 1000) + 3600,
      ),
    );
  }, [engagement?.id, open, references]);

  if (!open || !engagement) {
    return null;
  }

  const mutate = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await operation();
      onChanged();
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      setBusy(false);
    }
  };

  const addReference = (event: FormEvent) => {
    event.preventDefault();
    if (!label.trim() || !secret || !mutable || busy) {
      return;
    }
    void mutate(async () => {
      await createAssessmentCredential({
        engagementId: engagement.id,
        label: label.trim(),
        kind,
        username: username.trim() || null,
        domain: domain.trim() || null,
        secret,
      });
      setLabel("");
      setUsername("");
      setDomain("");
      setSecret("");
      setShowSecret(false);
    });
  };

  const addGrant = (event: FormEvent) => {
    event.preventDefault();
    const expiry = Math.floor(new Date(expiresAt).getTime() / 1000);
    const parsedPorts = splitValues(ports).map(Number);
    if (
      !credentialId ||
      capabilities.length === 0 ||
      !Number.isFinite(expiry) ||
      !mutable ||
      busy
    ) {
      return;
    }
    if (
      parsedPorts.some(
        (port) => !Number.isInteger(port) || port < 1 || port > 65_535,
      )
    ) {
      onError({
        code: "invalid_port",
        message: "Credential grant ports must be integers from 1 to 65535.",
      });
      return;
    }
    void mutate(() =>
      createCredentialGrant({
        engagementId: engagement.id,
        credentialId,
        cidrs: splitValues(cidrs),
        domains: splitValues(domains),
        ports: parsedPorts,
        capabilities,
        maxUses,
        maxFailuresPerIdentity: maxFailures,
        startsAt: null,
        expiresAt: expiry,
      }),
    );
  };

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) {
          onClose();
        }
      }}
    >
      <section
        className="credential-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="credential-dialog-title"
      >
        <header className="dialog-heading">
          <div>
            <span>{engagement.name}</span>
            <h2 id="credential-dialog-title">Credential grants</h2>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close credentials"
            title="Close"
            onClick={onClose}
            disabled={busy}
          >
            <X size={17} />
          </button>
        </header>

        <div className="settings-tabs" role="tablist" aria-label="Credentials">
          {(["references", "grants"] as CredentialTab[]).map((value) => (
            <button
              type="button"
              role="tab"
              aria-selected={tab === value}
              className={tab === value ? "active" : undefined}
              onClick={() => setTab(value)}
              key={value}
            >
              {value}
            </button>
          ))}
        </div>

        {!mutable && (
          <div className="credential-lock">
            <Ban size={15} />
            <span>Credential changes are locked for the current task state.</span>
          </div>
        )}

        {tab === "references" && (
          <div className="credential-content">
            <div className="credential-list">
              {references.length === 0 && (
                <div className="credential-empty">
                  <KeyRound size={18} />
                  <span>No credential references</span>
                </div>
              )}
              {references.map((reference) => (
                <article className="credential-row" key={reference.id}>
                  <div>
                    <strong>{reference.label}</strong>
                    <code>credential://{reference.id}</code>
                    <span>
                      {reference.kind}
                      {reference.username ? ` · ${reference.username}` : ""}
                      {reference.domain ? ` · ${reference.domain}` : ""}
                      {reference.configured
                        ? " · system keyring"
                        : " · secret removed"}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="icon-button"
                    aria-label={`Delete ${reference.label}`}
                    title={
                      grantHistory.has(reference.id)
                        ? "Remove secret and revoke grants"
                        : "Delete credential and secret"
                    }
                    disabled={busy || !mutable || !reference.configured}
                    onClick={() =>
                      void mutate(() =>
                        deleteAssessmentCredential(
                          engagement.id,
                          reference.id,
                        ),
                      )
                    }
                  >
                    <Trash2 size={14} />
                  </button>
                </article>
              ))}
            </div>

            <form className="credential-form" onSubmit={addReference}>
              <header>
                <strong>Add reference</strong>
                <Plus size={15} />
              </header>
              <div className="credential-form-grid">
                <label>
                  <span>Label</span>
                  <input
                    value={label}
                    maxLength={128}
                    disabled={!mutable || busy}
                    onChange={(event) => setLabel(event.target.value)}
                  />
                </label>
                <label>
                  <span>Kind</span>
                  <select
                    value={kind}
                    disabled={!mutable || busy}
                    onChange={(event) =>
                      setKind(event.target.value as CredentialKind)
                    }
                  >
                    {KINDS.map((option) => (
                      <option value={option.value} key={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Username</span>
                  <input
                    value={username}
                    maxLength={256}
                    disabled={!mutable || busy}
                    onChange={(event) => setUsername(event.target.value)}
                  />
                </label>
                <label>
                  <span>Domain</span>
                  <input
                    value={domain}
                    maxLength={256}
                    disabled={!mutable || busy}
                    onChange={(event) => setDomain(event.target.value)}
                  />
                </label>
                <label className="credential-secret-field">
                  <span>Secret</span>
                  <div>
                    <input
                      type={showSecret ? "text" : "password"}
                      value={secret}
                      autoComplete="new-password"
                      disabled={!mutable || busy}
                      onChange={(event) => setSecret(event.target.value)}
                    />
                    <button
                      type="button"
                      className="icon-button"
                      aria-label={showSecret ? "Hide secret" : "Show secret"}
                      title={showSecret ? "Hide secret" : "Show secret"}
                      onClick={() => setShowSecret((current) => !current)}
                    >
                      {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
                    </button>
                  </div>
                </label>
              </div>
              <footer>
                <button
                  type="submit"
                  className="primary-button"
                  disabled={!mutable || busy || !label.trim() || !secret}
                >
                  {busy && <LoaderCircle className="spin" size={14} />}
                  Add credential
                </button>
              </footer>
            </form>
          </div>
        )}

        {tab === "grants" && (
          <div className="credential-content">
            <div className="credential-list">
              {grants.length === 0 && (
                <div className="credential-empty">
                  <KeyRound size={18} />
                  <span>No credential grants</span>
                </div>
              )}
              {grants.map((grant) => (
                <article
                  className={`credential-row grant-row ${
                    grant.revokedAt ? "revoked" : ""
                  }`}
                  key={grant.id}
                >
                  <div>
                    <strong>
                      {labels.get(grant.credentialId) ?? grant.credentialId}
                    </strong>
                    <code>
                      {[...grant.allowedTargets.cidrs, ...grant.allowedTargets.domains]
                        .join(", ")}
                    </code>
                    <span>
                      {grant.allowedCapabilities.join(", ")} · max{" "}
                      {grant.maxUses} · expires{" "}
                      {new Date(grant.expiresAt * 1000).toLocaleString()}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="icon-button"
                    aria-label="Revoke grant"
                    title={grant.revokedAt ? "Revoked" : "Revoke grant"}
                    disabled={busy || !mutable || grant.revokedAt !== null}
                    onClick={() =>
                      void mutate(() =>
                        revokeCredentialGrant(engagement.id, grant.id),
                      )
                    }
                  >
                    <Ban size={14} />
                  </button>
                </article>
              ))}
            </div>

            <form className="credential-form" onSubmit={addGrant}>
              <header>
                <strong>Add grant</strong>
                <Plus size={15} />
              </header>
              <div className="credential-form-grid">
                <label className="wide">
                  <span>Credential</span>
                  <select
                    value={credentialId}
                    disabled={!mutable || busy || references.length === 0}
                    onChange={(event) => setCredentialId(event.target.value)}
                  >
                    {references.map((reference) => (
                      <option value={reference.id} key={reference.id}>
                        {reference.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>CIDRs</span>
                  <textarea
                    value={cidrs}
                    disabled={!mutable || busy}
                    onChange={(event) => setCidrs(event.target.value)}
                  />
                </label>
                <label>
                  <span>Domains</span>
                  <textarea
                    value={domains}
                    disabled={!mutable || busy}
                    onChange={(event) => setDomains(event.target.value)}
                  />
                </label>
                <label>
                  <span>Ports</span>
                  <input
                    value={ports}
                    disabled={!mutable || busy}
                    onChange={(event) => setPorts(event.target.value)}
                  />
                </label>
                <label>
                  <span>Expires</span>
                  <input
                    type="datetime-local"
                    value={expiresAt}
                    disabled={!mutable || busy}
                    onChange={(event) => setExpiresAt(event.target.value)}
                  />
                </label>
                <label>
                  <span>Max uses</span>
                  <input
                    type="number"
                    min={1}
                    value={maxUses}
                    disabled={!mutable || busy}
                    onChange={(event) => setMaxUses(Number(event.target.value))}
                  />
                </label>
                <label>
                  <span>Failures per identity</span>
                  <input
                    type="number"
                    min={1}
                    value={maxFailures}
                    disabled={!mutable || busy}
                    onChange={(event) =>
                      setMaxFailures(Number(event.target.value))
                    }
                  />
                </label>
                <fieldset className="credential-capabilities wide">
                  <legend>Capabilities</legend>
                  {engagement.authorization.capabilities.map((capability) => (
                    <label key={capability}>
                      <input
                        type="checkbox"
                        checked={capabilities.includes(capability)}
                        disabled={!mutable || busy}
                        onChange={(event) =>
                          setCapabilities((current) =>
                            event.target.checked
                              ? [...current, capability]
                              : current.filter((value) => value !== capability),
                          )
                        }
                      />
                      <span>{capability}</span>
                    </label>
                  ))}
                </fieldset>
              </div>
              <footer>
                <button
                  type="submit"
                  className="primary-button"
                  disabled={
                    !mutable ||
                    busy ||
                    !credentialId ||
                    capabilities.length === 0
                  }
                >
                  {busy && <LoaderCircle className="spin" size={14} />}
                  Add grant
                </button>
              </footer>
            </form>
          </div>
        )}
      </section>
    </div>
  );
}

function splitValues(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function toLocalDateTime(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}
