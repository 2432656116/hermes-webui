#!/usr/bin/env python3
"""
Hermes WebUI — Agent Bridge (IPC server).

Runs in a separate process. Accepts chat commands over a Unix socket,
executes the hermes-agent AIAgent in isolation, and streams results back
as newline-delimited JSON frames.

This eliminates GIL contention between HTTP handling (webui) and LLM
calls (bridge), since they now run in separate Python processes.

Protocol (newline-delimited JSON, one line per frame):
  Client → Bridge:
    {"cmd":"chat","session_id":"...","message":"...","model":"...","workspace":"...","stream_id":"..."}
    {"cmd":"stop","stream_id":"..."}
    {"cmd":"health"}

  Bridge → Client:
    {"event":"start","stream_id":"..."}
    {"event":"text_delta","content":"..."}
    {"event":"tool_start","name":"...","args":{...}}
    {"event":"tool_end","name":"...","result":"..."}
    {"event":"done","usage":{...}}
    {"event":"error","message":"..."}
    {"event":"health","status":"ok"}

Usage:
    python api/agent_bridge.py [--socket /path/to/sock] [--hermes-home ~/.hermes]

Start alongside webui:
    python api/agent_bridge.py &
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback

BRIDGE_VERSION = "1.0.0"

# ── Configuration ──────────────────────────────────────────────────────────
DEFAULT_SOCKET = "/tmp/hermes-webui-bridge.sock"
CHUNK_SIZE = 65536
BACKLOG = 5
HEARTBEAT_INTERVAL = 30  # seconds


def _resolve_hermes_home():
    return os.environ.get("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))


def _send_frame(sock, data: dict):
    """Send a JSON frame (newline-terminated) over the socket."""
    try:
        raw = json.dumps(data, ensure_ascii=False) + "\n"
        sock.sendall(raw.encode("utf-8"))
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


def _recv_frame(sock) -> dict | None:
    """Receive a JSON frame (newline-terminated) from the socket."""
    buf = b""
    while True:
        try:
            chunk = sock.recv(CHUNK_SIZE)
        except (ConnectionResetError, BrokenPipeError, OSError):
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


# ── Streaming callback class ──────────────────────────────────────────────
class BridgeStreamCallback:
    """Adapts AIAgent tool callbacks to bridge protocol frames."""

    def __init__(self, sock):
        self.sock = sock

    def on_text_delta(self, text: str):
        _send_frame(self.sock, {"event": "text_delta", "content": text})

    def on_tool_start(self, name: str, args: dict):
        _send_frame(self.sock, {"event": "tool_start", "name": name, "args": args})

    def on_tool_end(self, name: str, result: str):
        _send_frame(
            self.sock,
            {"event": "tool_end", "name": name, "result": result[:8000]},
        )

    def on_reasoning(self, text: str):
        _send_frame(self.sock, {"event": "reasoning_delta", "content": text})

    def on_done(self, usage: dict | None = None):
        _send_frame(self.sock, {"event": "done", "usage": usage or {}})

    def on_error(self, message: str):
        _send_frame(self.sock, {"event": "error", "message": message})


# ── Active run registry (in-process) ──────────────────────────────────────
_active_runs: dict[str, dict] = {}
_active_runs_lock = threading.Lock()

# Per-run cancel events
_cancel_events: dict[str, threading.Event] = {}


def _run_agent_in_process(
    sock,
    session_id: str,
    msg_text: str,
    model: str,
    workspace: str,
    stream_id: str,
    model_provider: str | None,
    attachments: list | None,
):
    """Execute an AIAgent turn in the bridge process, streaming results back."""
    callback = BridgeStreamCallback(sock)

    # Register active run
    with _active_runs_lock:
        _active_runs[stream_id] = {
            "session_id": session_id,
            "model": model,
            "workspace": workspace,
            "started_at": time.time(),
        }

    cancel_event = threading.Event()
    _cancel_events[stream_id] = cancel_event

    try:
        # Import hermes-agent internals (only when needed, inside bridge process)
        hermes_home = _resolve_hermes_home()
        sys.path.insert(0, os.path.join(hermes_home, "hermes-agent"))

        from run_agent import AIAgent

        # ── Set up environment for the agent ──
        os.environ["HERMES_HOME"] = hermes_home
        if workspace:
            os.chdir(workspace)

        # Load config
        config_path = os.path.join(hermes_home, "config.yaml")
        import yaml

        cfg = {}
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f) or {}

        agent_model = model
        provider = model_provider or cfg.get("model", {}).get("provider")

        # Resolve provider
        if provider and provider.startswith("custom:"):
            provider_name = provider[len("custom:"):]
            custom_providers = cfg.get("custom_providers", [])
            cp = next((p for p in custom_providers if p.get("name") == provider_name), None)
            if cp:
                base_url = cp.get("base_url", "")
                api_key = cp.get("api_key", "")
            else:
                base_url = ""
                api_key = ""
        else:
            base_url = ""
            api_key = ""

        # Build agent kwargs
        agent_kwargs = {
            "session_id": session_id,
            "model": agent_model,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "messages": [],  # Agent will load from session db
        }

        agent = AIAgent(**agent_kwargs)

        # ── Stream the response ──
        callback.on_text_delta("")  # trigger start

        # Attach tool callbacks
        if hasattr(agent, "set_tool_callback"):
            agent.set_tool_callback(
                lambda name, args: callback.on_tool_start(name, args),
                lambda name, result: callback.on_tool_end(name, str(result)[:8000]),
            )

        response_text = agent.run_conversation(msg_text, attachments=attachments or [])

        if isinstance(response_text, str):
            callback.on_text_delta(response_text)

        callback.on_done()
    except SystemExit:
        callback.on_error("Agent process terminated")
    except Exception as e:
        callback.on_error(f"Agent error: {e}")
        traceback.print_exc(file=sys.stderr)
    finally:
        with _active_runs_lock:
            _active_runs.pop(stream_id, None)
        _cancel_events.pop(stream_id, None)


# ── Socket server ─────────────────────────────────────────────────────────
def handle_client(conn: socket.socket):
    """Handle a single bridge client connection."""
    try:
        frame = _recv_frame(conn)
        if not frame:
            return

        cmd = frame.get("cmd")

        if cmd == "health":
            with _active_runs_lock:
                runs = len(_active_runs)
            _send_frame(
                conn,
                {
                    "event": "health",
                    "status": "ok",
                    "version": BRIDGE_VERSION,
                    "active_runs": runs,
                },
            )
        elif cmd == "chat":
            session_id = frame.get("session_id", "")
            msg_text = frame.get("message", "")
            model = frame.get("model", "")
            workspace = frame.get("workspace", "")
            stream_id = frame.get("stream_id", "")
            model_provider = frame.get("model_provider")
            attachments = frame.get("attachments", [])

            if not session_id or not msg_text:
                _send_frame(conn, {"event": "error", "message": "Missing session_id or message"})
                return

            _send_frame(conn, {"event": "start", "stream_id": stream_id})

            _run_agent_in_process(
                conn,
                session_id,
                msg_text,
                model,
                workspace,
                stream_id,
                model_provider,
                attachments,
            )
            # End of stream — close connection
        elif cmd == "stop":
            stream_id = frame.get("stream_id", "")
            evt = _cancel_events.get(stream_id)
            if evt:
                evt.set()
                _send_frame(conn, {"event": "stopped", "stream_id": stream_id})
            else:
                _send_frame(conn, {"event": "error", "message": "Stream not found"})
        else:
            _send_frame(conn, {"event": "error", "message": f"Unknown command: {cmd}"})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Hermes WebUI Agent Bridge")
    parser.add_argument(
        "--socket",
        default=DEFAULT_SOCKET,
        help=f"Unix socket path (default: {DEFAULT_SOCKET})",
    )
    parser.add_argument(
        "--hermes-home",
        default=_resolve_hermes_home(),
        help="Hermes home directory",
    )
    args = parser.parse_args()

    sock_path = args.socket
    hermes_home = args.hermes_home

    # Clean up stale socket
    if os.path.exists(sock_path):
        os.unlink(sock_path)

    # Create Unix socket server
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    os.chmod(sock_path, 0o600)
    server.listen(BACKLOG)

    print(f"[bridge] Agent Bridge v{BRIDGE_VERSION}")
    print(f"[bridge] Socket: {sock_path}")
    print(f"[bridge] Hermes home: {hermes_home}")
    print(f"[bridge] Ready to accept connections", flush=True)

    # Graceful shutdown on SIGTERM/SIGINT
    running = True

    def _shutdown(sig, frame):
        nonlocal running
        print(f"\n[bridge] Shutting down...", flush=True)
        running = False
        server.close()
        if os.path.exists(sock_path):
            os.unlink(sock_path)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Accept loop
    while running:
        try:
            server.settimeout(1.0)
            conn, _ = server.accept()
            t = threading.Thread(target=handle_client, args=(conn,), daemon=True)
            t.start()
        except socket.timeout:
            continue
        except OSError:
            if not running:
                break

    print("[bridge] Stopped", flush=True)


if __name__ == "__main__":
    main()
