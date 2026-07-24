import { Bell, BellRing, LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";
import {
  bridgeError,
  notificationSettings,
  requestNotificationPermission,
} from "../bridge";
import type {
  DesktopBridgeError,
  NotificationSettings,
} from "../models";

interface NotificationControlsProps {
  open: boolean;
  onError: (error: DesktopBridgeError) => void;
}

export function NotificationControls({
  open,
  onError,
}: NotificationControlsProps) {
  const [settings, setSettings] = useState<NotificationSettings | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) {
      setSettings(null);
      return;
    }
    setBusy(true);
    void notificationSettings()
      .then(setSettings)
      .catch((cause) => onError(bridgeError(cause)))
      .finally(() => setBusy(false));
  }, [onError, open]);

  const enable = async () => {
    if (busy) {
      return;
    }
    setBusy(true);
    try {
      setSettings(await requestNotificationPermission());
    } catch (cause) {
      onError(bridgeError(cause));
    } finally {
      setBusy(false);
    }
  };

  const granted = settings?.permission === "granted";
  const denied = settings?.permission === "denied";

  return (
    <section className="notification-setting">
      <div className="credential-title">
        <Bell size={16} />
        <div>
          <strong>Background notifications</strong>
          <span>
            {granted
              ? "Enabled"
              : denied
                ? "Denied in system settings"
                : "Not enabled"}
          </span>
        </div>
      </div>
      {busy && !settings ? (
        <LoaderCircle className="spin" size={16} />
      ) : (
        <button
          type="button"
          className="secondary-button"
          onClick={() => void enable()}
          disabled={busy || granted || denied}
        >
          {busy ? (
            <LoaderCircle className="spin" size={15} />
          ) : (
            <BellRing size={15} />
          )}
          {granted ? "Enabled" : denied ? "Blocked" : "Enable"}
        </button>
      )}
    </section>
  );
}
