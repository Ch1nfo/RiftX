import { ArrowLeft, Radar } from "lucide-react";
import { Link } from "react-router-dom";
import { useI18n } from "../i18n";

export function NotFoundPage() {
  const { t } = useI18n();
  return (
    <div className="not-found panel">
      <Radar size={34} />
      <span className="kicker">{t("404 / route not found")}</span>
      <h2>{t("This control-plane view does not exist.")}</h2>
      <Link className="secondary-button" to="/">
        <ArrowLeft size={16} /> {t("Return to dashboard")}
      </Link>
    </div>
  );
}
