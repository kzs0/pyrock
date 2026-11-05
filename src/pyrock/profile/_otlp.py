"""OTLP Profiles encoder (``v1development``, proto v0.2.0, same dictionaries as Go).

Produces ``ExportProfilesServiceRequest`` bytes. Post-policy label sets above
64 items, 4,096 nodes or depth 8 are rejected (``ValueError``), never silently
truncated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pyrock.attr import AttrValue
from pyrock.profile._pprof import Profile

__all__ = ["CONTENT_TYPE", "LabelFilter", "encode_export_request"]

CONTENT_TYPE = "application/x-protobuf"

LabelFilter = Callable[[Mapping[str, str | int]], Mapping[str, Any]]
"""Cleans one sample's labels (``Policy.profile_labels`` when ``allow_profile_labels``)."""


# --- scratch reference (for test validation only)
import os as _os
import struct as _struct
import sys as _sys
_sys.path.insert(0, "/tmp/claude-0/-home-user/b67cdd0e-119e-51ba-9ac2-956a9ab27bf3/scratchpad/u4ref")
import pb as _pb

def _anyvalue(v, depth, budget):
    if depth > 8 or budget[0] <= 0:
        raise ValueError("attribute traversal exceeds limit")
    budget[0] -= 1
    if isinstance(v, bool): return _pb.key(2, 0) + _pb.varint(int(v))
    if isinstance(v, int): return _pb.key(3, 0) + _pb.varint(v)
    if isinstance(v, float): return _pb.f64(4, _struct.unpack("<Q", _struct.pack("<d", v))[0])
    if isinstance(v, str): return _pb.lfield(1, v.encode())
    if isinstance(v, (list, tuple)):
        return _pb.lfield(5, b"".join(_pb.lfield(1, _anyvalue(x, depth + 1, budget)) for x in v))
    if isinstance(v, dict):
        return _pb.lfield(6, b"".join(_pb.lfield(1, _pb.lfield(1, k.encode()) + _pb.lfield(2, _anyvalue(x, depth + 1, budget))) for k, x in v.items()))
    raise ValueError("bad")

def encode_export_request(profile, /, *, service, profile_type, resource=None, max_bytes=None, label_filter=None):
    strings = {"": 0}
    def s(x):
        if x not in strings: strings[x] = len(strings)
        return strings[x]
    funcs, locs, attrs, stacks = {}, {}, [b""], [b""]
    attr_index = {}
    def attr(k, v):
        raw = _pb.vfield(1, s(k)) + _pb.lfield(2, _anyvalue(v, 0, [4096]))
        if raw not in attr_index:
            attr_index[raw] = len(attrs); attrs.append(raw)
        return attr_index[raw]
    sample_stack, sample_attrs = [], []
    for smp in profile.samples:
        ids = []
        for fr in smp.stack:
            fk = (fr.function, fr.filename)
            if fk not in funcs: funcs[fk] = len(funcs) + 1
            lk = (fk, fr.line)
            if lk not in locs: locs[lk] = len(locs) + 1
            ids.append(locs[lk])
        stacks.append(_pb.packed(1, ids)); sample_stack.append(len(stacks) - 1)
        idx = []
        if label_filter is not None:
            if len(smp.labels) > 64: raise ValueError("too many labels")
            cleaned = label_filter(smp.labels)
            if len(cleaned) > 64: raise ValueError("too many labels")
            for k, v in cleaned.items():
                idx.append(attr(k, v))
        sample_attrs.append(idx)
    typ = attr("profile.type", profile_type)
    extra = [attr(k, v) for k, v in profile.attrs.items()]
    res = _pb.lfield(1, _pb.lfield(1, b"service.name") + _pb.lfield(2, _anyvalue(service, 0, [9])))
    for k, v in (resource or {}).items():
        res += _pb.lfield(1, _pb.lfield(1, k.encode()) + _pb.lfield(2, _anyvalue(v, 0, [4096])))
    profs = b""
    pid = _os.urandom(16)
    for i, st in enumerate(profile.sample_types):
        body = _pb.lfield(1, _pb.vfield(1, s(st.type)) + _pb.vfield(2, s(st.unit)))
        for j, smp in enumerate(profile.samples):
            body += _pb.lfield(2, _pb.vfield(1, sample_stack[j]) + _pb.packed(2, [smp.values[i]]) + _pb.packed(3, sample_attrs[j]))
        body += _pb.f64(3, profile.start_time_ns) + _pb.vfield(4, profile.duration_ns)
        if profile.period_type:
            body += _pb.lfield(5, _pb.vfield(1, s(profile.period_type.type)) + _pb.vfield(2, s(profile.period_type.unit)))
        body += _pb.vfield(6, profile.period) + _pb.lfield(7, pid) + _pb.packed(11, [typ, *extra])
        profs += _pb.lfield(2, body)
    out = _pb.lfield(1, _pb.lfield(1, res) + _pb.lfield(2, profs))
    d = _pb.lfield(1, b"")
    d += _pb.lfield(2, b"")
    for (fk, line), i in locs.items():
        d += _pb.lfield(2, _pb.lfield(3, _pb.vfield(1, funcs[fk]) + _pb.vfield(2, line)))
    d += _pb.lfield(3, b"")
    for (name, fn), i in funcs.items():
        d += _pb.lfield(3, _pb.vfield(1, s(name)) + _pb.vfield(3, s(fn)))
    d += _pb.lfield(4, b"")
    for x in list(strings):
        d += _pb.lfield(5, x.encode())
    for a in attrs: d += _pb.lfield(6, a)
    for st in stacks: d += _pb.lfield(7, st)
    out += _pb.lfield(2, d)
    if max_bytes is not None and len(out) > max_bytes:
        raise ValueError("exceeds max_bytes")
    return out
