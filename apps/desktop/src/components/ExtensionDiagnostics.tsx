import {
  AlertTriangle,
  BookOpen,
  CircleCheck,
  CircleX,
  LoaderCircle,
  RefreshCw,
  Wrench,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  bridgeError,
  getToolsSettings,
  saveToolsSettings,
  skillCatalog as loadSkillCatalog,
  skillDoctor,
  toolDoctor,
  toolInventory as loadToolInventory,
} from "../bridge";
import type {
  DesktopBridgeError,
  ExtensionDiagnostic,
  SkillCatalog,
  ToolInventory,
  ToolsSettings,
} from "../models";

interface ExtensionDiagnosticsProps {
  onError: (error: DesktopBridgeError) => void;
}

export function ToolsSettingsView({ onError }: ExtensionDiagnosticsProps) {
  const [inventory, setInventory] = useState<ToolInventory | null>(null);
  const [toolsSettings, setToolsSettings] = useState<ToolsSettings | null>(
    null,
  );
  const [directoriesText, setDirectoriesText] = useState("");
  const [loadFailed, setLoadFailed] = useState(false);
  const [checking, setChecking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    void Promise.all([loadToolInventory(), getToolsSettings()])
      .then(([loadedInventory, loadedSettings]) => {
        setInventory(loadedInventory);
        setToolsSettings(loadedSettings);
        setDirectoriesText(loadedSettings.directories.join("\n"));
      })
      .catch((cause) => {
        setLoadFailed(true);
        onError(bridgeError(cause));
      });
  }, [onError]);

  const runDoctor = async () => {
    setChecking(true);
    try {
      setInventory(await toolDoctor());
      setLoadFailed(false);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      setChecking(false);
    }
  };

  const saveDirectories = async () => {
    setSaving(true);
    setNotice(null);
    try {
      const directories = directoriesText
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.length > 0);
      const updated = await saveToolsSettings(directories);
      setToolsSettings(updated);
      setDirectoriesText(updated.directories.join("\n"));
      setInventory(await toolDoctor());
      setNotice(
        updated.daemonRestartRequired
          ? "Directories saved. Restart the externally managed daemon to apply."
          : "Directories saved. Tool inventory refreshed.",
      );
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      setSaving(false);
    }
  };

  if (!inventory || !toolsSettings) {
    return (
      <SettingsLoading
        failed={loadFailed}
        label={loadFailed ? "Tools unavailable" : "Loading tools"}
      />
    );
  }

  return (
    <div className="extension-settings">
      <label className="tools-directories">
        <span>Tools directories</span>
        <textarea
          value={directoriesText}
          onChange={(event) => {
            setDirectoriesText(event.target.value);
            setNotice(null);
          }}
          rows={4}
          spellCheck={false}
          disabled={saving || checking}
          placeholder={"One path per line\nLeave empty to use the platform default"}
        />
        <span className="tools-directories-hint">
          Empty list uses the platform default Tools Directory. Changes write to
          riftx.toml and reload the local daemon.
        </span>
      </label>
      <div className="settings-actions">
        <button
          className="primary-button"
          disabled={saving || checking}
          type="button"
          onClick={() => void saveDirectories()}
        >
          {saving && <LoaderCircle className="spin" size={14} />}
          Save directories
        </button>
      </div>
      {notice && <p className="settings-notice">{notice}</p>}
      <ExtensionSummary
        count={inventory.tools.length}
        countLabel="Tools"
        locations={inventory.roots}
        locationLabel="Resolved roots"
        pathCount={inventory.pathEntries.length}
        snapshot={inventory.snapshotSha256}
      />
      <div className="extension-actions">
        <span>
          Doctor performs a fresh scan without changing active snapshots.
        </span>
        <button
          className="secondary-button"
          disabled={checking || saving}
          type="button"
          onClick={() => void runDoctor()}
        >
          <RefreshCw className={checking ? "spin" : undefined} size={14} />
          {checking ? "Checking" : "Run doctor"}
        </button>
      </div>
      <DiagnosticList diagnostics={inventory.diagnostics} />
      <div className="extension-list">
        {inventory.tools.map((tool) => (
          <div className="extension-row" key={`${tool.name}:${tool.path}`}>
            <Wrench size={16} />
            <div>
              <strong>{tool.name}</strong>
              <span title={tool.path}>{tool.path}</span>
              <code title={tool.sha256}>{tool.sha256.slice(0, 16)}</code>
            </div>
            <div className="extension-tags">
              {tool.metadata?.risk && (
                <span className={`risk-${tool.metadata.risk}`}>
                  {tool.metadata.risk}
                </span>
              )}
              {tool.metadata ? <span>managed</span> : <span>binary</span>}
              {tool.shadowedBy && <span className="tag-warning">shadowed</span>}
            </div>
          </div>
        ))}
        {inventory.tools.length === 0 && (
          <div className="extension-empty">
            <Wrench size={18} />
            <span>
              No tools discovered. Add a Tools Directory above, then run doctor.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export function SkillsSettingsView({ onError }: ExtensionDiagnosticsProps) {
  const [catalog, setCatalog] = useState<SkillCatalog | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    void loadSkillCatalog()
      .then(setCatalog)
      .catch((cause) => {
        setLoadFailed(true);
        onError(bridgeError(cause));
      });
  }, [onError]);

  const runDoctor = async () => {
    setChecking(true);
    try {
      setCatalog(await skillDoctor());
      setLoadFailed(false);
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      setChecking(false);
    }
  };

  if (!catalog) {
    return (
      <SettingsLoading
        failed={loadFailed}
        label={loadFailed ? "Skills unavailable" : "Loading skills"}
      />
    );
  }

  return (
    <div className="extension-settings">
      <ExtensionSummary
        count={catalog.skills.length}
        countLabel="Skills"
        locations={[catalog.root]}
        locationLabel="Directory"
        snapshot={catalog.snapshotSha256}
      />
      <div className="extension-actions">
        <span>Doctor force-reloads the Skills Directory without changing active snapshots.</span>
        <button
          className="secondary-button"
          disabled={checking}
          type="button"
          onClick={() => void runDoctor()}
        >
          <RefreshCw className={checking ? "spin" : undefined} size={14} />
          {checking ? "Checking" : "Run doctor"}
        </button>
      </div>
      <DiagnosticList diagnostics={catalog.diagnostics} />
      <div className="extension-list">
        {catalog.skills.map((skill) => (
          <div className="extension-row skill-row" key={skill.path}>
            <BookOpen size={16} />
            <div>
              <strong>{skill.name}</strong>
              <span>{skill.description}</span>
              <code title={skill.path}>{skill.path}</code>
            </div>
            <div className="extension-tags">
              <span>{skill.source === "builtIn" ? "built-in" : "user"}</span>
              <span className={skill.enabled ? "tag-enabled" : "tag-warning"}>
                {skill.enabled ? "enabled" : "disabled"}
              </span>
            </div>
          </div>
        ))}
        {catalog.skills.length === 0 && (
          <div className="extension-empty">
            <BookOpen size={18} />
            <span>No skills discovered in the configured Skills Directory.</span>
          </div>
        )}
      </div>
    </div>
  );
}

interface ExtensionSummaryProps {
  count: number;
  countLabel: string;
  locations: string[];
  locationLabel: string;
  pathCount?: number;
  snapshot: string;
}

function ExtensionSummary({
  count,
  countLabel,
  locations,
  locationLabel,
  pathCount,
  snapshot,
}: ExtensionSummaryProps) {
  return (
    <dl className="extension-summary">
      <div>
        <dt>{countLabel}</dt>
        <dd>{count}</dd>
      </div>
      {pathCount !== undefined && (
        <div>
          <dt>PATH entries</dt>
          <dd>{pathCount}</dd>
        </div>
      )}
      <div className="extension-location">
        <dt>{locationLabel}</dt>
        <dd>
          {locations.map((location) => (
            <code key={location}>{location}</code>
          ))}
        </dd>
      </div>
      <div className="extension-location">
        <dt>Startup snapshot</dt>
        <dd>
          <code title={snapshot}>{snapshot}</code>
        </dd>
      </div>
    </dl>
  );
}

function DiagnosticList({
  diagnostics,
}: {
  diagnostics: ExtensionDiagnostic[];
}) {
  if (diagnostics.length === 0) {
    return (
      <div className="diagnostic-ok">
        <CircleCheck size={15} />
        <span>No diagnostics</span>
      </div>
    );
  }

  return (
    <div className="diagnostic-list">
      {diagnostics.map((diagnostic, index) => (
        <div
          className={`diagnostic-row ${diagnostic.level}`}
          key={`${diagnostic.code}:${diagnostic.path ?? ""}:${index}`}
        >
          {diagnostic.level === "error" ? (
            <CircleX size={15} />
          ) : (
            <AlertTriangle size={15} />
          )}
          <div>
            <strong>{diagnostic.code.replace(/_/g, " ")}</strong>
            <span>{diagnostic.message}</span>
            {diagnostic.path && <code>{diagnostic.path}</code>}
          </div>
        </div>
      ))}
    </div>
  );
}

function SettingsLoading({
  failed = false,
  label,
}: {
  failed?: boolean;
  label: string;
}) {
  return (
    <div className="settings-loading">
      {failed ? (
        <CircleX size={19} />
      ) : (
        <LoaderCircle className="spin" size={19} />
      )}
      <span>{label}</span>
    </div>
  );
}
