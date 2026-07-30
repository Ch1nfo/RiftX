"""WebSocket terminal attachment for the API-only CLI client."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys
import threading
from contextlib import suppress
from typing import TextIO

from rich.console import Console
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .client import APIClient
from .i18n import tr

_DETACH_BYTE = b"\x1d"  # Ctrl+]
_INTERRUPT_BYTE = b"\x03"  # Ctrl+C


def attach_terminal(
    client: APIClient,
    session_id: str,
    console: Console,
    *,
    take_over: bool = True,
    cursor: int = 0,
) -> None:
    """Attach the local TTY to a terminal WebSocket until Ctrl+] or remote close."""

    if take_over and not sys.stdin.isatty():
        raise ValueError(
            tr("terminal attach requires an interactive TTY; use --read-only otherwise")
        )

    url = client.terminal_websocket_url(session_id, cursor=cursor)
    stop = threading.Event()
    resize_requested = threading.Event()
    output = _binary_output(sys.stdout)
    error_output = console

    with connect(url, open_timeout=10, close_timeout=2) as websocket:
        if take_over:
            websocket.send(json.dumps({"type": "takeover"}))
        _send_resize(websocket)

        receiver = threading.Thread(
            target=_receive_messages,
            args=(websocket, output, error_output, stop),
            name=f"riftx-attach-{session_id}",
            daemon=True,
        )
        receiver.start()
        old_handler = None
        if hasattr(signal, "SIGWINCH"):
            old_handler = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, lambda *_: resize_requested.set())

        try:
            if take_over:
                _forward_input(websocket, stop, resize_requested)
            else:
                while not stop.wait(0.1):
                    if resize_requested.is_set():
                        resize_requested.clear()
                        _send_resize(websocket)
        except KeyboardInterrupt:
            if take_over:
                _send_json(websocket, {"type": "interrupt"})
            else:
                stop.set()
        finally:
            if old_handler is not None:
                signal.signal(signal.SIGWINCH, old_handler)
            if take_over:
                with suppress(ConnectionClosed, OSError):
                    _send_json(websocket, {"type": "release"})
            stop.set()
            with suppress(ConnectionClosed, OSError):
                websocket.close()
            receiver.join(timeout=2)


def control_terminal(
    client: APIClient,
    session_id: str,
    action: str,
) -> dict[str, object]:
    """Send a one-shot ownership command and return the resulting state."""

    if action not in {"takeover", "release"}:
        raise ValueError(tr("unsupported terminal control action {action}", action=repr(action)))
    with connect(client.terminal_websocket_url(session_id), open_timeout=10, close_timeout=2) as ws:
        ws.send(json.dumps({"type": action}))
        while True:
            raw = ws.recv()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            message = json.loads(raw)
            if message.get("type") == "error":
                raise ValueError(str(message.get("message", tr("terminal control failed"))))
            if message.get("type") == "state":
                session = message.get("session")
                if isinstance(session, dict):
                    expected_owner = "user" if action == "takeover" else "agent"
                    if session.get("owner") == expected_owner:
                        return session


def _receive_messages(
    websocket: object,
    output: TextIO | object,
    error_output: Console,
    stop: threading.Event,
) -> None:
    try:
        while not stop.is_set():
            raw = websocket.recv()  # type: ignore[attr-defined]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            message = json.loads(raw)
            message_type = message.get("type")
            if message_type == "output":
                _write_output(output, str(message.get("data", "")).encode())
            elif message_type == "error":
                error_output.print(
                    f"[red]{message.get('message', tr('Terminal error'))}[/red] "
                    f"[dim]({message.get('code', 'terminal_error')})[/dim]"
                )
            elif message_type == "state":
                session = message.get("session", {})
                if isinstance(session, dict) and session.get("status") in {"closed", "lost"}:
                    stop.set()
    except (ConnectionClosed, json.JSONDecodeError, OSError):
        stop.set()


def _forward_input(
    websocket: object,
    stop: threading.Event,
    resize_requested: threading.Event,
) -> None:
    if os.name != "posix":
        raise ValueError(tr("interactive terminal attach currently requires a Unix TTY"))

    import termios
    import tty

    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        while not stop.is_set():
            if resize_requested.is_set():
                resize_requested.clear()
                _send_resize(websocket)
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            data = os.read(fd, 4096)
            if not data or _DETACH_BYTE in data:
                return
            if data == _INTERRUPT_BYTE:
                _send_json(websocket, {"type": "interrupt"})
            else:
                _send_json(websocket, {"type": "input", "data": data.decode(errors="replace")})
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def _send_resize(websocket: object) -> None:
    size = shutil.get_terminal_size(fallback=(120, 40))
    _send_json(websocket, {"type": "resize", "cols": size.columns, "rows": size.lines})


def _send_json(websocket: object, payload: dict[str, object]) -> None:
    websocket.send(json.dumps(payload))  # type: ignore[attr-defined]


def _binary_output(stream: TextIO) -> TextIO | object:
    return getattr(stream, "buffer", stream)


def _write_output(stream: TextIO | object, data: bytes) -> None:
    if hasattr(stream, "write"):
        try:
            stream.write(data)  # type: ignore[arg-type]
        except TypeError:
            stream.write(data.decode("utf-8", errors="replace"))  # type: ignore[arg-type]
    if hasattr(stream, "flush"):
        stream.flush()  # type: ignore[attr-defined]
