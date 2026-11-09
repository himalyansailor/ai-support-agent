"""Voice metrics. Labels are low-cardinality and never carry a phone number, transcript or token."""

from prometheus_client import Counter, Gauge, Histogram

VOICE_CALLS = Counter(
    "support_voice_calls_total",
    "Voice calls finished, by source (phone | browser | callback), mode (chained | speech_to_speech | none) and end "
    "reason",
    ["source", "mode", "end_reason"],
)
VOICE_ACTIVE_CALLS = Gauge(
    "support_voice_active_calls",
    "Voice sessions running in this process",
)
VOICE_RESPONSE_LATENCY = Histogram(
    "support_voice_response_latency_seconds",
    "Time from the caller finishing speaking (chained) or their transcript (speech-to-speech) to the first audio "
    "frame of the reply sent (chained) or queued for the transport (speech-to-speech)",
    buckets=(0.25, 0.5, 0.75, 1, 1.5, 2, 3, 4, 6, 8),
)
VOICE_PROVIDER_ERRORS = Counter(
    "support_voice_provider_errors_total",
    "Voice provider failures, by provider (twilio | deepgram | openai | openai_realtime) and error type",
    ["provider", "error_type"],
)


def mode_label(mode: str | None) -> str:
    """The `mode` label of a call: its Call.mode, `none` for a call without one (an outbound callback)."""
    return mode or "none"
