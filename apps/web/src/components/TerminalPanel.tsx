import "@xterm/xterm/css/xterm.css";

import { Terminal as XTerm } from "@xterm/xterm";
import { Loader2, PlugZap, RotateCcw, Square, TerminalSquare, UserRound } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { TerminalSession, TerminalWebSocketMessage } from "../api/types";
import { queryKeys, useTerminal, useTerminalControl } from "../hooks/queries";
import { useI18n } from "../i18n";
import { type Theme, useTheme } from "../theme";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { LoadingState } from "./LoadingState";

interface TerminalPanelProps {
  runId: string;
  initialSessionId?: string;
}

const terminalThemes: Record<Theme, NonNullable<ConstructorParameters<typeof XTerm>[0]>["theme"]> = {
  dark: {
    background: "#060b1c",
    foreground: "#d8e7ff",
    cursor: "#64d9ff",
    cursorAccent: "#060b1c",
    selectionBackground: "#193b70",
    black: "#060b1c",
    brightBlack: "#526789",
    red: "#ff6678",
    brightRed: "#ff8896",
    green: "#55d6a0",
    brightGreen: "#72e6b5",
    yellow: "#e6b95f",
    brightYellow: "#f2cf7a",
    blue: "#4e86ff",
    brightBlue: "#72a1ff",
    magenta: "#668bd8",
    brightMagenta: "#8babef",
    cyan: "#4bc7e8",
    brightCyan: "#76dff5",
    white: "#cbd9f0",
    brightWhite: "#f5f9ff",
  },
  light: {
    background: "#eaf3ff",
    foreground: "#12213d",
    cursor: "#1d5fd1",
    cursorAccent: "#eaf3ff",
    selectionBackground: "#c9dcff",
    black: "#12213d",
    brightBlack: "#5b6c88",
    red: "#c8324c",
    brightRed: "#e3455d",
    green: "#137a59",
    brightGreen: "#0b6549",
    yellow: "#8e5d00",
    brightYellow: "#a87400",
    blue: "#1d5fd1",
    brightBlue: "#174da9",
    magenta: "#4169ad",
    brightMagenta: "#31599c",
    cyan: "#087f9d",
    brightCyan: "#00667f",
    white: "#dce8fb",
    brightWhite: "#ffffff",
  },
};

export function TerminalPanel({ runId, initialSessionId }: TerminalPanelProps) {
  const { t } = useI18n();
  const { theme } = useTheme();
  const queryClient = useQueryClient();
  const [sessionId, setSessionId] = useState(initialSessionId ?? "");
  const [connection, setConnection] = useState<"idle" | "connecting" | "open" | "closed">(
    initialSessionId ? "connecting" : "idle",
  );
  const [socketError, setSocketError] = useState<string | null>(null);
  const terminalElement = useRef<HTMLDivElement>(null);
  const translateRef = useRef(t);
  const sessionRef = useRef<TerminalSession | undefined>(undefined);
  const socketRef = useRef<WebSocket | null>(null);
  const xtermRef = useRef<XTerm | null>(null);
  const terminalQuery = useTerminal(sessionId);
  const controls = useTerminalControl(runId);

  useEffect(() => {
    if (initialSessionId && !sessionId) {
      setSessionId(initialSessionId);
    }
  }, [initialSessionId, sessionId]);

  useEffect(() => {
    translateRef.current = t;
  }, [t]);

  useEffect(() => {
    sessionRef.current = terminalQuery.data;
  }, [terminalQuery.data]);

  useEffect(() => {
    if (xtermRef.current) xtermRef.current.options.theme = terminalThemes[theme];
  }, [theme]);

  useEffect(() => {
    const element = terminalElement.current;
    if (!sessionId || !element) return undefined;

    const terminal = new XTerm({
      convertEol: true,
      cursorBlink: true,
      cursorStyle: "bar",
      fontFamily: '"RiftX Mono", "JetBrains Mono", "SFMono-Regular", Consolas, "Liberation Mono", monospace',
      fontSize: 13,
      scrollback: 10_000,
      theme: terminalThemes[theme],
    });
    xtermRef.current = terminal;
    terminal.open(element);
    terminal.focus();
    let cursor = 0;
    let socket: WebSocket | undefined;
    let disposed = false;

    const send = (message: Record<string, unknown>) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
    };
    const resize = () => {
      const cols = Math.max(20, Math.floor(element.clientWidth / 8.2));
      const rows = Math.max(8, Math.floor(element.clientHeight / 17));
      if (cols !== terminal.cols || rows !== terminal.rows) terminal.resize(cols, rows);
      send({ type: "resize", cols, rows });
    };

    setConnection("connecting");
    setSocketError(null);
    socket = new WebSocket(
      api.terminalWebSocketUrl(sessionId, cursor),
      api.terminalWebSocketProtocols(),
    );
    socketRef.current = socket;
    socket.onopen = () => {
      if (disposed) return;
      setConnection("open");
      resize();
    };
    socket.onmessage = (event) => {
      const message = JSON.parse(String(event.data)) as TerminalWebSocketMessage;
      if (message.type === "output") {
        cursor = message.next_cursor;
        terminal.write(message.data);
      } else if (message.type === "state") {
        sessionRef.current = message.session;
        queryClient.setQueryData(queryKeys.terminal(message.session.id), message.session);
      } else if (message.type === "error") {
        setSocketError(message.message);
        terminal.writeln(`\r\n\x1b[31m[RiftX] ${message.message}\x1b[0m`);
      }
    };
    socket.onerror = () => {
      if (!disposed) setSocketError(translateRef.current("Terminal WebSocket connection failed."));
    };
    socket.onclose = () => {
      if (!disposed) setConnection("closed");
    };

    const input = terminal.onData((data) => {
      const session = sessionRef.current;
      if (!session || session.status !== "open" || session.owner !== "user") return;
      if (data === "\u0003") send({ type: "interrupt" });
      else send({ type: "input", data });
    });
    const observer =
      typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(() => resize());
    observer?.observe(element);

    return () => {
      disposed = true;
      observer?.disconnect();
      input.dispose();
      socket?.close();
      if (socketRef.current === socket) socketRef.current = null;
      if (xtermRef.current === terminal) xtermRef.current = null;
      terminal.dispose();
    };
  }, [queryClient, sessionId, terminalQuery.isLoading]);

  async function startTerminal() {
    const created = await controls.create.mutateAsync({ owner: "agent" });
    setSessionId(created.id);
  }

  function sendControl(type: "takeover" | "release" | "interrupt") {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type }));
    }
  }

  async function closeTerminal() {
    if (!sessionId) return;
    await controls.close.mutateAsync(sessionId);
  }

  if (!sessionId) {
    return (
      <EmptyState icon={TerminalSquare} title="No terminal session">
        <p>{t("Start a host-native shell in this Run workspace. Agent-owned sessions remain read-only until you take over.")}</p>
        <button
          className="primary-button"
          disabled={controls.create.isPending}
          onClick={() => void startTerminal()}
        >
          {controls.create.isPending ? <Loader2 className="spin" size={16} /> : <PlugZap size={16} />}
          {t("Start local shell")}
        </button>
        {controls.create.error ? <ErrorState error={controls.create.error} /> : null}
      </EmptyState>
    );
  }

  if (terminalQuery.isLoading && !terminalQuery.data) {
    return <LoadingState label="Restoring terminal session" />;
  }
  if (terminalQuery.error) return <ErrorState error={terminalQuery.error} />;

  const session = terminalQuery.data;
  const writable = session?.status === "open" && session.owner === "user";

  return (
    <div className="terminal-panel">
      <div className="terminal-toolbar">
        <div className="terminal-identity">
          <TerminalSquare size={17} />
          <span>{sessionId}</span>
          <span className={`terminal-connection terminal-connection-${connection}`}>{t(connection)}</span>
          {session ? <span className="mono-chip">{t(session.status)}</span> : null}
          {session ? <span className="mono-chip">{t("owner")} / {t(session.owner)}</span> : null}
        </div>
        <div className="terminal-actions">
          {session?.status === "open" && session.owner !== "user" ? (
            <button
              className="secondary-button"
              disabled={connection !== "open"}
              onClick={() => sendControl("takeover")}
            >
              <UserRound size={15} /> {t("Take over")}
            </button>
          ) : null}
          {session?.status === "open" && session.owner === "user" ? (
            <>
              <button
                className="secondary-button"
                disabled={connection !== "open"}
                onClick={() => sendControl("interrupt")}
              >
                Ctrl+C
              </button>
              <button
                className="secondary-button"
                disabled={connection !== "open"}
                onClick={() => sendControl("release")}
              >
                <RotateCcw size={15} /> {t("Release")}
              </button>
            </>
          ) : null}
          {session?.status === "open" ? (
            <button
              className="danger-button"
              disabled={controls.close.isPending}
              onClick={() => void closeTerminal()}
            >
              <Square size={14} /> {t("Close")}
            </button>
          ) : null}
          {session && session.status !== "open" ? (
            <button
              className="primary-button"
              disabled={controls.create.isPending}
              onClick={() => void startTerminal()}
            >
              <PlugZap size={15} /> {t("Start new shell")}
            </button>
          ) : null}
        </div>
      </div>
      <div className="terminal-notice">
        {session?.status === "lost"
          ? t("The Runner restarted and this native PTY cannot be reattached. The transcript remains available below.")
          : writable
            ? t("You own terminal input. Press Ctrl+C to interrupt; Release returns input to the Agent.")
            : t("Read-only while the Agent owns terminal input.")}
      </div>
      {socketError ? <p className="terminal-error">{socketError}</p> : null}
      <div className="xterm-host" ref={terminalElement} aria-label={t("Run terminal")} />
      {controls.close.error ? <ErrorState error={controls.close.error} /> : null}
    </div>
  );
}
