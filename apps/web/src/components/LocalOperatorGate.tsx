import { FormEvent, type ReactNode, useState } from "react";

import {
  api,
  clearLocalOperatorToken,
  RiftXAPIError,
  setLocalOperatorToken,
} from "../api/client";
import { useI18n } from "../i18n";

export function LocalOperatorGate({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const [authenticated, setAuthenticated] = useState(false);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    try {
      setLocalOperatorToken(token);
      await api.getSecurityProfile();
      setToken("");
      setAuthenticated(true);
    } catch (cause) {
      clearLocalOperatorToken();
      setError(
        cause instanceof RiftXAPIError
          ? `${cause.message} (${cause.code})`
          : cause instanceof Error
            ? cause.message
            : t("Local operator authentication failed."),
      );
    } finally {
      setPending(false);
    }
  }

  if (authenticated) return children;

  return (
    <main className="local-operator-gate">
      <form className="local-operator-card" onSubmit={(event) => void submit(event)}>
        <p className="eyebrow">{t("Local trust profile")}</p>
        <h1>{t("Unlock RiftX")}</h1>
        <p>
          {t("Enter RIFTX_ADMIN_TOKEN. It stays in this page's memory and is never stored in the browser.")}
        </p>
        <label>
          <span>{t("Local operator token")}</span>
          <input
            autoComplete="off"
            autoFocus
            name="local-operator-token"
            onChange={(event) => setToken(event.target.value)}
            required
            type="password"
            value={token}
          />
        </label>
        {error ? <p className="form-error" role="alert">{error}</p> : null}
        <button className="primary-button" disabled={pending || !token.trim()} type="submit">
          {pending ? t("Authenticating") : t("Continue")}
        </button>
      </form>
    </main>
  );
}
