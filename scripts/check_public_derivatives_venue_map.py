#!/usr/bin/env python3
"""Validate a public crypto derivatives venue-map metadata fixture.

Run:
  python3 scripts/check_public_derivatives_venue_map.py fixtures/public_derivatives_venue_map.json --pretty
  python3 scripts/check_public_derivatives_venue_map.py --self-test
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_FIXTURE = Path("fixtures/public_derivatives_venue_map.json")

REQUIRED_TOP_LEVEL_FIELDS = {
    "venue_map_version",
    "generated_at_utc",
    "scope",
    "required_row_fields",
    "rows",
}

REQUIRED_ROW_FIELDS = {
    "surface_id",
    "venue_id",
    "venue_name",
    "product_family",
    "underlying_exposure",
    "contract_type",
    "quote_asset",
    "collateral_asset",
    "settlement_asset",
    "settlement_style",
    "instrument_semantics",
    "session_caveats",
    "timestamp_caveats",
    "public_source_urls",
    "source_freshness",
    "degradation",
    "explicit_unknowns",
}

ALLOWED_PRODUCT_FAMILIES = {
    "crypto_option",
    "crypto_option_on_futures",
    "listed_etf_share_option",
    "crypto_perpetual_reference",
}

ALLOWED_FRESHNESS_STATUSES = {"fresh", "unknown", "degraded", "stale"}
ALLOWED_SEVERITIES = {"none", "info", "warning", "blocked"}
UNKNOWN_MARKERS = {"unknown", "explicit_unknown", "not_applicable"}

DISALLOWED_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\baccount(_id|_identifier|_address)?\b",
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


class ValidationResult:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.blocked: list[str] = []

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

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
        return "pass"

    def to_dict(self, *, fixture_path: str | None, row_count: int) -> dict[str, Any]:
        return {
            "status": self.status,
            "fixture_path": fixture_path,
            "row_count": row_count,
            "blocked_count": len(self.blocked),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "blocked": self.blocked,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("fixture root must be a JSON object")
    return data


def is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def parse_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def path_join(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def scan_disallowed_fields(value: Any, result: ValidationResult, path: str = "") -> None:
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


def validate_public_source_urls(row: dict[str, Any], result: ValidationResult, row_label: str) -> None:
    urls = row.get("public_source_urls")
    if not isinstance(urls, list) or not urls:
        result.add_error(f"{row_label}: public_source_urls must be a non-empty list")
        return
    for index, url in enumerate(urls):
        if not isinstance(url, str) or not url.strip():
            result.add_error(f"{row_label}: public_source_urls[{index}] must be a non-empty string")
            continue
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            result.add_error(f"{row_label}: public_source_urls[{index}] must be an https URL")


def unknown_fields(row: dict[str, Any]) -> set[str]:
    fields = set()
    for field in ("quote_asset", "collateral_asset", "settlement_asset", "settlement_style"):
        value = str(row.get(field, "")).strip().lower()
        if value in UNKNOWN_MARKERS or value.startswith("unknown"):
            fields.add(field)
    return fields


def explicit_unknown_fields(row: dict[str, Any], result: ValidationResult, row_label: str) -> set[str]:
    entries = row.get("explicit_unknowns")
    if not isinstance(entries, list):
        result.add_error(f"{row_label}: explicit_unknowns must be a list")
        return set()
    fields = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            result.add_error(f"{row_label}: explicit_unknowns[{index}] must be an object")
            continue
        field = entry.get("field")
        reason = entry.get("reason")
        if not isinstance(field, str) or not field.strip():
            result.add_error(f"{row_label}: explicit_unknowns[{index}].field is required")
        else:
            fields.add(field)
        if not isinstance(reason, str) or not reason.strip():
            result.add_error(f"{row_label}: explicit_unknowns[{index}].reason is required")
    return fields


def validate_freshness_and_degradation(row: dict[str, Any], result: ValidationResult, row_label: str) -> None:
    freshness = row.get("source_freshness")
    degradation = row.get("degradation")
    if not isinstance(freshness, dict):
        result.add_error(f"{row_label}: source_freshness must be an object")
        return
    if not isinstance(degradation, dict):
        result.add_error(f"{row_label}: degradation must be an object")
        return

    freshness_status = freshness.get("status")
    if freshness_status not in ALLOWED_FRESHNESS_STATUSES:
        result.add_error(f"{row_label}: source_freshness.status must be one of {sorted(ALLOWED_FRESHNESS_STATUSES)}")

    as_of = freshness.get("as_of_utc")
    if not isinstance(as_of, str) or not parse_utc_timestamp(as_of):
        result.add_error(f"{row_label}: source_freshness.as_of_utc must be a UTC ISO-8601 timestamp")

    degraded = degradation.get("degraded")
    severity = degradation.get("severity")
    reasons = degradation.get("reasons")
    block_reason = degradation.get("block_reason")
    if not isinstance(degraded, bool):
        result.add_error(f"{row_label}: degradation.degraded must be boolean")
    if severity not in ALLOWED_SEVERITIES:
        result.add_error(f"{row_label}: degradation.severity must be one of {sorted(ALLOWED_SEVERITIES)}")
    if not isinstance(reasons, list):
        result.add_error(f"{row_label}: degradation.reasons must be a list")
    if not isinstance(block_reason, str):
        result.add_error(f"{row_label}: degradation.block_reason must be a string")

    if freshness_status in {"degraded", "stale"} and degraded is False:
        result.add_warning(f"{row_label}: freshness is {freshness_status} but degradation.degraded is false")
    if degraded is True and severity == "none":
        result.add_error(f"{row_label}: degraded rows must not use severity=none")
    if degraded is False and severity in {"warning", "blocked"}:
        result.add_warning(f"{row_label}: non-degraded row uses severity={severity}")
    if severity == "blocked" and not block_reason:
        result.add_error(f"{row_label}: blocked severity requires degradation.block_reason")


def validate_row(row: Any, result: ValidationResult, index: int) -> None:
    row_label = f"rows[{index}]"
    if not isinstance(row, dict):
        result.add_error(f"{row_label}: row must be an object")
        return

    surface_id = row.get("surface_id")
    if isinstance(surface_id, str) and surface_id.strip():
        row_label = f"rows[{index}]({surface_id})"

    missing = sorted(field for field in REQUIRED_ROW_FIELDS if field not in row)
    if missing:
        result.add_error(f"{row_label}: missing required fields: {', '.join(missing)}")

    for field in REQUIRED_ROW_FIELDS:
        if field in row and not is_non_empty(row[field]):
            if field != "explicit_unknowns":
                result.add_error(f"{row_label}: {field} must be non-empty")

    if row.get("product_family") not in ALLOWED_PRODUCT_FAMILIES:
        result.add_error(f"{row_label}: product_family must be one of {sorted(ALLOWED_PRODUCT_FAMILIES)}")

    if not isinstance(row.get("instrument_semantics"), dict):
        result.add_error(f"{row_label}: instrument_semantics must be an object")
    else:
        normalized_key = row["instrument_semantics"].get("normalized_surface_key")
        if not isinstance(normalized_key, str) or normalized_key.count(".") < 2:
            result.add_error(f"{row_label}: instrument_semantics.normalized_surface_key must be a dotted semantic key")

    for field in ("session_caveats", "timestamp_caveats"):
        values = row.get(field)
        if not isinstance(values, list) or not all(isinstance(item, str) and item.strip() for item in values):
            result.add_error(f"{row_label}: {field} must be a non-empty list of strings")

    validate_public_source_urls(row, result, row_label)
    explicit_fields = explicit_unknown_fields(row, result, row_label)
    unresolved_fields = unknown_fields(row)
    missing_unknown_entries = sorted(unresolved_fields - explicit_fields)
    if missing_unknown_entries:
        result.add_error(
            f"{row_label}: unknown fields require explicit_unknowns entries: {', '.join(missing_unknown_entries)}"
        )

    validate_freshness_and_degradation(row, result, row_label)


def validate_fixture(data: dict[str, Any], *, fixture_path: str | None = None) -> dict[str, Any]:
    result = ValidationResult()
    scan_disallowed_fields(data, result)

    missing_top = sorted(field for field in REQUIRED_TOP_LEVEL_FIELDS if field not in data)
    if missing_top:
        result.add_error(f"fixture: missing top-level fields: {', '.join(missing_top)}")

    generated = data.get("generated_at_utc")
    if not isinstance(generated, str) or not parse_utc_timestamp(generated):
        result.add_error("fixture: generated_at_utc must be a UTC ISO-8601 timestamp")

    declared_required = data.get("required_row_fields")
    if not isinstance(declared_required, list):
        result.add_error("fixture: required_row_fields must be a list")
    else:
        missing_declarations = sorted(REQUIRED_ROW_FIELDS - set(declared_required))
        if missing_declarations:
            result.add_error(
                "fixture: required_row_fields omits checker-required fields: "
                + ", ".join(missing_declarations)
            )

    rows = data.get("rows")
    row_count = len(rows) if isinstance(rows, list) else 0
    if not isinstance(rows, list) or not rows:
        result.add_error("fixture: rows must be a non-empty list")
    else:
        seen_surface_ids: set[str] = set()
        for index, row in enumerate(rows):
            validate_row(row, result, index)
            if isinstance(row, dict):
                surface_id = row.get("surface_id")
                if isinstance(surface_id, str):
                    if surface_id in seen_surface_ids:
                        result.add_error(f"rows[{index}]({surface_id}): duplicate surface_id")
                    seen_surface_ids.add(surface_id)

    return result.to_dict(fixture_path=fixture_path, row_count=row_count)


def make_blocked_self_test(base: dict[str, Any]) -> dict[str, Any]:
    blocked = copy.deepcopy(base)
    blocked["rows"][0]["account_identifier"] = "placeholder-account"
    blocked["rows"][0]["degradation"]["severity"] = "blocked"
    blocked["rows"][0]["degradation"]["block_reason"] = "contains private account field"
    return blocked


def run_self_test() -> dict[str, Any]:
    fixture = load_json(DEFAULT_FIXTURE)
    valid_result = validate_fixture(fixture, fixture_path=str(DEFAULT_FIXTURE))

    unknown_case = copy.deepcopy(fixture)
    unknown_case["rows"][0]["settlement_asset"] = "unknown"
    unknown_case["rows"][0]["explicit_unknowns"] = []
    unknown_result = validate_fixture(unknown_case, fixture_path="<self-test:missing-explicit-unknown>")

    blocked_result = validate_fixture(make_blocked_self_test(fixture), fixture_path="<self-test:blocked-field>")

    tests = [
        {
            "name": "valid_public_fixture",
            "expected_status": ["pass", "degraded"],
            "actual_status": valid_result["status"],
            "passed": valid_result["status"] in {"pass", "degraded"},
            "result": valid_result,
        },
        {
            "name": "unknown_requires_explicit_unknown_entry",
            "expected_status": ["failed"],
            "actual_status": unknown_result["status"],
            "passed": unknown_result["status"] == "failed",
            "result": unknown_result,
        },
        {
            "name": "disallowed_private_account_field_blocks",
            "expected_status": ["blocked"],
            "actual_status": blocked_result["status"],
            "passed": blocked_result["status"] == "blocked",
            "result": blocked_result,
        },
    ]
    return {
        "status": "pass" if all(test["passed"] for test in tests) else "failed",
        "tests": tests,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fixture",
        nargs="?",
        default=str(DEFAULT_FIXTURE),
        help=f"Path to venue-map JSON fixture. Defaults to {DEFAULT_FIXTURE}.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument("--self-test", action="store_true", help="Run embedded synthetic QA tests.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        output = run_self_test()
        print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
        return 0 if output["status"] == "pass" else 2

    path = Path(args.fixture)
    output = validate_fixture(load_json(path), fixture_path=str(path))
    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    return 2 if output["status"] in {"blocked", "failed"} else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
