import { AlertTriangle } from "lucide-react";

import { RiftXAPIError } from "../api/client";

export function ErrorState({ error }: { error: Error }) {
  const code = error instanceof RiftXAPIError ? error.code : "client_error";
  return (
    <div className="error-state" role="alert">
      <AlertTriangle size={22} />
      <div>
        <strong>{error.message}</strong>
        <span>{code}</span>
      </div>
    </div>
  );
}
