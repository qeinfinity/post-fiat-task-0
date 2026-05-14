#!/usr/bin/env python3
"""Validate a public synthetic derivatives replay/parity QA fixture.

Run:
  python3 scripts/check_public_derivatives_replay_parity.py fixtures/public_derivatives_replay_parity_cases.json --pretty
  python3 scripts/check_public_derivatives_replay_parity.py --self-test --pretty
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_FIXTURE = Path("fixtures/public_derivatives_replay_parity_cases.json")

REQUIRED_TOP_LEVEL_FIELDS = {
    "harness_version",
    "generated_at_utc",
    "scope",
    "required_case_fields",
    "required_record_fields",
    "cases",
}

REQUIRED_CASE_FIELDS = {
    "case_id",
    "intent",
    "expected_status",
    "expected_finding_codes",
    "records",
}

REQUIRED_RECORD_FIELDS = {
    "record_id",
    "event_time_utc",
    "available_at_utc",
    "payload",
    "replay",
    "provenance",
}

REQUIRED_REPLAY_FIELDS = {
    "run_id",
    "config_hash",
    "decision_cutoff_utc",
    "sequence_number",
    "deterministic_record_id",
}

REQUIRED_PROVENANCE_FIELDS = {
    "source",
    "quality_status",
    "synthetic",
    "degraded",
    "failure_reason_code",
    "failure_reason",
}

ALLOWED_EXPECTED_STATUSES = {"pass", "degraded", "failed"}
ALLOWED_QUALITY_STATUSES = {"ok", "degraded", "failed"}
ALLOWED_FAILURE_CODES = {
    "none",
    "source_delayed_public_snapshot",
    "decode_error_public_fixture",
    "coverage_gap_public_fixture",
}

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


class HarnessResult:
    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.errors: list[str] = []
        self.case_results: list[dict[str, Any]] = []

    def add_block(self, message: str) -> None:
        self.blocked.append(message)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    @property
    def status(self) -> str:
        if self.blocked:
            return "blocked"
        if self.errors:
            return "failed"
        return "pass"

    def to_dict(self, *, fixture_path: str | None, case_count: int, record_count: int) -> dict[str, Any]:
        return {
            "status": self.status,
            "fixture_path": fixture_path,
            "case_count": case_count,
            "record_count": record_count,
            "blocked_count": len(self.blocked),
            "error_count": len(self.errors),
            "blocked": self.blocked,
            "errors": self.errors,
            "case_results": self.case_results,
        }


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("fixture root must be a JSON object")
    return data


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


def scan_disallowed_fields(value: Any, result: HarnessResult, path: str = "") -> None:
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


def finding(code: str, severity: str, message: str, *, record_id: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if record_id is not None:
        item["record_id"] = record_id
    return item


def status_from_findings(findings: list[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in findings}
    if "blocked" in severities:
        return "blocked"
    if "error" in severities:
        return "failed"
    if "warning" in severities:
        return "degraded"
    return "pass"


def deterministic_record_id(case_id: str, record: dict[str, Any]) -> str:
    replay = record["replay"]
    payload = record["payload"]
    seed_parts = [
        case_id,
        str(record["record_id"]),
        record["event_time_utc"],
        record["available_at_utc"],
        str(replay["sequence_number"]),
        str(payload.get("instrument_family", "")),
        str(payload.get("quote_currency", "")),
    ]
    digest = hashlib.sha256("|".join(seed_parts).encode("utf-8")).hexdigest()[:24]
    return f"sha256:{digest}"


def validate_record_shape(case_id: str, record: Any, index: int) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    label = f"{case_id}.records[{index}]"
    if not isinstance(record, dict):
        return [finding("record_shape_invalid", "error", f"{label} must be an object")]

    missing = sorted(REQUIRED_RECORD_FIELDS - set(record))
    if missing:
        findings.append(finding("record_shape_invalid", "error", f"{label} missing fields: {missing}"))

    replay = record.get("replay")
    if not isinstance(replay, dict):
        findings.append(finding("record_shape_invalid", "error", f"{label}.replay must be an object"))
    else:
        missing_replay = sorted(REQUIRED_REPLAY_FIELDS - set(replay))
        if missing_replay:
            findings.append(
                finding("record_shape_invalid", "error", f"{label}.replay missing fields: {missing_replay}")
            )

    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        findings.append(finding("record_shape_invalid", "error", f"{label}.provenance must be an object"))
    else:
        missing_provenance = sorted(REQUIRED_PROVENANCE_FIELDS - set(provenance))
        if missing_provenance:
            findings.append(
                finding(
                    "record_shape_invalid",
                    "error",
                    f"{label}.provenance missing fields: {missing_provenance}",
                )
            )

    payload = record.get("payload")
    if not isinstance(payload, dict):
        findings.append(finding("record_shape_invalid", "error", f"{label}.payload must be an object"))

    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        findings.append(finding("record_shape_invalid", "error", f"{label}.record_id must be non-empty"))

    return findings


def validate_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case.get("case_id", "<missing-case-id>"))
    records = case.get("records")
    findings: list[dict[str, Any]] = []

    if not isinstance(records, list) or not records:
        findings.append(finding("case_shape_invalid", "error", f"{case_id}: records must be a non-empty list"))
        records = []

    parsed_rows: list[tuple[dict[str, Any], datetime, datetime, datetime]] = []
    for index, record in enumerate(records):
        findings.extend(validate_record_shape(case_id, record, index))
        if not isinstance(record, dict):
            continue
        record_id = str(record.get("record_id", f"record-{index}"))
        event_time = parse_utc_timestamp(record.get("event_time_utc"))
        available_at = parse_utc_timestamp(record.get("available_at_utc"))
        replay = record.get("replay") if isinstance(record.get("replay"), dict) else {}
        cutoff = parse_utc_timestamp(replay.get("decision_cutoff_utc"))
        if event_time is None:
            findings.append(finding("timestamp_invalid", "error", "event_time_utc must be UTC", record_id=record_id))
        if available_at is None:
            findings.append(finding("timestamp_invalid", "error", "available_at_utc must be UTC", record_id=record_id))
        if cutoff is None:
            findings.append(
                finding("timestamp_invalid", "error", "decision_cutoff_utc must be UTC", record_id=record_id)
            )
        if event_time is None or available_at is None or cutoff is None:
            continue
        parsed_rows.append((record, event_time, available_at, cutoff))

        if available_at < event_time:
            findings.append(
                finding(
                    "future_leakage_window",
                    "error",
                    "available_at_utc precedes event_time_utc, which breaks replay visibility ordering",
                    record_id=record_id,
                )
            )
        if event_time > cutoff or available_at > cutoff:
            findings.append(
                finding(
                    "future_leakage_window",
                    "error",
                    "record event or availability time is after the replay decision cutoff",
                    record_id=record_id,
                )
            )

    previous_event_time: datetime | None = None
    for record, event_time, _available_at, _cutoff in parsed_rows:
        record_id = str(record.get("record_id", "<missing-record-id>"))
        if previous_event_time is not None and event_time < previous_event_time:
            findings.append(
                finding(
                    "event_time_out_of_order",
                    "error",
                    "event_time_utc moved backward inside the replay stream",
                    record_id=record_id,
                )
            )
        previous_event_time = event_time

    expected_sequence = 1
    first_run_id: str | None = None
    first_config_hash: str | None = None
    for record, _event_time, _available_at, _cutoff in parsed_rows:
        record_id = str(record.get("record_id", "<missing-record-id>"))
        replay = record["replay"]
        run_id = replay.get("run_id")
        config_hash = replay.get("config_hash")
        if first_run_id is None:
            first_run_id = run_id
        if first_config_hash is None:
            first_config_hash = config_hash
        if run_id != first_run_id or config_hash != first_config_hash:
            findings.append(
                finding(
                    "replay_metadata_mismatch",
                    "error",
                    "run_id and config_hash must stay constant inside one case",
                    record_id=record_id,
                )
            )

        sequence_number = replay.get("sequence_number")
        if sequence_number != expected_sequence:
            findings.append(
                finding(
                    "replay_metadata_mismatch",
                    "error",
                    f"sequence_number should be {expected_sequence}",
                    record_id=record_id,
                )
            )
        expected_sequence += 1

        expected_id = deterministic_record_id(case_id, record)
        if replay.get("deterministic_record_id") != expected_id:
            findings.append(
                finding(
                    "replay_metadata_mismatch",
                    "error",
                    f"deterministic_record_id should be {expected_id}",
                    record_id=record_id,
                )
            )

    for record, _event_time, _available_at, _cutoff in parsed_rows:
        record_id = str(record.get("record_id", "<missing-record-id>"))
        provenance = record["provenance"]
        quality_status = provenance.get("quality_status")
        failure_code = provenance.get("failure_reason_code")
        failure_reason = provenance.get("failure_reason")
        degraded = provenance.get("degraded")

        if provenance.get("synthetic") is not True:
            findings.append(
                finding("public_safety_block", "blocked", "record must be marked synthetic", record_id=record_id)
            )
        if quality_status not in ALLOWED_QUALITY_STATUSES:
            findings.append(
                finding(
                    "failure_provenance_invalid",
                    "error",
                    f"quality_status must be one of {sorted(ALLOWED_QUALITY_STATUSES)}",
                    record_id=record_id,
                )
            )
        if failure_code not in ALLOWED_FAILURE_CODES:
            findings.append(
                finding(
                    "failure_provenance_invalid",
                    "error",
                    f"failure_reason_code must be one of {sorted(ALLOWED_FAILURE_CODES)}",
                    record_id=record_id,
                )
            )

        if quality_status == "ok":
            if degraded is not False or failure_code != "none" or failure_reason:
                findings.append(
                    finding(
                        "failure_provenance_invalid",
                        "error",
                        "ok records must not carry degraded failure provenance",
                        record_id=record_id,
                    )
                )
        elif quality_status == "degraded":
            if degraded is not True or failure_code == "none" or not isinstance(failure_reason, str) or not failure_reason:
                findings.append(
                    finding(
                        "missing_failure_provenance",
                        "error",
                        "degraded records require a non-empty safe failure reason",
                        record_id=record_id,
                    )
                )
            else:
                findings.append(
                    finding(
                        "degraded_failure_provenance",
                        "warning",
                        "record is explicitly degraded with safe failure provenance",
                        record_id=record_id,
                    )
                )
        elif quality_status == "failed":
            if failure_code == "none" or not isinstance(failure_reason, str) or not failure_reason:
                findings.append(
                    finding(
                        "missing_failure_provenance",
                        "error",
                        "failed records require a non-empty safe failure reason",
                        record_id=record_id,
                    )
                )

    actual_status = status_from_findings(findings)
    actual_codes = sorted({item["code"] for item in findings})
    return {
        "case_id": case_id,
        "intent": case.get("intent"),
        "expected_status": case.get("expected_status"),
        "actual_status": actual_status,
        "expected_finding_codes": case.get("expected_finding_codes", []),
        "actual_finding_codes": actual_codes,
        "matched_expected": (
            actual_status == case.get("expected_status")
            and set(case.get("expected_finding_codes", [])) <= set(actual_codes)
        ),
        "findings": findings,
    }


def validate_fixture(data: dict[str, Any], *, fixture_path: str | None = None) -> dict[str, Any]:
    result = HarnessResult()
    scan_disallowed_fields(data, result)

    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS - set(data))
    if missing:
        result.add_error(f"fixture missing top-level fields: {missing}")

    generated_at = parse_utc_timestamp(data.get("generated_at_utc"))
    if generated_at is None:
        result.add_error("generated_at_utc must be a UTC timestamp")

    scope = data.get("scope")
    if not isinstance(scope, dict):
        result.add_error("scope must be an object")
    else:
        if scope.get("data_origin") != "synthetic_fixture_only":
            result.add_error("scope.data_origin must be synthetic_fixture_only")
        if scope.get("contains_live_market_data") is not False:
            result.add_error("scope.contains_live_market_data must be false")
        if scope.get("contains_user_specific_data") is not False:
            result.add_error("scope.contains_user_specific_data must be false")

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        result.add_error("cases must be a non-empty list")
        cases = []

    seen_case_ids: set[str] = set()
    record_count = 0
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            result.add_error(f"cases[{index}] must be an object")
            continue
        missing_case_fields = sorted(REQUIRED_CASE_FIELDS - set(case))
        if missing_case_fields:
            result.add_error(f"cases[{index}] missing fields: {missing_case_fields}")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            result.add_error(f"cases[{index}].case_id must be non-empty")
            continue
        if case_id in seen_case_ids:
            result.add_error(f"duplicate case_id: {case_id}")
        seen_case_ids.add(case_id)

        expected_status = case.get("expected_status")
        if expected_status not in ALLOWED_EXPECTED_STATUSES:
            result.add_error(f"{case_id}: expected_status must be one of {sorted(ALLOWED_EXPECTED_STATUSES)}")
        expected_codes = case.get("expected_finding_codes")
        if not isinstance(expected_codes, list):
            result.add_error(f"{case_id}: expected_finding_codes must be a list")

        records = case.get("records")
        if isinstance(records, list):
            record_count += len(records)

        case_result = validate_case(case)
        result.case_results.append(case_result)
        if not case_result["matched_expected"]:
            result.add_error(
                f"{case_id}: expected {case_result['expected_status']} with "
                f"{case_result['expected_finding_codes']}, got {case_result['actual_status']} with "
                f"{case_result['actual_finding_codes']}"
            )

    return result.to_dict(fixture_path=fixture_path, case_count=len(cases), record_count=record_count)


def self_test_fixture() -> dict[str, Any]:
    return load_json(DEFAULT_FIXTURE)


def run_self_tests() -> dict[str, Any]:
    tests: list[dict[str, Any]] = []
    fixture = self_test_fixture()

    baseline = validate_fixture(fixture, fixture_path=str(DEFAULT_FIXTURE))
    tests.append(
        {
            "name": "fixture_cases_match_expected",
            "passed": baseline["status"] == "pass",
            "result": baseline,
        }
    )

    disallowed = copy.deepcopy(fixture)
    disallowed["cases"][0]["records"][0]["account_identifier"] = "redacted-example"
    blocked = validate_fixture(disallowed)
    tests.append(
        {
            "name": "disallowed_account_identifier_blocks",
            "passed": blocked["status"] == "blocked",
            "result": {
                "status": blocked["status"],
                "blocked": blocked["blocked"],
            },
        }
    )

    missing_reason_case = copy.deepcopy(fixture["cases"][-1])
    missing_reason_case["records"][0]["provenance"]["failure_reason"] = ""
    missing_result = validate_case(missing_reason_case)
    tests.append(
        {
            "name": "missing_degraded_reason_fails",
            "passed": (
                missing_result["actual_status"] == "failed"
                and "missing_failure_provenance" in missing_result["actual_finding_codes"]
            ),
            "result": missing_result,
        }
    )

    passed = all(test["passed"] for test in tests)
    return {
        "status": "pass" if passed else "failed",
        "tests": tests,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", nargs="?", default=str(DEFAULT_FIXTURE), help="Replay/parity fixture JSON path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic self-tests")
    args = parser.parse_args(argv)

    try:
        if args.self_test:
            output = run_self_tests()
        else:
            fixture_path = Path(args.fixture)
            output = validate_fixture(load_json(fixture_path), fixture_path=str(fixture_path))
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        output = {"status": "error", "error": str(exc)}

    if args.pretty:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(json.dumps(output, sort_keys=True))
    return 0 if output.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
