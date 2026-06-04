#!/usr/bin/env python3
"""Validate public-safe validator evidence-bundle manifests.

Usage:
  python3 scripts/check_validator_evidence_bundle_manifest.py manifest.json --pretty
  python3 scripts/check_validator_evidence_bundle_manifest.py - --pretty < manifest.json
  python3 scripts/check_validator_evidence_bundle_manifest.py --self-test --pretty

The checker is stdlib-only and offline. It validates a manifest that points
reviewers to public evidence artifacts for a validator or metrics-node
submission. It checks completeness, declared reachability metadata, fixture
hashes, freshness, coverage, replay/provenance, verifier response references,
degraded reasons, and forbidden private fields without printing sensitive
values.
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


TOOL_NAME = "validator_evidence_bundle_manifest_checker"
TOOL_VERSION = "1.0"
VERDICT_ORDER = {"pass": 0, "needs_human_review": 1, "block": 2}
VERDICT_CONTRACT = ["pass", "needs_human_review", "block"]

REQUIRED_TOP_LEVEL_FIELDS = {
    "manifest_version",
    "bundle_id",
    "generated_at_utc",
    "artifacts",
    "freshness",
    "coverage",
    "replay",
    "provenance",
    "verifier_responses",
    "degradation",
    "forbidden_field_exclusions",
}

REQUIRED_ARTIFACT_ROLES = {
    "primary_checker",
    "manifest",
    "synthetic_fixture",
    "self_test_output",
}

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

ALLOWED_ARTIFACT_TYPES = {
    "github_commit",
    "raw_file",
    "synthetic_fixture",
    "manifest",
    "verifier_response",
    "public_url",
}
ALLOWED_REACHABILITY = {"reachable", "unreachable", "unknown", "unsupported"}
ALLOWED_FRESHNESS = {"fresh", "stale", "unknown", "unsupported"}
ALLOWED_COVERAGE = {"complete", "partial", "unknown", "unsupported"}
ALLOWED_REPLAY = {"replayable", "not_replayable", "unknown", "unsupported"}
ALLOWED_PROVENANCE = {"public", "public_synthetic", "documented", "unknown", "unsupported"}
ALLOWED_DEGRADED_REASON_CODES = {
    "none",
    "artifact_unreachable",
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

VALUE_SCAN_EXEMPT_PATH_PREFIXES = {
    "forbidden_field_exclusions[",
}


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


def normalize_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def validate_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def validate_commit_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


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
        findings.append(finding("unsupported_status_value", "review", path_join(path, "status"), "Status is outside the public verifier contract."))
        return normalized
    if normalized not in good:
        findings.append(finding(f"{path}_requires_review", "review", path_join(path, "status"), f"{path} status is not fully public-usable."))
    return normalized


def check_timestamp(section: dict[str, Any], path: str, field_name: str, findings: list[Finding]) -> datetime | None:
    parsed = parse_utc_timestamp(section.get(field_name))
    if parsed is None:
        findings.append(finding("invalid_utc_timestamp", "review", path_join(path, field_name), "Timestamp must be an ISO-8601 UTC value."))
    return parsed


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


def check_artifacts(record: dict[str, Any], findings: list[Finding]) -> None:
    artifacts = as_list(record.get("artifacts"), "artifacts", findings)
    if not artifacts:
        findings.append(finding("missing_artifacts", "review", "artifacts", "At least one artifact is required."))
        return

    seen_roles: set[str] = set()
    seen_names: set[str] = set()

    for index, artifact in enumerate(artifacts):
        path = f"artifacts[{index}]"
        obj = as_dict(artifact, path, findings)

        name = normalize_text(obj.get("name"))
        if name is None:
            findings.append(finding("missing_artifact_name", "review", path_join(path, "name"), "Artifact name is required."))
        elif name in seen_names:
            findings.append(finding("duplicate_artifact_name", "review", path_join(path, "name"), "Artifact names should be unique."))
        else:
            seen_names.add(name)

        role = normalize_text(obj.get("role"))
        if role is None:
            findings.append(finding("missing_artifact_role", "review", path_join(path, "role"), "Artifact role is required."))
        else:
            seen_roles.add(role)

        artifact_type = normalize_text(obj.get("type"))
        if artifact_type not in ALLOWED_ARTIFACT_TYPES:
            findings.append(finding("unsupported_artifact_type", "review", path_join(path, "type"), "Artifact type is outside the public evidence-bundle contract."))

        url = obj.get("url")
        if url is not None:
            check_url_static(url, path_join(path, "url"), findings)
        else:
            findings.append(finding("missing_artifact_url", "review", path_join(path, "url"), "Artifact URL is required."))

        if not validate_sha256(obj.get("sha256")):
            findings.append(finding("invalid_artifact_sha256", "review", path_join(path, "sha256"), "Artifact must declare sha256:<64 lowercase hex>."))

        reachability = as_dict(obj.get("reachability"), path_join(path, "reachability"), findings)
        check_reachability(reachability, path_join(path, "reachability"), findings)

        if artifact_type == "github_commit":
            if not validate_commit_hash(obj.get("commit_hash")):
                findings.append(finding("invalid_commit_hash", "review", path_join(path, "commit_hash"), "GitHub commit artifacts need a 40-character commit_hash."))
            repo_url = obj.get("repo_url")
            if not isinstance(repo_url, str) or "github.com/" not in repo_url:
                findings.append(finding("invalid_repo_url", "review", path_join(path, "repo_url"), "GitHub commit artifacts need a public GitHub repo_url."))
            else:
                check_url_static(repo_url, path_join(path, "repo_url"), findings)

    missing_roles = sorted(REQUIRED_ARTIFACT_ROLES.difference(seen_roles))
    for role in missing_roles:
        findings.append(finding("missing_required_artifact_role", "review", "artifacts", f"Missing required artifact role: {role}."))


def check_reachability(section: dict[str, Any], path: str, findings: list[Finding]) -> None:
    status = check_status(section, path, ALLOWED_REACHABILITY, {"reachable"}, findings)
    checked_at = check_timestamp(section, path, "checked_at_utc", findings)
    http_status = section.get("http_status")
    if status == "reachable":
        if not isinstance(http_status, int) or isinstance(http_status, bool):
            findings.append(finding("missing_http_status", "review", path_join(path, "http_status"), "Reachable artifacts need an integer http_status."))
        elif http_status < 200 or http_status >= 300:
            findings.append(finding("non_success_http_status", "block", path_join(path, "http_status"), "Reachable artifact metadata must use a 2xx http_status."))
    elif status == "unreachable":
        findings.append(finding("artifact_unreachable", "block", path_join(path, "status"), "Artifact is declared unreachable."))
    elif status in {"unknown", "unsupported"}:
        findings.append(finding("artifact_reachability_unproven", "review", path_join(path, "status"), "Artifact reachability is not proven."))
    generated = parse_utc_timestamp(section.get("generated_at_utc"))
    if generated is not None and checked_at is not None and checked_at > generated:
        findings.append(finding("reachability_checked_after_generation", "review", path_join(path, "checked_at_utc"), "Reachability check is after its own generation timestamp."))


def check_freshness(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("freshness"), "freshness", findings)
    check_status(section, "freshness", ALLOWED_FRESHNESS, {"fresh"}, findings)
    observed = check_timestamp(section, "freshness", "observed_at_utc", findings)
    generated = parse_utc_timestamp(record.get("generated_at_utc"))
    if generated is not None and observed is not None and observed > generated:
        findings.append(finding("freshness_after_generation", "block", "freshness.observed_at_utc", "Freshness observation occurs after manifest generation time."))
    max_age = section.get("max_age_seconds")
    if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age <= 0:
        findings.append(finding("invalid_max_age_seconds", "review", "freshness.max_age_seconds", "max_age_seconds must be a positive integer."))


def check_coverage(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("coverage"), "coverage", findings)
    check_status(section, "coverage", ALLOWED_COVERAGE, {"complete"}, findings)
    windows = as_list(section.get("windows"), "coverage.windows", findings)
    if not windows:
        findings.append(finding("missing_coverage_windows", "review", "coverage.windows", "At least one coverage window is required."))
    for index, window in enumerate(windows):
        path = f"coverage.windows[{index}]"
        obj = as_dict(window, path, findings)
        if normalize_text(obj.get("name")) is None:
            findings.append(finding("missing_coverage_window_name", "review", path_join(path, "name"), "Coverage window name is required."))
        start = check_timestamp(obj, path, "start_utc", findings)
        end = check_timestamp(obj, path, "end_utc", findings)
        if start is not None and end is not None and start > end:
            findings.append(finding("coverage_window_reversed", "block", path, "Coverage window start is after end."))
        if obj.get("complete") is not True:
            findings.append(finding("coverage_window_incomplete", "review", path_join(path, "complete"), "Coverage window is not marked complete."))


def check_replay(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("replay"), "replay", findings)
    check_status(section, "replay", ALLOWED_REPLAY, {"replayable"}, findings)
    if not validate_sha256(section.get("input_manifest_hash")):
        findings.append(finding("invalid_input_manifest_hash", "review", "replay.input_manifest_hash", "input_manifest_hash should be sha256:<64 lowercase hex>."))
    leakage = normalize_text(section.get("no_future_leakage_check"))
    if leakage in {"pass", "passed"}:
        pass
    elif leakage in {"fail", "failed", "violation"}:
        findings.append(finding("future_leakage_violation", "block", "replay.no_future_leakage_check", "Manifest declares a no-future-leakage failure."))
    elif leakage in {"unknown", "unsupported", "not_run"}:
        findings.append(finding("future_leakage_unproven", "review", "replay.no_future_leakage_check", "No-future-leakage check is not proven."))
    else:
        findings.append(finding("invalid_no_future_leakage_check", "review", "replay.no_future_leakage_check", "No-future-leakage check has an unsupported value."))


def check_provenance(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("provenance"), "provenance", findings)
    check_status(section, "provenance", ALLOWED_PROVENANCE, {"public", "public_synthetic", "documented"}, findings)
    urls = as_list(section.get("public_urls"), "provenance.public_urls", findings)
    if not urls:
        findings.append(finding("missing_public_urls", "review", "provenance.public_urls", "At least one public provenance URL is required."))
    for index, url in enumerate(urls):
        check_url_static(url, f"provenance.public_urls[{index}]", findings)
    if section.get("private_inputs_used") is not False:
        findings.append(finding("private_inputs_not_excluded", "block", "provenance.private_inputs_used", "Manifest must explicitly declare private_inputs_used=false."))


def check_verifier_responses(record: dict[str, Any], findings: list[Finding]) -> None:
    responses = as_list(record.get("verifier_responses"), "verifier_responses", findings)
    if not responses:
        findings.append(finding("missing_verifier_response_refs", "review", "verifier_responses", "At least one verifier response reference is required."))
        return
    for index, response in enumerate(responses):
        path = f"verifier_responses[{index}]"
        obj = as_dict(response, path, findings)
        if normalize_text(obj.get("request_summary")) is None:
            findings.append(finding("missing_verifier_request_summary", "review", path_join(path, "request_summary"), "Verifier request summary is required."))
        if normalize_text(obj.get("response_artifact")) is None:
            findings.append(finding("missing_verifier_response_artifact", "review", path_join(path, "response_artifact"), "Verifier response artifact reference is required."))
        if obj.get("public_safe") is not True:
            findings.append(finding("verifier_response_not_public_safe", "review", path_join(path, "public_safe"), "Verifier response must be marked public_safe=true."))
        if obj.get("contains_sensitive_values") is not False:
            findings.append(finding("verifier_response_may_contain_sensitive_values", "block", path_join(path, "contains_sensitive_values"), "Verifier response must declare contains_sensitive_values=false."))


def check_degradation(record: dict[str, Any], findings: list[Finding]) -> None:
    section = as_dict(record.get("degradation"), "degradation", findings)
    degraded = section.get("degraded")
    reason_code = normalize_text(section.get("reason_code"))
    if not isinstance(degraded, bool):
        findings.append(finding("invalid_degraded_flag", "review", "degradation.degraded", "degraded must be a boolean."))
    if reason_code not in ALLOWED_DEGRADED_REASON_CODES:
        findings.append(finding("unsupported_degraded_reason_code", "review", "degradation.reason_code", "reason_code is outside the public contract."))
        return
    if degraded is True and reason_code == "none":
        findings.append(finding("missing_degraded_reason", "review", "degradation.reason_code", "Degraded manifests need an explicit reason code."))
    if degraded is False and reason_code != "none":
        findings.append(finding("inconsistent_degraded_reason", "review", "degradation.reason_code", "Non-degraded manifests should use reason_code=none."))


def check_exclusions(record: dict[str, Any], findings: list[Finding]) -> None:
    values = as_list(record.get("forbidden_field_exclusions"), "forbidden_field_exclusions", findings)
    exclusions = {item for item in values if isinstance(item, str)}
    missing = sorted(REQUIRED_EXCLUSIONS.difference(exclusions))
    for exclusion in missing:
        findings.append(finding("missing_forbidden_field_exclusion", "review", "forbidden_field_exclusions", f"Missing forbidden-field exclusion: {exclusion}."))


def verdict_from_findings(findings: list[Finding]) -> str:
    verdict = "pass"
    for item in findings:
        if item.severity == "block":
            verdict = "block"
        elif item.severity == "review" and verdict != "block":
            verdict = "needs_human_review"
    return verdict


def validate_manifest(data: Any, *, source: str = "input") -> dict[str, Any]:
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

    check_artifacts(record, findings)
    check_freshness(record, findings)
    check_coverage(record, findings)
    check_replay(record, findings)
    check_provenance(record, findings)
    check_verifier_responses(record, findings)
    check_degradation(record, findings)
    check_exclusions(record, findings)

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


def base_public_manifest() -> dict[str, Any]:
    return {
        "manifest_version": "validator-evidence-bundle-v1",
        "bundle_id": "synthetic-validator-bundle-001",
        "generated_at_utc": "2026-06-04T20:45:00Z",
        "artifacts": [
            {
                "name": "checker_commit",
                "role": "primary_checker",
                "type": "github_commit",
                "url": "https://github.com/example/post-fiat-validator/commit/0123456789abcdef0123456789abcdef01234567",
                "repo_url": "https://github.com/example/post-fiat-validator",
                "commit_hash": "0123456789abcdef0123456789abcdef01234567",
                "sha256": "sha256:" + "a" * 64,
                "reachability": {"status": "reachable", "checked_at_utc": "2026-06-04T20:44:00Z", "http_status": 200},
            },
            {
                "name": "raw_checker",
                "role": "raw_checker",
                "type": "raw_file",
                "url": "https://raw.githubusercontent.com/example/post-fiat-validator/main/scripts/check_bundle.py",
                "sha256": "sha256:" + "b" * 64,
                "reachability": {"status": "reachable", "checked_at_utc": "2026-06-04T20:44:05Z", "http_status": 200},
            },
            {
                "name": "synthetic_manifest_fixture",
                "role": "synthetic_fixture",
                "type": "synthetic_fixture",
                "url": "https://github.com/example/post-fiat-validator/blob/main/fixtures/public_bundle.json",
                "sha256": "sha256:" + "c" * 64,
                "reachability": {"status": "reachable", "checked_at_utc": "2026-06-04T20:44:10Z", "http_status": 200},
            },
            {
                "name": "self_test_output",
                "role": "self_test_output",
                "type": "public_url",
                "url": "https://github.com/example/post-fiat-validator/blob/main/README.md#self-test",
                "sha256": "sha256:" + "d" * 64,
                "reachability": {"status": "reachable", "checked_at_utc": "2026-06-04T20:44:15Z", "http_status": 200},
            },
            {
                "name": "bundle_manifest",
                "role": "manifest",
                "type": "manifest",
                "url": "https://github.com/example/post-fiat-validator/blob/main/fixtures/evidence_bundle_manifest.json",
                "sha256": "sha256:" + "e" * 64,
                "reachability": {"status": "reachable", "checked_at_utc": "2026-06-04T20:44:20Z", "http_status": 200},
            },
        ],
        "freshness": {"status": "fresh", "observed_at_utc": "2026-06-04T20:44:30Z", "max_age_seconds": 300},
        "coverage": {
            "status": "complete",
            "windows": [
                {
                    "name": "evidence_bundle",
                    "start_utc": "2026-06-04T20:00:00Z",
                    "end_utc": "2026-06-04T20:45:00Z",
                    "complete": True,
                }
            ],
        },
        "replay": {
            "status": "replayable",
            "input_manifest_hash": "sha256:" + "f" * 64,
            "no_future_leakage_check": "pass",
        },
        "provenance": {
            "status": "public_synthetic",
            "public_urls": ["https://github.com/example/post-fiat-validator/tree/main"],
            "private_inputs_used": False,
        },
        "verifier_responses": [
            {
                "request_summary": "Show self-test output for pass, block, and needs_human_review.",
                "response_artifact": "self_test_output",
                "public_safe": True,
                "contains_sensitive_values": False,
            }
        ],
        "degradation": {"degraded": False, "reason_code": "none"},
        "forbidden_field_exclusions": sorted(REQUIRED_EXCLUSIONS),
    }


def self_test_cases() -> list[tuple[str, str, dict[str, Any]]]:
    pass_manifest = base_public_manifest()

    block_manifest = copy.deepcopy(pass_manifest)
    block_manifest["validator_private_key"] = "value intentionally not printed"
    block_manifest["artifacts"][1]["url"] = "https://example.com/private/raw.py?token=abc"
    block_manifest["verifier_responses"][0]["contains_sensitive_values"] = True

    review_manifest = copy.deepcopy(pass_manifest)
    review_manifest["artifacts"] = [item for item in review_manifest["artifacts"] if item["role"] != "self_test_output"]
    review_manifest["freshness"]["status"] = "unknown"
    review_manifest["degradation"] = {"degraded": True, "reason_code": "synthetic_fixture_only"}

    return [
        ("complete_public_bundle_passes", "pass", pass_manifest),
        ("forbidden_and_auth_bound_bundle_blocks", "block", block_manifest),
        ("incomplete_bundle_needs_human_review", "needs_human_review", review_manifest),
    ]


def run_self_tests() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    all_passed = True
    for name, expected, manifest in self_test_cases():
        result = validate_manifest(manifest, source=f"self_test:{name}")
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
    parser = argparse.ArgumentParser(description="Validate public evidence-bundle manifests for validator or metrics-node submissions.")
    parser.add_argument("input", nargs="?", help="Path to JSON evidence-bundle manifest, or '-' for stdin.")
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
            payload = validate_manifest(read_json_input(args.input), source=args.input)
    except json.JSONDecodeError as error:
        payload = {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "source": args.input or "input",
            "overall_verdict": "block",
            "reason_code": "invalid_json",
            "finding_count": 1,
            "findings": [finding("invalid_json", "block", "$", f"Input is not valid JSON: {scalar_text(error.msg)}.").to_dict()],
        }
    except OSError as error:
        payload = {
            "tool": TOOL_NAME,
            "tool_version": TOOL_VERSION,
            "source": args.input or "input",
            "overall_verdict": "needs_human_review",
            "reason_code": "input_read_error",
            "finding_count": 1,
            "findings": [finding("input_read_error", "review", "$", f"Could not read input: {scalar_text(type(error).__name__)}.").to_dict()],
        }
    emit_json(payload, pretty=args.pretty)
    return 0 if payload.get("overall_verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
