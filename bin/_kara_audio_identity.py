"""Identité PCM utilisée pour réassocier un cache sans se fier aux tags."""
from hashlib import sha256
import json
import subprocess


def pcm_digest(path):
    common = ("-v", "error", "-protocol_whitelist", "file,pipe")
    probe = subprocess.run(("ffprobe", *common, "-select_streams", "a:0", "-show_entries",
                            "stream=sample_rate,channels,channel_layout", "-of", "json", str(path)),
                           check=True, capture_output=True, timeout=10)
    stream = json.loads(probe.stdout)["streams"][0]
    # The demuxer's skips (e.g. Opus preskip) and PCM order are part of the
    # identity. Compressed tag or packet hashes alone cannot prove timing.
    pcm = subprocess.run(("ffmpeg", *common, "-xerror", "-nostdin", "-i", str(path),
                          "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "hash", "-hash", "sha256", "pipe:1"),
                         check=True, capture_output=True, timeout=90)
    checksum = pcm.stdout.strip()
    if not checksum.startswith(b"SHA256=") or len(checksum) != 71:
        raise ValueError("Identité du flux PCM absente")
    return sha256(json.dumps(stream, sort_keys=True).encode() + checksum).hexdigest()
