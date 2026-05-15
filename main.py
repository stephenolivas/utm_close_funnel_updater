"""Entry point. Orchestrates sheet read → Close search → lead updates → reports."""
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import close
import config
import matcher
import sheets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sync")


def main() -> int:
    start = time.time()
    stats = {
        "timestamp":                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run":                   config.DRY_RUN,
        "duration_sec":              0,
        "source_rows":               0,
        "contacts_scanned":          0,
        "leads_processed":           0,
        "leads_updated":             0,
        "leads_skipped_already_set": 0,
        "conflicts":                 0,
        "missing_campaigns":         0,
        "errors":                    0,
        "notes":                     "",
    }

    # -------------------------------------------------------------------------
    # 1. Open sheet and read source tab (with integrity checks)
    # -------------------------------------------------------------------------
    log.info("=== Step 1: Reading master sheet ===")
    sheet = sheets.open_sheet()
    try:
        source_map, known_campaigns, source_rows = sheets.read_source_tab(sheet)
    except sheets.IntegrityError as e:
        log.error("Integrity check failed: %s", e)
        stats["errors"] = 1
        stats["notes"] = f"Aborted: {e}"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 1
    except Exception as e:
        log.exception("Unexpected error reading source tab")
        stats["errors"] = 1
        stats["notes"] = f"Read failed: {e}"
        stats["duration_sec"] = int(time.time() - start)
        try:
            sheets.append_run_log(sheet, stats)
        except Exception:
            pass
        return 1

    stats["source_rows"] = source_rows
    log.info("Source map: %s", source_map)

    if "youtube" not in source_map:
        log.warning("No 'youtube' entry in source map — nothing to do this run")
        stats["notes"] = "No youtube source mapping found in source tab"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 0

    target_funnel = source_map["youtube"]
    log.info("Target funnel for utm_source=youtube: '%s'", target_funnel)

    # -------------------------------------------------------------------------
    # 2. Search Close for contacts with utm_source = youtube
    # -------------------------------------------------------------------------
    log.info("=== Step 2: Searching Close contacts ===")
    api_key = os.environ.get("CLOSE_API_KEY")
    if not api_key:
        log.error("CLOSE_API_KEY env var not set")
        stats["errors"] = 1
        stats["notes"] = "CLOSE_API_KEY missing"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 1

    cli = close.CloseClient(api_key)

    utm_source_field   = config.CLOSE_FIELDS["contact"]["utm_source"]
    utm_campaign_field = config.CLOSE_FIELDS["contact"]["utm_campaign"]
    funnel_name_field  = config.CLOSE_FIELDS["lead"]["funnel_name"]

    contact_fields = [
        "id",
        "lead_id",
        "date_created",
        "date_updated",
        f"custom.{utm_source_field}",
        f"custom.{utm_campaign_field}",
    ]

    # Push the date filter into the query itself. Close caps skip-based
    # pagination at ~10k records; without this filter we'd exceed that on
    # any account with a meaningful YouTube contact volume.
    # Syntax confirmed against Close's text query language docs:
    #   https://help.close.com/docs/searching-guide-single-queries
    query = (
        f'custom.{utm_source_field}:"youtube" '
        f'date_updated > "{config.LOOKBACK_DAYS} days ago"'
    )
    log.info("Close query: %s", query)

    # Dedupe contacts by lead_id. We keep the Python-side recency check as a
    # safety net in case the query-side date filter doesn't behave as expected.
    leads_to_process: dict[str, list[dict]] = defaultdict(list)
    try:
        for c in cli.search_contacts(query, contact_fields):
            stats["contacts_scanned"] += 1
            date_for_filter = c.get("date_updated") or c.get("date_created")
            if not matcher.is_recent(date_for_filter, config.LOOKBACK_DAYS):
                continue
            lead_id = c.get("lead_id")
            if not lead_id:
                continue
            leads_to_process[lead_id].append(c)
    except Exception as e:
        log.exception("Failed during contact search")
        stats["errors"] += 1
        stats["notes"] = f"Contact search failed: {e}"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 1

    log.info(
        "Scanned %d contacts; %d unique leads with activity in last %d days",
        stats["contacts_scanned"], len(leads_to_process), config.LOOKBACK_DAYS,
    )

    # -------------------------------------------------------------------------
    # 3. Process each lead
    # -------------------------------------------------------------------------
    log.info("=== Step 3: Processing leads ===")
    missing_funnels: dict[str, dict] = {}
    conflicts: list[dict] = []

    for lead_id, contacts in leads_to_process.items():
        stats["leads_processed"] += 1
        lead_url = f"https://app.close.com/lead/{lead_id}/"

        # Fetch the lead to read its current Funnel Name
        try:
            lead = cli.get_lead(lead_id, ["id", "display_name", f"custom.{funnel_name_field}"])
        except Exception as e:
            log.warning("Failed to fetch lead %s: %s", lead_id, e)
            stats["errors"] += 1
            continue

        current_funnel = str(lead.get(f"custom.{funnel_name_field}") or "").strip()

        # --- Check A: write decision based on utm_source ---
        if not current_funnel:
            action = "write"
        elif current_funnel == target_funnel:
            action = "skip_already_set"
        else:
            action = "conflict"

        if action == "write":
            if config.DRY_RUN:
                log.info("[DRY] Would update lead %s → Funnel Name = '%s'", lead_id, target_funnel)
                stats["leads_updated"] += 1
            else:
                try:
                    cli.update_lead(lead_id, {
                        f"custom.{funnel_name_field}": target_funnel,
                    })
                    log.info("Updated lead %s → '%s'", lead_id, target_funnel)
                    stats["leads_updated"] += 1
                except Exception as e:
                    log.warning("Failed to update lead %s: %s", lead_id, e)
                    stats["errors"] += 1
        elif action == "skip_already_set":
            stats["leads_skipped_already_set"] += 1
        elif action == "conflict":
            stats["conflicts"] += 1
            c = contacts[0]
            conflicts.append({
                "lead_id":               lead_id,
                "lead_url":              lead_url,
                "current_funnel_name":   current_funnel,
                "attempted_funnel_name": target_funnel,
                "contact_id":            c.get("id", ""),
                "utm_source":            c.get(f"custom.{utm_source_field}", ""),
                "utm_campaign":          c.get(f"custom.{utm_campaign_field}", ""),
            })
            log.info(
                "Conflict on lead %s: current='%s', attempted='%s'",
                lead_id, current_funnel, target_funnel,
            )

        # --- Check B: campaign monitoring (independent of write) ---
        for c in contacts:
            campaign = str(c.get(f"custom.{utm_campaign_field}") or "").strip()
            if campaign and campaign in known_campaigns:
                continue
            key = campaign or "(blank)"
            entry = missing_funnels.setdefault(key, {
                "count": 0,
                "sample_lead_url": lead_url,
            })
            entry["count"] += 1

    stats["missing_campaigns"] = len(missing_funnels)

    # -------------------------------------------------------------------------
    # 4. Write reports
    # -------------------------------------------------------------------------
    log.info("=== Step 4: Writing reports ===")
    # Always call update_missing_funnels — passing {} clears resolved campaigns.
    sheets.update_missing_funnels(sheet, missing_funnels)
    sheets.append_conflicts(sheet, conflicts)

    stats["duration_sec"] = int(time.time() - start)
    sheets.append_run_log(sheet, stats)

    log.info(
        "=== Done in %ds | scanned=%d processed=%d updated=%d already_set=%d "
        "conflicts=%d missing=%d errors=%d ===",
        stats["duration_sec"],
        stats["contacts_scanned"],
        stats["leads_processed"],
        stats["leads_updated"],
        stats["leads_skipped_already_set"],
        stats["conflicts"],
        stats["missing_campaigns"],
        stats["errors"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
