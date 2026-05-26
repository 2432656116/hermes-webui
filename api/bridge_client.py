"""
Hermes WebUI — Agent Bridge Client.

Replaces direct AIAgent import with a Unix socket connection to the
agent bridge process. Translates bridge protocol events into the
existing STREAMS queue-based SSE format so the rest of streaming.py
works unchanged.

Usage from streaming.py (drop-in for _run_agent_streaming):
    from api.bridge_client import run_via_bridge
    run_via_bridge(session_id, msg_text, model, workspace, stream_id, ...)
"""

import json
import logging
import queue
import socket
import time

logger = logging.getLogger(__name__)

DEFAULT_SOCKET = "/tmp/hermes-webui-bridge.sock"
CONNECT_TIMEOUT = 5.0
RECV_TIMEOUT = 60.0  # per-frame receive timeout
CHUNK_SIZE = 65536


class BridgeClient:
    """Manages connection to the agent bridge process."""

    def __init__(self, socket_path=None):
        self.socket_path = socket_path or DEFAULT_SOCKET
        self._sock = None

    def _connect(self):
        """Connect to the bridge socket. Returns the socket on success."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect(self.socket_path)
        sock.settimeout(RECV_TIMEOUT)
        return sock

    def health_check(self) -> dict:
        """Ping the bridge process to verify it's alive."""
        try:
            sock = self._connect()
            _send_frame(sock, {"cmd": "health"})
            response = _recv_frame(sock)
            sock.close()
            if response and response.get("status") == "ok":
                return {"healthy": True, **response}
            return {"healthy": False, "error": "unexpected response"}
        except Exception as e:
            return {"healthy": False, "error": str(e)}

    def run_chat(
        self,
        session_id: str,
        msg_text: str,
        model: str,
        workspace: str,
        stream_id: str,
        *,
        model_provider: str | None = None,
        attachments: list | None = None,
        cancel_event=None,
    ) -> None:
        """
        Execute a chat turn via the bridge process.

        Events are pushed to the global STREAMS[stream_id] queue so
        existing SSE handling works without changes.

        This is designed to be called from the same background thread
        context as _run_agent_streaming.
        """
        from api.config import STREAMS, CANCEL_FLAGS

        q = STREAMS.get(stream_id)
        if q is None:
            logger.warning("bridge_client: stream %s not found in STREAMS", stream_id)
            return

        sock = None
        try:
            sock = self._connect()

            # Send chat command
            _send_frame(
                sock,
                {
                    "cmd": "chat",
                    "session_id": session_id,
                    "message": msg_text,
                    "model": model,
                    "workspace": workspace,
                    "stream_id": stream_id,
                    "model_provider": model_provider,
                    "attachments": attachments or [],
                },
            )

            # Receive streaming events
            while True:
                # Check cancel
                flag = CANCEL_FLAGS.get(stream_id)
                if flag and flag.is_set():
                    _send_frame(sock, {"cmd": "stop", "stream_id": stream_id})
                    break

                frame = _recv_frame(sock)
                if frame is None:
                    break  # connection closed

                event_type = frame.get("event")

                if event_type == "start":
                    # Already handled by streaming thread
                    pass

                elif event_type == "text_delta":
                    q.put(("text", frame.get("content", "")))

                elif event_type == "reasoning_delta":
                    q.put(("reasoning", frame.get("content", "")))

                elif event_type == "tool_start":
                    q.put(
                        (
                            "tool_start",
                            frame.get("name", ""),
                            frame.get("args", {}),
                        )
                    )

                elif event_type == "tool_end":
                    q.put(
                        (
                            "tool_end",
                            frame.get("name", ""),
                            frame.get("result", ""),
                        )
                    )

                elif event_type == "done":
                    q.put(("done", frame.get("usage", {})))
                    break

                elif event_type == "error":
                    q.put(("text", f"\n\n> ⚠️ Bridge error: {frame.get('message', 'unknown')}\n"))
                    q.put(("done", {}))
                    break

                elif event_type == "stopped":
                    q.put(("done", {"cancelled": True}))
                    break

        except socket.timeout:
            logger.error("bridge_client: socket timeout for stream %s", stream_id)
            q.put(("text", "\n\n> ⚠️ Agent bridge timed out\n"))
            q.put(("done", {}))
        except (ConnectionRefusedError, FileNotFoundError) as e:
            logger.error("bridge_client: cannot connect to bridge: %s", e)
            q.put(
                (
                    "text",
                    "\n\n> ⚠️ Agent bridge is not running. Start it with:\n"
                    "> `python api/agent_bridge.py &`\n",
                )
            )
            q.put(("done", {}))
        except Exception as e:
            logger.error("bridge_client: unexpected error: %s", e)
            q.put(("text", f"\n\n> ⚠️ Bridge error: {e}\n"))
            q.put(("done", {}))
        finally:
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()


# ── Global singleton ──────────────────────────────────────────────────────
_client: BridgeClient | None = None


def get_client() -> BridgeClient:
    """Get or create the global bridge client singleton."""
    global _client
    if _client is None:
        _client = BridgeClient()
    return _client


# ── Wire helpers ──────────────────────────────────────────────────────────
def _send_frame(sock, data: dict):
    raw = json.dumps(data, ensure_ascii=False) + "\n"
    sock.sendall(raw.encode("utf-8"))


def _recv_frame(sock) -> dict | None:
    buf = b""
    while True:
        try:
            chunk = sock.recv(CHUNK_SIZE)
        except (socket.timeout, ConnectionResetError, BrokenPipeError, OSError):
            return None
        if not chunk:
            return None
        buf += chunk
        if b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                return json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue


# ── Drop-in replacement for _run_agent_streaming ──────────────────────────
def run_via_bridge(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    model_provider=None,
    ephemeral=False,
    goal_related=False,
):
    """
    Drop-in replacement for _run_agent_streaming in streaming.py.

    Instead of importing AIAgent directly (same process → GIL contention),
    this delegates the agent turn to the separate agent_bridge process
    over a Unix socket.
    """
    from api.config import (
        STREAMS,
        register_active_run,
        unregister_active_run,
    )

    q = STREAMS.get(stream_id)
    if q is None:
        return

    register_active_run(
        stream_id,
        session_id=session_id,
        started_at=time.time(),
        phase="starting",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        ephemeral=bool(ephemeral),
    )

    try:
        client = get_client()
        client.run_chat(
            session_id=session_id,
            msg_text=msg_text,
            model=model,
            workspace=str(workspace),
            stream_id=stream_id,
            model_provider=model_provider,
            attachments=attachments,
        )
    finally:
        unregister_active_run(stream_id)
