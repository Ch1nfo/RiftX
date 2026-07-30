import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { useI18n } from "../i18n";

export function EmptyState({
  icon: Icon,
  title,
  children,
}: {
  icon: LucideIcon;
  title: string;
  children: ReactNode;
}) {
  const { t } = useI18n();
  return (
    <div className="empty-state">
      <Icon size={24} strokeWidth={1.5} />
      <h3>{t(title)}</h3>
      <p>{typeof children === "string" ? t(children) : children}</p>
    </div>
  );
}
