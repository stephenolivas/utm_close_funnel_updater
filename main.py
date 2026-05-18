"""Entry point. Orchestrates sheet read → Close search → lead updates → reports."""
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import close
import config
import matcher
import sheets

LOCAL_TZ = ZoneInfo(config.TIMEZONE)


def _local_now_iso() -> str:
    """Current time in the configured local timezone, ISO 8601 with offset."""
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


# Python logging: timestamps in local timezone too, so log lines line up
# with both the sheet timestamps and the GitHub Actions UI.
class _LocalFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # type: ignore[override]
        dt = datetime.fromtimestamp(record.created, LOCAL_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(timespec="seconds")

_handler = logging.StreamHandler(stream=sys.stdout)
_handler.setFormatter(_LocalFormatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %Z",
))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("sync")


def main() -> int:
    start = time.time()
    stats = {
        "timestamp":                 _local_now_iso(),
        "dry_run":                   config.DRY_RUN,
        "duration_sec":              0,
        "source_rows":               0,
        "contacts_scanned":          0,
        "contacts_false_positive":   0,   # Close returned them, but utm_source wasn't actually a match
        "leads_processed":           0,
        "leads_updated":             0,
        "leads_skipped_already_set": 0,
        "leads_raced":               0,   # Zap (or other integration) populated the field between our two reads
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
    utm_medium_field   = config.CLOSE_FIELDS["contact"]["utm_medium"]
    utm_campaign_field = config.CLOSE_FIELDS["contact"]["utm_campaign"]
    utm_content_field  = config.CLOSE_FIELDS["contact"]["utm_content"]
    funnel_name_field  = config.CLOSE_FIELDS["lead"]["funnel_name"]

    contact_fields = [
        "id",
        "lead_id",
        "date_created",
        "date_updated",
        f"custom.{utm_source_field}",
        f"custom.{utm_medium_field}",
        f"custom.{utm_campaign_field}",
        f"custom.{utm_content_field}",
    ]

    # Note: the /contact/ endpoint silently ignores date filters in the query
    # string. We work around the 10k pagination ceiling by sorting newest-first
    # in close.search_contacts() and breaking out of the loop below as soon as
    # a contact older than LOOKBACK_DAYS appears.
    query = f'custom.{utm_source_field}:"youtube"'
    log.info("Close query: %s (sorted by -date_updated)", query)

    # Dedupe contacts by lead_id. Track false positives so we can see how
    # noisy Close's search is.
    leads_to_process: dict[str, list[dict]] = defaultdict(list)
    sample_false_positives: list[tuple[str, str]] = []   # (contact_id, actual_utm_source)

    try:
        for c in cli.search_contacts(query, contact_fields):
            stats["contacts_scanned"] += 1
            date_for_filter = c.get("date_updated") or c.get("date_created")
            if not matcher.is_recent(date_for_filter, config.LOOKBACK_DAYS):
                # Sorted desc — every subsequent contact is also older. Done.
                log.info(
                    "Hit lookback cutoff at contact %s (date_updated=%s) after "
                    "scanning %d contacts — stopping pagination",
                    c.get("id"), date_for_filter, stats["contacts_scanned"],
                )
                break

            # --- SAFETY CHECK ---
            # Close's search returns false positives — contacts whose
            # utm_source field is empty, stale, or unrelated. Before
            # queueing the lead for update, confirm this contact's
            # utm_source actually matches our source map.
            actual_utm_source = matcher.normalize(c.get(f"custom.{utm_source_field}"))
            if actual_utm_source not in source_map:
                stats["contacts_false_positive"] += 1
                if len(sample_false_positives) < 5:
                    sample_false_positives.append((c.get("id", ""), actual_utm_source or "(empty)"))
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

    if stats["contacts_false_positive"] > 0:
        log.warning(
            "Close search returned %d false positives "
            "(contacts whose utm_source did not actually match). "
            "Sample: %s",
            stats["contacts_false_positive"], sample_false_positives,
        )

    log.info(
        "Scanned %d contacts; %d verified as utm_source match; "
        "%d unique leads with activity in last %d days",
        stats["contacts_scanned"],
        stats["contacts_scanned"] - stats["contacts_false_positive"],
        len(leads_to_process),
        config.LOOKBACK_DAYS,
    )

    # -------------------------------------------------------------------------
    # 3. Process each lead
    # -------------------------------------------------------------------------
    log.info("=== Step 3: Processing leads ===")
    missing_funnels: dict[str, dict] = {}
    conflicts: list[dict] = []

    def _format_contacts(cs: list[dict]) -> str:
        """Build a human-readable block listing the contacts that triggered this lead."""
        lines = []
        for c in cs:
            lines.append(
                f"    {c.get('id', '?')} | "
                f"utm_source={c.get(f'custom.{utm_source_field}', '') or '(empty)'!r} | "
                f"utm_medium={c.get(f'custom.{utm_medium_field}', '') or '(empty)'!r} | "
                f"utm_campaign={c.get(f'custom.{utm_campaign_field}', '') or '(empty)'!r} | "
                f"utm_content={c.get(f'custom.{utm_content_field}', '') or '(empty)'!r} | "
                f"updated={c.get('date_updated', '?')}"
            )
        return "\n".join(lines)

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

        display_name = lead.get("display_name") or "(no name)"
        current_funnel = str(lead.get(f"custom.{funnel_name_field}") or "").strip()
        raw_funnel_value = lead.get(f"custom.{funnel_name_field}")

        # --- Check A: write decision based on utm_source ---
        if not current_funnel:
            action = "write"
        elif current_funnel == target_funnel:
            action = "skip_already_set"
        else:
            action = "conflict"

        if action == "write":
            # Verbose decision log: full context so each "would update" can be
            # spot-checked without re-querying Close. The funnel field is also
            # written by a flaky Zap; this evidence proves what we saw at the
            # moment of decision in case the UI shows a value later.
            tag = "[DRY] Would update" if config.DRY_RUN else "Updating"
            log.info(
                "%s lead %s '%s'\n"
                "  url:            %s\n"
                "  current funnel: raw=%r (treated as empty)\n"
                "  target funnel:  %r\n"
                "  triggering contacts (%d):\n%s",
                tag, lead_id, display_name, lead_url, raw_funnel_value,
                target_funnel, len(contacts), _format_contacts(contacts),
            )

            if config.DRY_RUN:
                stats["leads_updated"] += 1
            else:
                # Race-protection: re-fetch immediately before write. If the
                # Zap (or anything else) populated the field between our two
                # reads, don't redundantly overwrite.
                try:
                    lead_recheck = cli.get_lead(
                        lead_id, ["id", f"custom.{funnel_name_field}"],
                    )
                except Exception as e:
                    log.warning("Failed to re-fetch lead %s before write: %s", lead_id, e)
                    stats["errors"] += 1
                    continue

                recheck_raw = lead_recheck.get(f"custom.{funnel_name_field}")
                recheck_funnel = str(recheck_raw or "").strip()

                if recheck_funnel == target_funnel:
                    log.info(
                        "Race detected on lead %s — funnel populated to '%s' between reads "
                        "(raw=%r); skipping write (url=%s)",
                        lead_id, recheck_funnel, recheck_raw, lead_url,
                    )
                    stats["leads_raced"] += 1
                    continue

                if recheck_funnel and recheck_funnel != target_funnel:
                    log.info(
                        "Race-to-conflict on lead %s — funnel changed to '%s' between reads "
                        "(raw=%r); recording as conflict (url=%s)",
                        lead_id, recheck_funnel, recheck_raw, lead_url,
                    )
                    stats["conflicts"] += 1
                    c = contacts[0]
                    conflicts.append({
                        "lead_id":               lead_id,
                        "lead_url":              lead_url,
                        "current_funnel_name":   recheck_funnel,
                        "attempted_funnel_name": target_funnel,
                        "contact_id":            c.get("id", ""),
                        "utm_source":            c.get(f"custom.{utm_source_field}", ""),
                        "utm_campaign":          c.get(f"custom.{utm_campaign_field}", ""),
                    })
                    continue

                try:
                    cli.update_lead(lead_id, {
                        f"custom.{funnel_name_field}": target_funnel,
                    })
                    log.info(
                        "  → wrote: lead %s funnel set to '%s' (was raw=%r)",
                        lead_id, target_funnel, recheck_raw,
                    )
                    stats["leads_updated"] += 1
                except Exception as e:
                    log.warning("Failed to update lead %s: %s", lead_id, e)
                    stats["errors"] += 1
        elif action == "skip_already_set":
            stats["leads_skipped_already_set"] += 1
            # One-line audit so the user can spot-check every YouTube lead in
            # the window, not just the ones that need writes.
            log.info(
                "Already set: lead %s '%s' — funnel=%r (url=%s)",
                lead_id, display_name, current_funnel, lead_url,
            )
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
                "Conflict on lead %s '%s'\n"
                "  url:             %s\n"
                "  current funnel:  %r  ← NOT overwriting\n"
                "  attempted funnel: %r\n"
                "  triggering contacts (%d):\n%s",
                lead_id, display_name, lead_url, current_funnel,
                target_funnel, len(contacts), _format_contacts(contacts),
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
        "=== Done in %ds | scanned=%d false_pos=%d processed=%d updated=%d "
        "already_set=%d raced=%d conflicts=%d missing=%d errors=%d ===",
        stats["duration_sec"],
        stats["contacts_scanned"],
        stats["contacts_false_positive"],
        stats["leads_processed"],
        stats["leads_updated"],
        stats["leads_skipped_already_set"],
        stats["leads_raced"],
        stats["conflicts"],
        stats["missing_campaigns"],
        stats["errors"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
