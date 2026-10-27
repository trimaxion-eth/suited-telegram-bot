"""A very small WebSocket client (RFC 6455), stdlib only.

The bot needs one socket: open, send `hello`, read frames until `sync`, close.
That is a fraction of the protocol, so this implements exactly that fraction —
text frames, ping/pong, close, and fragment reassembly — rather than pulling in
a library the rest of the project does not have.

Client frames are masked (as required), server frames are not. Lengths use the
7/16/64-bit forms. Nothing here handles extensions, compression, or binary
payloads beyond returning them as bytes.
"""

from __future__ import annotations

import base64
import os
import select
import socket
import ssl
import struct
import time
import urllib.parse


class WSError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 10.0, user_agent: str = "suited-telegram-bot/1.0"):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("ws", "wss"):
            raise WSError(f"unsupported scheme {parsed.scheme!r}")
        self.secure = parsed.scheme == "wss"
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if self.secure else 80)
        self.path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        self.timeout = timeout
        self.user_agent = user_agent
        self.sock: socket.socket | None = None
        self._buffer = b""

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> "WebSocket":
        raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        if self.secure:
            context = ssl.create_default_context()
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        else:
            self.sock = raw

        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"User-Agent: {self.user_agent}\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise WSError("connection closed during handshake")
            head += chunk
            if len(head) > 65536:
                raise WSError("handshake response too large")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WSError(f"handshake refused: {status}")
        trailer = head.split(b"\r\n\r\n", 1)[1]
        if trailer:
            self._buffer = trailer  # server was eager; keep the bytes
        return self

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def __enter__(self) -> "WebSocket":
        return self.connect()

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- send --------------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), opcode=0x1)

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        if not self.sock:
            raise WSError("not connected")
        mask = os.urandom(4)
        length = len(payload)
        header = bytes([0x80 | opcode])
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    # -- receive -----------------------------------------------------------

    def _fill(self, want: int, timeout: float | None) -> bytes:
        """Read exactly `want` bytes, honouring one overall deadline."""
        if self.sock is None:
            raise WSError("not connected")
        deadline = None if timeout is None else time.monotonic() + timeout
        data = self._buffer
        self._buffer = b""
        while len(data) < want:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._buffer = data
                    raise TimeoutError("websocket read timed out")
                ready, _, _ = select.select([self.sock], [], [], remaining)
                if not ready:
                    self._buffer = data
                    raise TimeoutError("websocket read timed out")
            chunk = self.sock.recv(want - len(data))
            if not chunk:
                raise WSError("connection closed")
            data += chunk
        head, tail = data[:want], data[want:]
        self._buffer = tail
        return head

    def recv_frame(self, timeout: float | None = None) -> tuple[int, bytes]:
        first = self._fill(1, timeout)[0]
        opcode = first & 0x0F
        second = self._fill(1, timeout)[0]
        length = second & 0x7F
        masked = bool(second & 0x80)
        if length == 126:
            length = struct.unpack(">H", self._fill(2, timeout))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._fill(8, timeout))[0]
        mask = self._fill(4, timeout) if masked else b""
        payload = self._fill(length, timeout) if length else b""
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, payload

    def recv_text(self, timeout: float = 8.0) -> str | None:
        """Next whole text frame, transparently answering pings. None on close.

        suited.club sends one compact JSON object per frame, so fragments are
        not reassembled; a continuation frame would be dropped rather than
        half-parsed.
        """
        while True:
            opcode, payload = self.recv_frame(timeout)
            if opcode == 0x9:  # ping -> pong
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                self.close()
                return None
            if opcode in (0x1, 0x2):
                return payload.decode("utf-8", "replace")
