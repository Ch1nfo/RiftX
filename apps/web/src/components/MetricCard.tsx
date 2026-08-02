import type { LucideIcon } from "lucide-react";

import { useI18n } from "../i18n";

interface MetricCardProps {
  label: string;
  value: number | string;
  note: string;
  icon: LucideIcon;
  tone?: "mint" | "amber" | "blue" | "neutral";
}

export function MetricCard({
  label,
  value,
  note,
  icon: Icon,
  tone = "neutral",
}: MetricCardProps) {
  const { t } = useI18n();
  return (
    <article className={`metric-card metric-${tone}`}>
      <div className="metric-icon">
        <Icon size={18} strokeWidth={1.8} />
      </div>
      <p>{t(label)}</p>
      <strong>{value}</strong>
      <span>{t(note)}</span>
    </article>
  );
}
