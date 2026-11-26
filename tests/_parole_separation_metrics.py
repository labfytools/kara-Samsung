"""Mesures hors ligne pour le comparatif de transcription."""
from collections import Counter
from pathlib import Path


def edit_distance(expected, observed):
    """Levenshtein bit-parallel, with one bit per reference character."""
    if not expected:
        return len(observed)
    positions = {}
    for i, char in enumerate(expected):
        positions[char] = positions.get(char, 0) | (1 << i)
    top = 1 << (len(expected) - 1)
    positive, negative = ~0, 0
    distance = len(expected)
    for char in observed:
        equal = positions.get(char, 0)
        vertical = equal | negative
        horizontal = (((equal & positive) + positive) ^ positive) | equal
        up = negative | ~(horizontal | positive)
        down = positive & horizontal
        distance += bool(up & top) - bool(down & top)
        up, down = (up << 1) | 1, down << 1
        positive, negative = down | ~(vertical | up), up & vertical
    return distance


def word_details(expected, heard, argot):
    observed = [word.text for word in heard]
    reference_text, observed_text = " ".join(expected), " ".join(observed)
    errors = edit_distance(reference_text, observed_text)
    jargon, found = Counter(w for w in expected if w in argot), Counter(w for w in observed if w in argot)
    uncertain = [word.start for word in heard if word.confidence < .62]
    return dict(characters=len(reference_text), character_errors=errors,
                cer=errors / len(reference_text) if reference_text else None,
                uncertain_words=len(uncertain),
                uncertain_passages=sum(a - b > .75 for a, b in zip(uncertain, [-2., *uncertain])),
                argot_reference=jargon.total(), argot_hits=(jargon & found).total())


def process_tree(pid):
    """Sample live descendants, with PSS and per-client DRM where provided."""
    proc = Path("/proc") / str(pid)
    try:
        status = (proc / "status").read_text().splitlines()
        children = (proc / "task" / str(pid) / "children").read_text().split()
    except (FileNotFoundError, ProcessLookupError):
        return []
    fields = {line.split(":")[0]: int(line.split()[1]) for line in status
              if line.startswith(("VmRSS:", "RssAnon:", "VmSwap:"))}
    fields["pid"] = int(pid)
    try:
        fields["Pss"] = next(int(line.split()[1]) for line in (proc / "smaps_rollup").read_text().splitlines()
                             if line.startswith("Pss:"))
    except (OSError, StopIteration):
        pass
    clients = {}
    for info in (proc / "fdinfo").glob("[0-9]*"):
        try:
            data = dict(line.split(":", 1) for line in info.read_text().splitlines() if line.startswith("drm-"))
            client = data.get("drm-client-id")
            if client and data.get("drm-driver", "").strip() == "amdgpu":
                clients[client.strip()] = {key: int(data[key].split()[0])
                                           for key in ("drm-memory-gtt", "drm-resident-gtt",
                                                       "drm-memory-vram", "drm-resident-vram") if key in data}
        except (OSError, ValueError):
            continue
    fields["drm_kib"] = clients
    return [fields, *(child for worker in children for child in process_tree(worker))]


def system_reclaim():
    facts = {}
    for device in Path("/sys/block").glob("zram*"):
        try:
            occupied, encoded, allocated = map(int, (device / "mm_stat").read_text().split()[:3])
            facts[device.name] = {"stored_bytes": occupied, "compressed_bytes": encoded, "ram_bytes": allocated}
        except (OSError, ValueError):
            continue
    fields = {"pswpin", "pswpout", "pgscan_direct", "pgscan_kswapd"}
    return {"zram": facts, "counters": {key: int(value) for key, value in (
        line.split() for line in Path("/proc/vmstat").read_text().splitlines()) if key in fields}}
