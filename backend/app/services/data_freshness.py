"""Measured source age, separate from HTTP latency and collection time."""
from datetime import datetime, timezone


def freshness_observation(fact, now=None, target_seconds=5):
    now = now or datetime.now(timezone.utc)
    stamp = fact.source_timestamp
    result = {"source_timestamp": stamp.isoformat() if stamp else None,
              "captured_at": fact.snapshot_time.isoformat(), "target_seconds": target_seconds,
              "source_age_seconds": None, "seconds_level_verified": False}
    if stamp is None:
        result["status"] = "source_timestamp_unavailable"
        return result
    age = (now - stamp).total_seconds()
    result["source_age_seconds"] = round(age, 3)
    result["status"] = "clock_error" if age < -1 else "within_target" if age <= target_seconds else "delayed"
    result["seconds_level_verified"] = -1 <= age <= target_seconds
    return result
