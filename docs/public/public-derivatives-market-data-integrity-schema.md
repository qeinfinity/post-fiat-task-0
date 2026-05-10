# Public Derivatives Market-Data Integrity Schema

Purpose: define one agent-readable Markdown schema for public crypto options and perpetuals market-data integrity. The schema lets a contributor declare what public derivatives data was observed, which venues and instruments were covered, how fresh the timestamps were, where open interest, implied volatility, Greeks, mark, and index inputs came from, and whether the record is replayable, degraded, or blocked.

This is a public-data schema for Post Fiat intelligence ingestion. It is not a trading signal, venue endorsement, private account export, wallet artifact, or claim of proprietary edge.

## Scope

Use this schema only for public crypto derivatives inputs:

- Public options data.
- Public perpetual futures data.
- Public futures data used as a hedge or basis reference.
- Public ETF-options metadata when the product is relevant to crypto nonlinear exposure.
- Public spot, index, or mark inputs used only as reference prices.

Explicit exclusions:

- Private account balances, positions, fills, orders, or PnL.
- Wallet mnemonics, private keys, auth headers, cookies, OAuth state, validator keys, or browser storage.
- MNPI, confidential employer/client data, customer data, and private trading records.
- Trading signal labels, model thresholds, venue rankings, execution instructions, or investment advice.

## Minimal Schema Shape

```yaml
derivatives_market_data_integrity:
  schema_version: "1.0"
  artifact_id: "<stable_public_artifact_id>"
  declared_at_utc: "2026-05-10T00:00:00Z"
  public_data_only: true
  trading_signal_claims: false
  venue_coverage:
    - venue: "<venue_name>"
      venue_type: "offshore_cex|regulated_futures_exchange|listed_etf_options|spot_index_provider|other_public"
      market_segment: "options|perpetuals|futures|etf_options|spot_index|mark_index"
      coverage_role: "primary_surface|hedge_reference|basis_reference|index_reference|context_only"
      coverage_window:
        start_utc: "2026-05-10T00:00:00Z"
        end_utc: "2026-05-10T00:05:00Z"
        full_window_coverage: true
        known_gaps: []
      instruments:
        - instrument_type: "option|perpetual|future|etf_option|index|mark"
          venue_symbol: "<exchange_or_listing_symbol>"
          normalized_instrument_id: "<canonical_identifier>"
          base_asset: "BTC"
          quote_asset: "USD|USDT|USDC|BTC|other"
          settlement_asset: "USD|USDT|USDC|BTC|shares|other|not_applicable"
          expiry_utc: "2026-05-29T08:00:00Z"
          strike: "optional_numeric_string"
          option_right: "call|put|not_applicable"
          contract_style: "european|american|perpetual|cash_settled|physically_settled|unknown"
          contract_multiplier: "optional_numeric_string"
          source:
            source_type: "public_api|public_url|public_repo|local_public_scan"
            source_url_or_endpoint_label: "<public_url_or_endpoint_label>"
            source_method: "rest|websocket|static_page|download|manual_public_review"
          timestamps:
            capture_time_utc: "2026-05-10T00:05:01Z"
            exchange_timestamp_utc: "2026-05-10T00:05:00Z"
            exchange_timestamp_field: "<field_name_or_not_available>"
            observed_lag_ms: 1000
            freshness_status: "fresh|acceptable|stale|missing|unknown|blocked"
            freshness_reason: "exchange timestamp within declared tolerance"
          provenance:
            open_interest:
              status: "present|missing|not_applicable|degraded|blocked"
              source_field: "<field_name_or_not_available>"
              unit: "contracts|base_asset|quote_notional|shares|unknown"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            implied_volatility:
              status: "present|missing|not_applicable|degraded|blocked"
              source_field: "<field_name_or_not_available>"
              unit: "decimal|percent|unknown"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            greeks:
              status: "present|missing|not_applicable|degraded|blocked"
              greek_set: ["delta", "gamma", "vega", "theta"]
              source_field_or_method: "<public_field_or_public_formula_label>"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            mark_price:
              status: "present|missing|not_applicable|degraded|blocked"
              source_field: "<field_name_or_not_available>"
              quote_unit: "USD|USDT|USDC|BTC|shares|unknown"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            index_price:
              status: "present|missing|not_applicable|degraded|blocked"
              source_field: "<field_name_or_not_available>"
              quote_unit: "USD|USDT|USDC|BTC|shares|unknown"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
          replay_parity:
            status: "exact|approximate|not_replayable|blocked|unknown"
            event_time_ordering: "preserved|approximated|missing|blocked"
            snapshot_delta_consistency: "validated|not_applicable|missing|failed|blocked"
            no_future_leakage_checked: true
            replay_notes: "sufficient public timestamps and ordered records"
          degradation:
            degraded: false
            severity: "none|info|warning|blocked"
            reasons: []
            block_reason: ""
  artifact_validation:
    accessible_without_login: true
    contains_private_data: false
    contains_wallet_or_auth_material: false
    contains_mnpi: false
    contains_trading_signal_claims: false
    reviewer_notes: "public schema artifact only"
```

## Required Fields

Every submitted schema record must define these fields:

| Field | Required | Purpose |
| --- | --- | --- |
| `schema_version` | yes | Version the schema so agents can detect breaking changes. |
| `artifact_id` | yes | Stable public identifier for the artifact or record. |
| `declared_at_utc` | yes | UTC time when the integrity declaration was produced. |
| `public_data_only` | yes | Must be `true` for autonomous public submission. |
| `trading_signal_claims` | yes | Must be `false`; this schema describes data integrity, not trade direction. |
| `venue_coverage[].venue` | yes | Public venue or listing source name. |
| `venue_coverage[].market_segment` | yes | Declares whether the row is options, perps, futures, ETF options, spot index, or mark/index data. |
| `coverage_window` | yes | Declares the observed time window and known gaps. |
| `instrument_type` | yes | Separates option, perpetual, future, ETF option, index, and mark records. |
| `venue_symbol` | yes | Exact public venue or listing symbol as observed. |
| `normalized_instrument_id` | yes | Canonical ID used by downstream agents. |
| `source_url_or_endpoint_label` | yes | Public URL or public endpoint label used for the observation. |
| `capture_time_utc` | yes | Time the agent captured or computed the record. |
| `exchange_timestamp_utc` | yes when available | Native venue/listing timestamp. Use `missing` status when unavailable. |
| `freshness_status` | yes | Declares whether the observation is fresh enough for the stated purpose. |
| `provenance.open_interest` | yes | Declares OI presence, absence, degradation, or block status. |
| `provenance.implied_volatility` | yes | Declares IV presence, absence, degradation, or block status. |
| `provenance.greeks` | yes | Declares Greeks presence, absence, degradation, or block status. |
| `provenance.mark_price` | yes | Declares mark-price provenance. |
| `provenance.index_price` | yes | Declares index-price provenance. |
| `replay_parity.status` | yes | Declares whether the record can be replayed with event-time fidelity. |
| `degradation.degraded` | yes | Boolean summary for partial, stale, missing, or fallback evidence. |
| `degradation.block_reason` | yes when blocked | Reason the record must not be used. |

## Normalized Instrument IDs

Use stable, explicit IDs. Do not rely only on venue-native symbols.

Recommended grammar:

```text
option:<venue>:<underlier>:<expiry_utc>:<strike>:<call_or_put>:<settlement_asset>
perpetual:<venue>:<base_asset>-<quote_asset>:<settlement_asset>
future:<venue>:<base_asset>-<quote_asset>:<expiry_utc>:<settlement_asset>
etf_option:<listing_venue>:<ticker>:<expiry_utc>:<strike>:<call_or_put>:shares
index:<provider>:<asset_pair>
mark:<venue>:<instrument_or_asset_pair>
```

Examples:

```text
option:deribit:BTC:2026-05-29T08:00:00Z:100000:call:BTC
perpetual:bybit:BTC-USDT:USDT
future:cme:BTC-USD:2026-06-26T00:00:00Z:USD
etf_option:nasdaq:IBIT:2026-06-19T00:00:00Z:70:call:shares
```

## Status Values

### Freshness

| Value | Meaning |
| --- | --- |
| `fresh` | Exchange/listing timestamp is within the artifact tolerance. |
| `acceptable` | Slightly delayed but still valid for the declared use. |
| `stale` | Too old for live inference, but may remain useful for historical review. |
| `missing` | No native timestamp was available. |
| `unknown` | Agent cannot determine timestamp quality. |
| `blocked` | Timestamp quality is too poor or unsafe to use. |

### Provenance

| Value | Meaning |
| --- | --- |
| `present` | Public field or public method is available and named. |
| `missing` | The value is not available from the public source. |
| `not_applicable` | The field does not apply to this instrument type. |
| `degraded` | Value exists, but source quality, timestamping, units, or coverage are partial. |
| `blocked` | Value must not be used because provenance is unsafe or unverifiable. |

### Replay Parity

| Value | Meaning |
| --- | --- |
| `exact` | Event ordering, timestamps, and coverage are sufficient for deterministic replay. |
| `approximate` | Replay is possible but uses partial timestamps, sampled records, or lower-fidelity ordering. |
| `not_replayable` | Public source is useful for inspection but cannot reconstruct event-time state. |
| `blocked` | Replay would require private data, future leakage, or unsafe assumptions. |
| `unknown` | Agent has not determined replay quality. |

## Validation Rules

A public artifact passes this schema only when:

- `public_data_only` is `true`.
- `trading_signal_claims` is `false`.
- `artifact_validation.accessible_without_login` is `true`.
- `contains_private_data`, `contains_wallet_or_auth_material`, and `contains_mnpi` are all `false`.
- Every instrument has a `venue_symbol`, `normalized_instrument_id`, `source_url_or_endpoint_label`, `capture_time_utc`, and `freshness_status`.
- OI, IV, Greeks, mark price, and index price are each explicitly labeled as `present`, `missing`, `not_applicable`, `degraded`, or `blocked`.
- Any degraded field has a `degraded_reason`.
- Any blocked field has a `block_reason`.
- Replay status is explicit and does not imply exact replay when ordering, coverage, or timestamps are missing.

Block autonomous ingestion when:

- Any private account, wallet, auth, cookie, customer, employer/client, or MNPI material is required.
- A public URL does not load without login.
- The artifact includes trading instructions, venue rankings, model thresholds, or private signal claims.
- Timestamp freshness is `unknown` or `blocked` for fields used in live inference.
- Replay parity is asserted as `exact` without event-time ordering and full-window coverage.

## Sanitized Example

```yaml
derivatives_market_data_integrity:
  schema_version: "1.0"
  artifact_id: "public-btc-options-perps-sample"
  declared_at_utc: "2026-05-10T00:05:30Z"
  public_data_only: true
  trading_signal_claims: false
  venue_coverage:
    - venue: "example_options_venue"
      venue_type: "offshore_cex"
      market_segment: "options"
      coverage_role: "primary_surface"
      coverage_window:
        start_utc: "2026-05-10T00:00:00Z"
        end_utc: "2026-05-10T00:05:00Z"
        full_window_coverage: true
        known_gaps: []
      instruments:
        - instrument_type: "option"
          venue_symbol: "BTC-29MAY26-100000-C"
          normalized_instrument_id: "option:example_options_venue:BTC:2026-05-29T08:00:00Z:100000:call:BTC"
          base_asset: "BTC"
          quote_asset: "USD"
          settlement_asset: "BTC"
          expiry_utc: "2026-05-29T08:00:00Z"
          strike: "100000"
          option_right: "call"
          contract_style: "european"
          contract_multiplier: "1"
          source:
            source_type: "public_api"
            source_url_or_endpoint_label: "public instruments/ticker endpoint"
            source_method: "rest"
          timestamps:
            capture_time_utc: "2026-05-10T00:05:01Z"
            exchange_timestamp_utc: "2026-05-10T00:05:00Z"
            exchange_timestamp_field: "timestamp"
            observed_lag_ms: 1000
            freshness_status: "fresh"
            freshness_reason: "captured one second after exchange timestamp"
          provenance:
            open_interest:
              status: "present"
              source_field: "open_interest"
              unit: "contracts"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            implied_volatility:
              status: "present"
              source_field: "mark_iv"
              unit: "decimal"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            greeks:
              status: "present"
              greek_set: ["delta", "gamma", "vega", "theta"]
              source_field_or_method: "public ticker greeks fields"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            mark_price:
              status: "present"
              source_field: "mark_price"
              quote_unit: "USD"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
            index_price:
              status: "present"
              source_field: "index_price"
              quote_unit: "USD"
              timestamp_utc: "2026-05-10T00:05:00Z"
              degraded_reason: ""
              block_reason: ""
          replay_parity:
            status: "approximate"
            event_time_ordering: "preserved"
            snapshot_delta_consistency: "not_applicable"
            no_future_leakage_checked: true
            replay_notes: "ticker snapshots are ordered by exchange timestamp, but full orderbook deltas are not included"
          degradation:
            degraded: true
            severity: "info"
            reasons: ["ticker-only record without full orderbook replay"]
            block_reason: ""
  artifact_validation:
    accessible_without_login: true
    contains_private_data: false
    contains_wallet_or_auth_material: false
    contains_mnpi: false
    contains_trading_signal_claims: false
    reviewer_notes: "schema example uses placeholders and public-data labels only"
```

## Reviewer Checklist

- The page defines a schema, not a strategy.
- Venue coverage and instrument identity are visible.
- Capture time, exchange timestamp, and freshness are visible.
- OI, IV, Greeks, mark price, and index price provenance are explicitly labeled.
- Replay parity is explicit and conservative.
- Degraded and blocked states include reasons.
- The artifact contains no private account data, wallet/auth material, MNPI, or trading signal claims.
