"""HPACK (RFC 7541) in ``pyrock.trace.otlp._h2``: integers, Huffman, decoder, encoder.

The decoder vectors are RFC 7541 Appendix C verbatim (C.2-C.6), including the
dynamic table contents and sizes after each block.
"""

from __future__ import annotations

import pytest

from pyrock.trace.otlp._h2 import (
    HPACKDecoder,
    ProtocolError,
    decode_integer,
    encode_headers,
    encode_integer,
    huffman_decode,
)

# ------------------------------------------------------------- integers


@pytest.mark.parametrize(
    ("value", "prefix", "first_byte", "expected"),
    [
        (10, 5, 0, "0a"),  # C.1.1
        (1337, 5, 0, "1f9a0a"),  # C.1.2
        (42, 8, 0, "2a"),  # C.1.3
        (31, 5, 0, "1f00"),
        (30, 5, 0, "1e"),
        (0, 7, 0x80, "80"),
        (62, 6, 0x40, "7e"),
        (63, 6, 0x40, "7f00"),
        (127, 7, 0, "7f00"),
        (128, 7, 0, "7f01"),
        (4096, 5, 0x20, "3fe11f"),
        (2**21, 4, 0, "0ff1ff7f"),
    ],
)
def test_encode_integer(value: int, prefix: int, first_byte: int, expected: str) -> None:
    assert encode_integer(value, prefix, first_byte).hex() == expected


@pytest.mark.parametrize(
    ("data", "prefix", "value", "consumed"),
    [
        ("0a", 5, 10, 1),
        ("ea", 5, 10, 1),  # high flag bits are not part of the value
        ("1f9a0a", 5, 1337, 3),
        ("2a", 8, 42, 1),
        ("7f00", 7, 127, 2),
        ("3fe11f", 5, 4096, 3),
        ("1f9a0aff", 5, 1337, 3),  # trailing data untouched
    ],
)
def test_decode_integer(data: str, prefix: int, value: int, consumed: int) -> None:
    assert decode_integer(bytes.fromhex(data), 0, prefix) == (value, consumed)


def test_decode_integer_at_offset() -> None:
    assert decode_integer(bytes.fromhex("ff1f9a0a"), 1, 5) == (1337, 4)


@pytest.mark.parametrize(
    "data",
    [
        "",  # nothing at all
        "1f",  # prefix saturated, continuation missing
        "1f9a",  # continuation bit set on last byte
        "1f" + "ff" * 12 + "01",  # absurdly large
    ],
)
def test_decode_integer_errors(data: str) -> None:
    with pytest.raises(ProtocolError):
        decode_integer(bytes.fromhex(data), 0, 5)


def test_integer_round_trip() -> None:
    for prefix in range(1, 9):
        for value in [0, 1, 2**prefix - 2, 2**prefix - 1, 2**prefix, 300, 65535, 2**28]:
            encoded = encode_integer(value, prefix)
            assert decode_integer(encoded, 0, prefix) == (value, len(encoded))


# -------------------------------------------------------------- Huffman

HUFFMAN_VECTORS = [
    ("f1e3c2e5f23a6ba0ab90f4ff", b"www.example.com"),
    ("a8eb10649cbf", b"no-cache"),
    ("25a849e95ba97d7f", b"custom-key"),
    ("25a849e95bb8e8b4bf", b"custom-value"),
    ("6402", b"302"),
    ("aec3771a4b", b"private"),
    ("d07abe941054d444a8200595040b8166e082a62d1bff", b"Mon, 21 Oct 2013 20:13:21 GMT"),
    ("9d29ad171863c78f0b97c8e9ae82ae43d3", b"https://www.example.com"),
    ("640eff", b"307"),
    ("d07abe941054d444a8200595040b8166e084a62d1bff", b"Mon, 21 Oct 2013 20:13:22 GMT"),
    ("9bd9ab", b"gzip"),
    (
        "94e7821dd7f2e6c7b335dfdfcd5b3960d5af27087f3672c1ab270fb5291f9587316065c003ed4ee5b1063d5007",
        b"foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1",
    ),
    ("07", b"0"),
    ("9acac9", b"grpc"),
    ("90a3928354550be4852c8593", b"deadline exceeded"),
    ("", b""),
]


@pytest.mark.parametrize(("encoded", "decoded"), HUFFMAN_VECTORS)
def test_huffman_decode_vectors(encoded: str, decoded: bytes) -> None:
    assert huffman_decode(bytes.fromhex(encoded)) == decoded


# All 256 octets, encoded with the RFC 7541 Appendix B code table.
ALL_OCTETS_HUFFMAN = (
    "ffc7fffd8fffffe2fffffe3fffffe4fffffe5fffffe6fffffe7fffffe8ffffeafffffff3fffffa7fffffabff"
    "ffffdfffffebfffffecfffffedfffffeefffffefffffff0ffffff1ffffff2fffffffbfffffcffffffd3fffffd"
    "7fffffdbfffffdffffffe3fffffe7fffffebfffffed4fe3f9ffaffcabf1febfafefe7fdfd2cbb00089969b71"
    "d79fb9f7fff20ffbff3ff50ddbd7f061c58f265cd9f469d5af66dddbf871e5f9cff7ff7fffc3ff9ffe45fff47"
    "19242cb34e6e9d68a6a3d7dac426defe3cfaf7fffbfe7ffbffdffffffcfffe6ffff4bfff9ffffa3fffd3ffff"
    "53fffd5ffffb3fffeb7fffdaffffb7ffff73fffeeffffdeffffebffffbfffffd9ffffdbfffebffffe0ffffee"
    "ffffc3ffff8bffff1ffffe4fffee7fffb1ffff97fffd9ffffcdffff9fffffbffffdafffeeffff4ffffb7fffe"
    "e7fffe8ffffd3fffdeffffd5fffeeffffbdffffe1fffdfffff7fffff5ffffecffff07fff87fffe0ffff17fff"
    "edffff87ffff77fffeffffeaffff8bfffe3ffff93ffff87fffcbffff37ffff1fffff83ffffe1fffebfffe3ff"
    "ff3fffff2ffffa3ffffd9fffff17ffffc7fffff27ffffdefffffbffffff2fffff8fffffb7fff97fff8fffffe"
    "6fffffc1fffff87ffffe7fffffc5ffffe5fffe4ffff2fffffd1fffff4ffffffefffffe3fffffc9fffff97fff"
    "b3ffffcffffb7fffcdffff4ffff9ffffd1ffffcffffeaffffafffffddffffeffffff4fffff5fffffabffffa7"
    "ffffd7fffff9bffffecfffffb7fffff3fffffe8fffffd3fffffabfffff5fffffff7ffffecfffffdbfffffbbf"
    "ffff7ffffff0fffffbbf"
)


def test_huffman_decode_every_octet() -> None:
    assert huffman_decode(bytes.fromhex(ALL_OCTETS_HUFFMAN)) == bytes(range(256))


@pytest.mark.parametrize(
    "encoded",
    [
        pytest.param("00", id="padding-not-all-ones"),
        pytest.param("ff", id="padding-longer-than-7-bits"),
        pytest.param("ffff", id="sixteen-one-bits"),
        pytest.param("fffffffc", id="eos-symbol"),
        pytest.param("ffffffff", id="eos-symbol-with-padding"),
        pytest.param("a8eb10649c", id="truncated-symbol-zero-padding"),
        pytest.param("a8eb10649cbfff", id="trailing-ones-byte"),
    ],
)
def test_huffman_decode_rejects_invalid(encoded: str) -> None:
    with pytest.raises(ProtocolError):
        huffman_decode(bytes.fromhex(encoded))


# ------------------------------------------------------- decoder: C.2


def test_c2_1_literal_with_indexing() -> None:
    d = HPACKDecoder()
    block = bytes.fromhex("400a637573746f6d2d6b65790d637573746f6d2d686561646572")
    assert d.decode(block) == [("custom-key", "custom-header")]
    assert d.dynamic_table == [("custom-key", "custom-header")]
    assert d.table_size == 55


def test_c2_2_literal_without_indexing() -> None:
    d = HPACKDecoder()
    assert d.decode(bytes.fromhex("040c2f73616d706c652f70617468")) == [(":path", "/sample/path")]
    assert d.dynamic_table == []
    assert d.table_size == 0


def test_c2_3_literal_never_indexed() -> None:
    d = HPACKDecoder()
    assert d.decode(bytes.fromhex("100870617373776f726406736563726574")) == [("password", "secret")]
    assert d.dynamic_table == []


def test_c2_4_indexed() -> None:
    d = HPACKDecoder()
    assert d.decode(bytes.fromhex("82")) == [(":method", "GET")]
    assert d.dynamic_table == []


# --------------------------------------------------- decoder: C.3-C.6

REQ1 = [(":method", "GET"), (":scheme", "http"), (":path", "/"), (":authority", "www.example.com")]
REQ2 = [*REQ1, ("cache-control", "no-cache")]
REQ3 = [
    (":method", "GET"),
    (":scheme", "https"),
    (":path", "/index.html"),
    (":authority", "www.example.com"),
    ("custom-key", "custom-value"),
]
REQ_TABLES = [
    ([(":authority", "www.example.com")], 57),
    ([("cache-control", "no-cache"), (":authority", "www.example.com")], 110),
    (
        [
            ("custom-key", "custom-value"),
            ("cache-control", "no-cache"),
            (":authority", "www.example.com"),
        ],
        164,
    ),
]

C3 = [
    "828684410f7777772e6578616d706c652e636f6d",
    "828684be58086e6f2d6361636865",
    "828785bf400a637573746f6d2d6b65790c637573746f6d2d76616c7565",
]
C4 = [
    "828684418cf1e3c2e5f23a6ba0ab90f4ff",
    "828684be5886a8eb10649cbf",
    "828785bf408825a849e95ba97d7f8925a849e95bb8e8b4bf",
]

DATE1 = "Mon, 21 Oct 2013 20:13:21 GMT"
DATE2 = "Mon, 21 Oct 2013 20:13:22 GMT"
LOC = "https://www.example.com"
COOKIE = "foo=ASDJKHQKBZXOQWEOPIUAXQWEOIU; max-age=3600; version=1"
RESP1 = [(":status", "302"), ("cache-control", "private"), ("date", DATE1), ("location", LOC)]
RESP2 = [(":status", "307"), ("cache-control", "private"), ("date", DATE1), ("location", LOC)]
RESP3 = [
    (":status", "200"),
    ("cache-control", "private"),
    ("date", DATE2),
    ("location", LOC),
    ("content-encoding", "gzip"),
    ("set-cookie", COOKIE),
]
RESP_TABLES = [
    ([("location", LOC), ("date", DATE1), ("cache-control", "private"), (":status", "302")], 222),
    ([(":status", "307"), ("location", LOC), ("date", DATE1), ("cache-control", "private")], 222),
    ([("set-cookie", COOKIE), ("content-encoding", "gzip"), ("date", DATE2)], 215),
]

C5 = [
    (
        "4803333032580770726976617465611d4d6f6e2c203231204f637420323031332032303a31333a323120474d54"
        "6e1768747470733a2f2f7777772e6578616d706c652e636f6d"
    ),
    "4803333037c1c0bf",
    (
        "88c1611d4d6f6e2c203231204f637420323031332032303a31333a323220474d54c05a04677a69707738666f6f"
        "3d4153444a4b48514b425a584f5157454f50495541585157454f49553b206d61782d6167653d333630303b2076"
        "657273696f6e3d31"
    ),
]
C6 = [
    (
        "488264025885aec3771a4b6196d07abe941054d444a8200595040b8166e082a62d1bff6e919d29ad171863c78f"
        "0b97c8e9ae82ae43d3"
    ),
    "4883640effc1c0bf",
    (
        "88c16196d07abe941054d444a8200595040b8166e084a62d1bffc05a839bd9ab77ad94e7821dd7f2e6c7b335df"
        "dfcd5b3960d5af27087f3672c1ab270fb5291f9587316065c003ed4ee5b1063d5007"
    ),
]


@pytest.mark.parametrize("blocks", [C3, C4], ids=["C.3-plain", "C.4-huffman"])
def test_request_sequence(blocks: list[str]) -> None:
    d = HPACKDecoder()
    for block, expected, (table, size) in zip(blocks, [REQ1, REQ2, REQ3], REQ_TABLES, strict=True):
        assert d.decode(bytes.fromhex(block)) == expected
        assert d.dynamic_table == table
        assert d.table_size == size


@pytest.mark.parametrize("blocks", [C5, C6], ids=["C.5-plain", "C.6-huffman"])
def test_response_sequence_with_eviction(blocks: list[str]) -> None:
    d = HPACKDecoder(max_table_size=256)
    assert d.max_table_size == 256
    for block, expected, (table, size) in zip(
        blocks, [RESP1, RESP2, RESP3], RESP_TABLES, strict=True
    ):
        assert d.decode(bytes.fromhex(block)) == expected
        assert d.dynamic_table == table
        assert d.table_size == size


# ----------------------------------------- dynamic table: sizes and updates


def _literal_indexed(name: str, value: str) -> bytes:
    """Literal with incremental indexing, new name, no Huffman."""
    return b"\x40" + _s(name) + _s(value)


def _s(text: str) -> bytes:
    data = text.encode()
    assert len(data) < 127
    return bytes([len(data)]) + data


def test_default_table_size_is_4096() -> None:
    d = HPACKDecoder()
    assert d.max_table_size == 4096
    assert d.table_size == 0


def test_size_update_to_zero_evicts_everything() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("a", "1") + _literal_indexed("b", "2"))
    assert d.table_size == 2 * 34
    assert d.decode(b"\x20") == []
    assert d.dynamic_table == []
    assert d.table_size == 0
    assert d.max_table_size == 0


def test_size_update_shrinks_and_evicts_oldest() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("a", "1") + _literal_indexed("b", "2") + _literal_indexed("c", "3"))
    # 0x20 | 31, then 69 - 31 = 38: room for two 34-octet entries.
    assert d.decode(bytes([0x3F, 69 - 31])) == []
    assert d.dynamic_table == [("c", "3"), ("b", "2")]
    assert d.table_size == 68
    assert d.max_table_size == 69


def test_size_update_then_headers_in_same_block() -> None:
    d = HPACKDecoder()
    block = b"\x20" + b"\x3f\xe1\x1f" + _literal_indexed("k", "v") + b"\xbe"
    assert d.decode(block) == [("k", "v"), ("k", "v")]
    assert d.max_table_size == 4096
    assert d.dynamic_table == [("k", "v")]


def test_size_update_above_limit_is_error() -> None:
    d = HPACKDecoder(max_table_size=256)
    with pytest.raises(ProtocolError):
        d.decode(bytes.fromhex("3fe11f"))  # 4096 > 256


def test_size_update_after_header_is_error() -> None:
    d = HPACKDecoder()
    with pytest.raises(ProtocolError):
        d.decode(b"\x82\x20")


def test_entry_larger_than_table_empties_it() -> None:
    d = HPACKDecoder(max_table_size=64)
    d.decode(_literal_indexed("a", "1"))
    assert d.table_size == 34
    assert d.decode(_literal_indexed("k" * 20, "v" * 20)) == [("k" * 20, "v" * 20)]
    assert d.dynamic_table == []
    assert d.table_size == 0


def test_eviction_keeps_size_within_limit() -> None:
    d = HPACKDecoder(max_table_size=100)
    for i in range(20):
        d.decode(_literal_indexed(f"n{i:02d}", "x"))
        assert d.table_size <= 100
    assert d.dynamic_table == [("n19", "x"), ("n18", "x")]


def test_dynamic_table_indexing_newest_first() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("a", "1") + _literal_indexed("b", "2"))
    assert d.decode(b"\xbe\xbf") == [("b", "2"), ("a", "1")]


def test_literal_with_indexed_dynamic_name() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("x-name", "1"))
    # Literal without indexing, name index 62 (4-bit prefix: 0x0f then 62 - 15 = 47).
    assert d.decode(bytes([0x0F, 47]) + _s("2")) == [("x-name", "2")]


def test_set_max_table_size_evicts() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("a", "1") + _literal_indexed("b", "2"))
    d.set_max_table_size(40)
    assert d.dynamic_table == [("b", "2")]
    with pytest.raises(ProtocolError):
        d.decode(bytes([0x3F, 41 - 31]))  # above the new protocol limit


def test_decoder_state_persists_across_blocks() -> None:
    d = HPACKDecoder()
    d.decode(_literal_indexed("content-type", "application/grpc"))
    assert d.decode(b"\x88\xbe") == [(":status", "200"), ("content-type", "application/grpc")]


@pytest.mark.parametrize("index", [1, 2, 8, 14, 31, 61])
def test_static_table_entries(index: int) -> None:
    expected = {
        1: (":authority", ""),
        2: (":method", "GET"),
        8: (":status", "200"),
        14: (":status", "500"),
        31: ("content-type", ""),
        61: ("www-authenticate", ""),
    }[index]
    assert HPACKDecoder().decode(encode_integer(index, 7, 0x80)) == [expected]


@pytest.mark.parametrize(
    "block",
    [
        pytest.param("80", id="index-zero"),
        pytest.param("be", id="index-62-empty-table"),
        pytest.param("ff00", id="index-beyond-table"),
        pytest.param("41", id="truncated-literal"),
        pytest.param("4005616263", id="string-shorter-than-length"),
        pytest.param("00", id="literal-missing-name"),
        pytest.param("0003616263", id="literal-missing-value"),
        pytest.param("0081ff0161", id="bad-huffman-name"),
        pytest.param("4f", id="indexed-name-truncated"),
    ],
)
def test_decoder_errors(block: str) -> None:
    with pytest.raises(ProtocolError):
        HPACKDecoder().decode(bytes.fromhex(block))


def test_decoder_huffman_name_and_value() -> None:
    # Literal without indexing, new Huffman-coded name and value.
    block = bytes.fromhex("00") + bytes([0x83]) + bytes.fromhex("9acac9") + bytes([0x81, 0x07])
    assert HPACKDecoder().decode(block) == [("grpc", "0")]


def test_decoder_empty_block() -> None:
    assert HPACKDecoder().decode(b"") == []


# --------------------------------------------------------------- encoder

STATIC_NAMES = [
    ":authority",
    ":method",
    ":method",
    ":path",
    ":path",
    ":scheme",
    ":scheme",
    ":status",
    ":status",
    ":status",
    ":status",
    ":status",
    ":status",
    ":status",
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
assert len(STATIC_NAMES) == 61


def _parse_literals(block: bytes) -> list[tuple[str, str]]:
    """Parse a block that must contain only literal-without-indexing, non-Huffman fields."""
    out: list[tuple[str, str]] = []
    i = 0

    def string() -> str:
        nonlocal i
        assert not block[i] & 0x80, "Huffman bit set"
        n, i = decode_integer(block, i, 7)
        text = block[i : i + n].decode()
        i += n
        return text

    static = dict(enumerate(STATIC_NAMES, start=1))
    while i < len(block):
        assert block[i] & 0xF0 == 0x00, f"not literal-without-indexing: {block[i]:#x}"
        index, i = decode_integer(block, i, 4)
        name = string() if index == 0 else static.get(index, f"#{index}")
        out.append((name, string()))
    return out


HEADERS = [
    (":method", "POST"),
    (":scheme", "http"),
    (":path", "/opentelemetry.proto.collector.trace.v1.TraceService/Export"),
    (":authority", "127.0.0.1:4317"),
    ("content-type", "application/grpc"),
    ("te", "trailers"),
    ("x-key", "v" * 300),
]


def test_encode_headers_is_literal_without_indexing_and_no_huffman() -> None:
    assert _parse_literals(encode_headers(HEADERS)) == HEADERS


def test_encode_headers_round_trips_and_leaves_table_empty() -> None:
    d = HPACKDecoder()
    assert d.decode(encode_headers(HEADERS)) == HEADERS
    assert d.dynamic_table == []


def test_encode_headers_simple_literal_bytes() -> None:
    block = encode_headers([("grpc-status", "0")])
    assert block == b"\x00\x0bgrpc-status\x010"


def test_encode_headers_empty() -> None:
    assert encode_headers([]) == b""


def test_encode_headers_non_ascii_value_round_trips() -> None:
    headers = [("x-utf8", "café")]
    assert HPACKDecoder().decode(encode_headers(headers)) == headers
