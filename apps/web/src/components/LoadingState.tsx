import { useI18n } from "../i18n";

export function LoadingState({ label = "Loading control plane" }: { label?: string }) {
  const { t } = useI18n();
  return (
    <div className="loading-state" role="status">
      <span className="loading-spinner" aria-hidden="true" />
      <span>{t(label)}</span>
    </div>
  );
}
