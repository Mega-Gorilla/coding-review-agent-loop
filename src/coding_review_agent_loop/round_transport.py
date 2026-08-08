"""Bounded, dependency-leaf transport for durable round comments."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from collections.abc import Mapping, Sequence

from .errors import AgentLoopError

MAX_GITHUB_BODY_CHARS = 60_000
ROUND_RESUME_MARKER_RE = re.compile(r"<!--\s*AGENT_LOOP_META:\s*(?P<payload>[A-Za-z0-9+/=_:-]+)\s*-->", re.I)
ROUND_TRANSPORT_SIDECAR_RE = re.compile(r"<!--\s*AGENT_LOOP_SIDECAR:\s*(?P<payload>[A-Za-z0-9+/=_-]+)\s*-->", re.I)
_SPILL_FIELDS = frozenset({"canonical_plan", "raw_structured_coder_response", "canonical_reviewer_response"})
_MAX_COMPRESSED = 8_000_000
_MAX_DECOMPRESSED = 16_000_000
_PART_CHARS = 40_000

def _b64(data: bytes) -> str: return base64.urlsafe_b64encode(data).decode("ascii")
def _unb64(value: str) -> bytes: return base64.urlsafe_b64decode(value.encode("ascii"))

def encode_mapping(payload: Mapping[str, object]) -> str:
    raw = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()
    compressed = zlib.compress(raw, 9)
    if len(compressed) > _MAX_COMPRESSED: raise AgentLoopError("Round metadata is too large to transport safely.")
    return "v1_" + _b64(compressed)

def decode_mapping(encoded: str) -> dict[str, object]:
    try:
        if encoded.startswith("v1_"):
            packed = _unb64(encoded[3:])
            if len(packed) > _MAX_COMPRESSED: raise ValueError("compressed payload too large")
            dec = zlib.decompressobj(); raw = dec.decompress(packed, _MAX_DECOMPRESSED + 1) + dec.flush()
            if len(raw) > _MAX_DECOMPRESSED or dec.unused_data: raise ValueError("decompressed payload too large")
        else:
            raw = _unb64(encoded)
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict): raise ValueError("mapping required")
        return value
    except Exception as exc:
        raise AgentLoopError("Invalid AGENT_LOOP_META payload.") from exc

def is_round_transport_sidecar(body: str) -> bool:
    return bool(ROUND_TRANSPORT_SIDECAR_RE.search(body))

def _sidecar(payload: Mapping[str, object]) -> str:
    return "<!-- AGENT_LOOP_SIDECAR: " + _b64(json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode()) + " -->"

def prepare_round_comment(body: str) -> tuple[str, ...]:
    """Return sidecars followed by anchor; non-round bodies are strictly bounded."""
    if len(body) > MAX_GITHUB_BODY_CHARS:
        match = ROUND_RESUME_MARKER_RE.search(body)
        if not match: raise AgentLoopError(f"GitHub comment body exceeds {MAX_GITHUB_BODY_CHARS} characters; shorten the response.")
    match = ROUND_RESUME_MARKER_RE.search(body)
    if not match: return (body,)
    payload = decode_mapping(match.group("payload")); sidecars: list[str] = []
    anchor_id = hashlib.sha256(body.encode()).hexdigest()[:24]
    for field in _SPILL_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str): continue
        packed = zlib.compress(value.encode(), 9)
        if len(packed) > _MAX_COMPRESSED: raise AgentLoopError(f"Round metadata field {field} is too large to spill safely.")
        # Spill only when retaining it makes the anchor too large.
        trial = dict(payload); trial[field] = {"$round_transport_spill": anchor_id, "field": field, "sha256": hashlib.sha256(value.encode()).hexdigest()}
        trial_body = body[:match.start("payload")] + encode_mapping(trial) + body[match.end("payload"):]
        if len(trial_body) >= len(body) or len(body) <= MAX_GITHUB_BODY_CHARS: continue
        chunks = [_b64(packed)[i:i + _PART_CHARS] for i in range(0, len(_b64(packed)), _PART_CHARS)]
        trial[field]["parts"] = len(chunks)  # type: ignore[index]
        payload[field] = trial[field]
        digest = hashlib.sha256(packed).hexdigest()
        for index, chunk in enumerate(chunks):
            sidecars.append(_sidecar({"v": 1, "anchor": anchor_id, "spill": digest, "field": field, "index": index, "count": len(chunks), "sha256": hashlib.sha256(value.encode()).hexdigest(), "data": chunk}))
    anchor = body[:match.start("payload")] + encode_mapping(payload) + body[match.end("payload"):]
    if len(anchor) > MAX_GITHUB_BODY_CHARS:
        raise AgentLoopError(f"Round comment exceeds {MAX_GITHUB_BODY_CHARS} characters even after metadata spill; shorten the visible response or metadata.")
    if any(len(item) > MAX_GITHUB_BODY_CHARS for item in sidecars): raise AgentLoopError("Round metadata sidecar exceeds GitHub body budget.")
    return (*sidecars, anchor)

def hydrate_mapping(payload: Mapping[str, object], bodies: Sequence[str]) -> tuple[dict[str, object], set[str]]:
    """Hydrate references from an unordered whole comment list; missing fields are reported."""
    parts: dict[tuple[str, str], dict[int, dict[str, object]]] = {}
    for body in bodies:
        for match in ROUND_TRANSPORT_SIDECAR_RE.finditer(body):
            try:
                item = json.loads(_unb64(match.group("payload")).decode())
                key = (str(item["anchor"]), str(item["field"])); index = int(item["index"])
                if not isinstance(item, dict) or index < 0 or int(item["count"]) < 1 or index >= int(item["count"]): continue
                old = parts.setdefault(key, {}).get(index)
                if old is None or old == item: parts[key][index] = item
                else: parts[key].pop(index, None)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError): continue
    result = dict(payload); missing: set[str] = set()
    for field in _SPILL_FIELDS:
        ref = result.get(field)
        if not isinstance(ref, dict) or "$round_transport_spill" not in ref: continue
        anchor, count = str(ref["$round_transport_spill"]), int(ref.get("parts", 0)); entries = parts.get((anchor, field), {})
        if count < 1 or len(entries) != count or any(i not in entries for i in range(count)):
            missing.add(field); result[field] = None; continue
        try:
            ordered = [entries[i] for i in range(count)]
            if any(str(item.get("anchor")) != anchor or str(item.get("field")) != field or int(item.get("count", -1)) != count for item in ordered): raise ValueError
            packed = _unb64("".join(str(item["data"]) for item in ordered))
            if hashlib.sha256(packed).hexdigest() != str(ordered[0]["spill"]): raise ValueError
            raw = zlib.decompress(packed); value = raw.decode()
            if len(raw) > _MAX_DECOMPRESSED or hashlib.sha256(raw).hexdigest() != str(ref["sha256"]): raise ValueError
            result[field] = value
        except Exception:
            missing.add(field); result[field] = None
    return result, missing
