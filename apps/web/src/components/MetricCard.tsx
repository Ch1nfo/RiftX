import type { LucideIcon } from "lucide-react";

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
  return (
    <article className={`metric-card metric-${tone}`}>
      <div className="metric-icon">
        <Icon size={18} strokeWidth={1.8} />
      </div>
      <p>{label}</p>
      <strong>{value}</strong>
      <span>{note}</span>
    </article>
  );
}
