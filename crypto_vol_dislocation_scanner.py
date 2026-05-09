#!/usr/bin/env python3
"""Live BTC/ETH crypto options volatility-dislocation scanner.

Default usage:
    python3 crypto_vol_dislocation_scanner.py

Dashboard usage:
    python3 crypto_vol_dislocation_scanner.py --serve
"""

from __future__ import annotations

import argparse
import html
import json
import math
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import median
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


DERIBIT_API = "https://www.deribit.com/api/v2/public"
ASSETS = ("BTC", "ETH")
USER_AGENT = "post-fiat-crypto-vol-dislocation-scanner/1.0"


@dataclass(frozen=True)
class AtmPoint:
    asset: str
    expiry: str
    expiry_ts_ms: int
    dte_days: float
    underlying: float
    atm_strike: float
    atm_iv: float
    call_iv: float | None
    put_iv: float | None
    call_instrument: str | None
    put_instrument: str | None
    source: str
    degraded: bool
    degraded_reason: str


@dataclass(frozen=True)
class TermSlope:
    kind: str
    asset: str
    pair: str
    near_expiry: str
    far_expiry: str
    near_dte_days: float
    far_dte_days: float
    near_iv: float
    far_iv: float
    iv_diff: float
    slope_vol_pts_per_30d: float
    shape: str
    degraded: bool
    degraded_reason: str


@dataclass(frozen=True)
class VolSpread:
    kind: str
    expiry: str
    btc_iv: float
    eth_iv: float
    eth_minus_btc: float
    degraded: bool
    degraded_reason: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def expiry_label(expiry_ts_ms: int) -> str:
    return datetime.fromtimestamp(expiry_ts_ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def deribit_get(method: str, params: dict[str, Any], timeout: float, retries: int = 2) -> dict[str, Any]:
    """Fetch a Deribit public API method and return the JSON-RPC payload."""
    query = urlencode(params)
    url = f"{DERIBIT_API}/{method}?{query}"
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if "error" in payload:
                raise RuntimeError(f"Deribit API error for {method}: {payload['error']}")
            return payload
        except (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.35 * (attempt + 1))

    raise RuntimeError(f"failed to fetch Deribit {method}: {last_error}")


def fetch_asset_market(asset: str, timeout: float) -> dict[str, list[dict[str, Any]]]:
    instruments = deribit_get(
        "get_instruments",
        {"currency": asset, "kind": "option", "expired": "false"},
        timeout=timeout,
    ).get("result", [])
    summaries = deribit_get(
        "get_book_summary_by_currency",
        {"currency": asset, "kind": "option"},
        timeout=timeout,
    ).get("result", [])
    return {"instruments": instruments, "summaries": summaries}


def fetch_markets(timeout: float) -> dict[str, dict[str, list[dict[str, Any]]]]:
    markets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    with ThreadPoolExecutor(max_workers=len(ASSETS)) as pool:
        futures = {pool.submit(fetch_asset_market, asset, timeout): asset for asset in ASSETS}
        for future in as_completed(futures):
            markets[futures[future]] = future.result()
    return markets


def side_record(mark_iv: float, instrument_name: str, open_interest: float | None) -> dict[str, Any]:
    return {
        "iv": mark_iv,
        "instrument": instrument_name,
        "open_interest": open_interest or 0.0,
    }


def select_atm_points(
    asset: str,
    instruments: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    now: datetime,
    min_dte_hours: float,
) -> tuple[list[AtmPoint], list[str]]:
    """Approximate ATM IV by averaging call/put mark_iv at the strike nearest underlying."""
    summary_by_name = {str(row.get("instrument_name")): row for row in summaries if row.get("instrument_name")}
    groups: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    min_expiry = now + timedelta(hours=min_dte_hours)

    for instrument in instruments:
        if instrument.get("is_active") is False or instrument.get("state") not in (None, "open"):
            continue
        name = str(instrument.get("instrument_name") or "")
        summary = summary_by_name.get(name)
        if not summary:
            continue

        expiry_ts = int(instrument.get("expiration_timestamp") or 0)
        expiry_dt = datetime.fromtimestamp(expiry_ts / 1000, timezone.utc)
        if expiry_dt <= min_expiry:
            continue

        strike = safe_float(instrument.get("strike"))
        mark_iv = safe_float(summary.get("mark_iv"))
        option_type = str(instrument.get("option_type") or "").lower()
        if strike is None or mark_iv is None or mark_iv <= 0 or option_type not in {"call", "put"}:
            continue

        underlying = safe_float(summary.get("underlying_price"))
        open_interest = safe_float(summary.get("open_interest"))
        group = groups.setdefault(expiry_ts, {"underlyings": [], "strikes": {}})
        if underlying is not None and underlying > 0:
            group["underlyings"].append(underlying)

        strike_bucket = group["strikes"].setdefault(strike, {"call": None, "put": None})
        existing = strike_bucket.get(option_type)
        current = side_record(mark_iv, name, open_interest)
        if existing is None or current["open_interest"] > existing["open_interest"]:
            strike_bucket[option_type] = current

    points: list[AtmPoint] = []
    for expiry_ts in sorted(groups):
        group = groups[expiry_ts]
        underlyings = [u for u in group["underlyings"] if u > 0]
        if not underlyings:
            warnings.append(f"{asset}: missing underlying prices for {expiry_label(expiry_ts)}")
            continue
        underlying = float(median(underlyings))

        candidates: list[tuple[float, int, float, float, dict[str, Any]]] = []
        for strike, sides in group["strikes"].items():
            ivs = [side["iv"] for side in (sides.get("call"), sides.get("put")) if side is not None]
            if not ivs:
                continue
            side_count = len(ivs)
            open_interest = sum(
                side["open_interest"] for side in (sides.get("call"), sides.get("put")) if side is not None
            )
            rel_distance = abs(float(strike) - underlying) / underlying if underlying else abs(float(strike))
            candidates.append((rel_distance, -side_count, -open_interest, float(strike), sides))

        if not candidates:
            warnings.append(f"{asset}: no usable IV candidates for {expiry_label(expiry_ts)}")
            continue

        _, neg_side_count, _, strike, sides = sorted(candidates)[0]
        call = sides.get("call")
        put = sides.get("put")
        call_iv = call["iv"] if call else None
        put_iv = put["iv"] if put else None
        ivs = [iv for iv in (call_iv, put_iv) if iv is not None]
        side_count = -neg_side_count
        degraded = side_count < 2
        degraded_reason = "single-side ATM approximation" if degraded else ""
        dte_days = (datetime.fromtimestamp(expiry_ts / 1000, timezone.utc) - now).total_seconds() / 86400

        points.append(
            AtmPoint(
                asset=asset,
                expiry=expiry_label(expiry_ts),
                expiry_ts_ms=expiry_ts,
                dte_days=dte_days,
                underlying=underlying,
                atm_strike=strike,
                atm_iv=sum(ivs) / len(ivs),
                call_iv=call_iv,
                put_iv=put_iv,
                call_instrument=call["instrument"] if call else None,
                put_instrument=put["instrument"] if put else None,
                source="Deribit public/get_book_summary_by_currency mark_iv joined to public/get_instruments",
                degraded=degraded,
                degraded_reason=degraded_reason,
            )
        )

    return points, warnings


def compute_term_slopes(points_by_asset: dict[str, list[AtmPoint]]) -> list[TermSlope]:
    slopes: list[TermSlope] = []
    for asset in ASSETS:
        points = points_by_asset.get(asset, [])
        pairs: list[tuple[AtmPoint, AtmPoint]] = []
        pairs.extend((points[i], points[i + 1]) for i in range(max(0, len(points) - 1)))
        if len(points) > 2:
            pairs.append((points[0], points[-1]))

        seen: set[tuple[int, int]] = set()
        for near, far in pairs:
            key = (near.expiry_ts_ms, far.expiry_ts_ms)
            if key in seen:
                continue
            seen.add(key)
            dte_delta = far.dte_days - near.dte_days
            if dte_delta <= 0:
                continue
            iv_diff = far.atm_iv - near.atm_iv
            slope_30d = (iv_diff / dte_delta) * 30
            if iv_diff > 0.25:
                shape = "contango"
            elif iv_diff < -0.25:
                shape = "backwardation"
            else:
                shape = "flat"
            degraded_reasons = [p.degraded_reason for p in (near, far) if p.degraded_reason]
            slopes.append(
                TermSlope(
                    kind="term_slope",
                    asset=asset,
                    pair=f"{near.expiry}->{far.expiry}",
                    near_expiry=near.expiry,
                    far_expiry=far.expiry,
                    near_dte_days=near.dte_days,
                    far_dte_days=far.dte_days,
                    near_iv=near.atm_iv,
                    far_iv=far.atm_iv,
                    iv_diff=iv_diff,
                    slope_vol_pts_per_30d=slope_30d,
                    shape=shape,
                    degraded=bool(degraded_reasons),
                    degraded_reason="; ".join(sorted(set(degraded_reasons))),
                )
            )
    return slopes


def compute_eth_btc_spreads(points_by_asset: dict[str, list[AtmPoint]]) -> list[VolSpread]:
    btc_by_expiry = {p.expiry: p for p in points_by_asset.get("BTC", [])}
    eth_by_expiry = {p.expiry: p for p in points_by_asset.get("ETH", [])}
    spreads: list[VolSpread] = []

    for expiry in sorted(set(btc_by_expiry) & set(eth_by_expiry)):
        btc = btc_by_expiry[expiry]
        eth = eth_by_expiry[expiry]
        degraded_reasons = [p.degraded_reason for p in (btc, eth) if p.degraded_reason]
        spreads.append(
            VolSpread(
                kind="eth_btc_spread",
                expiry=expiry,
                btc_iv=btc.atm_iv,
                eth_iv=eth.atm_iv,
                eth_minus_btc=eth.atm_iv - btc.atm_iv,
                degraded=bool(degraded_reasons),
                degraded_reason="; ".join(sorted(set(degraded_reasons))),
            )
        )
    return spreads


def rank_dislocations(slopes: list[TermSlope], spreads: list[VolSpread], top_n: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for slope in slopes:
        candidates.append(
            {
                "kind": "term_slope",
                "market": slope.asset,
                "label": slope.pair,
                "signal": f"{slope.slope_vol_pts_per_30d:+.2f} vol pts/30d",
                "score": abs(slope.slope_vol_pts_per_30d),
                "note": slope.shape,
                "degraded": slope.degraded,
                "degraded_reason": slope.degraded_reason,
            }
        )

    for spread in spreads:
        candidates.append(
            {
                "kind": "eth_btc_spread",
                "market": "ETH-BTC",
                "label": spread.expiry,
                "signal": f"{spread.eth_minus_btc:+.2f} vol pts",
                "score": abs(spread.eth_minus_btc),
                "note": "ETH IV premium" if spread.eth_minus_btc > 0 else "BTC IV premium",
                "degraded": spread.degraded,
                "degraded_reason": spread.degraded_reason,
            }
        )

    return sorted(candidates, key=lambda row: row["score"], reverse=True)[:top_n]


def make_snapshot(max_expiries: int, min_dte_hours: float, top_n: int, timeout: float) -> dict[str, Any]:
    started = utc_now()
    markets = fetch_markets(timeout=timeout)
    now = utc_now()
    warnings: list[str] = []
    points_by_asset: dict[str, list[AtmPoint]] = {}

    for asset in ASSETS:
        points, asset_warnings = select_atm_points(
            asset=asset,
            instruments=markets[asset]["instruments"],
            summaries=markets[asset]["summaries"],
            now=now,
            min_dte_hours=min_dte_hours,
        )
        warnings.extend(asset_warnings)
        selected = points[:max_expiries]
        if len(selected) < 3:
            warnings.append(
                f"{asset}: only {len(selected)} expiries with usable ATM IV after min DTE filter "
                f"({min_dte_hours:g}h)"
            )
        points_by_asset[asset] = selected

    slopes = compute_term_slopes(points_by_asset)
    spreads = compute_eth_btc_spreads(points_by_asset)
    flags = rank_dislocations(slopes, spreads, top_n=top_n)

    return {
        "timestamp_utc": iso_z(now),
        "request_started_utc": iso_z(started),
        "source": DERIBIT_API,
        "parameters": {
            "assets": list(ASSETS),
            "max_expiries": max_expiries,
            "min_dte_hours": min_dte_hours,
            "top_n": top_n,
        },
        "atm_points": {asset: [asdict(p) for p in points] for asset, points in points_by_asset.items()},
        "term_slopes": [asdict(row) for row in slopes],
        "eth_btc_spreads": [asdict(row) for row in spreads],
        "top_flags": flags,
        "warnings": warnings,
    }


def fmt_num(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:,.{digits}f}{suffix}"


def fmt_days(value: float | None) -> str:
    if value is None:
        return "-"
    if value < 1:
        return f"{value * 24:.1f}h"
    return f"{value:.1f}d"


def quality(row: dict[str, Any]) -> str:
    return row.get("degraded_reason") or ("degraded" if row.get("degraded") else "ok")


def short_expiry(label: str) -> str:
    return label[5:] if len(label) >= 10 else label


def table(headers: list[str], rows: list[list[Any]]) -> str:
    string_rows = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in string_rows)) if string_rows else len(headers[i])
        for i in range(len(headers))
    ]
    sep = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    out = [sep, "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |", sep]
    for row in string_rows:
        out.append("| " + " | ".join(row[i].ljust(widths[i]) for i in range(len(headers))) + " |")
    out.append(sep)
    return "\n".join(out)


def print_snapshot(snapshot: dict[str, Any]) -> None:
    print("Crypto Options Volatility Dislocation Scanner")
    print(f"Timestamp UTC: {snapshot['timestamp_utc']}")
    print(f"Source: {snapshot['source']} (public Deribit option instruments and book summaries)")
    print()

    atm_rows: list[list[str]] = []
    for asset in ASSETS:
        for row in snapshot["atm_points"].get(asset, []):
            atm_rows.append(
                [
                    asset,
                    row["expiry"],
                    fmt_days(row["dte_days"]),
                    fmt_num(row["underlying"], 2),
                    fmt_num(row["atm_strike"], 2),
                    fmt_num(row["atm_iv"], 2, "%"),
                    fmt_num(row["call_iv"], 2, "%"),
                    fmt_num(row["put_iv"], 2, "%"),
                    quality(row),
                ]
            )

    print("ATM IV Snapshot")
    print(
        table(
            ["Asset", "Expiry", "DTE", "Underlying", "ATM Strike", "ATM IV", "Call IV", "Put IV", "Quality"],
            atm_rows,
        )
    )
    print()

    slope_rows = [
        [
            row["asset"],
            row["pair"],
            fmt_days(row["far_dte_days"] - row["near_dte_days"]),
            fmt_num(row["iv_diff"], 2, " pts"),
            fmt_num(row["slope_vol_pts_per_30d"], 2),
            row["shape"],
            quality(row),
        ]
        for row in snapshot["term_slopes"]
    ]
    print("Term-Structure Slopes")
    print(table(["Asset", "Pair", "DTE Delta", "IV Delta", "Vol Pts/30d", "Shape", "Quality"], slope_rows))
    print()

    spread_rows = [
        [
            row["expiry"],
            fmt_num(row["btc_iv"], 2, "%"),
            fmt_num(row["eth_iv"], 2, "%"),
            fmt_num(row["eth_minus_btc"], 2, " pts"),
            quality(row),
        ]
        for row in snapshot["eth_btc_spreads"]
    ]
    print("ETH minus BTC ATM IV Spread")
    print(table(["Expiry", "BTC ATM IV", "ETH ATM IV", "ETH-BTC", "Quality"], spread_rows))
    print()

    flag_rows = [
        [
            i + 1,
            row["kind"],
            row["market"],
            row["label"],
            row["signal"],
            fmt_num(row["score"], 2),
            row["note"] + (f" ({row['degraded_reason']})" if row.get("degraded_reason") else ""),
        ]
        for i, row in enumerate(snapshot["top_flags"])
    ]
    print("Top Dislocation Flags")
    print(table(["Rank", "Type", "Market", "Expiry/Pair", "Signal", "Abs Score", "Note"], flag_rows))

    if snapshot["warnings"]:
        print()
        print("Warnings")
        for warning in snapshot["warnings"]:
            print(f"- {warning}")


def scale(value: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    if in_max == in_min:
        return (out_min + out_max) / 2
    return out_min + ((value - in_min) / (in_max - in_min)) * (out_max - out_min)


def svg_term_structure(snapshot: dict[str, Any]) -> str:
    width, height = 760, 300
    left, right, top, bottom = 54, 24, 24, 48
    plot_w = width - left - right
    plot_h = height - top - bottom
    series = {asset: snapshot["atm_points"].get(asset, []) for asset in ASSETS}
    all_points = [point for points in series.values() for point in points]
    if not all_points:
        return "<p>No ATM IV points available.</p>"

    min_x = min(point["dte_days"] for point in all_points)
    max_x = max(point["dte_days"] for point in all_points)
    min_y = min(point["atm_iv"] for point in all_points)
    max_y = max(point["atm_iv"] for point in all_points)
    y_pad = max((max_y - min_y) * 0.15, 1.0)
    min_y -= y_pad
    max_y += y_pad
    colors = {"BTC": "#2563eb", "ETH": "#059669"}

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="BTC and ETH ATM IV term structures">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1):
        y = top + plot_h * frac
        value = max_y - (max_y - min_y) * frac
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        parts.append(
            f'<text x="{left-8}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#4b5563">'
            f"{value:.1f}%</text>"
        )
    parts.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#9ca3af"/>')
    parts.append(f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#9ca3af"/>')
    for frac in (0, 0.33, 0.66, 1):
        value = min_x + (max_x - min_x) * frac
        x = scale(value, min_x, max_x, left, width - right)
        label = f"{value:.1f}d" if value < 10 else f"{value:.0f}d"
        parts.append(f'<line x1="{x:.1f}" y1="{height-bottom}" x2="{x:.1f}" y2="{height-bottom+5}" stroke="#9ca3af"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{height-bottom+20}" text-anchor="middle" font-size="11" fill="#4b5563">'
            f"{label}</text>"
        )

    for asset, points in series.items():
        if not points:
            continue
        coords = []
        for point in points:
            x = scale(point["dte_days"], min_x, max_x, left, width - right)
            y = scale(point["atm_iv"], min_y, max_y, height - bottom, top)
            coords.append((x, y, point))
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in coords)
        parts.append(
            f'<polyline points="{polyline}" fill="none" stroke="{colors[asset]}" stroke-width="3" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
        for x, y, point in coords:
            title = html.escape(f"{asset} {point['expiry']}: {point['atm_iv']:.2f}% ATM IV")
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colors[asset]}">'
                f"<title>{title}</title></circle>"
            )
    parts.append(f'<circle cx="{width-148}" cy="24" r="5" fill="{colors["BTC"]}"/>')
    parts.append(f'<text x="{width-136}" y="28" font-size="12" fill="#111827">BTC</text>')
    parts.append(f'<circle cx="{width-92}" cy="24" r="5" fill="{colors["ETH"]}"/>')
    parts.append(f'<text x="{width-80}" y="28" font-size="12" fill="#111827">ETH</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_spread_bars(snapshot: dict[str, Any]) -> str:
    spreads = snapshot["eth_btc_spreads"]
    width, height = 760, 260
    left, right, top, bottom = 52, 24, 22, 48
    plot_w = width - left - right
    plot_h = height - top - bottom
    if not spreads:
        return "<p>No matched ETH/BTC expiries available.</p>"
    max_abs = max(abs(row["eth_minus_btc"]) for row in spreads) or 1.0
    max_abs *= 1.2
    baseline = top + plot_h / 2
    bar_w = max(18, plot_w / max(len(spreads), 1) * 0.55)
    gap = plot_w / max(len(spreads), 1)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="ETH minus BTC ATM IV spread by expiry">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<line x1="{left}" y1="{baseline:.1f}" x2="{width-right}" y2="{baseline:.1f}" stroke="#6b7280"/>',
        f'<text x="{left-8}" y="{top+4}" text-anchor="end" font-size="11" fill="#4b5563">+{max_abs:.1f}</text>',
        f'<text x="{left-8}" y="{height-bottom+4}" text-anchor="end" font-size="11" fill="#4b5563">-{max_abs:.1f}</text>',
    ]
    for i, row in enumerate(spreads):
        value = row["eth_minus_btc"]
        x_mid = left + gap * i + gap / 2
        y = scale(value, -max_abs, max_abs, height - bottom, top)
        rect_y = min(y, baseline)
        rect_h = abs(y - baseline)
        color = "#059669" if value >= 0 else "#dc2626"
        parts.append(
            f'<rect x="{x_mid-bar_w/2:.1f}" y="{rect_y:.1f}" width="{bar_w:.1f}" height="{rect_h:.1f}" '
            f'fill="{color}"/>'
        )
        parts.append(
            f'<text x="{x_mid:.1f}" y="{rect_y-6 if value >= 0 else rect_y+rect_h+14:.1f}" '
            f'text-anchor="middle" font-size="11" fill="#111827">{value:+.1f}</text>'
        )
        parts.append(
            f'<text x="{x_mid:.1f}" y="{height-bottom+18}" text-anchor="middle" font-size="10" fill="#4b5563">'
            f"{html.escape(short_expiry(row['expiry']))}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def svg_slope_bars(snapshot: dict[str, Any]) -> str:
    slopes = snapshot["term_slopes"]
    width, height = 760, 280
    left, right, top, bottom = 52, 24, 22, 70
    plot_w = width - left - right
    plot_h = height - top - bottom
    if not slopes:
        return "<p>No term-structure slopes available.</p>"
    max_abs = max(abs(row["slope_vol_pts_per_30d"]) for row in slopes) or 1.0
    max_abs *= 1.15
    baseline = top + plot_h / 2
    bar_w = max(14, plot_w / max(len(slopes), 1) * 0.45)
    gap = plot_w / max(len(slopes), 1)

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Term-structure slope steepness">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        f'<line x1="{left}" y1="{baseline:.1f}" x2="{width-right}" y2="{baseline:.1f}" stroke="#6b7280"/>',
        f'<text x="{left-8}" y="{top+4}" text-anchor="end" font-size="11" fill="#4b5563">+{max_abs:.1f}</text>',
        f'<text x="{left-8}" y="{height-bottom+4}" text-anchor="end" font-size="11" fill="#4b5563">-{max_abs:.1f}</text>',
        '<rect x="600" y="18" width="10" height="10" fill="#d97706"/>',
        '<text x="616" y="27" font-size="12" fill="#111827">BTC</text>',
        '<rect x="660" y="18" width="10" height="10" fill="#7c3aed"/>',
        '<text x="676" y="27" font-size="12" fill="#111827">ETH</text>',
    ]
    for i, row in enumerate(slopes):
        value = row["slope_vol_pts_per_30d"]
        x_mid = left + gap * i + gap / 2
        y = scale(value, -max_abs, max_abs, height - bottom, top)
        rect_y = min(y, baseline)
        rect_h = abs(y - baseline)
        color = "#d97706" if row["asset"] == "BTC" else "#7c3aed"
        title = html.escape(f"{row['asset']} {row['pair']}: {value:+.2f} vol pts/30d")
        parts.append(
            f'<rect x="{x_mid-bar_w/2:.1f}" y="{rect_y:.1f}" width="{bar_w:.1f}" height="{rect_h:.1f}" '
            f'fill="{color}"><title>{title}</title></rect>'
        )
    parts.append("</svg>")
    return "".join(parts)


def rows_to_html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_dashboard(snapshot: dict[str, Any]) -> str:
    atm_rows = []
    for asset in ASSETS:
        for row in snapshot["atm_points"].get(asset, []):
            atm_rows.append(
                [
                    asset,
                    row["expiry"],
                    fmt_days(row["dte_days"]),
                    fmt_num(row["underlying"], 2),
                    fmt_num(row["atm_strike"], 2),
                    fmt_num(row["atm_iv"], 2, "%"),
                    fmt_num(row["call_iv"], 2, "%"),
                    fmt_num(row["put_iv"], 2, "%"),
                    quality(row),
                ]
            )
    flag_rows = [
        [
            str(i + 1),
            row["kind"],
            row["market"],
            row["label"],
            row["signal"],
            fmt_num(row["score"], 2),
            row["note"] + (f" ({row['degraded_reason']})" if row.get("degraded_reason") else ""),
        ]
        for i, row in enumerate(snapshot["top_flags"])
    ]
    spread_rows = [
        [
            row["expiry"],
            fmt_num(row["btc_iv"], 2, "%"),
            fmt_num(row["eth_iv"], 2, "%"),
            fmt_num(row["eth_minus_btc"], 2, " pts"),
            quality(row),
        ]
        for row in snapshot["eth_btc_spreads"]
    ]
    warnings = "".join(f"<li>{html.escape(warning)}</li>" for warning in snapshot["warnings"])

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Crypto Volatility Dislocation Scanner</title>
  <style>
    :root {{
      --ink: #111827;
      --muted: #4b5563;
      --line: #d1d5db;
      --panel: #ffffff;
      --page: #f4f6f8;
      --accent: #2563eb;
      --good: #059669;
      --bad: #dc2626;
      --warn: #d97706;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--page);
      letter-spacing: 0;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    .wrap {{
      width: min(1180px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 0;
      flex-wrap: wrap;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(22px, 3vw, 34px);
      line-height: 1.08;
      font-weight: 760;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
      text-align: right;
    }}
    main {{
      padding: 18px 0 32px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 14px;
      align-items: start;
    }}
    section {{
      grid-column: span 12;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    section.half {{ grid-column: span 6; }}
    section h2 {{
      margin: 0;
      padding: 12px 14px;
      font-size: 15px;
      border-bottom: 1px solid var(--line);
      background: #fafafa;
    }}
    .chart {{
      padding: 10px 12px 8px;
      overflow-x: auto;
    }}
    svg {{
      display: block;
      width: 100%;
      min-width: 620px;
      height: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      color: #374151;
      font-weight: 680;
      background: #fafafa;
    }}
    td {{
      color: #111827;
    }}
    .table-scroll {{
      overflow-x: auto;
    }}
    .warnings {{
      padding: 0 14px 12px 28px;
      color: #7c2d12;
      font-size: 13px;
    }}
    .source {{
      color: var(--muted);
      font-size: 12px;
      padding: 10px 14px;
      border-top: 1px solid var(--line);
      background: #fafafa;
    }}
    @media (max-width: 860px) {{
      section.half {{ grid-column: span 12; }}
      .meta {{ text-align: left; }}
      th, td {{ padding: 8px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>Crypto Volatility Dislocation Scanner</h1>
      <div class="meta">
        <div>Snapshot: {html.escape(snapshot["timestamp_utc"])}</div>
        <div>Assets: BTC, ETH | Source: Deribit public API</div>
      </div>
    </div>
  </header>
  <main class="wrap">
    <div class="grid">
      <section>
        <h2>ATM IV Term Structure</h2>
        <div class="chart">{svg_term_structure(snapshot)}</div>
      </section>
      <section class="half">
        <h2>ETH minus BTC ATM IV Spread</h2>
        <div class="chart">{svg_spread_bars(snapshot)}</div>
      </section>
      <section class="half">
        <h2>Term-Structure Steepness</h2>
        <div class="chart">{svg_slope_bars(snapshot)}</div>
      </section>
      <section>
        <h2>Top Dislocation Flags</h2>
        <div class="table-scroll">{rows_to_html_table(["Rank", "Type", "Market", "Expiry/Pair", "Signal", "Abs Score", "Note"], flag_rows)}</div>
      </section>
      <section>
        <h2>ATM IV Snapshot</h2>
        <div class="table-scroll">{rows_to_html_table(["Asset", "Expiry", "DTE", "Underlying", "ATM Strike", "ATM IV", "Call IV", "Put IV", "Quality"], atm_rows)}</div>
      </section>
      <section>
        <h2>Matched ETH/BTC Spreads</h2>
        <div class="table-scroll">{rows_to_html_table(["Expiry", "BTC ATM IV", "ETH ATM IV", "ETH-BTC", "Quality"], spread_rows)}</div>
      </section>
      {"<section><h2>Warnings</h2><ul class=\"warnings\">" + warnings + "</ul></section>" if warnings else ""}
      <section>
        <div class="source">API methods: public/get_instruments and public/get_book_summary_by_currency. ATM IV uses the strike nearest each expiry's median underlying price and averages call/put mark_iv when both sides are available.</div>
      </section>
    </div>
  </main>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    config: dict[str, Any] = {}

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("dashboard: " + (fmt % args) + "\n")

    def send_payload(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def build_snapshot(self) -> dict[str, Any]:
        return make_snapshot(
            max_expiries=self.config["max_expiries"],
            min_dte_hours=self.config["min_dte_hours"],
            top_n=self.config["top"],
            timeout=self.config["timeout"],
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self.send_payload(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if parsed.path == "/favicon.ico":
            self.send_payload(204, b"", "image/x-icon")
            return

        try:
            snapshot = self.build_snapshot()
            if parsed.path == "/api/snapshot":
                pretty = "pretty" in parse_qs(parsed.query)
                body = json.dumps(snapshot, indent=2 if pretty else None).encode("utf-8")
                self.send_payload(200, body, "application/json; charset=utf-8")
                return
            if parsed.path in {"/", "/index.html"}:
                body = render_dashboard(snapshot).encode("utf-8")
                self.send_payload(200, body, "text/html; charset=utf-8")
                return
            self.send_payload(404, b"not found\n", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - top-level HTTP error rendering.
            body = (
                "<!doctype html><title>Scanner error</title>"
                f"<h1>Scanner error</h1><pre>{html.escape(str(exc))}</pre>"
            ).encode("utf-8")
            self.send_payload(502, body, "text/html; charset=utf-8")


def serve_dashboard(args: argparse.Namespace) -> None:
    DashboardHandler.config = {
        "max_expiries": args.max_expiries,
        "min_dte_hours": args.min_dte_hours,
        "top": args.top,
        "timeout": args.timeout,
    }
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    host, port = server.server_address[:2]
    print(f"Crypto vol dashboard: http://{host}:{port}/", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.", flush=True)
    finally:
        server.server_close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan live BTC/ETH Deribit options IV dislocations.")
    parser.add_argument("--serve", action="store_true", help="serve the browser dashboard instead of printing once")
    parser.add_argument("--host", default="127.0.0.1", help="dashboard bind host")
    parser.add_argument("--port", type=int, default=8765, help="dashboard bind port")
    parser.add_argument("--max-expiries", type=int, default=6, help="expiries per asset to include")
    parser.add_argument("--min-dte-hours", type=float, default=6.0, help="skip options expiring sooner than this")
    parser.add_argument("--top", type=int, default=3, help="number of dislocation flags to print")
    parser.add_argument("--timeout", type=float, default=20.0, help="per-request Deribit timeout in seconds")
    parser.add_argument("--json", action="store_true", help="print raw snapshot JSON")
    args = parser.parse_args(argv)
    if args.max_expiries < 3:
        parser.error("--max-expiries must be at least 3")
    if args.top < 1:
        parser.error("--top must be at least 1")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.serve:
            serve_dashboard(args)
            return 0
        snapshot = make_snapshot(
            max_expiries=args.max_expiries,
            min_dte_hours=args.min_dte_hours,
            top_n=args.top,
            timeout=args.timeout,
        )
        if args.json:
            print(json.dumps(snapshot, indent=2))
        else:
            print_snapshot(snapshot)
        return 0
    except Exception as exc:  # noqa: BLE001 - command-line boundary.
        print(f"scanner: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
