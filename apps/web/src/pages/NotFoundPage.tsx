import { ArrowLeft, Radar } from "lucide-react";
import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <div className="not-found panel">
      <Radar size={34} />
      <span className="kicker">404 / route not found</span>
      <h2>This control-plane view does not exist.</h2>
      <Link className="secondary-button" to="/">
        <ArrowLeft size={16} /> Return to dashboard
      </Link>
    </div>
  );
}
