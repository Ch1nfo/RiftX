import type { ReactNode } from "react";

import type { DemoRunStatus } from "../data/demo";
import { useLocale } from "../i18n";
import { PixelIcon, type PixelIconName } from "./PixelIcon";

export function StatusPill({ status }: { status: DemoRunStatus }) {
  const { label } = useLocale();
  return (
    <span className={`status-pill status-${status}`}>
      <span className="status-pip" aria-hidden="true" />
      {label(status)}
    </span>
  );
}

export function PanelHeading({
  title,
  detail,
  icon,
  action,
}: {
  title: string;
  detail?: string;
  icon: PixelIconName;
  action?: ReactNode;
}) {
  return (
    <header className="panel-heading">
      <div className="panel-heading-main">
        <PixelIcon name={icon} />
        <div>
          <h3>{title}</h3>
          {detail ? <p>{detail}</p> : null}
        </div>
      </div>
      {action ? <div className="panel-heading-action">{action}</div> : null}
    </header>
  );
}

export function DemoStamp({ compact = false }: { compact?: boolean }) {
  return (
    <span className={`demo-stamp${compact ? " demo-stamp-compact" : ""}`}>
      <PixelIcon name="shield" />
      DEMO / SANITIZED
      {compact ? <small className="demo-stamp-local">LOCAL ONLY</small> : null}
    </span>
  );
}

export function EmptyDemoState({
  icon,
  title,
  children,
}: {
  icon: PixelIconName;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-demo-state">
      <PixelIcon name={icon} />
      <strong>{title}</strong>
      <p>{children}</p>
    </div>
  );
}
