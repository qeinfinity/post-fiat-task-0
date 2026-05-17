#!/usr/bin/env python3
"""Preflight public Task Node evidence URLs without storing page bodies.

Usage:
  python3 scripts/check_tasknode_public_evidence_preflight.py \
    https://github.com/example/repo/commit/abc123 --pretty
  python3 scripts/check_tasknode_public_evidence_preflight.py --self-test --pretty

The checker emits JSON only during normal runs. It probes caller-supplied
public URLs, follows redirects, redacts query values in output, and returns
deterministic verdicts: pass, block, or needs_human_review.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPResponse
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


TOOL_VERSION = "1.0"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_BODY_SCAN_BYTES = 4096

VERDICTS = {"pass", "block", "needs_human_review"}

UNSAFE_QUERY_KEY_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(^|[_-])(access|auth|bearer|credential|jwt|key|password|secret|seed|session|signature|signed|token)([_-]|$)",
        r"(^|[_-])(cookie|csrf|xsrf|sid|sso|ticket|private|mnemonic|wallet)([_-]|$)",
        r"^x-amz-(credential|security-token|signature|expires|algorithm|signedheaders)$",
        r"^oauth(_|-)?.*(code|state|token)$",
        r"^(code|state)$",
    )
]

AUTH_PATH_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"/(login|signin|sign-in|auth|oauth|sso)(/|$)",
        r"/(private|account|settings|billing|checkout|admin)(/|$)",
        r"/api/auth/",
    )
]

AUTH_BODY_PATTERNS = [
    ("login_required", re.compile(rb"(sign in|log in|login required|authenticate|authentication required)", re.I)),
    ("private_repo", re.compile(rb"(private repository|private repo|repository is private|not authorized)", re.I)),
    ("private_account", re.compile(rb"(private account|account required|members only)", re.I)),
    ("cookie_required", re.compile(rb"(enable cookies|cookies required|cookie is required)", re.I)),
]

SAFE_PUBLIC_CONTENT_TYPES = (
    "text/plain",
    "text/markdown",
    "text/html",
    "application/json",
    "application/pdf",
    "application/xml",
    "text/xml",
    "image/",
)


@dataclass(frozen=True)
class ProbeObservation:
    input_url: str
    final_url: str | None
    http_status: int | None
    content_type: str | None
    redirect_chain: tuple[str, ...]
    method_used: str | None
    body_markers: tuple[str, ...]
    error_type: str | None
    error_message: str | None


class TrackingRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_chain: list[str] = []

    def redirect_request(
        self,
        req: Request,
        fp: HTTPResponse,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        self.redirect_chain.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_text(value: Any) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").strip()[:240]


def split_url(url: str) -> Any:
    return urlsplit(url.strip())


def query_keys(url: str) -> list[str]:
    parsed = split_url(url)
    return [key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)]


def unsafe_query_keys(url: str) -> list[str]:
    keys = query_keys(url)
    flagged: list[str] = []
    for key in keys:
        decoded = unquote(key)
        if any(pattern.search(decoded) for pattern in UNSAFE_QUERY_KEY_PATTERNS):
            flagged.append(decoded)
    return sorted(set(flagged))


def redact_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = split_url(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    path = quote(unquote(parsed.path), safe="/:@-._~")
    if parsed.query:
        redacted_pairs = [(key, "REDACTED") for key in query_keys(url)]
        query = "&".join(f"{quote(key, safe='-._~')}={value}" for key, value in redacted_pairs)
    else:
        query = ""
    return urlunsplit((parsed.scheme, netloc, path, query, ""))


def has_auth_path(url: str | None) -> bool:
    if not url:
        return False
    parsed = split_url(url)
    return any(pattern.search(parsed.path) for pattern in AUTH_PATH_PATTERNS)


def content_type_base(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def content_type_is_public_reviewable(content_type: str | None) -> bool:
    base = content_type_base(content_type)
    if base is None:
        return False
    return any(base == allowed or base.startswith(allowed) for allowed in SAFE_PUBLIC_CONTENT_TYPES)


def scan_body_markers(response: Any, limit: int) -> tuple[str, ...]:
    if limit <= 0:
        return ()
    content_type = content_type_base(response.headers.get("content-type"))
    if content_type is None or not (content_type.startswith("text/") or content_type in {"application/json", "application/xml"}):
        return ()
    try:
        chunk = response.read(limit)
    except Exception:
        return ()
    matches = [label for label, pattern in AUTH_BODY_PATTERNS if pattern.search(chunk)]
    return tuple(sorted(set(matches)))


def open_for_headers(url: str, timeout: float, body_scan_bytes: int) -> ProbeObservation:
    https_handler = HTTPSHandler(context=ssl.create_default_context())
    redirect_handler = TrackingRedirectHandler()
    opener = build_opener(https_handler, redirect_handler)
    headers = {
        "User-Agent": "tasknode-public-evidence-preflight/1.0",
        "Accept": "text/html,application/json,text/plain,application/pdf,image/*,*/*;q=0.5",
    }
    request = Request(url, headers=headers, method="HEAD")

    try:
        with opener.open(request, timeout=timeout) as response:
            return ProbeObservation(
                input_url=url,
                final_url=response.geturl(),
                http_status=getattr(response, "status", None),
                content_type=response.headers.get("content-type"),
                redirect_chain=tuple(redirect_handler.redirect_chain),
                method_used="HEAD",
                body_markers=(),
                error_type=None,
                error_message=None,
            )
    except HTTPError as error:
        if error.code not in {405, 501}:
            return ProbeObservation(
                input_url=url,
                final_url=error.geturl(),
                http_status=error.code,
                content_type=error.headers.get("content-type") if error.headers else None,
                redirect_chain=tuple(redirect_handler.redirect_chain),
                method_used="HEAD",
                body_markers=(),
                error_type="http_error",
                error_message=safe_text(error.reason),
            )
    except (URLError, TimeoutError, ssl.SSLError, OSError) as error:
        return ProbeObservation(
            input_url=url,
            final_url=None,
            http_status=None,
            content_type=None,
            redirect_chain=tuple(redirect_handler.redirect_chain),
            method_used="HEAD",
            body_markers=(),
            error_type=type(error).__name__,
            error_message=safe_text(error),
        )

    redirect_handler = TrackingRedirectHandler()
    opener = build_opener(https_handler, redirect_handler)
    request = Request(url, headers={**headers, "Range": f"bytes=0-{max(body_scan_bytes - 1, 0)}"}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return ProbeObservation(
                input_url=url,
                final_url=response.geturl(),
                http_status=getattr(response, "status", None),
                content_type=response.headers.get("content-type"),
                redirect_chain=tuple(redirect_handler.redirect_chain),
                method_used="GET",
                body_markers=scan_body_markers(response, body_scan_bytes),
                error_type=None,
                error_message=None,
            )
    except HTTPError as error:
        markers = scan_body_markers(error, body_scan_bytes)
        return ProbeObservation(
            input_url=url,
            final_url=error.geturl(),
            http_status=error.code,
            content_type=error.headers.get("content-type") if error.headers else None,
            redirect_chain=tuple(redirect_handler.redirect_chain),
            method_used="GET",
            body_markers=markers,
            error_type="http_error",
            error_message=safe_text(error.reason),
        )
    except (URLError, TimeoutError, ssl.SSLError, OSError) as error:
        return ProbeObservation(
            input_url=url,
            final_url=None,
            http_status=None,
            content_type=None,
            redirect_chain=tuple(redirect_handler.redirect_chain),
            method_used="GET",
            body_markers=(),
            error_type=type(error).__name__,
            error_message=safe_text(error),
        )


def classify(observation: ProbeObservation) -> dict[str, Any]:
    parsed = split_url(observation.input_url)
    input_unsafe_keys = unsafe_query_keys(observation.input_url)
    final_unsafe_keys = unsafe_query_keys(observation.final_url or "")
    all_unsafe_keys = sorted(set(input_unsafe_keys + final_unsafe_keys))
    auth_redirect = has_auth_path(observation.final_url) or any(has_auth_path(url) for url in observation.redirect_chain)

    verdict = "pass"
    reason_code = "public_accessible"
    reasons: list[str] = []

    if parsed.scheme not in {"http", "https"}:
        verdict = "block"
        reason_code = "unsupported_url_scheme"
        reasons.append("URL scheme must be http or https.")
    elif parsed.username or parsed.password:
        verdict = "block"
        reason_code = "url_contains_userinfo"
        reasons.append("URL contains username or password material.")
    elif all_unsafe_keys:
        verdict = "block"
        reason_code = "unsafe_query_or_session_indicator"
        reasons.append("URL query contains session-like or secret-bearing keys.")
    elif has_auth_path(observation.input_url):
        verdict = "block"
        reason_code = "auth_bound_url_path"
        reasons.append("Input URL path appears auth-bound.")
    elif auth_redirect:
        verdict = "block"
        reason_code = "auth_bound_redirect"
        reasons.append("Redirect chain appears to land on an auth/login/private path.")
    elif observation.http_status in {401, 403, 407}:
        verdict = "block"
        reason_code = "auth_bound_http_status"
        reasons.append("HTTP status indicates auth or permission is required.")
    elif observation.body_markers:
        verdict = "block"
        reason_code = "auth_or_private_body_marker"
        reasons.append("Limited in-memory body scan matched auth/private wording.")
    elif observation.error_type and observation.http_status is None:
        verdict = "needs_human_review"
        reason_code = "network_probe_error"
        reasons.append("Network probe failed; reviewer should check manually.")
    elif observation.http_status is None:
        verdict = "needs_human_review"
        reason_code = "missing_http_status"
        reasons.append("Probe did not produce an HTTP status.")
    elif 300 <= observation.http_status < 400:
        verdict = "needs_human_review"
        reason_code = "unresolved_redirect_status"
        reasons.append("Probe ended on a redirect status.")
    elif observation.http_status >= 500:
        verdict = "needs_human_review"
        reason_code = "server_error_status"
        reasons.append("Server error may be temporary.")
    elif observation.http_status >= 400:
        verdict = "needs_human_review"
        reason_code = "non_public_or_missing_status"
        reasons.append("HTTP status is not a login-free public success.")
    elif not content_type_is_public_reviewable(observation.content_type):
        verdict = "needs_human_review"
        reason_code = "unreviewable_or_missing_content_type"
        reasons.append("Content type is missing or not obviously reviewable.")

    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "reasons": reasons,
        "input_url": redact_url(observation.input_url),
        "final_url": redact_url(observation.final_url),
        "http_status": observation.http_status,
        "content_type": content_type_base(observation.content_type),
        "redirect_count": len(observation.redirect_chain),
        "redirect_chain": [redact_url(url) for url in observation.redirect_chain],
        "method_used": observation.method_used,
        "unsafe_query_keys": all_unsafe_keys,
        "auth_private_body_markers": list(observation.body_markers),
        "error_type": observation.error_type,
        "error_message": observation.error_message,
        "body_stored": False,
    }


def static_preflight_result(url: str) -> dict[str, Any] | None:
    parsed = split_url(url)
    keys = unsafe_query_keys(url)
    verdict = "block"
    reason_code: str | None = None
    reasons: list[str] = []

    if parsed.scheme not in {"http", "https"}:
        reason_code = "unsupported_url_scheme"
        reasons.append("URL scheme must be http or https.")
    elif parsed.username or parsed.password:
        reason_code = "url_contains_userinfo"
        reasons.append("URL contains username or password material.")
    elif keys:
        reason_code = "unsafe_query_or_session_indicator"
        reasons.append("URL query contains session-like or secret-bearing keys.")
    elif has_auth_path(url):
        reason_code = "auth_bound_url_path"
        reasons.append("Input URL path appears auth-bound.")

    if reason_code is None:
        return None

    return {
        "verdict": verdict,
        "reason_code": reason_code,
        "reasons": reasons,
        "input_url": redact_url(url),
        "final_url": None,
        "http_status": None,
        "content_type": None,
        "redirect_count": 0,
        "redirect_chain": [],
        "method_used": "STATIC",
        "unsafe_query_keys": keys,
        "auth_private_body_markers": [],
        "error_type": None,
        "error_message": None,
        "body_stored": False,
    }


def overall_verdict(results: list[dict[str, Any]]) -> str:
    verdicts = {item["verdict"] for item in results}
    if "block" in verdicts:
        return "block"
    if "needs_human_review" in verdicts:
        return "needs_human_review"
    return "pass"


def run_urls(urls: list[str], timeout: float, body_scan_bytes: int) -> dict[str, Any]:
    results = []
    for url in urls:
        static_result = static_preflight_result(url)
        if static_result is not None:
            results.append(static_result)
        else:
            results.append(classify(open_for_headers(url, timeout, body_scan_bytes)))
    return {
        "tool": "tasknode_public_evidence_preflight",
        "tool_version": TOOL_VERSION,
        "generated_at_utc": utc_now_iso(),
        "overall_verdict": overall_verdict(results),
        "url_count": len(results),
        "verdict_contract": sorted(VERDICTS),
        "results": results,
    }


def synthetic_observation(
    *,
    url: str,
    final_url: str | None = None,
    status: int | None = 200,
    content_type: str | None = "text/html; charset=utf-8",
    redirect_chain: tuple[str, ...] = (),
    body_markers: tuple[str, ...] = (),
    error_type: str | None = None,
) -> ProbeObservation:
    return ProbeObservation(
        input_url=url,
        final_url=final_url or url,
        http_status=status,
        content_type=content_type,
        redirect_chain=redirect_chain,
        method_used="SYNTHETIC",
        body_markers=body_markers,
        error_type=error_type,
        error_message="synthetic error" if error_type else None,
    )


def run_self_test() -> dict[str, Any]:
    cases = [
        {
            "name": "public_github_commit_passes",
            "expected": "pass",
            "observation": synthetic_observation(url="https://github.com/example/repo/commit/abc123"),
        },
        {
            "name": "session_query_blocks",
            "expected": "block",
            "observation": synthetic_observation(url="https://example.org/evidence?session_token=abc123"),
        },
        {
            "name": "login_redirect_blocks",
            "expected": "block",
            "observation": synthetic_observation(
                url="https://example.org/private/report",
                final_url="https://example.org/login",
                redirect_chain=("https://example.org/login",),
            ),
        },
        {
            "name": "forbidden_status_blocks",
            "expected": "block",
            "observation": synthetic_observation(url="https://example.org/forbidden", status=403),
        },
        {
            "name": "network_error_needs_review",
            "expected": "needs_human_review",
            "observation": synthetic_observation(
                url="https://public.example.invalid/evidence",
                status=None,
                content_type=None,
                error_type="URLError",
            ),
        },
        {
            "name": "private_body_marker_blocks",
            "expected": "block",
            "observation": synthetic_observation(
                url="https://example.org/repo",
                body_markers=("private_repo",),
            ),
        },
    ]
    test_results = []
    for case in cases:
        result = classify(case["observation"])
        matched = result["verdict"] == case["expected"]
        test_results.append(
            {
                "name": case["name"],
                "expected": case["expected"],
                "actual": result["verdict"],
                "matched_expected": matched,
                "reason_code": result["reason_code"],
            }
        )
    passed = all(item["matched_expected"] for item in test_results)
    return {
        "tool": "tasknode_public_evidence_preflight",
        "tool_version": TOOL_VERSION,
        "generated_at_utc": utc_now_iso(),
        "overall_verdict": "pass" if passed else "block",
        "self_test": True,
        "tests": test_results,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight public Task Node evidence URLs and emit JSON verdicts.",
        epilog=(
            "Verdicts: pass, block, needs_human_review. "
            "The checker redacts query values and never emits or stores page bodies."
        ),
    )
    parser.add_argument("urls", nargs="*", help="Public evidence URLs to probe.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-URL timeout in seconds.")
    parser.add_argument(
        "--body-scan-bytes",
        type=int,
        default=DEFAULT_BODY_SCAN_BYTES,
        help="Maximum bytes to scan in memory for auth/private wording. Output never includes the body.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run deterministic synthetic self-tests.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.self_test:
        payload = run_self_test()
    else:
        if not args.urls:
            payload = {
                "tool": "tasknode_public_evidence_preflight",
                "tool_version": TOOL_VERSION,
                "generated_at_utc": utc_now_iso(),
                "overall_verdict": "block",
                "error": "at_least_one_url_required",
                "verdict_contract": sorted(VERDICTS),
            }
        else:
            payload = run_urls(args.urls, max(args.timeout, 0.1), max(args.body_scan_bytes, 0))
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
    return 0 if payload.get("overall_verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
