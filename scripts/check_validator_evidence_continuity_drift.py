#!/usr/bin/env python3
"""Compare two public-safe validator evidence snapshots for continuity drift.

Usage:
  python3 scripts/check_validator_evidence_continuity_drift.py previous.json current.json --pretty
  python3 scripts/check_validator_evidence_continuity_drift.py - current.json --pretty < previous.json
  python3 scripts/check_validator_evidence_continuity_drift.py --self-test --pretty

The checker is stdlib-only and offline. It compares exactly two public-safe
validator or metrics evidence snapshots/manifests and emits deterministic JSON
with one of: pass, block, or needs_human_review. It checks artifact identity,
event-time ordering, freshness progression, replay/provenance hash continuity,
coverage-window continuity, degraded-mode consistency, monotonic public
counters, forbidden private fields, and future-leakage markers without printing
sensitive values.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit


TOOL_NAME = "validator_evidence_continuity_drift_checker"
TOOL_VERSION = "1.0"
VERDICT_CONTRACT = ["pass", "needs_human_review", "block"]
VERDICT_ORDER = {"pass": 0, "needs_human_review": 1, "block": 2}

REQUIRED_TOP_LEVEL_FIELDS = {
    "snapshot_version",
    "snapshot_id",
    "subject",
    "generated_at_utc",
    "freshness",
    "coverage",
    "replay",
    "provenance",
    "degradation",
    "public_counters",
    "forbidden_field_exclusions",
}

REQUIRED_SUBJECT_FIELDS = {"subject_id", "artifact_kind", "pipeline_id"}

REQUIRED_EXCLUSIONS = {
    "wallet_material",
    "auth_headers",
    "cookies",
    "private_logs",
    "pnl",
    "trading_signals",
    "proprietary_thresholds",
    "live_validator_secrets",
}

ALLOWED_FRESHNESS = {"fresh", "stale", "unknown", "unsupported"}
ALLOWED_COVERAGE = {"complete", "partial", "unknown", "unsupported"}
ALLOWED_REPLAY = {"replayable", "not_replayable", "unknown", "unsupported"}
ALLOWED_PROVENANCE = {"public", "public_synthetic", "documented", "unknown", "unsupported"}
ALLOWED_DEGRADED_REASON_CODES = {
    "none",
    "source_stale",
    "coverage_gap",
    "coverage_partial",
    "freshness_stale",
    "replay_unavailable",
    "provenance_incomplete",
    "synthetic_fixture_only",
    "unsupported_public_source",
}

FORBIDDEN_KEY_PATTERNS = [
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("wallet_material", r"(^|[_-])(wallet|seed|mnemonic|private[_-]?key)([_-]|$)"),
        ("auth_material", r"(^|[_-])(auth|authorization|bearer|credential|jwt|oauth|password|secret|session|token)([_-]|$)"),
        ("browser_session_material", r"(^|[_-])(cookie|csrf|xsrf|local[_-]?storage|session[_-]?storage)([_-]|$)"),
        ("private_runtime_data", r"(^|[_-])(private[_-]?log|raw[_-]?log|trace|terminal[_-]?scrollback)([_-]|$)"),
        ("trading_result_data", r"(^|[_-])(pnl|profit[_-]?and[_-]?loss|account[_-]?equity|position[_-]?size)([_-]|$)"),
        ("trading_signal_data", r"(^|[_-])(alpha|signal|trade[_-]?trigger|execution|order[_-]?instruction)([_-]|$)"),
        ("proprietary_threshold_data", r"(^|[_-])(threshold|thresholds|model[_-]?weight|feature[_-]?weight|calibration[_-]?internal)([_-]|$)"),
        ("live_validator_secret", r"(^|[_-])(validator[_-]?secret|validator[_-]?key|node[_-]?secret|signing[_-]?key)([_-]|$)"),
    )
]

FORBIDDEN_VALUE_PATTERNS = [
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("wallet_material", r"(wallet seed|seed phrase|mnemonic|private key)"),
        ("auth_material", r"(auth header|authorization header|bearer token|api key|session token|oauth token)"),
        ("browser_session_material", r"(browser cookie|local storage|session storage|csrf token|xsrf token)"),
        ("private_runtime_data", r"(private log|raw terminal|full terminal scrollback|private payload)"),
        ("trading_result_data", r"(\bpnl\b|profit and loss|private account equity|position sizing)"),
        ("trading_signal_data", r"(trading signal|trade trigger|execution advice|order instruction)"),
        ("proprietary_threshold_data", r"(proprietary threshold|model threshold|feature weight|calibration secret)"),
        ("live_validator_secret", r"(validator secret|validator private key|node signing key|live validator key)"),
    )
]

UNSAFE_QUERY_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(^|[_-])(access|auth|bearer|credential|jwt|key|password|secret|seed|session|signature|signed|token)([_-]|$)",
        r"(^|[_-])(cookie|csrf|xsrf|sid|sso|ticket|private|mnemonic|wallet)([_-]|$)",
        r"^x-amz-(credential|security-token|signature|expires|algorithm|signedheaders)$",
        r"^oauth[_-]?.*(code|state|token)$",
    )
]

AUTH_BOUND_PATH_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"/(login|signin|sign-in|auth|oauth|sso)(/|$)",
        r"/(private|account|settings|billing|checkout|admin)(/|$)",
        r"/api/auth/",
    )
]

VALUE_SCAN_EXEMPT_PATH_PREFIXES = {"previous.forbidden_field_exclusions[", "current.forbidden_field_exclusions["}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


def path_join(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def finding(code: str, severity: str, path: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, path=path or "$", message=message)


def scalar_text(value: Any) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()[:240]


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def read_json_input(path: str | None) -> Any:
    if path in {None, "-"}:
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def as_dict(value: Any, path: str, findings: list[Finding]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    findings.append(finding("object_required", "review", path, "Expected a JSON object."))
    return {}


def as_list(value: Any, path: str, findings: list[Finding]) -> list[Any]:
    if isinstance(value, list):
        return value
    findings.append(finding("list_required", "review", path, "Expected a JSON array."))
    return []


def normalized_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def validate_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def scan_forbidden_fields(value: Any, findings: list[Finding], path: str = "") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = path_join(path, key)
            for label, pattern in FORBIDDEN_KEY_PATTERNS:
                if pattern.search(key):
                    findings.append(
                        finding(
                            f"forbidden_key_{label}",
                            "block",
                            child_path,
                            "Forbidden sensitive or proprietary field key detected; value not printed.",
                        )
                    )
                    break
            scan_forbidden_fields(child, findings, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_forbidden_fields(child, findings, f"{path}[{index}]")
    elif isinstance(value, str):
        if any(path.startswith(prefix) for prefix in VALUE_SCAN_EXEMPT_PATH_PREFIXES):
            return
        for label, pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(value):
                findings.append(
                    finding(
                        f"forbidden_value_{label}",
                        "block",
                        path,
                        "Forbidden sensitive or proprietary field value detected; value not printed.",
                    )
                )
                break


def check_url_static(value: Any, path: str, findings: list[Finding]) -> None:
    if not isinstance(value, str) or not value.strip():
        findings.append(finding("invalid_public_url", "review", path, "URL must be a non-empty string."))
        return
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        findings.append(finding("invalid_public_url", "review", path, "URL must use http or https."))
        return
    if any(pattern.search(parsed.path) for pattern in AUTH_BOUND_PATH_PATTERNS):
        findings.append(finding("auth_bound_url", "block", path, "URL path appears auth-bound."))
    unsafe_keys = sorted(
        {
            unquote(key)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
            if any(pattern.search(unquote(key)) for pattern in UNSAFE_QUERY_KEY_PATTERNS)
        }
    )
    for key in unsafe_keys:
        findings.append(finding("unsafe_query_key", "block", path, f"URL query contains a sensitive-looking key: {key}."))


def check_required_fields(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS.difference(snapshot))
    for field_name in missing:
        findings.append(
            finding(
                "missing_required_field",
                "review",
                f"{label}.{field_name}",
                f"Required top-level field is missing: {field_name}.",
            )
        )


def check_status(
    section: dict[str, Any],
    path: str,
    allowed: set[str],
    good: set[str],
    findings: list[Finding],
) -> str | None:
    value = section.get("status")
    if not isinstance(value, str):
        findings.append(finding("missing_status", "review", path_join(path, "status"), "Status is required."))
        return None
    normalized = value.strip().lower()
    if normalized not in allowed:
        findings.append(finding("unsupported_status_value", "review", path_join(path, "status"), "Status is outside the public verifier contract."))
        return normalized
    if normalized not in good:
        findings.append(finding(f"{path.split('.')[-1]}_requires_review", "review", path_join(path, "status"), "Status is not fully public-usable."))
    return normalized


def check_timestamp_field(section: dict[str, Any], path: str, field_name: str, findings: list[Finding]) -> datetime | None:
    parsed = parse_utc_timestamp(section.get(field_name))
    if parsed is None:
        findings.append(finding("invalid_utc_timestamp", "review", path_join(path, field_name), "Timestamp must be an ISO-8601 UTC value."))
    return parsed


def snapshot_hash(snapshot: dict[str, Any]) -> str | None:
    direct = normalized_string(snapshot.get("snapshot_hash"))
    if direct:
        return direct
    provenance = snapshot.get("provenance")
    if isinstance(provenance, dict):
        return normalized_string(provenance.get("snapshot_hash"))
    return None


def check_snapshot_shape(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> dict[str, Any]:
    check_required_fields(snapshot, label, findings)

    generated = parse_utc_timestamp(snapshot.get("generated_at_utc"))
    if generated is None:
        findings.append(finding("invalid_generated_at_utc", "review", f"{label}.generated_at_utc", "generated_at_utc must be an ISO-8601 UTC value."))

    subject = as_dict(snapshot.get("subject"), f"{label}.subject", findings)
    for field_name in sorted(REQUIRED_SUBJECT_FIELDS):
        if normalized_string(subject.get(field_name)) is None:
            findings.append(finding("missing_subject_field", "review", f"{label}.subject.{field_name}", f"Subject field is missing: {field_name}."))

    sequence = snapshot.get("sequence")
    if sequence is not None and (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0):
        findings.append(finding("invalid_sequence", "review", f"{label}.sequence", "sequence must be a non-negative integer when present."))

    check_freshness(snapshot, label, generated, findings)
    check_coverage(snapshot, label, generated, findings)
    check_replay(snapshot, label, findings)
    check_provenance(snapshot, label, findings)
    check_degradation(snapshot, label, findings)
    check_public_counters(snapshot, label, findings)
    check_exclusions(snapshot, label, findings)

    if snapshot_hash(snapshot) is None or not validate_sha256(snapshot_hash(snapshot)):
        findings.append(finding("invalid_snapshot_hash", "review", f"{label}.provenance.snapshot_hash", "Snapshot hash should be sha256:<64 lowercase hex>."))

    return subject


def check_freshness(snapshot: dict[str, Any], label: str, generated: datetime | None, findings: list[Finding]) -> datetime | None:
    section = as_dict(snapshot.get("freshness"), f"{label}.freshness", findings)
    check_status(section, f"{label}.freshness", ALLOWED_FRESHNESS, {"fresh"}, findings)
    observed = check_timestamp_field(section, f"{label}.freshness", "observed_at_utc", findings)
    max_age = section.get("max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        findings.append(finding("invalid_max_age_seconds", "review", f"{label}.freshness.max_age_seconds", "max_age_seconds must be a positive integer."))
    if generated is not None and observed is not None and observed > generated:
        findings.append(finding("freshness_after_generation", "block", f"{label}.freshness.observed_at_utc", "Freshness observation occurs after snapshot generation time."))
    return observed


def coverage_windows(snapshot: dict[str, Any], label: str, generated: datetime | None, findings: list[Finding]) -> dict[str, tuple[datetime, datetime]]:
    section = as_dict(snapshot.get("coverage"), f"{label}.coverage", findings)
    check_status(section, f"{label}.coverage", ALLOWED_COVERAGE, {"complete"}, findings)
    windows = as_list(section.get("windows"), f"{label}.coverage.windows", findings)
    parsed_windows: dict[str, tuple[datetime, datetime]] = {}
    if not windows:
        findings.append(finding("missing_coverage_windows", "review", f"{label}.coverage.windows", "At least one coverage window is required."))
    for index, window in enumerate(windows):
        path = f"{label}.coverage.windows[{index}]"
        obj = as_dict(window, path, findings)
        name = normalized_string(obj.get("name"))
        if name is None:
            findings.append(finding("missing_coverage_window_name", "review", path_join(path, "name"), "Coverage window name is required."))
        start = check_timestamp_field(obj, path, "start_utc", findings)
        end = check_timestamp_field(obj, path, "end_utc", findings)
        if start is not None and end is not None:
            if start > end:
                findings.append(finding("coverage_window_reversed", "block", path, "Coverage window start is after end."))
            if generated is not None and end > generated:
                findings.append(finding("coverage_window_after_generation", "block", path_join(path, "end_utc"), "Coverage window ends after snapshot generation time."))
            if name is not None:
                parsed_windows[name] = (start, end)
        if obj.get("complete") is not True:
            findings.append(finding("coverage_window_incomplete", "review", path_join(path, "complete"), "Coverage window is not marked complete."))
    return parsed_windows


def check_coverage(snapshot: dict[str, Any], label: str, generated: datetime | None, findings: list[Finding]) -> None:
    coverage_windows(snapshot, label, generated, findings)


def check_replay(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    section = as_dict(snapshot.get("replay"), f"{label}.replay", findings)
    check_status(section, f"{label}.replay", ALLOWED_REPLAY, {"replayable"}, findings)
    input_hash = section.get("input_snapshot_hash")
    if not validate_sha256(input_hash):
        findings.append(finding("invalid_input_snapshot_hash", "review", f"{label}.replay.input_snapshot_hash", "input_snapshot_hash should be sha256:<64 lowercase hex>."))
    previous_hash = section.get("previous_snapshot_hash")
    if previous_hash is not None and not validate_sha256(previous_hash):
        findings.append(finding("invalid_previous_snapshot_hash", "review", f"{label}.replay.previous_snapshot_hash", "previous_snapshot_hash should be sha256:<64 lowercase hex>."))
    leakage = normalized_string(section.get("no_future_leakage_check"))
    if leakage in {"pass", "passed"}:
        return
    if leakage in {"fail", "failed", "violation"}:
        findings.append(finding("future_leakage_violation", "block", f"{label}.replay.no_future_leakage_check", "Snapshot declares a no-future-leakage failure."))
    elif leakage in {"unknown", "unsupported", "not_run"}:
        findings.append(finding("future_leakage_unproven", "review", f"{label}.replay.no_future_leakage_check", "No-future-leakage check is not proven."))
    else:
        findings.append(finding("invalid_no_future_leakage_check", "review", f"{label}.replay.no_future_leakage_check", "No-future-leakage check has an unsupported value."))


def check_provenance(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    section = as_dict(snapshot.get("provenance"), f"{label}.provenance", findings)
    check_status(section, f"{label}.provenance", ALLOWED_PROVENANCE, {"public", "public_synthetic", "documented"}, findings)
    urls = as_list(section.get("public_urls"), f"{label}.provenance.public_urls", findings)
    if not urls:
        findings.append(finding("missing_public_urls", "review", f"{label}.provenance.public_urls", "At least one public provenance URL is required."))
    for index, url in enumerate(urls):
        check_url_static(url, f"{label}.provenance.public_urls[{index}]", findings)
    if section.get("private_inputs_used") is not False:
        findings.append(finding("private_inputs_not_excluded", "block", f"{label}.provenance.private_inputs_used", "Snapshot must explicitly declare private_inputs_used=false."))


def check_degradation(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    section = as_dict(snapshot.get("degradation"), f"{label}.degradation", findings)
    degraded = section.get("degraded")
    reason_code = normalized_string(section.get("reason_code"))
    if not isinstance(degraded, bool):
        findings.append(finding("invalid_degraded_flag", "review", f"{label}.degradation.degraded", "degraded must be a boolean."))
    if reason_code not in ALLOWED_DEGRADED_REASON_CODES:
        findings.append(finding("unsupported_degraded_reason_code", "review", f"{label}.degradation.reason_code", "reason_code is outside the public contract."))
        return
    if degraded is True and reason_code == "none":
        findings.append(finding("missing_degraded_reason", "review", f"{label}.degradation.reason_code", "Degraded snapshots need an explicit reason code."))
    if degraded is False and reason_code != "none":
        findings.append(finding("inconsistent_degraded_reason", "review", f"{label}.degradation.reason_code", "Non-degraded snapshots should use reason_code=none."))


def check_public_counters(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    counters = as_dict(snapshot.get("public_counters"), f"{label}.public_counters", findings)
    if not counters:
        findings.append(finding("missing_public_counters", "review", f"{label}.public_counters", "At least one public counter is required for drift comparison."))
    for key, value in counters.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            findings.append(finding("non_numeric_public_counter", "review", f"{label}.public_counters.{key}", "Public counters must be numeric."))


def check_exclusions(snapshot: dict[str, Any], label: str, findings: list[Finding]) -> None:
    values = as_list(snapshot.get("forbidden_field_exclusions"), f"{label}.forbidden_field_exclusions", findings)
    exclusions = {item for item in values if isinstance(item, str)}
    missing = sorted(REQUIRED_EXCLUSIONS.difference(exclusions))
    for exclusion in missing:
        findings.append(finding("missing_forbidden_field_exclusion", "review", f"{label}.forbidden_field_exclusions", f"Missing forbidden-field exclusion: {exclusion}."))


def compare_identity(previous_subject: dict[str, Any], current_subject: dict[str, Any], findings: list[Finding]) -> None:
    for field_name in sorted(REQUIRED_SUBJECT_FIELDS):
        previous = normalized_string(previous_subject.get(field_name))
        current = normalized_string(current_subject.get(field_name))
        if previous is not None and current is not None and previous != current:
            findings.append(finding("artifact_identity_changed", "block", f"subject.{field_name}", "Compared snapshots describe different validator evidence subjects."))


def compare_event_time(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_generated = parse_utc_timestamp(previous.get("generated_at_utc"))
    current_generated = parse_utc_timestamp(current.get("generated_at_utc"))
    if previous_generated is not None and current_generated is not None and current_generated <= previous_generated:
        findings.append(finding("event_time_not_increasing", "block", "current.generated_at_utc", "Current snapshot generation time must be after previous snapshot generation time."))

    previous_sequence = previous.get("sequence")
    current_sequence = current.get("sequence")
    if isinstance(previous_sequence, int) and not isinstance(previous_sequence, bool) and isinstance(current_sequence, int) and not isinstance(current_sequence, bool):
        if current_sequence <= previous_sequence:
            findings.append(finding("sequence_not_increasing", "block", "current.sequence", "Current sequence must be greater than previous sequence."))
    elif previous_sequence is None or current_sequence is None:
        findings.append(finding("sequence_missing_for_continuity", "review", "sequence", "Both snapshots should declare sequence for deterministic continuity ordering."))


def compare_freshness(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_section = as_dict(previous.get("freshness"), "previous.freshness", findings)
    current_section = as_dict(current.get("freshness"), "current.freshness", findings)
    previous_observed = parse_utc_timestamp(previous_section.get("observed_at_utc"))
    current_observed = parse_utc_timestamp(current_section.get("observed_at_utc"))
    if previous_observed is not None and current_observed is not None and current_observed < previous_observed:
        findings.append(finding("freshness_regressed", "block", "current.freshness.observed_at_utc", "Current freshness observation is older than previous freshness observation."))


def compare_replay_hash_continuity(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_hash = snapshot_hash(previous)
    replay = as_dict(current.get("replay"), "current.replay", findings)
    declared_previous_hash = normalized_string(replay.get("previous_snapshot_hash"))
    if declared_previous_hash is None:
        findings.append(finding("missing_previous_snapshot_hash", "review", "current.replay.previous_snapshot_hash", "Current replay metadata should point to the previous public snapshot hash."))
        return
    if previous_hash is not None and validate_sha256(previous_hash) and declared_previous_hash != previous_hash:
        findings.append(finding("previous_snapshot_hash_mismatch", "block", "current.replay.previous_snapshot_hash", "Current replay metadata does not chain to the previous public snapshot hash."))


def compare_coverage(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_windows = coverage_windows(previous, "previous", parse_utc_timestamp(previous.get("generated_at_utc")), [])
    current_windows = coverage_windows(current, "current", parse_utc_timestamp(current.get("generated_at_utc")), [])
    common = sorted(set(previous_windows).intersection(current_windows))
    if not common:
        findings.append(finding("no_common_coverage_windows", "review", "coverage.windows", "No same-named coverage windows can be compared across snapshots."))
        return
    for name in common:
        previous_start, previous_end = previous_windows[name]
        current_start, current_end = current_windows[name]
        if current_end < previous_end:
            findings.append(finding("coverage_window_regressed", "block", f"coverage.windows.{name}", "Current coverage window ends before the previous window end."))
        if current_start > previous_end:
            findings.append(finding("coverage_gap", "review", f"coverage.windows.{name}", "Current coverage window starts after the previous window end."))
        if current_start < previous_start:
            findings.append(finding("coverage_start_moved_backward", "review", f"coverage.windows.{name}", "Current coverage window start moved earlier than the previous window start."))


def compare_degradation(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_degradation = as_dict(previous.get("degradation"), "previous.degradation", findings)
    current_degradation = as_dict(current.get("degradation"), "current.degradation", findings)
    previous_degraded = previous_degradation.get("degraded")
    current_degraded = current_degradation.get("degraded")
    previous_reason = normalized_string(previous_degradation.get("reason_code"))
    current_reason = normalized_string(current_degradation.get("reason_code"))
    if previous_degraded is True and current_degraded is False:
        recovered = normalized_string(current_degradation.get("recovered_from_reason_code"))
        if recovered != previous_reason:
            findings.append(finding("degraded_recovery_unlinked", "review", "current.degradation.recovered_from_reason_code", "Recovery should link to the previous degraded reason code."))
    if previous_degraded is True and current_degraded is True and previous_reason != current_reason:
        findings.append(finding("degraded_reason_changed", "review", "current.degradation.reason_code", "Degraded reason changed between snapshots and needs reviewer context."))


def compare_public_counters(previous: dict[str, Any], current: dict[str, Any], findings: list[Finding]) -> None:
    previous_counters = as_dict(previous.get("public_counters"), "previous.public_counters", findings)
    current_counters = as_dict(current.get("public_counters"), "current.public_counters", findings)
    for key, previous_value in sorted(previous_counters.items()):
        if key not in current_counters:
            findings.append(finding("public_counter_missing_in_current", "review", f"current.public_counters.{key}", "Current snapshot is missing a previously declared public counter."))
            continue
        current_value = current_counters[key]
        if (
            isinstance(previous_value, (int, float))
            and not isinstance(previous_value, bool)
            and isinstance(current_value, (int, float))
            and not isinstance(current_value, bool)
            and current_value < previous_value
        ):
            findings.append(finding("public_counter_regressed", "block", f"current.public_counters.{key}", "Public counter decreased across snapshots."))


def verdict_from_findings(findings: list[Finding]) -> str:
    verdict = "pass"
    for item in findings:
        if item.severity == "block":
            verdict = "block"
        elif item.severity == "review" and verdict != "block":
            verdict = "needs_human_review"
    return verdict


def validate_pair(previous_data: Any, current_data: Any, *, previous_source: str = "previous", current_source: str = "current") -> dict[str, Any]:
    findings: list[Finding] = []

    if not isinstance(previous_data, dict):
        findings.append(finding("previous_root_object_required", "review", "previous", "Previous snapshot root must be a JSON object."))
        previous: dict[str, Any] = {}
    else:
        previous = previous_data
    if not isinstance(current_data, dict):
        findings.append(finding("current_root_object_required", "review", "current", "Current snapshot root must be a JSON object."))
        current: dict[str, Any] = {}
    else:
        current = current_data

    scan_forbidden_fields(previous, findings, "previous")
    scan_forbidden_fields(current, findings, "current")

    previous_subject = check_snapshot_shape(previous, "previous", findings)
    current_subject = check_snapshot_shape(current, "current", findings)

    compare_identity(previous_subject, current_subject, findings)
    compare_event_time(previous, current, findings)
    compare_freshness(previous, current, findings)
    compare_replay_hash_continuity(previous, current, findings)
    compare_coverage(previous, current, findings)
    compare_degradation(previous, current, findings)
    compare_public_counters(previous, current, findings)

    verdict = verdict_from_findings(findings)
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "sources": {"previous": previous_source, "current": current_source},
        "overall_verdict": verdict,
        "reason_code": "ok" if verdict == "pass" else findings[0].code,
        "finding_count": len(findings),
        "findings": [item.to_dict() for item in findings],
    }


def base_snapshot(sequence: int, generated_at: str, observed_at: str, window_start: str, window_end: str, snapshot_hash_value: str) -> dict[str, Any]:
    return {
        "snapshot_version": "validator-evidence-continuity-v1",
        "snapshot_id": f"synthetic-validator-snapshot-{sequence:03d}",
        "sequence": sequence,
        "subject": {
            "subject_id": "public-validator-metrics-node-alpha",
            "artifact_kind": "validator_metrics_snapshot",
            "pipeline_id": "ouroboros-mini-public-integrity-export",
        },
        "generated_at_utc": generated_at,
        "freshness": {"status": "fresh", "observed_at_utc": observed_at, "max_age_seconds": 300},
        "coverage": {
            "status": "complete",
            "windows": [
                {
                    "name": "validator_metrics",
                    "start_utc": window_start,
                    "end_utc": window_end,
                    "complete": True,
                }
            ],
        },
        "replay": {
            "status": "replayable",
            "input_snapshot_hash": "sha256:" + "c" * 64,
            "previous_snapshot_hash": None,
            "no_future_leakage_check": "pass",
        },
        "provenance": {
            "status": "public_synthetic",
            "public_urls": ["https://github.com/example/post-fiat-validator-evidence/commit/0123456789abcdef0123456789abcdef01234567"],
            "private_inputs_used": False,
            "snapshot_hash": snapshot_hash_value,
        },
        "degradation": {"degraded": False, "reason_code": "none"},
        "public_counters": {
            "samples_observed": 1000 + sequence,
            "public_artifacts_checked": 4,
            "coverage_windows_completed": sequence,
        },
        "forbidden_field_exclusions": sorted(REQUIRED_EXCLUSIONS),
    }


def base_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    previous = base_snapshot(
        41,
        "2026-06-08T20:55:00Z",
        "2026-06-08T20:54:30Z",
        "2026-06-08T20:00:00Z",
        "2026-06-08T20:55:00Z",
        "sha256:" + "a" * 64,
    )
    current = base_snapshot(
        42,
        "2026-06-08T21:00:00Z",
        "2026-06-08T20:59:30Z",
        "2026-06-08T20:55:00Z",
        "2026-06-08T21:00:00Z",
        "sha256:" + "b" * 64,
    )
    current["replay"]["previous_snapshot_hash"] = previous["provenance"]["snapshot_hash"]
    return previous, current


def self_test_cases() -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    pass_previous, pass_current = base_pair()

    block_previous, block_current = base_pair()
    block_current["subject"]["subject_id"] = "different-validator-subject"
    block_current["generated_at_utc"] = "2026-06-08T20:50:00Z"
    block_current["wallet_seed"] = "value intentionally not printed"
    block_current["replay"]["previous_snapshot_hash"] = "sha256:" + "d" * 64
    block_current["replay"]["no_future_leakage_check"] = "failed"
    block_current["public_counters"]["samples_observed"] = 1

    review_previous, review_current = base_pair()
    review_current["generated_at_utc"] = "2026-06-08T21:15:00Z"
    review_current["freshness"]["status"] = "unknown"
    review_current["coverage"]["windows"][0]["start_utc"] = "2026-06-08T21:10:00Z"
    review_current["coverage"]["windows"][0]["end_utc"] = "2026-06-08T21:10:00Z"
    review_current["degradation"] = {"degraded": True, "reason_code": "synthetic_fixture_only"}
    review_current["replay"].pop("previous_snapshot_hash")
    review_current["public_counters"].pop("public_artifacts_checked")

    return [
        ("continuous_public_snapshots_pass", "pass", pass_previous, pass_current),
        ("identity_hash_leakage_and_counter_breaks_block", "block", block_previous, block_current),
        ("incomplete_continuity_needs_human_review", "needs_human_review", review_previous, review_current),
    ]


def run_self_tests() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    all_passed = True
    for name, expected, previous, current in self_test_cases():
        result = validate_pair(previous, current, previous_source=f"self_test:{name}:previous", current_source=f"self_test:{name}:current")
        actual = result["overall_verdict"]
        passed = actual == expected
        all_passed = all_passed and passed
        tests.append(
            {
                "name": name,
                "expected_verdict": expected,
                "actual_verdict": actual,
                "passed": passed,
                "result": result,
            }
        )
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "self_test": True,
        "overall_verdict": "pass" if all_passed else "block",
        "verdict_contract": VERDICT_CONTRACT,
        "tests": tests,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare two public-safe validator evidence snapshots for continuity drift.")
    parser.add_argument("previous", nargs="?", help="Path to previous JSON snapshot, or '-' for stdin.")
    parser.add_argument("current", nargs="?", help="Path to current JSON snapshot.")
    parser.add_argument("--self-test", action="store_true", help="Run embedded synthetic self-tests.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def emit_json(payload: dict[str, Any], *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def error_payload(reason_code: str, source: str, severity: str, message: str) -> dict[str, Any]:
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "sources": {"previous": source, "current": source},
        "overall_verdict": "block" if severity == "block" else "needs_human_review",
        "reason_code": reason_code,
        "finding_count": 1,
        "findings": [finding(reason_code, severity, "$", message).to_dict()],
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            payload = run_self_tests()
        else:
            if not args.previous or not args.current:
                parser.error("previous and current inputs are required unless --self-test is used")
            previous = read_json_input(args.previous)
            current = read_json_input(args.current)
            payload = validate_pair(previous, current, previous_source=args.previous, current_source=args.current)
    except json.JSONDecodeError as error:
        payload = error_payload("invalid_json", "input", "block", f"Input is not valid JSON: {scalar_text(error.msg)}.")
    except OSError as error:
        payload = error_payload("input_read_error", "input", "review", f"Could not read input: {scalar_text(type(error).__name__)}.")
    emit_json(payload, pretty=args.pretty)
    return 0 if payload.get("overall_verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
