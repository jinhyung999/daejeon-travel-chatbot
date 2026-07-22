"""Strict JSON helpers for preserving restaurant recommendation metadata."""

import json


def _reject_nonstandard_constant(value):
    raise ValueError(f"nonstandard JSON constant: {value}")


def load_json_object(raw_json, *, label="extra_json"):
    try:
        value = json.loads(
            raw_json or "{}",
            parse_constant=_reject_nonstandard_constant,
        )
    except (TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} must contain valid standard JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def dump_json_object(value, *, label="extra_json"):
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must contain standard JSON values") from error


def merge_recommendation_object(
    primary_raw,
    secondary_raw,
    *,
    prefer_secondary=False,
):
    """Merge durable recommendation fields from secondary into primary JSON.

    Primary values win by default; callers may prefer secondary recommendation
    fields when secondary is the durable source. Other top-level keys retain the
    primary payload's replacement semantics.
    """
    primary = load_json_object(primary_raw)
    secondary = load_json_object(secondary_raw)
    secondary_recommendation = secondary.get("recommendation")
    if secondary_recommendation is None:
        return dump_json_object(primary)
    if not isinstance(secondary_recommendation, dict):
        raise ValueError("extra_json.recommendation must contain a JSON object")

    primary_recommendation = primary.get("recommendation", {})
    if not isinstance(primary_recommendation, dict):
        raise ValueError("extra_json.recommendation must contain a JSON object")
    if prefer_secondary:
        merged_recommendation = dict(primary_recommendation)
        merged_recommendation.update(secondary_recommendation)
    else:
        merged_recommendation = dict(secondary_recommendation)
        merged_recommendation.update(primary_recommendation)
    primary["recommendation"] = merged_recommendation
    return dump_json_object(primary)
