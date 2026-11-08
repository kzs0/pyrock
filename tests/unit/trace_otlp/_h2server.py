"""A scripted, in-process HTTP/2 prior-knowledge (h2c) server for component tests.

Self-contained on purpose: it has its own frame codec and a minimal HPACK
decoder, so it never trusts the code under test. It speaks just enough HTTP/2
for unary gRPC calls and records everything the client sends.

Responses are produced by a ``handler(conn, request)`` callable running on the
server thread; :meth:`ServerConn.respond_grpc` is the usual one. Sending DATA
honours the client's flow-control windows and blocks (processing incoming
frames) until the client grants more window.
"""

from __future__ import annotations

import contextlib
import socket
import struct
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

DATA, HEADERS, PRIORITY, RST_STREAM, SETTINGS, PUSH_PROMISE, PING, GOAWAY = range(8)
WINDOW_UPDATE, CONTINUATION = 8, 9
END_STREAM = ACK = 0x1
END_HEADERS = 0x4
PADDED = 0x8
PRIORITY_FLAG = 0x20

S_HEADER_TABLE_SIZE = 1
S_ENABLE_PUSH = 2
S_INITIAL_WINDOW_SIZE = 4
S_MAX_FRAME_SIZE = 5

STATIC = [
    (":authority", ""),
    (":method", "GET"),
    (":method", "POST"),
    (":path", "/"),
    (":path", "/index.html"),
    (":scheme", "http"),
    (":scheme", "https"),
    (":status", "200"),
    (":status", "204"),
    (":status", "206"),
    (":status", "304"),
    (":status", "400"),
    (":status", "404"),
    (":status", "500"),
]
STATIC_NAMES = [
    "accept-charset",
    "accept-encoding",
    "accept-language",
    "accept-ranges",
    "accept",
    "access-control-allow-origin",
    "age",
    "allow",
    "authorization",
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-language",
    "content-length",
    "content-location",
    "content-range",
    "content-type",
    "cookie",
    "date",
    "etag",
    "expect",
    "expires",
    "from",
    "host",
    "if-match",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "if-unmodified-since",
    "last-modified",
    "link",
    "location",
    "max-forwards",
    "proxy-authenticate",
    "proxy-authorization",
    "range",
    "referer",
    "refresh",
    "retry-after",
    "server",
    "set-cookie",
    "strict-transport-security",
    "transfer-encoding",
    "user-agent",
    "vary",
    "via",
    "www-authenticate",
]
STATIC += [(name, "") for name in STATIC_NAMES]
STATIC[15] = ("accept-encoding", "gzip, deflate")
assert len(STATIC) == 61


# ------------------------------------------------------------------ codec


def frame(ftype: int, flags: int, stream: int, payload: bytes = b"") -> bytes:
    return (
        len(payload).to_bytes(3, "big")
        + bytes([ftype, flags])
        + stream.to_bytes(4, "big")
        + payload
    )


def hpack_int(value: int, prefix: int, first: int = 0) -> bytes:
    limit = (1 << prefix) - 1
    if value < limit:
        return bytes([first | value])
    out = bytearray([first | limit])
    value -= limit
    while value >= 128:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def hpack_str(text: str) -> bytes:
    data = text.encode()
    return hpack_int(len(data), 7) + data


def lit(name: str, value: str) -> bytes:
    """Literal without indexing, new name, no Huffman."""
    return b"\x00" + hpack_str(name) + hpack_str(value)


def lit_indexed(name: str, value: str) -> bytes:
    """Literal with incremental indexing, new name, no Huffman."""
    return b"\x40" + hpack_str(name) + hpack_str(value)


def lit_huffman_value(name: str, huffman: bytes) -> bytes:
    """Literal without indexing, new name, value given pre-Huffman-encoded."""
    return b"\x00" + hpack_str(name) + hpack_int(len(huffman), 7, 0x80) + huffman


def status_block(status: int) -> bytes:
    if status == 200:
        return b"\x88"
    return b"\x08" + hpack_str(str(status))  # literal without indexing, name index 8


class _Decoder:
    """Decodes what a literal-only client sends (static table, no Huffman)."""

    def __init__(self) -> None:
        self.dynamic: list[tuple[str, str]] = []

    def _int(self, data: bytes, i: int, prefix: int) -> tuple[int, int]:
        limit = (1 << prefix) - 1
        value = data[i] & limit
        i += 1
        if value < limit:
            return value, i
        shift = 0
        while True:
            byte = data[i]
            i += 1
            value += (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value, i

    def _str(self, data: bytes, i: int) -> tuple[str, int]:
        if data[i] & 0x80:
            raise AssertionError("client used Huffman coding")
        n, i = self._int(data, i, 7)
        return data[i : i + n].decode(), i + n

    def _entry(self, index: int) -> tuple[str, str]:
        if 1 <= index <= 61:
            return STATIC[index - 1]
        return self.dynamic[index - 62]

    def decode(self, data: bytes) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        i = 0
        while i < len(data):
            byte = data[i]
            if byte & 0x80:
                index, i = self._int(data, i, 7)
                out.append(self._entry(index))
                continue
            if byte & 0xE0 == 0x20:
                _, i = self._int(data, i, 5)
                continue
            prefix = 6 if byte & 0x40 else 4
            index, i = self._int(data, i, prefix)
            if index:
                name = self._entry(index)[0]
            else:
                name, i = self._str(data, i)
            value, i = self._str(data, i)
            if prefix == 6:
                self.dynamic.insert(0, (name, value))
            out.append((name, value))
        return out


# --------------------------------------------------------------- records


@dataclass
class Request:
    stream_id: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    header_frames: list[tuple[int, int, int]] = field(default_factory=list)
    """(type, flags, payload length) of the HEADERS and CONTINUATION frames."""
    data_frames: list[int] = field(default_factory=list)
    """Payload length of every DATA frame."""
    complete: bool = False

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key == name:
                return value
        return None


@dataclass
class ConnRecord:
    frames: list[tuple[int, int, int, bytes]] = field(default_factory=list)
    """Every frame received after the preface: (type, flags, stream, payload)."""
    client_settings: dict[int, int] = field(default_factory=dict)
    settings_acked: bool = False
    pings: list[bytes] = field(default_factory=list)
    ping_acks: list[bytes] = field(default_factory=list)
    goaway: tuple[int, int] | None = None
    rst_streams: list[tuple[int, int]] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    flow_violations: list[str] = field(default_factory=list)
    window_updates: list[tuple[int, int]] = field(default_factory=list)
    eof: bool = False


Handler = Callable[["ServerConn", Request], None]


def grpc_ok(conn: ServerConn, request: Request) -> None:
    conn.respond_grpc(request.stream_id)


# ------------------------------------------------------------ connection


class ServerConn:
    def __init__(self, server: H2TestServer, sock: socket.socket) -> None:
        self.server = server
        self.sock = sock
        self.record = ConnRecord()
        self.decoder = _Decoder()
        self.buffer = b""
        self.max_frame = 16384  # client's SETTINGS_MAX_FRAME_SIZE
        self.client_initial_window = 65535
        self.conn_send_window = 65535
        self.stream_send_windows: dict[int, int] = {}
        self.recv_initial = dict(server.settings).get(S_INITIAL_WINDOW_SIZE, 65535)
        self.conn_recv_window = 65535
        self.stream_recv_windows: dict[int, int] = {}
        self.open: dict[int, Request] = {}
        self.header_stream: int | None = None
        self.header_block = b""
        self.ready: list[Request] = []
        self.content_type_indexed = False
        self.in_handler = False

    # raw I/O
    def send(self, ftype: int, flags: int, stream: int, payload: bytes = b"") -> None:
        self.sock.sendall(frame(ftype, flags, stream, payload))

    def _read_frame(self) -> tuple[int, int, int, bytes] | None:
        while True:
            if len(self.buffer) >= 9:
                length = int.from_bytes(self.buffer[:3], "big")
                if len(self.buffer) >= 9 + length:
                    head, self.buffer = self.buffer[: 9 + length], self.buffer[9 + length :]
                    stream = int.from_bytes(head[5:9], "big") & 0x7FFFFFFF
                    return head[3], head[4], stream, head[9:]
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk

    def _read_exact(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("eof before preface")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    # main loop
    def serve(self) -> None:
        preface = self._read_exact(len(PREFACE))
        assert preface == PREFACE, preface
        payload = b"".join(struct.pack(">HI", k, v) for k, v in self.server.settings)
        self.send(SETTINGS, 0, 0, payload)
        if self.server.on_connect is not None:
            self.server.on_connect(self)
        while self.step():
            self._run_ready()

    def _run_ready(self) -> None:
        if self.in_handler:
            return
        while self.ready:
            request = self.ready.pop(0)
            self.in_handler = True
            try:
                self.server.handler(self, request)
            finally:
                self.in_handler = False

    def step(self) -> bool:
        got = self._read_frame()
        if got is None:
            self.record.eof = True
            return False
        ftype, flags, stream, payload = got
        rec = self.record
        rec.frames.append(got)
        if ftype == SETTINGS:
            if flags & ACK:
                rec.settings_acked = True
            else:
                for i in range(0, len(payload), 6):
                    key, value = struct.unpack(">HI", payload[i : i + 6])
                    rec.client_settings[key] = value
                    if key == S_INITIAL_WINDOW_SIZE:
                        delta = value - self.client_initial_window
                        self.client_initial_window = value
                        for sid in self.stream_send_windows:
                            self.stream_send_windows[sid] += delta
                    elif key == S_MAX_FRAME_SIZE:
                        self.max_frame = value
                self.send(SETTINGS, ACK, 0)
        elif ftype == PING:
            if flags & ACK:
                rec.ping_acks.append(payload)
            else:
                rec.pings.append(payload)
                self.send(PING, ACK, 0, payload)
        elif ftype == WINDOW_UPDATE:
            increment = int.from_bytes(payload, "big") & 0x7FFFFFFF
            rec.window_updates.append((stream, increment))
            if stream == 0:
                self.conn_send_window += increment
            else:
                self.stream_send_windows[stream] = (
                    self.stream_send_windows.get(stream, self.client_initial_window) + increment
                )
        elif ftype == GOAWAY:
            rec.goaway = struct.unpack(">II", payload[:8])
        elif ftype == RST_STREAM:
            rec.rst_streams.append((stream, int.from_bytes(payload, "big")))
            self.open.pop(stream, None)
        elif ftype in (HEADERS, CONTINUATION):
            self._on_headers(ftype, flags, stream, payload)
        elif ftype == DATA:
            self._on_data(flags, stream, payload)
        with self.server.cond:
            self.server.cond.notify_all()
        return True

    def _request(self, stream: int) -> Request:
        if stream not in self.open:
            request = Request(stream)
            self.open[stream] = request
            self.record.requests.append(request)
            self.stream_send_windows.setdefault(stream, self.client_initial_window)
            self.stream_recv_windows[stream] = self.recv_initial
        return self.open[stream]

    def _on_headers(self, ftype: int, flags: int, stream: int, payload: bytes) -> None:
        request = self._request(stream)
        request.header_frames.append((ftype, flags, len(payload)))
        body = payload
        if ftype == HEADERS:
            if flags & PADDED:
                body = body[1 : len(body) - body[0]]
            if flags & PRIORITY_FLAG:
                body = body[5:]
            if flags & END_STREAM:
                request.complete = True
            self.header_block = b""
        self.header_block += body
        if flags & END_HEADERS:
            request.headers = self.decoder.decode(self.header_block)
            self.header_block = b""
            if request.complete:
                self._finish(request)

    def _on_data(self, flags: int, stream: int, payload: bytes) -> None:
        request = self._request(stream)
        request.data_frames.append(len(payload))
        self.conn_recv_window -= len(payload)
        self.stream_recv_windows[stream] -= len(payload)
        if self.conn_recv_window < 0 or self.stream_recv_windows[stream] < 0:
            self.record.flow_violations.append(
                f"stream {stream}: conn={self.conn_recv_window} "
                f"stream={self.stream_recv_windows[stream]}"
            )
        data = payload[1 : len(payload) - payload[0]] if flags & PADDED else payload
        request.body += data
        if payload and self.server.auto_window_update:
            self.send(WINDOW_UPDATE, 0, 0, len(payload).to_bytes(4, "big"))
            self.conn_recv_window += len(payload)
            if not flags & END_STREAM:
                self.send(WINDOW_UPDATE, 0, stream, len(payload).to_bytes(4, "big"))
                self.stream_recv_windows[stream] += len(payload)
        if flags & END_STREAM:
            request.complete = True
            self._finish(request)

    def _finish(self, request: Request) -> None:
        self.ready.append(request)
        with self.server.cond:
            self.server.cond.notify_all()

    # response helpers (handler side)
    def send_headers(
        self,
        stream: int,
        block: bytes,
        *,
        end_stream: bool = False,
        split: int | None = None,
        pad: int = 0,
        priority: bool = False,
    ) -> None:
        flags = END_STREAM if end_stream else 0
        prefix = b""
        if pad:
            flags |= PADDED
            prefix = bytes([pad])
        if priority:
            flags |= PRIORITY_FLAG
            prefix += struct.pack(">IB", 0, 15)
        chunks = [block[i : i + split] for i in range(0, len(block), split)] if split else [block]
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            if index == 0:
                payload = prefix + chunk + b"\x00" * pad
                self.send(HEADERS, flags | (END_HEADERS if last else 0), stream, payload)
            else:
                self.send(CONTINUATION, END_HEADERS if last else 0, stream, chunk)

    def send_data(
        self, stream: int, data: bytes, *, end_stream: bool = False, pad: int = 0
    ) -> None:
        overhead = (pad + 1) if pad else 0
        remaining = data
        while True:
            window = min(self.conn_send_window, self.stream_send_windows.get(stream, 0))
            room = min(window, self.max_frame) - overhead
            if remaining and room <= 0:
                if not self.step():
                    raise ConnectionError("client went away while server waited for window")
                continue
            chunk, remaining = remaining[: max(room, 0)], remaining[max(room, 0) :]
            flags = END_STREAM if (end_stream and not remaining) else 0
            payload = chunk
            if pad:
                flags |= PADDED
                payload = bytes([pad]) + chunk + b"\x00" * pad
            self.send(DATA, flags, stream, payload)
            self.conn_send_window -= len(payload)
            self.stream_send_windows[stream] = self.stream_send_windows.get(stream, 0) - len(
                payload
            )
            if not remaining:
                return

    def response_headers(self, status: int = 200) -> bytes:
        block = status_block(status)
        if self.content_type_indexed:
            return block + b"\xbe"
        self.content_type_indexed = True
        return block + lit_indexed("content-type", "application/grpc")

    def respond_grpc(
        self,
        stream: int,
        *,
        message: bytes = b"",
        grpc_status: str | None = "0",
        trailers: bytes | None = None,
        status: int = 200,
        data_pad: int = 0,
    ) -> None:
        self.send_headers(stream, self.response_headers(status))
        self.send_data(stream, b"\x00" + len(message).to_bytes(4, "big") + message, pad=data_pad)
        block = trailers if trailers is not None else b""
        if trailers is None and grpc_status is not None:
            block = lit("grpc-status", grpc_status)
        self.send_headers(stream, block, end_stream=True)


# ---------------------------------------------------------------- server


class H2TestServer:
    """Accepts h2c connections on 127.0.0.1:0, one thread per connection."""

    def __init__(
        self,
        handler: Handler = grpc_ok,
        *,
        settings: Sequence[tuple[int, int]] = (),
        auto_window_update: bool = True,
        on_connect: Callable[[ServerConn], None] | None = None,
    ) -> None:
        self.handler = handler
        self.settings = list(settings)
        self.auto_window_update = auto_window_update
        self.on_connect = on_connect
        self.cond = threading.Condition()
        self.connections: list[ServerConn] = []
        self.errors: list[BaseException] = []
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(None)
        self.host, self.port = self._listener.getsockname()[:2]
        self._closed = False
        self._threads: list[threading.Thread] = []
        accept = threading.Thread(target=self._accept, name="h2-test-accept", daemon=True)
        accept.start()
        self._threads.append(accept)

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def _accept(self) -> None:
        while not self._closed:
            try:
                sock, _ = self._listener.accept()
            except OSError:
                return
            sock.settimeout(15)
            conn = ServerConn(self, sock)
            with self.cond:
                self.connections.append(conn)
                self.cond.notify_all()
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn: ServerConn) -> None:
        try:
            conn.serve()
        except (OSError, ConnectionError):
            pass
        except BaseException as exc:  # recorded so tests can assert on it
            self.errors.append(exc)
        finally:
            conn.record.eof = True
            with self.cond:
                self.cond.notify_all()
            with contextlib.suppress(OSError):
                conn.sock.close()

    @property
    def requests(self) -> list[Request]:
        return [r for c in self.connections for r in c.record.requests]

    def conn(self, index: int = 0) -> ConnRecord:
        return self.connections[index].record

    def wait(self, predicate: Callable[[], bool], timeout: float = 5.0) -> None:
        with self.cond:
            if not self.cond.wait_for(predicate, timeout):
                raise AssertionError("timed out waiting for the test server")

    def wait_eof(self, index: int = 0, timeout: float = 5.0) -> None:
        self.wait(lambda: len(self.connections) > index and self.conn(index).eof, timeout)

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()
        for conn in list(self.connections):
            with contextlib.suppress(OSError):
                conn.sock.shutdown(socket.SHUT_RDWR)
            conn.sock.close()
        for thread in self._threads:
            thread.join(2)

    def __enter__(self) -> H2TestServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
