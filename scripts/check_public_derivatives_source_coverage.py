#!/usr/bin/env python3
"""Validate and optionally probe a public derivatives source-coverage manifest.

Run:
  python3 scripts/check_public_derivatives_source_coverage.py fixtures/public_derivatives_source_coverage_manifest.json --pretty
  python3 scripts/check_public_derivatives_source_coverage.py fixtures/public_derivatives_source_coverage_manifest.json --probe --pretty
  python3 scripts/check_public_derivatives_source_coverage.py --self-test --pretty
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_MANIFEST = Path("fixtures/public_derivatives_source_coverage_manifest.json")

REQUIRED_TOP_LEVEL_FIELDS = {
    "manifest_version",
    "generated_at_utc",
    "scope",
    "required_source_fields",
    "sources",
}

REQUIRED_SOURCE_FIELDS = {
    "source_id",
    "source_url",
    "venue_label",
    "product_family",
    "expected_content_type",
    "auth_requirement",
    "timestamp_or_session_caveat",
    "freshness_or_degradation_status",
    "unsupported_cases",
    "safe_failure_reasons",
}

ALLOWED_PRODUCT_FAMILIES = {
    "crypto_option",
    "crypto_option_on_futures",
    "listed_etf_share_option",
    "crypto_perpetual_reference",
}

ALLOWED_CONTENT_TYPES = {"json", "html", "text"}
ALLOWED_AUTH_REQUIREMENTS = {"none_public_endpoint", "none_public_page", "none_public_docs"}
ALLOWED_FRESHNESS_STATUSES = {"fresh", "unknown", "degraded", "unsupported"}
SOFT_HTTP_FAILURES = {401, 403, 405, 408, 429, 451, 500, 502, 503, 504}

DISALLOWED_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\baccount(_id|_identifier|_address|_state)?\b",
        r"\bauth(_header|entication)?\b",
        r"\bbearer\b",
        r"\bcookie\b",
        r"\blocal_storage\b",
        r"\bsession(_id|_token|_storage)?\b",
        r"\bwallet\b",
        r"\bmnemonic\b",
        r"\bprivate(_key)?\b",
        r"\bsecret\b",
        r"\btoken\b",
        r"\bmnpi\b",
        r"\brank(ing)?\b",
        r"\bsignal(s)?\b",
        r"\bthreshold(s)?\b",
        r"\bexecution(_instruction|_advice)?\b",
        r"\bstrategy(_logic)?\b",
        r"\bpnl\b",
    )
]

DISALLOWED_VALUE_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"private key",
        r"mnemonic",
        r"auth header",
        r"bearer token",
        r"session token",
        r"wallet seed",
        r"private account",
        r"\bmnpi\b",
        r"venue ranking",
        r"trading signal",
        r"model threshold",
        r"execution advice",
        r"proprietary strategy",
    )
]


class CheckResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.unsupported: list[str] = []
        self.blocked: list[str] = []
        self.probes: list[dict[str, Any]] = []

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_unsupported(self, message: str) -> None:
        self.unsupported.append(message)

    def add_block(self, message: str) -> None:
        self.blocked.append(message)

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked"
        if self.errors:
            return "failed"
        if self.warnings:
            return "degraded"
        if self.unsupported:
            return "unsupported"
        return "pass"

    def to_dict(self, *, manifest_path: str | None, source_count: int) -> dict[str, Any]:
        return {
            "status": self.status,
            "manifest_path": manifest_path,
            "source_count": source_count,
            "blocked_count": len(self.blocked),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "unsupported_count": len(self.unsupported),
            "probe_count": len(self.probes),
            "blocked": self.blocked,
            "errors": self.errors,
            "warnings": self.warnings,
            "unsupported": self.unsupported,
            "probes": self.probes,
        }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a JSON object")
    return data


def parse_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def path_join(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def scan_disallowed_fields(value: Any, result: CheckResult, path: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path_join(path, str(key))
            for pattern in DISALLOWED_KEY_PATTERNS:
                if pattern.search(str(key)):
                    result.add_block(f"disallowed field key at {child_path}: {key}")
                    break
            scan_disallowed_fields(child, result, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_disallowed_fields(child, result, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in DISALLOWED_VALUE_PATTERNS:
            if pattern.search(value):
                result.add_block(f"disallowed field value at {path}: matched {pattern.pattern!r}")
                break


def validate_url(value: Any, result: CheckResult, source_label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        result.add_error(f"{source_label}: source_url must be a non-empty string")
        return
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        result.add_error(f"{source_label}: source_url must be an https URL")


def validate_string_list(value: Any, result: CheckResult, source_label: str, field: str) -> None:
    if not isinstance(value, list) or not value:
        result.add_error(f"{source_label}: {field} must be a non-empty list")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            result.add_error(f"{source_label}: {field}[{index}] must be a non-empty string")


def validate_freshness(value: Any, result: CheckResult, source_label: str) -> None:
    if not isinstance(value, dict):
        result.add_error(f"{source_label}: freshness_or_degradation_status must be an object")
        return
    status = value.get("status")
    as_of = value.get("as_of_utc")
    reason = value.get("reason")
    if status not in ALLOWED_FRESHNESS_STATUSES:
        result.add_error(f"{source_label}: freshness status must be one of {sorted(ALLOWED_FRESHNESS_STATUSES)}")
    if not isinstance(as_of, str) or not parse_utc_timestamp(as_of):
        result.add_error(f"{source_label}: freshness as_of_utc must be a UTC timestamp")
    if not isinstance(reason, str) or not reason.strip():
        result.add_error(f"{source_label}: freshness reason must be a non-empty string")
    if status == "unsupported":
        result.add_unsupported(f"{source_label}: source is explicitly unsupported for option-only ingestion")
    elif status == "degraded":
        result.add_warning(f"{source_label}: source is explicitly degraded: {reason}")


def content_type_matches(expected: str, content_type: str, body_prefix: bytes) -> bool:
    normalized = content_type.lower()
    stripped = body_prefix.lstrip().lower()
    if expected == "json":
        return "json" in normalized or stripped.startswith((b"{", b"["))
    if expected == "html":
        return "html" in normalized or b"<html" in stripped[:512] or b"<!doctype html" in stripped[:512]
    if expected == "text":
        return normalized.startswith("text/") or bool(body_prefix)
    return False


def probe_public_url(source: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    url = source["source_url"]
    request = Request(
        url,
        headers={
            "User-Agent": "post-fiat-public-source-coverage-probe/1.0",
            "Accept": "application/json,text/html,text/plain,*/*;q=0.2",
            "Range": "bytes=0-4095",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(4096)
            return {
                "source_id": source.get("source_id"),
                "source_url": url,
                "http_status": response.getcode(),
                "content_type": response.headers.get("content-type", ""),
                "body_prefix_bytes": len(body),
                "network_error": "",
            }
    except HTTPError as exc:
        return {
            "source_id": source.get("source_id"),
            "source_url": url,
            "http_status": exc.code,
            "content_type": exc.headers.get("content-type", "") if exc.headers else "",
            "body_prefix_bytes": 0,
            "network_error": f"HTTPError: {exc.reason}",
        }
    except (URLError, TimeoutError, socket.timeout) as exc:
        return {
            "source_id": source.get("source_id"),
            "source_url": url,
            "http_status": None,
            "content_type": "",
            "body_prefix_bytes": 0,
            "network_error": exc.__class__.__name__,
        }


def apply_probe_result(
    source: dict[str, Any],
    probe: dict[str, Any],
    result: CheckResult,
    source_label: str,
    *,
    body_prefix: bytes = b"",
) -> None:
    result.probes.append(probe)
    status = probe.get("http_status")
    expected = source.get("expected_content_type")
    if status is None:
        result.add_warning(f"{source_label}: probe unavailable: {probe.get('network_error') or 'network error'}")
        return
    if 200 <= int(status) < 400:
        if not content_type_matches(str(expected), str(probe.get("content_type", "")), body_prefix):
            result.add_warning(
                f"{source_label}: content type {probe.get('content_type')!r} did not clearly match expected {expected!r}"
            )
        return
    if int(status) in SOFT_HTTP_FAILURES:
        if source.get("safe_failure_reasons"):
            result.add_warning(f"{source_label}: public probe returned HTTP {status}; safe failure reason is declared")
        else:
            result.add_error(f"{source_label}: public probe returned HTTP {status} without safe_failure_reasons")
        return
    result.add_warning(f"{source_label}: public probe returned unexpected HTTP {status}")


def validate_source(source: Any, result: CheckResult, index: int, *, probe: bool, timeout: float) -> None:
    source_label = f"sources[{index}]"
    if not isinstance(source, dict):
        result.add_error(f"{source_label}: source record must be an object")
        return
    source_id = source.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        source_label = f"{source_label}({source_id})"
    missing = sorted(REQUIRED_SOURCE_FIELDS - set(source))
    if missing:
        result.add_error(f"{source_label}: missing required fields: {', '.join(missing)}")
        return
    for field in REQUIRED_SOURCE_FIELDS:
        if field in {"unsupported_cases", "safe_failure_reasons", "freshness_or_degradation_status"}:
            continue
        if not is_non_empty(source.get(field)):
            result.add_error(f"{source_label}: {field} must be non-empty")
    validate_url(source.get("source_url"), result, source_label)
    if source.get("product_family") not in ALLOWED_PRODUCT_FAMILIES:
        result.add_error(f"{source_label}: product_family must be one of {sorted(ALLOWED_PRODUCT_FAMILIES)}")
    if source.get("expected_content_type") not in ALLOWED_CONTENT_TYPES:
        result.add_error(f"{source_label}: expected_content_type must be one of {sorted(ALLOWED_CONTENT_TYPES)}")
    if source.get("auth_requirement") not in ALLOWED_AUTH_REQUIREMENTS:
        result.add_error(f"{source_label}: auth_requirement must be one of {sorted(ALLOWED_AUTH_REQUIREMENTS)}")
    validate_freshness(source.get("freshness_or_degradation_status"), result, source_label)
    validate_string_list(source.get("unsupported_cases"), result, source_label, "unsupported_cases")
    validate_string_list(source.get("safe_failure_reasons"), result, source_label, "safe_failure_reasons")
    if probe and not result.blocked:
        probe_result = probe_public_url(source, timeout=timeout)
        apply_probe_result(source, probe_result, result, source_label)


def validate_manifest(data: dict[str, Any], *, manifest_path: str | None, probe: bool, timeout: float) -> dict[str, Any]:
    result = CheckResult()
    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS - set(data))
    if missing:
        result.add_error(f"manifest missing required top-level fields: {', '.join(missing)}")
        return result.to_dict(manifest_path=manifest_path, source_count=0)
    scan_disallowed_fields(data, result)
    generated_at = data.get("generated_at_utc")
    if not isinstance(generated_at, str) or not parse_utc_timestamp(generated_at):
        result.add_error("generated_at_utc must be a UTC timestamp")
    declared_fields = data.get("required_source_fields")
    if not isinstance(declared_fields, list) or set(declared_fields) != REQUIRED_SOURCE_FIELDS:
        result.add_error("required_source_fields must exactly match the checker-required source fields")
    scope = data.get("scope")
    if not isinstance(scope, dict) or scope.get("data_class") != "public_metadata_only":
        result.add_error("scope.data_class must be public_metadata_only")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        result.add_error("sources must be a non-empty list")
        return result.to_dict(manifest_path=manifest_path, source_count=0)
    seen_ids: set[str] = set()
    for index, source in enumerate(sources):
        if isinstance(source, dict):
            source_id = source.get("source_id")
            if isinstance(source_id, str):
                if source_id in seen_ids:
                    result.add_error(f"sources[{index}]({source_id}): duplicate source_id")
                seen_ids.add(source_id)
        validate_source(source, result, index, probe=probe, timeout=timeout)
    return result.to_dict(manifest_path=manifest_path, source_count=len(sources))


def make_blocked_self_test(base: dict[str, Any]) -> dict[str, Any]:
    blocked = copy.deepcopy(base)
    blocked["sources"][0]["account_identifier"] = "placeholder-account"
    blocked["sources"][0]["safe_failure_reasons"].append("private account export")
    return blocked


def make_unsupported_self_test(base: dict[str, Any]) -> dict[str, Any]:
    unsupported = copy.deepcopy(base)
    unsupported["sources"] = [copy.deepcopy(base["sources"][-1])]
    return unsupported


def run_self_test() -> dict[str, Any]:
    manifest = load_json(DEFAULT_MANIFEST)
    valid_result = validate_manifest(manifest, manifest_path=str(DEFAULT_MANIFEST), probe=False, timeout=2.0)
    unsupported_result = validate_manifest(
        make_unsupported_self_test(manifest),
        manifest_path="<self-test:unsupported-source>",
        probe=False,
        timeout=2.0,
    )
    blocked_result = validate_manifest(
        make_blocked_self_test(manifest),
        manifest_path="<self-test:blocked-field>",
        probe=False,
        timeout=2.0,
    )

    tests = [
        {
            "name": "valid_public_manifest_offline",
            "expected_status": ["degraded"],
            "actual_status": valid_result["status"],
            "passed": valid_result["status"] == "degraded",
            "result": valid_result,
        },
        {
            "name": "explicit_unsupported_source_status",
            "expected_status": ["unsupported"],
            "actual_status": unsupported_result["status"],
            "passed": unsupported_result["status"] == "unsupported",
            "result": unsupported_result,
        },
        {
            "name": "disallowed_private_account_field_blocks",
            "expected_status": ["blocked"],
            "actual_status": blocked_result["status"],
            "passed": blocked_result["status"] == "blocked",
            "result": blocked_result,
        },
    ]
    return {"status": "pass" if all(test["passed"] for test in tests) else "failed", "tests": tests}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", nargs="?", default=str(DEFAULT_MANIFEST), help="Path to the source manifest JSON")
    parser.add_argument("--probe", action="store_true", help="Probe public URLs with bounded GET requests")
    parser.add_argument("--timeout", type=float, default=8.0, help="Per-source probe timeout in seconds")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic synthetic self-tests")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.self_test:
            output = run_self_test()
        else:
            manifest_path = Path(args.manifest)
            output = validate_manifest(
                load_json(manifest_path),
                manifest_path=str(manifest_path),
                probe=args.probe,
                timeout=args.timeout,
            )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        output = {"status": "failed", "error": str(exc)}
    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    if output.get("status") in {"blocked", "failed"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
