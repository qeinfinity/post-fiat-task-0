#!/usr/bin/env python3
"""Validate public-safe validator or metrics-node export records.

Usage:
  python3 scripts/check_validator_export_sanitization.py export.json --pretty
  python3 scripts/check_validator_export_sanitization.py - --pretty < export.json
  python3 scripts/check_validator_export_sanitization.py --self-test --pretty

The checker is intentionally stdlib-only and offline. It validates whether a
sanitized Post Fiat validator/metrics-node export record is safe to use as
public evidence. Normal output is JSON only and never prints field values that
match sensitive patterns.
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


TOOL_NAME = "validator_export_sanitization_checker"
TOOL_VERSION = "1.0"
VERDICT_ORDER = {"pass": 0, "needs_human_review": 1, "block": 2}
VERDICT_CONTRACT = ["pass", "needs_human_review", "block"]

REQUIRED_TOP_LEVEL_FIELDS = {
    "export_version",
    "generated_at_utc",
    "health",
    "freshness",
    "coverage",
    "replay",
    "provenance",
    "degradation",
}

ALLOWED_HEALTH_STATUSES = {"ok", "degraded", "failed", "unknown", "unsupported"}
ALLOWED_FRESHNESS_STATUSES = {"fresh", "stale", "unknown", "unsupported"}
ALLOWED_COVERAGE_STATUSES = {"complete", "partial", "unknown", "unsupported"}
ALLOWED_REPLAY_STATUSES = {"replayable", "not_replayable", "unknown", "unsupported"}
ALLOWED_PROVENANCE_STATUSES = {"public", "public_synthetic", "documented", "unknown", "unsupported"}
ALLOWED_DEGRADED_REASON_CODES = {
    "none",
    "source_stale",
    "coverage_partial",
    "replay_unavailable",
    "provenance_incomplete",
    "unsupported_public_source",
    "synthetic_fixture_only",
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
        ("proprietary_threshold_data", r"(^|[_-])(threshold|model[_-]?weight|feature[_-]?weight|calibration[_-]?internal)([_-]|$)"),
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


def scalar_text(value: Any) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()[:240]


def finding(code: str, severity: str, path: str, message: str) -> Finding:
    return Finding(code=code, severity=severity, path=path or "$", message=message)


def escalate(current: str, candidate: str) -> str:
    return candidate if VERDICT_ORDER[candidate] > VERDICT_ORDER[current] else current


def read_json_input(path: str | None) -> Any:
    if path in {None, "-"}:
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw)


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def as_dict(value: Any, path: str, findings: list[Finding]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    findings.append(finding("object_required", "review", path, "Expected a JSON object."))
    return {}


def check_required_fields(record: dict[str, Any], findings: list[Finding]) -> None:
    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS.difference(record))
    for field_name in missing:
        findings.append(
            finding(
                "missing_required_field",
                "review",
                field_name,
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
        findings.append(
            finding(
                "unsupported_status_value",
                "review",
                path_join(path, "status"),
                "Status is outside the public-safe status contract.",
            )
        )
        return normalized
    if normalized not in good:
        findings.append(
            finding(
                f"{path}_requires_review",
                "review",
                path_join(path, "status"),
                f"{path} status is not fully public-usable.",
            )
        )
    return normalized


def check_timestamp_field(section: dict[str, Any], path: str, field_name: str, findings: list[Finding]) -> datetime | None:
    value = section.get(field_name)
    parsed = parse_utc_timestamp(value)
    if parsed is None:
        findings.append(
            finding(
                "invalid_utc_timestamp",
                "review",
                path_join(path, field_name),
                "Timestamp must be an ISO-8601 UTC value.",
            )
        )
    return parsed


def check_url_static(value: Any, path: str, findings: list[Finding]) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        findings.append(finding("invalid_public_url", "review", path, "URL must be a non-empty string."))
        return
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        findings.append(finding("invalid_public_url", "review", path, "URL must use http or https."))
        return
    if any(pattern.search(parsed.path) for pattern in AUTH_BOUND_PATH_PATTERNS):
        findings.append(finding("auth_bound_public_url", "block", path, "URL path appears auth-bound."))
    unsafe_keys = sorted(
        {
            unquote(key)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
            if any(pattern.search(unquote(key)) for pattern in UNSAFE_QUERY_KEY_PATTERNS)
        }
    )
    for key in unsafe_keys:
        findings.append(
            finding(
                "unsafe_query_key",
                "block",
                path,
                f"URL query contains a sensitive-looking key: {key}.",
            )
        )


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


def check_health(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("health"), "health", findings)
    check_status(section, "health", ALLOWED_HEALTH_STATUSES, {"ok"}, findings)
    check_timestamp_field(section, "health", "checked_at_utc", findings)
    components = section.get("components", [])
    if components is not None and not isinstance(components, list):
        findings.append(finding("invalid_components", "review", "health.components", "Components must be a list."))
    for index, component in enumerate(components if isinstance(components, list) else []):
        component_path = f"health.components[{index}]"
        component_obj = as_dict(component, component_path, findings)
        if not isinstance(component_obj.get("name"), str) or not component_obj.get("name", "").strip():
            findings.append(finding("missing_component_name", "review", path_join(component_path, "name"), "Component name is required."))
        check_status(component_obj, component_path, ALLOWED_HEALTH_STATUSES, {"ok"}, findings)


def check_freshness(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("freshness"), "freshness", findings)
    check_status(section, "freshness", ALLOWED_FRESHNESS_STATUSES, {"fresh"}, findings)
    observed = check_timestamp_field(section, "freshness", "observed_at_utc", findings)
    generated = parse_utc_timestamp(record.get("generated_at_utc"))
    if generated is not None and observed is not None and observed > generated:
        findings.append(
            finding(
                "freshness_observed_after_generation",
                "block",
                "freshness.observed_at_utc",
                "Freshness observation occurs after export generation time.",
            )
        )
    max_age = section.get("max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        findings.append(finding("invalid_max_age_seconds", "review", "freshness.max_age_seconds", "max_age_seconds must be a positive integer."))


def check_coverage(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("coverage"), "coverage", findings)
    check_status(section, "coverage", ALLOWED_COVERAGE_STATUSES, {"complete"}, findings)
    windows = section.get("windows", section.get("declared_windows"))
    if not isinstance(windows, list) or not windows:
        findings.append(finding("missing_coverage_windows", "review", "coverage.windows", "At least one coverage window is required."))
        return
    for index, window in enumerate(windows):
        window_path = f"coverage.windows[{index}]"
        window_obj = as_dict(window, window_path, findings)
        if not isinstance(window_obj.get("name"), str) or not window_obj.get("name", "").strip():
            findings.append(finding("missing_coverage_window_name", "review", path_join(window_path, "name"), "Coverage window name is required."))
        start = check_timestamp_field(window_obj, window_path, "start_utc", findings)
        end = check_timestamp_field(window_obj, window_path, "end_utc", findings)
        if start is not None and end is not None and start > end:
            findings.append(finding("coverage_window_reversed", "block", window_path, "Coverage window start is after end."))
        if window_obj.get("complete") is not True:
            findings.append(finding("coverage_window_incomplete", "review", path_join(window_path, "complete"), "Coverage window is not marked complete."))


def check_replay(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("replay"), "replay", findings)
    check_status(section, "replay", ALLOWED_REPLAY_STATUSES, {"replayable"}, findings)
    leakage_value = section.get("no_future_leakage_check")
    if not isinstance(leakage_value, str):
        findings.append(finding("missing_no_future_leakage_check", "review", "replay.no_future_leakage_check", "No-future-leakage check is required."))
    else:
        normalized = leakage_value.strip().lower()
        if normalized in {"pass", "passed"}:
            pass
        elif normalized in {"fail", "failed", "violation"}:
            findings.append(finding("future_leakage_violation", "block", "replay.no_future_leakage_check", "Export declares a future-leakage failure."))
        elif normalized in {"unknown", "unsupported", "not_run"}:
            findings.append(finding("future_leakage_unknown", "review", "replay.no_future_leakage_check", "No-future-leakage check is not proven."))
        else:
            findings.append(finding("invalid_no_future_leakage_check", "review", "replay.no_future_leakage_check", "No-future-leakage check has an unsupported value."))
    manifest_hash = section.get("input_manifest_hash")
    if not isinstance(manifest_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_hash):
        findings.append(finding("invalid_input_manifest_hash", "review", "replay.input_manifest_hash", "input_manifest_hash should be sha256:<64 lowercase hex>."))


def check_provenance(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("provenance"), "provenance", findings)
    check_status(section, "provenance", ALLOWED_PROVENANCE_STATUSES, {"public", "public_synthetic", "documented"}, findings)
    check_url_static(section.get("artifact_url"), "provenance.artifact_url", findings)
    evidence_type = section.get("evidence_type")
    if evidence_type is not None and evidence_type not in {"github_commit", "public_url", "synthetic_fixture", "signed_manifest"}:
        findings.append(finding("unsupported_evidence_type", "review", "provenance.evidence_type", "Evidence type is outside the public verifier contract."))


def check_degradation(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("degradation"), "degradation", findings)
    degraded = section.get("degraded")
    reason_code = section.get("reason_code")
    if not isinstance(degraded, bool):
        findings.append(finding("invalid_degraded_flag", "review", "degradation.degraded", "degraded must be a boolean."))
    if not isinstance(reason_code, str):
        findings.append(finding("missing_degraded_reason_code", "review", "degradation.reason_code", "reason_code is required."))
        return
    normalized = reason_code.strip().lower()
    if normalized not in ALLOWED_DEGRADED_REASON_CODES:
        findings.append(finding("unsupported_degraded_reason_code", "review", "degradation.reason_code", "reason_code is outside the public contract."))
    if degraded is True and normalized == "none":
        findings.append(finding("missing_degraded_reason", "review", "degradation.reason_code", "Degraded exports need an explicit reason code."))
    if degraded is False and normalized != "none":
        findings.append(finding("inconsistent_degraded_reason", "review", "degradation.reason_code", "Non-degraded exports should use reason_code=none."))


def check_unsupported_states(record: dict[str, Any], findings: list[Finding]) -> None:
    for path in ("unsupported_states", "unsupported"):
        values = string_list(record.get(path))
        if values:
            findings.append(
                finding(
                    "unsupported_states_declared",
                    "review",
                    path,
                    "Export declares unsupported states that require reviewer attention.",
                )
            )


def verdict_from_findings(findings: list[Finding]) -> str:
    verdict = "pass"
    for item in findings:
        if item.severity == "block":
            verdict = escalate(verdict, "block")
        elif item.severity == "review":
            verdict = escalate(verdict, "needs_human_review")
    return verdict


def validate_record(data: Any, *, source: str = "input") -> dict[str, Any]:
    findings: list[Finding] = []
    if not isinstance(data, dict):
        findings.append(finding("root_object_required", "review", "$", "Root must be a JSON object."))
        record: dict[str, Any] = {}
    else:
        record = data

    scan_forbidden_fields(record, findings)
    check_required_fields(record, findings)
    if "generated_at_utc" in record and parse_utc_timestamp(record.get("generated_at_utc")) is None:
        findings.append(finding("invalid_generated_at_utc", "review", "generated_at_utc", "generated_at_utc must be an ISO-8601 UTC value."))

    check_health(record, findings)
    check_freshness(record, findings)
    check_coverage(record, findings)
    check_replay(record, findings)
    check_provenance(record, findings)
    check_degradation(record, findings)
    check_unsupported_states(record, findings)

    verdict = verdict_from_findings(findings)
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "source": source,
        "overall_verdict": verdict,
        "reason_code": "ok" if verdict == "pass" else findings[0].code,
        "finding_count": len(findings),
        "findings": [item.to_dict() for item in findings],
    }


def base_public_record() -> dict[str, Any]:
    return {
        "export_version": "validator-export-sanitized-v1",
        "generated_at_utc": "2026-06-04T18:30:00Z",
        "health": {
            "status": "ok",
            "checked_at_utc": "2026-06-04T18:29:30Z",
            "components": [
                {"name": "exporter", "status": "ok"},
                {"name": "public_manifest", "status": "ok"},
            ],
        },
        "freshness": {
            "status": "fresh",
            "observed_at_utc": "2026-06-04T18:29:00Z",
            "max_age_seconds": 300,
        },
        "coverage": {
            "status": "complete",
            "windows": [
                {
                    "name": "health_and_metrics",
                    "start_utc": "2026-06-04T18:00:00Z",
                    "end_utc": "2026-06-04T18:30:00Z",
                    "complete": True,
                }
            ],
        },
        "replay": {
            "status": "replayable",
            "input_manifest_hash": "sha256:" + "a" * 64,
            "no_future_leakage_check": "pass",
        },
        "provenance": {
            "status": "public_synthetic",
            "artifact_url": "https://github.com/example/post-fiat-validator-export/commit/0123456789abcdef0123456789abcdef01234567",
            "evidence_type": "github_commit",
        },
        "degradation": {
            "degraded": False,
            "reason_code": "none",
        },
        "unsupported_states": [],
    }


def self_test_cases() -> list[tuple[str, str, dict[str, Any]]]:
    pass_record = base_public_record()

    block_record = copy.deepcopy(pass_record)
    block_record["wallet_seed"] = "value intentionally not printed"
    block_record["provenance"]["artifact_url"] = "https://example.com/private/report?token=abc"
    block_record["replay"]["no_future_leakage_check"] = "failed"

    review_record = copy.deepcopy(pass_record)
    review_record["freshness"]["status"] = "unknown"
    review_record["coverage"]["windows"][0]["complete"] = False
    review_record["degradation"] = {"degraded": True, "reason_code": "coverage_partial"}

    return [
        ("complete_public_export_passes", "pass", pass_record),
        ("forbidden_and_leaky_export_blocks", "block", block_record),
        ("incomplete_public_export_needs_human_review", "needs_human_review", review_record),
    ]


def run_self_tests() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    all_passed = True
    for name, expected, record in self_test_cases():
        result = validate_record(record, source=f"self_test:{name}")
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
    parser = argparse.ArgumentParser(description="Validate sanitized public validator/metrics export records.")
    parser.add_argument("input", nargs="?", help="Path to JSON export record, or '-' for stdin.")
    parser.add_argument("--self-test", action="store_true", help="Run embedded synthetic self-tests.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def emit_json(payload: dict[str, Any], *, pretty: bool) -> None:
    if pretty:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            payload = run_self_tests()
        else:
            if not args.input:
                parser.error("input is required unless --self-test is used")
            payload = validate_record(read_json_input(args.input), source=args.input)
    except json.JSONDecodeError as error:
        payload = {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "source": args.input or "input",
            "overall_verdict": "block",
            "reason_code": "invalid_json",
            "finding_count": 1,
            "findings": [
                finding("invalid_json", "block", "$", f"Input is not valid JSON: {scalar_text(error.msg)}.").to_dict()
            ],
        }
    except OSError as error:
        payload = {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "source": args.input or "input",
            "overall_verdict": "needs_human_review",
            "reason_code": "input_read_error",
            "finding_count": 1,
            "findings": [
                finding("input_read_error", "review", "$", f"Could not read input: {scalar_text(type(error).__name__)}.").to_dict()
            ],
        }
    emit_json(payload, pretty=args.pretty)
    return 0 if payload.get("overall_verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
