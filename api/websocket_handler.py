"""
Hermes WebUI — Zero-dependency WebSocket upgrade.

Implements RFC 6455 WebSocket handshake and framing using only
Python stdlib (no pip installs). The browser's native WebSocket API
connects directly to this handler.

Usage: call `ws_upgrade(handler)` at the top of a GET handler to
upgrade an HTTP connection to WebSocket, then use `ws_send()`,
`ws_recv()` for frame-level I/O.

This replaces the SSE StreamChannel model with true full-duplex
communication — no long-lived threads per stream needed.
"""

import base64
import hashlib
import struct
import socket as _socket_module
from http.server import BaseHTTPRequestHandler

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes
OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def ws_upgrade(handler: BaseHTTPRequestHandler) -> bool:
    """
    Attempt WebSocket upgrade on an HTTP GET request.

    Returns True on success. On failure, returns False — the caller
    should fall back to regular HTTP handling.
    """
    upgrade = handler.headers.get("Upgrade", "").lower()
    if upgrade != "websocket":
        return False

    key = handler.headers.get("Sec-WebSocket-Key", "")
    if not key:
        handler.send_error(400, "Missing Sec-WebSocket-Key")
        return False

    accept = base64.b64encode(
        hashlib.sha1((key + WEBSOCKET_GUID).encode()).digest()
    ).decode()

    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()

    return True


def ws_send(handler: BaseHTTPRequestHandler, message: str | bytes):
    """Send a WebSocket text frame."""
    if isinstance(message, str):
        message = message.encode("utf-8")

    rfile = handler.rfile
    wfile = handler.wfile

    # Frame: FIN=1, opcode=TEXT(0x1)
    frame = bytearray()
    frame.append(0x81)  # FIN + TEXT opcode

    length = len(message)
    if length < 126:
        frame.append(length)
    elif length < 65536:
        frame.append(126)
        frame.extend(struct.pack(">H", length))
    else:
        frame.append(127)
        frame.extend(struct.pack(">Q", length))

    frame.extend(message)
    wfile.write(frame)
    wfile.flush()


def ws_send_json(handler: BaseHTTPRequestHandler, data: dict):
    """Send a WebSocket text frame with JSON payload."""
    import json
    ws_send(handler, json.dumps(data, ensure_ascii=False))


def ws_recv(handler: BaseHTTPRequestHandler) -> str | None:
    """
    Receive a WebSocket text frame. Returns the decoded string.

    Handles ping/pong and close frames transparently.
    Returns None on connection close or error.
    """
    rfile = handler.rfile
    try:
        while True:
            # Read first 2 bytes (opcode + length)
            header = rfile.read(2)
            if not header or len(header) < 2:
                return None

            opcode = header[0] & 0x0F
            masked = (header[1] & 0x80) != 0
            length = header[1] & 0x7F

            # Extended length
            if length == 126:
                ext = rfile.read(2)
                if len(ext) < 2:
                    return None
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = rfile.read(8)
                if len(ext) < 8:
                    return None
                length = struct.unpack(">Q", ext)[0]

            # Read mask key (4 bytes)
            mask_key = rfile.read(4) if masked else None
            if masked and (not mask_key or len(mask_key) < 4):
                return None

            # Read payload
            payload = b""
            while len(payload) < length:
                chunk = rfile.read(min(length - len(payload), 65536))
                if not chunk:
                    return None
                payload += chunk

            # Unmask client data
            if masked:
                payload = bytes(
                    b ^ mask_key[i % 4] for i, b in enumerate(payload)
                )

            # Handle Ping / Pong / Close
            if opcode == OP_PING:
                # Auto-respond with pong
                pong = bytearray()
                pong.append(0x8A)  # FIN + PONG
                if len(payload) < 126:
                    pong.append(len(payload))
                pong.extend(payload or b"")
                handler.wfile.write(pong)
                handler.wfile.flush()
                continue

            if opcode == OP_CLOSE:
                # Send close frame back
                close = bytearray()
                close.append(0x88)  # FIN + CLOSE
                close.append(0)
                handler.wfile.write(close)
                handler.wfile.flush()
                return None

            if opcode == OP_PONG:
                continue  # application-level pong, ignore

            if opcode == OP_TEXT:
                return payload.decode("utf-8", errors="replace")

            # Binary / continuation frames — unsupported for now
            continue
    except (BrokenPipeError, ConnectionResetError, OSError):
        return None


def ws_ping(handler: BaseHTTPRequestHandler, payload: bytes = b""):
    """Send a WebSocket ping frame (keep-alive)."""
    wfile = handler.wfile
    frame = bytearray()
    frame.append(0x89)  # FIN + PING
    frame.append(len(payload))
    frame.extend(payload)
    wfile.write(frame)
    wfile.flush()
