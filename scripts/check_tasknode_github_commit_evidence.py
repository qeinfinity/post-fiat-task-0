#!/usr/bin/env python3
"""Validate public GitHub commit evidence for Task Node.

Usage:
  python3 scripts/check_tasknode_github_commit_evidence.py \
    --repo https://github.com/example/repo \
    --commit 0123456789abcdef0123456789abcdef01234567 --pretty
  python3 scripts/check_tasknode_github_commit_evidence.py \
    --commit-url https://github.com/example/repo/commit/0123456789abcdef0123456789abcdef01234567 --pretty
  python3 scripts/check_tasknode_github_commit_evidence.py --self-test --pretty

The checker emits JSON only during normal runs. It validates Task Node
github_commit evidence fields without reading cookies, browser storage, auth
headers, wallet material, private account data, MNPI, or proprietary strategy
logic.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener


TOOL_VERSION = "1.0"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_RAW_CHECK_LIMIT = 5
COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
HASH_CANDIDATE_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
HEXISH_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{7,64}(?![0-9a-fA-F])")
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERDICT_ORDER = {"pass": 0, "needs_human_review": 1, "block": 2}
VERDICT_CONTRACT = ["block", "needs_human_review", "pass"]


@dataclass(frozen=True)
class NormalizedRepo:
    owner: str
    repo: str
    normalized_url: str
    commit_from_url: str | None


@dataclass(frozen=True)
class HttpObservation:
    url: str
    method: str
    status: int | None
    content_type: str | None
    error_type: str | None
    error_message: str | None
    json_body: Any | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    repo_url: str
    commit_value: str
    source: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_text(value: Any, limit: int = 240) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()[:limit]


def clean_input(value: str | None) -> str:
    return (value or "").strip()


def normalize_github_repo_url(raw_url: str) -> tuple[NormalizedRepo | None, list[str], list[str]]:
    """Return normalized GitHub owner/repo, reasons, and warnings."""
    reasons: list[str] = []
    warnings: list[str] = []
    value = clean_input(raw_url)
    if not value:
        return None, ["missing_repo_url"], warnings

    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value[len("git@github.com:") :]

    parsed = urlsplit(value)
    if not parsed.scheme and re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$", value):
        parsed = urlsplit(f"https://github.com/{value}")

    if parsed.scheme not in {"http", "https"}:
        return None, ["repo_url_must_be_http_or_https"], warnings
    if (parsed.hostname or "").lower() != "github.com":
        return None, ["repo_url_must_be_github_com"], warnings

    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        return None, ["repo_url_missing_owner_or_repo"], warnings

    owner, repo = path_parts[0], path_parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_REPO_RE.fullmatch(owner) or not OWNER_REPO_RE.fullmatch(repo):
        return None, ["repo_url_has_invalid_owner_or_repo"], warnings

    commit_from_url: str | None = None
    if len(path_parts) >= 4 and path_parts[2] == "commit":
        commit_from_url = path_parts[3]
        warnings.append("repo_url_included_commit_path")
    elif len(path_parts) > 2:
        warnings.append("repo_url_included_extra_path")

    normalized = f"https://github.com/{owner}/{repo}"
    return NormalizedRepo(owner=owner, repo=repo, normalized_url=normalized, commit_from_url=commit_from_url), reasons, warnings


def parse_commit_url(raw_url: str) -> tuple[EvidenceRecord | None, list[str]]:
    value = clean_input(raw_url)
    if not value:
        return None, ["missing_commit_url"]
    parsed = urlsplit(value)
    if (parsed.hostname or "").lower() != "github.com":
        return None, ["commit_url_must_be_github_com"]
    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(path_parts) < 4 or path_parts[2] != "commit":
        return None, ["commit_url_must_use_owner_repo_commit_hash_path"]
    repo_url = f"https://github.com/{path_parts[0]}/{path_parts[1]}"
    return EvidenceRecord(repo_url=repo_url, commit_value=path_parts[3], source="commit_url"), []


def analyze_commit_field(raw_commit: str, commit_from_repo_url: str | None = None) -> tuple[str | None, list[str], list[str]]:
    """Extract an intended hash while blocking contaminated commit fields."""
    value = clean_input(raw_commit)
    reasons: list[str] = []
    warnings: list[str] = []
    if not value and commit_from_repo_url:
        value = commit_from_repo_url
        warnings.append("commit_hash_extracted_from_repo_url")

    if not value:
        return None, ["missing_commit_hash"], warnings

    candidates = HASH_CANDIDATE_RE.findall(value)
    hexish = HEXISH_RE.findall(value)
    has_url = "github.com/" in value.lower() or "http://" in value.lower() or "https://" in value.lower()
    has_pipe = "|" in value
    has_internal_whitespace = bool(re.search(r"\s", value.strip()))
    exact_hash = bool(COMMIT_HASH_RE.fullmatch(value))

    if has_url:
        reasons.append("contaminated_commit_field_contains_url")
    if has_pipe:
        reasons.append("contaminated_commit_field_contains_pipe")
    if len(candidates) > 1:
        reasons.append("contaminated_commit_field_multiple_hashes")
    if has_internal_whitespace and not exact_hash:
        reasons.append("contaminated_commit_field_contains_extra_text")

    if exact_hash:
        return value.lower(), sorted(set(reasons)), warnings

    if len(candidates) == 1:
        candidate = candidates[0].lower()
        if value != candidates[0]:
            reasons.append("contaminated_commit_field_contains_extra_text")
        return candidate, sorted(set(reasons)), warnings

    if hexish:
        reasons.append("malformed_commit_hash_length")
    else:
        reasons.append("commit_hash_must_be_40_hex_characters")
    return None, sorted(set(reasons)), warnings


def github_api_commit_url(repo: NormalizedRepo, commit_hash: str) -> str:
    owner = quote(repo.owner, safe="")
    name = quote(repo.repo, safe="")
    commit = quote(commit_hash, safe="")
    return f"https://api.github.com/repos/{owner}/{name}/commits/{commit}"


def github_commit_page_url(repo: NormalizedRepo, commit_hash: str) -> str:
    return f"{repo.normalized_url}/commit/{commit_hash}"


def github_raw_url(repo: NormalizedRepo, commit_hash: str, filename: str) -> str:
    path = quote(filename, safe="/-._~")
    return f"https://raw.githubusercontent.com/{quote(repo.owner, safe='')}/{quote(repo.repo, safe='')}/{commit_hash}/{path}"


def open_json(url: str, timeout: float) -> HttpObservation:
    request = Request(
        url,
        headers={
            "User-Agent": f"tasknode-github-commit-evidence/{TOOL_VERSION}",
            "Accept": "application/vnd.github+json,application/json;q=0.9,*/*;q=0.5",
        },
        method="GET",
    )
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(2_000_000)
            parsed = json.loads(body.decode("utf-8"))
            return HttpObservation(
                url=url,
                method="GET",
                status=getattr(response, "status", None),
                content_type=response.headers.get("content-type"),
                error_type=None,
                error_message=None,
                json_body=parsed,
            )
    except HTTPError as error:
        return HttpObservation(
            url=url,
            method="GET",
            status=error.code,
            content_type=error.headers.get("content-type") if error.headers else None,
            error_type="http_error",
            error_message=safe_text(error.reason),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        return HttpObservation(
            url=url,
            method="GET",
            status=None,
            content_type=None,
            error_type=type(error).__name__,
            error_message=safe_text(error),
        )
    except (URLError, TimeoutError, ssl.SSLError, OSError) as error:
        return HttpObservation(
            url=url,
            method="GET",
            status=None,
            content_type=None,
            error_type=type(error).__name__,
            error_message=safe_text(error),
        )


def head_url(url: str, timeout: float) -> HttpObservation:
    request = Request(
        url,
        headers={
            "User-Agent": f"tasknode-github-commit-evidence/{TOOL_VERSION}",
            "Accept": "*/*",
        },
        method="HEAD",
    )
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpObservation(
                url=url,
                method="HEAD",
                status=getattr(response, "status", None),
                content_type=response.headers.get("content-type"),
                error_type=None,
                error_message=None,
            )
    except HTTPError as error:
        return HttpObservation(
            url=url,
            method="HEAD",
            status=error.code,
            content_type=error.headers.get("content-type") if error.headers else None,
            error_type="http_error",
            error_message=safe_text(error.reason),
        )
    except (URLError, TimeoutError, ssl.SSLError, OSError) as error:
        return HttpObservation(
            url=url,
            method="HEAD",
            status=None,
            content_type=None,
            error_type=type(error).__name__,
            error_message=safe_text(error),
        )


def observation_to_dict(observation: HttpObservation) -> dict[str, Any]:
    return {
        "url": observation.url,
        "method": observation.method,
        "status": observation.status,
        "content_type": observation.content_type,
        "error_type": observation.error_type,
        "error_message": observation.error_message,
    }


def summarize_files(api_body: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(api_body, dict):
        return [], 0
    files = api_body.get("files")
    if not isinstance(files, list):
        return [], 0
    summary: list[dict[str, Any]] = []
    for item in files[:20]:
        if not isinstance(item, dict):
            continue
        filename = safe_text(item.get("filename"), 500)
        if not filename:
            continue
        summary.append(
            {
                "filename": filename,
                "status": safe_text(item.get("status")),
                "additions": item.get("additions") if isinstance(item.get("additions"), int) else None,
                "deletions": item.get("deletions") if isinstance(item.get("deletions"), int) else None,
                "changes": item.get("changes") if isinstance(item.get("changes"), int) else None,
            }
        )
    return summary, len(files)


def verdict_from_reasons(block_reasons: list[str], review_reasons: list[str]) -> tuple[str, str]:
    if block_reasons:
        return "block", block_reasons[0]
    if review_reasons:
        return "needs_human_review", review_reasons[0]
    return "pass", "public_commit_evidence_verified"


def validate_record(
    record: EvidenceRecord,
    *,
    timeout: float,
    raw_check_limit: int,
    offline_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block_reasons: list[str] = []
    review_reasons: list[str] = []
    warnings: list[str] = []

    repo, repo_reasons, repo_warnings = normalize_github_repo_url(record.repo_url)
    block_reasons.extend(repo_reasons)
    warnings.extend(repo_warnings)

    commit_hash, commit_reasons, commit_warnings = analyze_commit_field(
        record.commit_value,
        repo.commit_from_url if repo else None,
    )
    block_reasons.extend(commit_reasons)
    warnings.extend(commit_warnings)

    result: dict[str, Any] = {
        "source": record.source,
        "input_repo_url": record.repo_url,
        "input_commit": record.commit_value,
        "normalized_repo_url": repo.normalized_url if repo else None,
        "owner": repo.owner if repo else None,
        "repo": repo.repo if repo else None,
        "commit_hash": commit_hash,
        "warnings": sorted(set(warnings)),
        "reasons": [],
        "reason_code": None,
        "verdict": None,
        "commit_page_url": github_commit_page_url(repo, commit_hash) if repo and commit_hash else None,
        "github_api_url": github_api_commit_url(repo, commit_hash) if repo and commit_hash else None,
        "github_api_observation": None,
        "commit_page_observation": None,
        "changed_file_count": 0,
        "changed_files_summary": [],
        "raw_file_checks": [],
    }

    if not block_reasons and repo and commit_hash:
        if offline_observation is not None:
            api_observation = HttpObservation(
                url=github_api_commit_url(repo, commit_hash),
                method="GET",
                status=offline_observation.get("api_status"),
                content_type="application/json" if offline_observation.get("api_status") == 200 else None,
                error_type=offline_observation.get("api_error_type"),
                error_message=offline_observation.get("api_error_message"),
                json_body=offline_observation.get("api_body"),
            )
        else:
            api_observation = open_json(github_api_commit_url(repo, commit_hash), timeout)
        result["github_api_observation"] = observation_to_dict(api_observation)

        if api_observation.status == 200 and isinstance(api_observation.json_body, dict):
            files_summary, file_count = summarize_files(api_observation.json_body)
            result["changed_file_count"] = file_count
            result["changed_files_summary"] = files_summary
            if file_count == 0:
                review_reasons.append("github_api_returned_no_changed_files")

            raw_checks: list[dict[str, Any]] = []
            checkable_files = [item for item in files_summary if item.get("status") != "removed"]
            for item in checkable_files[: max(raw_check_limit, 0)]:
                filename = str(item["filename"])
                raw_url = github_raw_url(repo, commit_hash, filename)
                if offline_observation is not None:
                    raw_status = offline_observation.get("raw_status", 200)
                    raw_observation = HttpObservation(
                        url=raw_url,
                        method="HEAD",
                        status=raw_status,
                        content_type="text/plain" if raw_status == 200 else None,
                        error_type=None if raw_status == 200 else "offline_fixture",
                        error_message=None if raw_status == 200 else "synthetic raw check failure",
                    )
                else:
                    raw_observation = head_url(raw_url, timeout)
                raw_checks.append({"filename": filename, **observation_to_dict(raw_observation)})
                if raw_observation.status not in {200, 302}:
                    review_reasons.append("raw_changed_file_not_publicly_reachable")
            result["raw_file_checks"] = raw_checks
        elif api_observation.status == 404:
            block_reasons.append("github_commit_not_public_or_not_found")
        elif api_observation.status in {401, 403, 429}:
            review_reasons.append("github_api_unreachable_or_rate_limited")
        else:
            if offline_observation is not None:
                page_status = offline_observation.get("page_status")
                page_observation = HttpObservation(
                    url=github_commit_page_url(repo, commit_hash),
                    method="HEAD",
                    status=page_status,
                    content_type="text/html" if page_status == 200 else None,
                    error_type=offline_observation.get("page_error_type"),
                    error_message=offline_observation.get("page_error_message"),
                )
            else:
                page_observation = head_url(github_commit_page_url(repo, commit_hash), timeout)
            result["commit_page_observation"] = observation_to_dict(page_observation)
            if page_observation.status == 404:
                block_reasons.append("github_commit_page_not_found")
            elif page_observation.status == 200:
                review_reasons.append("commit_page_reachable_but_changed_files_not_verified")
            else:
                review_reasons.append("github_commit_reachability_not_proven")

    verdict, reason_code = verdict_from_reasons(sorted(set(block_reasons)), sorted(set(review_reasons)))
    result["verdict"] = verdict
    result["reason_code"] = reason_code
    result["reasons"] = sorted(set(block_reasons + review_reasons))
    return result


def collect_records(args: argparse.Namespace) -> tuple[list[EvidenceRecord], list[str]]:
    records: list[EvidenceRecord] = []
    errors: list[str] = []
    if args.commit_url:
        record, reasons = parse_commit_url(args.commit_url)
        if record:
            records.append(record)
        else:
            errors.extend(reasons)
    if args.repo or args.commit:
        if not args.repo:
            errors.append("missing_repo_for_commit_field")
        if not args.commit:
            errors.append("missing_commit_for_repo_field")
        if args.repo and args.commit:
            records.append(EvidenceRecord(repo_url=args.repo, commit_value=args.commit, source="repo_commit_fields"))
    if args.evidence_json:
        try:
            with open(args.evidence_json, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"could_not_read_evidence_json:{safe_text(error)}")
        else:
            records.extend(records_from_json(loaded))
    if not records and not errors:
        errors.append("provide_commit_url_or_repo_and_commit")
    return records, errors


def records_from_json(value: Any) -> list[EvidenceRecord]:
    raw_records = value if isinstance(value, list) else [value]
    records: list[EvidenceRecord] = []
    for index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            continue
        repo_url = (
            item.get("repo")
            or item.get("repo_url")
            or item.get("repository")
            or item.get("repository_url")
            or item.get("github_repo")
            or ""
        )
        commit_value = (
            item.get("commit")
            or item.get("commit_hash")
            or item.get("commit_sha")
            or item.get("github_commit")
            or ""
        )
        records.append(EvidenceRecord(repo_url=str(repo_url), commit_value=str(commit_value), source=f"evidence_json[{index}]"))
    return records


def aggregate_verdict(results: list[dict[str, Any]], intake_errors: list[str]) -> tuple[str, str]:
    if intake_errors:
        return "block", intake_errors[0]
    if not results:
        return "block", "no_evidence_records"
    highest = max(results, key=lambda item: VERDICT_ORDER.get(str(item.get("verdict")), 2))
    return str(highest.get("verdict")), str(highest.get("reason_code"))


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    records, intake_errors = collect_records(args)
    results = [
        validate_record(record, timeout=args.timeout, raw_check_limit=args.raw_check_limit)
        for record in records
    ]
    overall_verdict, reason_code = aggregate_verdict(results, intake_errors)
    return {
        "tool": "tasknode_github_commit_evidence_integrity",
        "tool_version": TOOL_VERSION,
        "generated_at_utc": utc_now_iso(),
        "verdict_contract": VERDICT_CONTRACT,
        "overall_verdict": overall_verdict,
        "reason_code": reason_code,
        "intake_errors": intake_errors,
        "evidence_count": len(records),
        "results": results,
    }


def self_test_report() -> dict[str, Any]:
    clean_repo = "https://github.com/example/repo"
    clean_hash = "0123456789abcdef0123456789abcdef01234567"
    clean_body = {
        "files": [
            {
                "filename": "scripts/check_tasknode_github_commit_evidence.py",
                "status": "added",
                "additions": 25,
                "deletions": 0,
                "changes": 25,
            }
        ]
    }
    tests = [
        {
            "name": "clean_commit_fields_pass",
            "expected_verdict": "pass",
            "record": EvidenceRecord(clean_repo, clean_hash, "self_test"),
            "offline_observation": {"api_status": 200, "api_body": clean_body, "raw_status": 200},
        },
        {
            "name": "malformed_duplicate_commit_field_blocks",
            "expected_verdict": "block",
            "record": EvidenceRecord(clean_repo, f"{clean_repo} | {clean_hash}", "self_test"),
            "offline_observation": {"api_status": 200, "api_body": clean_body, "raw_status": 200},
        },
        {
            "name": "api_rate_limit_needs_human_review",
            "expected_verdict": "needs_human_review",
            "record": EvidenceRecord(clean_repo, clean_hash, "self_test"),
            "offline_observation": {
                "api_status": 403,
                "api_error_type": "http_error",
                "api_error_message": "rate limit",
            },
        },
    ]
    test_results = []
    all_passed = True
    for test in tests:
        result = validate_record(
            test["record"],
            timeout=0.1,
            raw_check_limit=2,
            offline_observation=test["offline_observation"],
        )
        passed = result["verdict"] == test["expected_verdict"]
        all_passed = all_passed and passed
        test_results.append(
            {
                "name": test["name"],
                "expected_verdict": test["expected_verdict"],
                "passed": passed,
                "result": result,
            }
        )
    return {
        "tool": "tasknode_github_commit_evidence_integrity",
        "tool_version": TOOL_VERSION,
        "generated_at_utc": utc_now_iso(),
        "self_test": True,
        "overall_verdict": "pass" if all_passed else "block",
        "reason_code": "self_tests_passed" if all_passed else "self_tests_failed",
        "tests": test_results,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="Public GitHub repository URL, such as https://github.com/owner/repo")
    parser.add_argument("--commit", help="40-character commit hash field from Task Node github_commit evidence")
    parser.add_argument("--commit-url", help="Public GitHub commit URL")
    parser.add_argument("--evidence-json", help="Optional JSON file containing repo/commit evidence records")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout in seconds")
    parser.add_argument("--raw-check-limit", type=int, default=DEFAULT_RAW_CHECK_LIMIT, help="Max changed-file raw URLs to check")
    parser.add_argument("--self-test", action="store_true", help="Run embedded synthetic self-tests")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    report = self_test_report() if args.self_test else build_report(args)
    json.dump(report, sys.stdout, indent=2 if args.pretty else None, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report.get("overall_verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
