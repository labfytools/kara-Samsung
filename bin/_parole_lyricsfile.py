"""Conversion des débuts observés de Lyricsfile 1.0 vers Enhanced LRC."""

from _paroles_lrc import MAX_LRC_BYTES, WORD_STAMP


def enhanced(raw, duration):
    if not isinstance(raw, str) or len(raw) > 256_000 or len(raw.encode()) > 256_000:
        return None
    try:
        import yaml
    except ImportError:
        # LRCLIB's line LRC remains independently available.
        return None

    class Restricted(yaml.SafeLoader):
        depth = 0

        def compose_node(self, parent, index):
            self.depth += 1
            try:
                event = self.peek_event()
                if self.depth > 12 or getattr(event, "anchor", None) or getattr(event, "tag", None):
                    raise ValueError("Structure Lyricsfile non prise en charge")
                return super().compose_node(parent, index)
            finally:
                self.depth -= 1

        def construct_mapping(self, node, deep=False):
            result = super().construct_mapping(node, deep=deep)
            if len(result) != len(node.value):
                raise ValueError("Clé Lyricsfile répétée")
            return result

    def timestamp(ms, left, right):
        minutes, rest = divmod(ms, 60_000)
        seconds, millis = divmod(rest, 1000)
        return f"{left}{minutes:02d}:{seconds:02d}.{millis:03d}{right}"

    def segment(item, minimum):
        if not isinstance(item, dict):
            raise ValueError("Segment Lyricsfile invalide")
        text, start, end = item.get("text"), item.get("start_ms"), item.get("end_ms")
        if (not isinstance(text, str) or any(c in text for c in "\0\r\n") or WORD_STAMP.search(text)
                or type(start) is not int or not minimum <= start <= (duration + 3) * 1000
                or end is not None and (type(end) is not int or not start <= end <= (duration + 3) * 1000)):
            raise ValueError("Temps ou texte Lyricsfile hors limites")
        return text, start

    try:
        data = yaml.load(raw, Loader=Restricted)
        if (not isinstance(data, dict) or data.get("version") != "1.0"
                or not isinstance(data.get("metadata"), dict) or data["metadata"].get("offset_ms") not in (None, 0)
                or not isinstance(data.get("lines"), list)):
            return None
        lines, start = [], 0
        for line in data["lines"]:
            text, start = segment(line, start)
            if text.lstrip().startswith("["):
                return None
            words = line.get("words")
            if words:
                if not isinstance(words, list):
                    return None
                parts, chunks, onset = [], [], start
                for word in words:
                    chunk, onset = segment(word, onset)
                    if line.get("end_ms") is not None and onset > line["end_ms"]:
                        return None
                    parts.append(chunk)
                    chunks.append(timestamp(onset, "<", ">") + chunk)
                # Preserve every character of the source line and only
                # export observed starts. Missing word boundaries stay absent.
                if "".join(parts) != text:
                    return None
                text = "".join(chunks)
            lines.append(timestamp(start, "[", "]") + text)
        result = "\n".join(lines)
        return result if WORD_STAMP.search(result) and len(result.encode()) <= MAX_LRC_BYTES else None
    except (yaml.YAMLError, ValueError, TypeError):
        return None
