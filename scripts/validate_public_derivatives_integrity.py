#!/usr/bin/env python3
"""
Validate public crypto derivatives market-data integrity records.

This script is intentionally self-contained and stdlib-only. It accepts a JSON
record from stdin or --file, or runs embedded synthetic self-tests with
--self-test. It checks data-quality and shareability fields only; it does not
produce trading signals, venue rankings, execution advice, or model thresholds.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


FRESHNESS_STATUSES = {"fresh", "acceptable", "stale", "missing", "unknown", "blocked"}
PROVENANCE_STATUSES = {"present", "missing", "not_applicable", "degraded", "blocked"}
REPLAY_STATUSES = {"exact", "approximate", "not_replayable", "blocked", "unknown"}
OUTCOMES = {"pass", "degraded", "blocked", "needs_human_review"}

PROVENANCE_FIELDS = (
    "open_interest",
    "implied_volatility",
    "greeks",
    "mark_price",
    "index_price",
)

FORBIDDEN_TEXT_MARKERS = (
    "private key",
    "mnemonic",
    "seed phrase",
    "auth header",
    "authorization:",
    "cookie:",
    "oauth token",
    "api secret",
    "wallet secret",
    "account balance",
    "private position",
    "customer data",
    "mnpi",
    "model threshold",
    "venue ranking",
    "execution signal",
    "buy signal",
    "sell signal",
    "investment advice",
)


@dataclass
class Finding:
    severity: str
    path: str
    message: str


@dataclass
class ValidationResult:
    outcome: str = "pass"
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: str, path: str, message: str) -> None:
        self.findings.append(Finding(severity, path, message))

    def finalize(self) -> "ValidationResult":
        severities = {finding.severity for finding in self.findings}
        if "blocked" in severities:
            self.outcome = "blocked"
        elif "needs_human_review" in severities:
            self.outcome = "needs_human_review"
        elif "degraded" in severities:
            self.outcome = "degraded"
        else:
            self.outcome = "pass"
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "finding_count": len(self.findings),
            "findings": [finding.__dict__ for finding in self.findings],
        }


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def root_record(record: dict[str, Any]) -> dict[str, Any]:
    if "derivatives_market_data_integrity" in record:
        nested = record["derivatives_market_data_integrity"]
        if isinstance(nested, dict):
            return nested
    return record


def iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from iter_string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_string_values(nested)


def require(result: ValidationResult, condition: bool, severity: str, path: str, message: str) -> None:
    if not condition:
        result.add(severity, path, message)


def require_value(result: ValidationResult, record: dict[str, Any], key: str, path: str) -> Any:
    value = record.get(key)
    if value in (None, ""):
        result.add("needs_human_review", path, f"missing required field {key}")
    return value


def validate_artifact_policy(record: dict[str, Any], result: ValidationResult) -> None:
    require(result, record.get("public_data_only") is True, "blocked", "public_data_only", "must be true")
    require(
        result,
        record.get("trading_signal_claims") is False,
        "blocked",
        "trading_signal_claims",
        "must be false for this public QA artifact",
    )

    validation = record.get("artifact_validation")
    if isinstance(validation, dict):
        boolean_blocks = {
            "accessible_without_login": False,
            "contains_private_data": True,
            "contains_wallet_or_auth_material": True,
            "contains_mnpi": True,
            "contains_trading_signal_claims": True,
        }
        for key, blocking_value in boolean_blocks.items():
            if key not in validation:
                result.add("needs_human_review", f"artifact_validation.{key}", "missing shareability field")
            elif validation.get(key) is blocking_value:
                result.add("blocked", f"artifact_validation.{key}", "shareability policy block")
    else:
        result.add("needs_human_review", "artifact_validation", "missing artifact validation object")

    blob = "\n".join(iter_string_values(record)).lower()
    for marker in FORBIDDEN_TEXT_MARKERS:
        if marker in blob:
            result.add("blocked", "record", f"forbidden public-artifact marker found: {marker}")


def validate_normalized_id(instrument: dict[str, Any], path: str, result: ValidationResult) -> None:
    instrument_type = instrument.get("instrument_type")
    normalized_id = instrument.get("normalized_instrument_id")
    settlement_asset = instrument.get("settlement_asset")

    if not isinstance(normalized_id, str) or ":" not in normalized_id:
        result.add("needs_human_review", f"{path}.normalized_instrument_id", "missing or malformed normalized ID")
        return

    expected_prefix = {
        "option": "option:",
        "perpetual": "perpetual:",
        "future": "future:",
        "etf_option": "etf_option:",
        "index": "index:",
        "mark": "mark:",
    }.get(str(instrument_type))
    if expected_prefix and not normalized_id.startswith(expected_prefix):
        result.add(
            "needs_human_review",
            f"{path}.normalized_instrument_id",
            f"ID prefix does not match instrument_type {instrument_type}",
        )

    if settlement_asset and settlement_asset != "not_applicable":
        if not normalized_id.endswith(f":{settlement_asset}"):
            result.add(
                "needs_human_review",
                f"{path}.normalized_instrument_id",
                "ID should preserve settlement_asset as the final segment",
            )


def validate_timestamps(instrument: dict[str, Any], path: str, result: ValidationResult) -> str:
    timestamps = instrument.get("timestamps")
    if not isinstance(timestamps, dict):
        result.add("needs_human_review", f"{path}.timestamps", "missing timestamps object")
        return "unknown"

    capture_time = timestamps.get("capture_time_utc")
    exchange_time = timestamps.get("exchange_timestamp_utc")
    freshness = timestamps.get("freshness_status")

    if parse_utc(capture_time) is None:
        result.add("needs_human_review", f"{path}.timestamps.capture_time_utc", "missing or invalid UTC timestamp")
    if freshness not in FRESHNESS_STATUSES:
        result.add("needs_human_review", f"{path}.timestamps.freshness_status", "unknown freshness status")
    elif freshness == "blocked":
        result.add("blocked", f"{path}.timestamps.freshness_status", "timestamp quality is blocked")
    elif freshness in {"stale", "missing"}:
        result.add("degraded", f"{path}.timestamps.freshness_status", f"freshness is {freshness}")
    elif freshness == "unknown":
        result.add("needs_human_review", f"{path}.timestamps.freshness_status", "freshness is unknown")

    if exchange_time in (None, "") and freshness not in {"missing", "unknown", "blocked"}:
        result.add(
            "needs_human_review",
            f"{path}.timestamps.exchange_timestamp_utc",
            "exchange timestamp missing without matching freshness status",
        )
    elif exchange_time not in (None, "") and parse_utc(exchange_time) is None:
        result.add("needs_human_review", f"{path}.timestamps.exchange_timestamp_utc", "invalid UTC timestamp")

    return str(freshness)


def validate_provenance(instrument: dict[str, Any], path: str, result: ValidationResult) -> None:
    provenance = instrument.get("provenance")
    if not isinstance(provenance, dict):
        result.add("needs_human_review", f"{path}.provenance", "missing provenance object")
        return

    for field_name in PROVENANCE_FIELDS:
        field_value = provenance.get(field_name)
        field_path = f"{path}.provenance.{field_name}"
        if not isinstance(field_value, dict):
            result.add("needs_human_review", field_path, "missing provenance field object")
            continue
        status = field_value.get("status")
        if status not in PROVENANCE_STATUSES:
            result.add("needs_human_review", f"{field_path}.status", "unknown provenance status")
            continue
        if status == "blocked":
            if not field_value.get("block_reason"):
                result.add("needs_human_review", f"{field_path}.block_reason", "blocked field lacks block_reason")
            result.add("blocked", f"{field_path}.status", "blocked provenance field")
        elif status == "degraded":
            if not field_value.get("degraded_reason"):
                result.add("needs_human_review", f"{field_path}.degraded_reason", "degraded field lacks reason")
            result.add("degraded", f"{field_path}.status", "degraded provenance field")
        elif status == "missing":
            result.add("degraded", f"{field_path}.status", "public provenance field is missing")


def validate_replay_and_degradation(instrument: dict[str, Any], path: str, result: ValidationResult) -> None:
    replay = instrument.get("replay_parity")
    if not isinstance(replay, dict):
        result.add("needs_human_review", f"{path}.replay_parity", "missing replay parity object")
    else:
        status = replay.get("status")
        if status not in REPLAY_STATUSES:
            result.add("needs_human_review", f"{path}.replay_parity.status", "unknown replay status")
        elif status == "blocked":
            result.add("blocked", f"{path}.replay_parity.status", "replay parity is blocked")
        elif status == "approximate":
            result.add("degraded", f"{path}.replay_parity.status", "replay parity is approximate")
        elif status in {"not_replayable", "unknown"}:
            result.add("needs_human_review", f"{path}.replay_parity.status", f"replay parity is {status}")
        if replay.get("no_future_leakage_checked") is not True:
            result.add("needs_human_review", f"{path}.replay_parity.no_future_leakage_checked", "must be true")

    degradation = instrument.get("degradation")
    if not isinstance(degradation, dict):
        result.add("needs_human_review", f"{path}.degradation", "missing degradation object")
        return
    severity = degradation.get("severity")
    if severity == "blocked" or degradation.get("block_reason"):
        result.add("blocked", f"{path}.degradation", "record is explicitly blocked")
    elif degradation.get("degraded") is True or severity in {"info", "warning"}:
        result.add("degraded", f"{path}.degradation", "record declares degradation")
    elif degradation.get("degraded") is not False:
        result.add("needs_human_review", f"{path}.degradation.degraded", "missing boolean degradation summary")


def iter_instruments(record: dict[str, Any], result: ValidationResult) -> Iterable[tuple[str, dict[str, Any]]]:
    venue_coverage = record.get("venue_coverage")
    if not isinstance(venue_coverage, list) or not venue_coverage:
        result.add("needs_human_review", "venue_coverage", "must be a non-empty list")
        return []

    instruments: list[tuple[str, dict[str, Any]]] = []
    for venue_index, venue in enumerate(venue_coverage):
        venue_path = f"venue_coverage[{venue_index}]"
        if not isinstance(venue, dict):
            result.add("needs_human_review", venue_path, "venue coverage entry must be an object")
            continue
        require_value(result, venue, "venue", f"{venue_path}.venue")
        require_value(result, venue, "market_segment", f"{venue_path}.market_segment")
        coverage_window = venue.get("coverage_window")
        if not isinstance(coverage_window, dict):
            result.add("needs_human_review", f"{venue_path}.coverage_window", "missing coverage window")
        else:
            if coverage_window.get("full_window_coverage") is not True:
                result.add("degraded", f"{venue_path}.coverage_window.full_window_coverage", "partial window coverage")
        venue_instruments = venue.get("instruments")
        if not isinstance(venue_instruments, list) or not venue_instruments:
            result.add("needs_human_review", f"{venue_path}.instruments", "must be a non-empty list")
            continue
        for instrument_index, instrument in enumerate(venue_instruments):
            instrument_path = f"{venue_path}.instruments[{instrument_index}]"
            if isinstance(instrument, dict):
                instruments.append((instrument_path, instrument))
            else:
                result.add("needs_human_review", instrument_path, "instrument entry must be an object")
    return instruments


def validate_record(raw_record: dict[str, Any]) -> ValidationResult:
    result = ValidationResult()
    record = root_record(raw_record)

    for key in ("schema_version", "artifact_id", "declared_at_utc"):
        require_value(result, record, key, key)
    if parse_utc(record.get("declared_at_utc")) is None:
        result.add("needs_human_review", "declared_at_utc", "missing or invalid UTC timestamp")

    validate_artifact_policy(record, result)

    for path, instrument in iter_instruments(record, result):
        for key in ("instrument_type", "venue_symbol", "normalized_instrument_id", "settlement_asset"):
            require_value(result, instrument, key, f"{path}.{key}")
        source = instrument.get("source")
        if not isinstance(source, dict):
            result.add("needs_human_review", f"{path}.source", "missing source object")
        else:
            require_value(result, source, "source_url_or_endpoint_label", f"{path}.source.source_url_or_endpoint_label")
        validate_normalized_id(instrument, path, result)
        validate_timestamps(instrument, path, result)
        validate_provenance(instrument, path, result)
        validate_replay_and_degradation(instrument, path, result)

    return result.finalize()


def base_record() -> dict[str, Any]:
    return {
        "derivatives_market_data_integrity": {
            "schema_version": "1.0",
            "artifact_id": "synthetic-public-btc-option",
            "declared_at_utc": "2026-05-10T14:00:00Z",
            "public_data_only": True,
            "trading_signal_claims": False,
            "venue_coverage": [
                {
                    "venue": "example_options_venue",
                    "market_segment": "options",
                    "coverage_window": {
                        "start_utc": "2026-05-10T14:00:00Z",
                        "end_utc": "2026-05-10T14:05:00Z",
                        "full_window_coverage": True,
                        "known_gaps": [],
                    },
                    "instruments": [
                        {
                            "instrument_type": "option",
                            "venue_symbol": "BTC-29MAY26-100000-C",
                            "normalized_instrument_id": (
                                "option:example_options_venue:BTC:"
                                "2026-05-29T08:00:00Z:100000:call:BTC"
                            ),
                            "base_asset": "BTC",
                            "quote_asset": "USD",
                            "settlement_asset": "BTC",
                            "source": {
                                "source_type": "public_api",
                                "source_url_or_endpoint_label": "public.example.invalid/options",
                                "source_method": "rest",
                            },
                            "timestamps": {
                                "capture_time_utc": "2026-05-10T14:05:01Z",
                                "exchange_timestamp_utc": "2026-05-10T14:05:00Z",
                                "freshness_status": "fresh",
                            },
                            "provenance": {
                                "open_interest": {
                                    "status": "present",
                                    "source_field": "open_interest",
                                    "unit": "contracts",
                                },
                                "implied_volatility": {
                                    "status": "present",
                                    "source_field": "mark_iv",
                                    "unit": "decimal",
                                },
                                "greeks": {
                                    "status": "present",
                                    "source_field_or_method": "public greeks fields",
                                    "greek_set": ["delta", "gamma", "vega", "theta"],
                                },
                                "mark_price": {
                                    "status": "present",
                                    "source_field": "mark_price",
                                    "quote_unit": "USD",
                                },
                                "index_price": {
                                    "status": "present",
                                    "source_field": "index_price",
                                    "quote_unit": "USD",
                                },
                            },
                            "replay_parity": {
                                "status": "exact",
                                "event_time_ordering": "preserved",
                                "snapshot_delta_consistency": "validated",
                                "no_future_leakage_checked": True,
                            },
                            "degradation": {
                                "degraded": False,
                                "severity": "none",
                                "reasons": [],
                                "block_reason": "",
                            },
                        }
                    ],
                }
            ],
            "artifact_validation": {
                "accessible_without_login": True,
                "contains_private_data": False,
                "contains_wallet_or_auth_material": False,
                "contains_mnpi": False,
                "contains_trading_signal_claims": False,
            },
        }
    }


def synthetic_records() -> dict[str, tuple[str, dict[str, Any]]]:
    passing = base_record()

    degraded = copy.deepcopy(passing)
    degraded_root = degraded["derivatives_market_data_integrity"]
    degraded_root["artifact_id"] = "synthetic-degraded-public-btc-option"
    degraded_instrument = degraded_root["venue_coverage"][0]["instruments"][0]
    degraded_instrument["timestamps"]["freshness_status"] = "stale"
    degraded_instrument["provenance"]["open_interest"] = {
        "status": "degraded",
        "source_field": "open_interest",
        "unit": "contracts",
        "degraded_reason": "public endpoint reports delayed open interest",
    }
    degraded_instrument["replay_parity"]["status"] = "approximate"
    degraded_instrument["degradation"] = {
        "degraded": True,
        "severity": "warning",
        "reasons": ["stale timestamp", "delayed open interest"],
        "block_reason": "",
    }

    blocked = copy.deepcopy(passing)
    blocked_root = blocked["derivatives_market_data_integrity"]
    blocked_root["artifact_id"] = "synthetic-blocked-private-material"
    blocked_root["public_data_only"] = False
    blocked_root["artifact_validation"]["contains_wallet_or_auth_material"] = True
    blocked_instrument = blocked_root["venue_coverage"][0]["instruments"][0]
    blocked_instrument["timestamps"]["freshness_status"] = "blocked"
    blocked_instrument["provenance"]["greeks"] = {
        "status": "blocked",
        "source_field_or_method": "private account export required",
        "block_reason": "would require private account data",
    }
    blocked_instrument["replay_parity"]["status"] = "blocked"
    blocked_instrument["degradation"] = {
        "degraded": True,
        "severity": "blocked",
        "reasons": ["private material required"],
        "block_reason": "not public-data-only",
    }

    review = copy.deepcopy(passing)
    review_root = review["derivatives_market_data_integrity"]
    review_root["artifact_id"] = "synthetic-review-missing-replay"
    review_instrument = review_root["venue_coverage"][0]["instruments"][0]
    review_instrument["normalized_instrument_id"] = "option:example_options_venue:BTC:missing_settlement"
    review_instrument["timestamps"]["freshness_status"] = "unknown"
    review_instrument["replay_parity"]["status"] = "unknown"

    return {
        "pass": ("pass", passing),
        "degraded": ("degraded", degraded),
        "blocked": ("blocked", blocked),
        "needs_human_review": ("needs_human_review", review),
    }


def run_self_test() -> int:
    failed = False
    results: dict[str, Any] = {}
    for name, (expected, record) in synthetic_records().items():
        result = validate_record(record)
        results[name] = result.to_dict()
        if result.outcome != expected:
            failed = True
            results[name]["expected"] = expected
    print(json.dumps({"self_test": "failed" if failed else "passed", "cases": results}, indent=2, sort_keys=True))
    return 1 if failed else 0


def load_input(args: argparse.Namespace) -> dict[str, Any]:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as handle:
            data = handle.read()
    else:
        data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("no input JSON provided; use --self-test or pass JSON via --file/stdin")
    try:
        loaded = json.loads(data)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON input: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit("top-level input must be a JSON object")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="Read a JSON integrity record from this path instead of stdin.")
    parser.add_argument("--self-test", action="store_true", help="Run embedded synthetic validation cases.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print validation JSON output.")
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()

    result = validate_record(load_input(args))
    indent = 2 if args.pretty else None
    print(json.dumps(result.to_dict(), indent=indent, sort_keys=True))
    return 2 if result.outcome in {"blocked", "needs_human_review"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
