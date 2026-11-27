"""Bounded reference snapshots in the existing consented BlackBox journal."""
from base64 import b64decode, b64encode
from hashlib import sha256
import json

from _kara_blackbox import clean, events

CHUNK_BYTES = 8192
MAX_BYTES = 1024 * 1024


def snapshot(audio, source_kind, original, displayed, reference=None):
    """Keep the original text and historical display and scoring references."""
    result = {"version": 1, "audio": audio, "source": source_kind,
              "original": original, "displayed": displayed, "historical": None}
    if reference:
        result["historical"] = {key: reference.get(key) for key in (
            "song_id", "prepared_at", "audio", "lrc_sha256", "source_order",
            "language", "duration", "method", "settings", "model", "lines")}
    return result


def record(box, session, context, document):
    # The existing secret filter runs before encoding. The envelope carries
    # only sanitized JSON and remains smaller than the V3 event limit.
    raw = json.dumps(clean(document), ensure_ascii=False, allow_nan=False,
                     separators=(",", ":")).encode()
    if len(raw) > MAX_BYTES:
        box.emit("lyrics", "reference-unavailable", **context, recording_session=session,
                 reason="snapshot-too-large")
        return
    identity = sha256(raw).hexdigest()
    count = (len(raw) + CHUNK_BYTES - 1) // CHUNK_BYTES
    for index in range(count):
        encoded = b64encode(raw[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]).decode("ascii").rstrip("=")
        box.emit("lyrics", "reference-chunk", **context, recording_session=session,
                 snapshot_sha256=identity, index=index, count=count, payload=encoded)


def load(folder, turn):
    """Read a complete snapshot for one turn. A queue loss is visible."""
    parts = []
    identity = count = None
    last = None
    complete = False
    for row in events(folder / "events.jsonl"):
        if row["component"] != "lyrics" or row.get("turn") != turn or row["event"] != "reference-chunk":
            continue
        data = row["data"]
        current, index, total = (data.get(k) for k in ("snapshot_sha256", "index", "count"))
        if (not isinstance(current, str) or len(current) != 64 or type(index) is not int
                or type(total) is not int or not 0 <= index < total <= MAX_BYTES // CHUNK_BYTES):
            raise ValueError("Enveloppe de référence BlackBox invalide.")
        if complete and identity == current:
            continue
        if index == 0 or identity != current:
            parts, identity, count, complete = [], current, total, False
        if count != total or index != len(parts):
            continue
        payload = data.get("payload")
        if not isinstance(payload, str) or len(payload) > 4 * ((CHUNK_BYTES + 2) // 3):
            raise ValueError("Fragment de référence BlackBox invalide.")
        part = b64decode(payload + "=" * (-len(payload) % 4), validate=True)
        if not (0 < len(part) <= CHUNK_BYTES) or index + 1 < total and len(part) != CHUNK_BYTES:
            raise ValueError("Taille de fragment BlackBox invalide.")
        parts.append(part)
        if len(parts) == count:
            raw = b"".join(parts)
            if sha256(raw).hexdigest() != identity:
                raise ValueError("Empreinte de référence BlackBox invalide.")
            document = json.loads(raw)
            if not isinstance(document, dict) or document.get("version") != 1:
                raise ValueError("Version de référence BlackBox invalide.")
            last, complete = {"sha256": identity, "reference": document}, True
    if last is not None and complete:
        return last
    raise ValueError("Référence du tour absente ou incomplète dans cette capture.")
