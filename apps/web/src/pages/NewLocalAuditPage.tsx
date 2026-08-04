import { FolderSearch, Loader2, ScanSearch, ShieldCheck } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ErrorState } from "../components/ErrorState";
import { PixelIcon } from "../components/PixelIcon";
import { useCreateLocalAudit } from "../hooks/queries";
import { useI18n } from "../i18n";

export function NewLocalAuditPage() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const createAudit = useCreateLocalAudit();
  const [sourcePath, setSourcePath] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const job = await createAudit.mutateAsync({ source_path: sourcePath.trim() });
      navigate(`/audits/${job.audit_id}`);
    } catch {
      // The mutation error is rendered below the field without exposing the path.
    }
  }

  return (
    <div className="page-stack">
      <section className="section-heading split-heading">
        <div>
          <h2>{t("Audit a local folder")}</h2>
          <p>
            {t("RiftX reads source files on this machine, scans a sealed copy, and reports security findings without executing project code.")}
          </p>
        </div>
        <span className="mono-chip">
          <ShieldCheck size={14} /> {t("Read-only static analysis")}
        </span>
      </section>

      <section className="panel local-audit-launch">
        <form onSubmit={(event) => void submit(event)}>
          <div className="local-audit-form-copy">
            <FolderSearch size={24} />
            <div>
              <h3>{t("Choose the folder on the RiftX machine")}</h3>
              <p>
                {t("Enter an absolute folder path inside an allowed Audit source root. The path is used only by the local Control Plane and is not returned in Audit results.")}
              </p>
            </div>
          </div>

          <label className="field local-audit-path-field">
            <span>{t("Local folder path")}</span>
            <input
              required
              autoFocus
              aria-label={t("Local folder path")}
              value={sourcePath}
              onChange={(event) => setSourcePath(event.target.value)}
              placeholder="/Users/name/project"
              autoComplete="off"
              spellCheck={false}
            />
            <small>{t("The folder must exist on the same machine that runs RiftX.")}</small>
          </label>

          {createAudit.error ? <ErrorState error={createAudit.error} /> : null}

          <div className="local-audit-submit-row">
            <Link className="secondary-button" to="/">
              {t("Back to dashboard")}
            </Link>
            <button
              type="submit"
              className="primary-button"
              disabled={createAudit.isPending || !sourcePath.trim()}
            >
              {createAudit.isPending ? (
                <Loader2 className="spin" size={17} />
              ) : (
                <ScanSearch size={17} />
              )}
              {t(createAudit.isPending ? "Starting audit" : "Start local audit")}
            </button>
          </div>
        </form>

        <aside className="local-audit-boundary" aria-label={t("Audit boundary")}>
          <h3>{t("What this audit does")}</h3>
          <ul>
            <BoundaryItem
              icon="file"
              title={t("Reads selected source files")}
              body={t("Files are inventoried with bounded size and count limits.")}
            />
            <BoundaryItem
              icon="lock"
              title={t("Scans a sealed local snapshot")}
              body={t("Findings are produced from RiftX-owned snapshot bytes, not a changing working folder.")}
            />
            <BoundaryItem
              icon="shield"
              title={t("Runs built-in static rules only")}
              body={t("No builds, tests, package managers, Git helpers, plugins, containers, or remote machines are used.")}
            />
          </ul>
        </aside>
      </section>
    </div>
  );
}

function BoundaryItem({
  icon,
  title,
  body,
}: {
  icon: "file" | "lock" | "shield";
  title: string;
  body: string;
}) {
  return (
    <li>
      <PixelIcon name={icon} />
      <span>
        <strong>{title}</strong>
        <small>{body}</small>
      </span>
    </li>
  );
}
