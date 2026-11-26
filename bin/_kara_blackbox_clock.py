"""Conversions for the software clocks retained by BlackBox."""
import math


def browser_anchor(sent_ns, received_ns, payload):
    """Bound the browser-to-server offset assuming nonnegative transport delays.

    The Web Audio reading describes the renderer, with a render quantum and
    unknown input latency between the sample and its acoustic origin.
    """
    if not isinstance(payload, dict):
        return None
    keys = ("received_ms", "context_begin_ms", "context_end_ms", "sent_ms", "context_frame")
    values = [payload.get(key) for key in keys]
    if any(type(value) not in (int, float) or not 0 <= value < 2 ** 53 for value in values):
        return None
    received, first, last, sent, frame = values
    if type(frame) is not int or not received <= first <= last <= sent or sent - received > 100:
        return None
    lower = sent_ns - round(received * 1_000_000)
    upper = received_ns - round(sent * 1_000_000)
    if not 0 <= upper - lower <= 2_000_000_000:
        return None
    return {"server_sent_ns": sent_ns, "server_received_ns": received_ns,
            **dict(zip(keys, values)), "offset_min_ns": lower, "offset_max_ns": upper,
            "offset_estimate_ns": (lower + upper) // 2, "offset_uncertainty_ns": math.ceil((upper - lower) / 2),
            "renderer_frame_min_ns": lower + round(first * 1_000_000),
            "renderer_frame_max_ns": upper + round(last * 1_000_000),
            "physical_bound_ns": None}
