"""Google Sheets I/O: read source tab, perform integrity checks, write reports."""
import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class IntegrityError(Exception):
    """Raised when the source tab contains error sentinels or fails the row-count baseline."""


# -----------------------------------------------------------------------------
# Auth + open
# -----------------------------------------------------------------------------
def get_client() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var not set")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_sheet() -> gspread.Spreadsheet:
    if not config.SHEET_ID:
        raise RuntimeError("MASTER_SHEET_ID env var not set")
    return get_client().open_by_key(config.SHEET_ID)


# -----------------------------------------------------------------------------
# Source tab read + integrity checks
# -----------------------------------------------------------------------------
def read_source_tab(sheet) -> tuple[dict, set, int]:
    """
    Read the source tab and return (source_map, known_campaigns, row_count).

    source_map: {utm_source_lower: funnel_name}
    known_campaigns: set of non-empty utm_campaign values
    row_count: number of non-empty data rows (used for the baseline check)

    Raises IntegrityError if the tab looks broken.
    """
    try:
        ws = sheet.worksheet(config.SOURCE_TAB)
    except gspread.WorksheetNotFound:
        raise RuntimeError(f"Tab '{config.SOURCE_TAB}' not found in master sheet")

    rows = ws.get_all_records()

    # 1. Header validation
    if rows:
        present = set(rows[0].keys())
        missing = [h for h in config.REQUIRED_HEADERS if h not in present]
        if missing:
            raise RuntimeError(
                f"Tab '{config.SOURCE_TAB}' missing required headers: {missing}. "
                f"Found: {sorted(present)}"
            )

    # 2. Integrity check: error sentinels anywhere in the tab
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        for k, v in row.items():
            if isinstance(v, str) and v.strip() in config.INTEGRITY_FAIL_VALUES:
                raise IntegrityError(
                    f"Tab '{config.SOURCE_TAB}' row {i} column '{k}' contains "
                    f"error sentinel '{v.strip()}' — aborting run"
                )

    # 3. Count non-empty rows
    row_count = sum(1 for r in rows if any(str(v).strip() for v in r.values()))

    # 4. Row-count baseline check (catches a silently broken IMPORTRANGE)
    _check_row_count_baseline(sheet, row_count)

    # 5. Build outputs
    source_map: dict[str, str] = {}
    known_campaigns: set[str] = set()
    for row in rows:
        utm_source   = str(row.get("utm_source", "")).strip().lower()
        funnel       = str(row.get(config.FUNNEL_NAME_HEADER, "")).strip()
        utm_campaign = str(row.get("utm_campaign", "")).strip()
        if utm_source and funnel:
            source_map[utm_source] = funnel
        if utm_campaign:
            known_campaigns.add(utm_campaign)

    log.info(
        "Loaded %d rows from '%s': %d source mappings, %d known campaigns",
        row_count, config.SOURCE_TAB, len(source_map), len(known_campaigns),
    )
    return source_map, known_campaigns, row_count


def _check_row_count_baseline(sheet, current_count: int) -> None:
    """Abort if source row count dropped more than the configured threshold."""
    try:
        ws = sheet.worksheet(config.RUN_LOG_TAB)
    except gspread.WorksheetNotFound:
        log.info("No prior run log found; skipping row-count baseline check")
        return

    rows = ws.get_all_records()
    last = None
    for r in reversed(rows):
        val = r.get("source_rows")
        if val in (None, ""):
            continue
        try:
            last = int(val)
            break
        except (TypeError, ValueError):
            continue

    if last is None:
        log.info("No prior source_rows recorded; skipping baseline check")
        return

    threshold = last * (1 - config.ROW_DROP_ABORT_THRESHOLD)
    if last > 0 and current_count < threshold:
        raise IntegrityError(
            f"Source tab row count dropped from {last} to {current_count} "
            f"(>{int(config.ROW_DROP_ABORT_THRESHOLD * 100)}% drop) — aborting run. "
            f"Possible broken IMPORTRANGE or sharing revoked."
        )


# -----------------------------------------------------------------------------
# Report writers
# -----------------------------------------------------------------------------
def _get_or_create_worksheet(sheet, title: str, headers: list[str]) -> gspread.Worksheet:
    try:
        return sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=title, rows=1000, cols=max(len(headers), 8))
        ws.update(range_name="A1", values=[headers])
        return ws


def update_missing_funnels(sheet, missing: dict) -> None:
    """
    Accumulate unmatched campaigns across runs.

    Input: {campaign: {"count": int, "sample_lead_url": str}}

    Rules:
      * Preserves first_seen for campaigns seen previously
      * Increments count by this run's occurrences
      * DROPS campaigns that no longer appear missing (Marketing added the rule)
    """
    headers = ["campaign", "count", "first_seen", "last_seen", "sample_lead_url"]
    ws = _get_or_create_worksheet(sheet, config.MISSING_TAB, headers)

    existing = ws.get_all_records()
    by_campaign = {r["campaign"]: r for r in existing if r.get("campaign")}
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    updated_rows = []
    for campaign, data in missing.items():
        if campaign in by_campaign:
            prev = by_campaign[campaign]
            try:
                prev_count = int(prev.get("count") or 0)
            except (TypeError, ValueError):
                prev_count = 0
            row = {
                "campaign":        campaign,
                "count":           prev_count + data["count"],
                "first_seen":      prev.get("first_seen") or now_iso,
                "last_seen":       now_iso,
                "sample_lead_url": prev.get("sample_lead_url") or data.get("sample_lead_url", ""),
            }
        else:
            row = {
                "campaign":        campaign,
                "count":           data["count"],
                "first_seen":      now_iso,
                "last_seen":       now_iso,
                "sample_lead_url": data.get("sample_lead_url", ""),
            }
        updated_rows.append(row)

    updated_rows.sort(key=lambda r: -int(r["count"]))
    body = [headers] + [[r.get(h, "") for h in headers] for r in updated_rows]

    ws.clear()
    ws.update(range_name="A1", values=body)
    log.info("Wrote %d rows to '%s'", len(updated_rows), config.MISSING_TAB)


def append_conflicts(sheet, conflicts: list[dict]) -> None:
    """Append-only conflict log."""
    if not conflicts:
        return
    headers = [
        "timestamp", "lead_id", "lead_url", "current_funnel_name",
        "attempted_funnel_name", "contact_id", "utm_source", "utm_campaign",
    ]
    ws = _get_or_create_worksheet(sheet, config.CONFLICTS_TAB, headers)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = [[now_iso] + [c.get(h, "") for h in headers[1:]] for c in conflicts]
    ws.append_rows(body, value_input_option="USER_ENTERED")
    log.info("Appended %d rows to '%s'", len(body), config.CONFLICTS_TAB)


def append_run_log(sheet, stats: dict) -> None:
    """Append one row per run with summary stats."""
    headers = [
        "timestamp", "duration_sec", "dry_run", "source_rows",
        "contacts_scanned", "leads_processed", "leads_updated",
        "leads_skipped_already_set", "conflicts", "missing_campaigns",
        "errors", "notes",
    ]
    ws = _get_or_create_worksheet(sheet, config.RUN_LOG_TAB, headers)
    row = [[stats.get(h, "") for h in headers]]
    ws.append_rows(row, value_input_option="USER_ENTERED")
    log.info("Appended run log entry to '%s'", config.RUN_LOG_TAB)
