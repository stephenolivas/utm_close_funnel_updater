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


def _classify_source(value, medium_value, source_map: dict[str, str]) -> str | None:
    """Map a contact's raw utm_source value to a funnel name, or None if no
    match. Combines:
      - source+medium override (SOURCE_MEDIUM_OVERRIDES) — checked first;
        used to distinguish owned vs agency-managed channels
      - exact lookup against source_map (sheet + simple config mappings)
      - prefix match against MALFORMED_SOURCE_PREFIXES (catches integrations
        that stuff the full query string into utm_source)
    """
    if not value:
        return None
    v = matcher.normalize(value)
    if not v:
        return None
    # Source + medium override (e.g. instagram + organic-social → Anthony IG)
    if v in config.SOURCE_MEDIUM_OVERRIDES:
        med = matcher.normalize(medium_value or "")
        override = config.SOURCE_MEDIUM_OVERRIDES[v].get(med)
        if override:
            return override
    if v in source_map:
        return source_map[v]
    # Malformed-prefix fallback: e.g. "linkedin&utm_medium=..." → LinkedIn
    for prefix, funnel in config.MALFORMED_SOURCE_PREFIXES.items():
        if v.startswith(prefix + "&") or v.startswith(prefix + "?"):
            return funnel
    return None


def _format_per_funnel(by_funnel: dict[str, dict[str, int]]) -> str:
    """Render per-funnel UPDATE counts as a multi-line string for the run log
    cell. Only funnels with updated > 0 are listed (this matches the rule
    that we only write a run-log row when updates happened). Sorted by
    funnel name for stable ordering across runs."""
    if not by_funnel:
        return ""
    lines = []
    for funnel in sorted(by_funnel):
        n = by_funnel[funnel].get("updated", 0)
        if n > 0:
            lines.append(f"{funnel}: {n}")
    return "\n".join(lines)


# ANSI color codes for GitHub Actions log output. GitHub renders these
# inline, making it easy to skim the log for lines that represent actual
# writes. Used only on the live "wrote" confirmation lines so they stand
# out against the wall of "Already set" / dry-run / processing chatter.
_GREEN = "\033[1;92m"   # bold bright green
_RESET = "\033[0m"


def _decide_action(current_funnel: str, target_funnel: str) -> str:
    """Return one of: 'write', 'skip_already_set', 'overwrite', 'conflict'.

    Centralized so initial read and race re-fetch use the same logic.
    Policy:
      - empty current             → write
      - current == target         → skip_already_set
      - current is overridable
        AND target may overwrite  → overwrite
      - otherwise                 → conflict (fill-only protection)
    """
    if not current_funnel:
        return "write"
    if current_funnel == target_funnel:
        return "skip_already_set"
    if (current_funnel in config.OVERRIDABLE_CURRENT_FUNNELS
            and target_funnel not in config.NON_OVERRIDING_TARGET_FUNNELS):
        return "overwrite"
    return "conflict"


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
        "leads_overwritten":         0,   # subset of leads_updated where we replaced an OVERRIDABLE current value
        "leads_skipped_already_set": 0,
        "leads_raced":               0,   # Zap (or other integration) populated the field between our two reads
        "conflicts":                 0,
        "missing_campaigns":         0,
        "errors":                    0,
        "notes":                     "",
        "per_funnel":                "",  # formatted breakdown for the run log cell
    }
    # Per-funnel counters, populated as we encounter funnels. Rendered into
    # stats["per_funnel"] right before the run log is written.
    by_funnel: dict[str, dict[str, int]] = {}

    def _bump(funnel: str, key: str) -> None:
        by_funnel.setdefault(funnel, {
            "updated": 0, "already_set": 0, "conflicts": 0, "raced": 0,
        })[key] += 1

    # -------------------------------------------------------------------------
    # 1. Open sheet and read every source tab (with integrity checks)
    # -------------------------------------------------------------------------
    log.info("=== Step 1: Reading master sheet ===")
    sheet = sheets.open_sheet()
    sheet_source_map: dict[str, str] = {}                 # utm_source → funnel
    known_campaigns_by_funnel: dict[str, set[str]] = {}   # funnel → set of campaigns
    total_source_rows = 0

    try:
        for tab_name in config.SOURCE_TABS:
            sm, kc, rows = sheets.read_source_tab(sheet, tab_name)
            total_source_rows += rows
            # Merge: later tabs override earlier on key collision (shouldn't
            # happen in practice — each tab owns a distinct utm_source).
            for src, fn in sm.items():
                if src in sheet_source_map and sheet_source_map[src] != fn:
                    log.warning(
                        "Tab '%s' overrides utm_source=%r: '%s' → '%s'",
                        tab_name, src, sheet_source_map[src], fn,
                    )
                sheet_source_map[src] = fn
            # Attribute campaigns to the funnel that owns this tab. A tab
            # with a single funnel column value (the normal case) puts all
            # its campaigns under that funnel.
            for fn in set(sm.values()):
                known_campaigns_by_funnel.setdefault(fn, set()).update(kc)

        # Row-count baseline runs against the SUM of all tabs.
        sheets.check_total_row_count_baseline(sheet, total_source_rows)
    except sheets.IntegrityError as e:
        log.error("Integrity check failed: %s", e)
        stats["errors"] = 1
        stats["notes"] = f"Aborted: {e}"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 1
    except Exception as e:
        log.exception("Unexpected error reading source tabs")
        stats["errors"] = 1
        stats["notes"] = f"Read failed: {e}"
        stats["duration_sec"] = int(time.time() - start)
        try:
            sheets.append_run_log(sheet, stats)
        except Exception:
            pass
        return 1

    stats["source_rows"] = total_source_rows
    sheet_driven_sources = set(sheet_source_map.keys())

    # Combined source map = sheet entries + simple config entries.
    # Sheet wins on key collision so Marketing can override config without a deploy.
    combined_source_map: dict[str, str] = dict(config.SIMPLE_SOURCE_MAPPINGS)
    combined_source_map.update(sheet_source_map)

    log.info("Sheet-driven sources: %s", sorted(sheet_driven_sources))
    log.info("Combined source map (%d entries): %s",
             len(combined_source_map), combined_source_map)

    if not combined_source_map:
        log.warning("Source map is empty — nothing to do this run")
        stats["notes"] = "Empty source map (no sheet entries and no config entries)"
        stats["duration_sec"] = int(time.time() - start)
        sheets.append_run_log(sheet, stats)
        return 0

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
    search_terms = sorted(combined_source_map.keys())
    query = " OR ".join(
        f'custom.{utm_source_field}:"{term}"' for term in search_terms
    )
    log.info("Close query (%d source terms, sorted -date_updated): %s",
             len(search_terms), query)

    # Dedupe contacts by lead_id. Each contact gets a "_funnel" attribute
    # attached when classified so we know which funnel to write per lead.
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

            # --- CLASSIFY ---
            # Close's search returns false positives — contacts whose
            # utm_source field is empty, unrelated, or for a source we don't
            # configure. The classifier returns None for those and we drop
            # them. For known sources (including malformed-prefix variants),
            # it returns the funnel name we'll write.
            raw_utm_source = c.get(f"custom.{utm_source_field}")
            raw_utm_medium = c.get(f"custom.{utm_medium_field}")
            funnel = _classify_source(raw_utm_source, raw_utm_medium, combined_source_map)
            if funnel is None:
                stats["contacts_false_positive"] += 1
                if len(sample_false_positives) < 5:
                    sample_false_positives.append(
                        (c.get("id", ""), str(raw_utm_source or "(empty)"))
                    )
                continue

            lead_id = c.get("lead_id")
            if not lead_id:
                continue
            c["_funnel"] = funnel
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

        # Target funnel = funnel from this lead's most-recently-updated
        # matching contact. search_contacts returns newest-first, so
        # contacts[0] is the most recent. If a lead has matches from
        # multiple sources, most-recent wins — log if that happens so we
        # have visibility into the edge case.
        target_funnel = contacts[0]["_funnel"]
        distinct_funnels = {c["_funnel"] for c in contacts}
        if len(distinct_funnels) > 1:
            log.info(
                "Lead %s has contacts from multiple sources: %s — using "
                "most-recent funnel '%s'",
                lead_id, sorted(distinct_funnels), target_funnel,
            )

        # Tag every per-lead log line with the funnel so they can be filtered
        # in the GitHub Actions log search (e.g. search for "[Instagram]").
        ftag = f"[{target_funnel}]"

        # Fetch the lead to read its current Funnel Name
        try:
            lead = cli.get_lead(lead_id, ["id", "display_name", f"custom.{funnel_name_field}"])
        except Exception as e:
            log.warning("%s Failed to fetch lead %s: %s", ftag, lead_id, e)
            stats["errors"] += 1
            continue

        display_name = lead.get("display_name") or "(no name)"
        current_funnel = str(lead.get(f"custom.{funnel_name_field}") or "").strip()
        raw_funnel_value = lead.get(f"custom.{funnel_name_field}")

        # --- Check A: write decision based on utm_source ---
        action = _decide_action(current_funnel, target_funnel)

        if action in ("write", "overwrite"):
            # Verbose decision log: full context so each pending update can be
            # spot-checked without re-querying Close. The funnel field is also
            # written by a flaky Zap; this evidence proves what we saw at the
            # moment of decision in case the UI shows a value later.
            if action == "overwrite":
                overwrite_note = f" (OVERWRITING current funnel: {current_funnel!r})"
                current_funnel_desc = f"raw={raw_funnel_value!r} (in OVERRIDABLE_CURRENT_FUNNELS — will be replaced)"
            else:
                overwrite_note = ""
                current_funnel_desc = f"raw={raw_funnel_value!r} (treated as empty)"

            tag = "[DRY] Would update" if config.DRY_RUN else "Updating"
            log.info(
                "%s %s lead %s '%s'%s\n"
                "  url:            %s\n"
                "  current funnel: %s\n"
                "  target funnel:  %r\n"
                "  triggering contacts (%d):\n%s",
                ftag, tag, lead_id, display_name, overwrite_note, lead_url,
                current_funnel_desc, target_funnel, len(contacts),
                _format_contacts(contacts),
            )

            if config.DRY_RUN:
                stats["leads_updated"] += 1
                _bump(target_funnel, "updated")
                if action == "overwrite":
                    stats["leads_overwritten"] += 1
            else:
                # Race-protection: re-fetch immediately before write. Use
                # _decide_action to re-evaluate against whatever the field
                # contains NOW — handles cases where another integration
                # cleared it, set it to our target, or set it to something
                # we shouldn't overwrite.
                try:
                    lead_recheck = cli.get_lead(
                        lead_id, ["id", f"custom.{funnel_name_field}"],
                    )
                except Exception as e:
                    log.warning("%s Failed to re-fetch lead %s before write: %s", ftag, lead_id, e)
                    stats["errors"] += 1
                    continue

                recheck_raw = lead_recheck.get(f"custom.{funnel_name_field}")
                recheck_funnel = str(recheck_raw or "").strip()
                recheck_action = _decide_action(recheck_funnel, target_funnel)

                if recheck_action == "skip_already_set":
                    log.info(
                        "%s Race detected on lead %s — funnel populated to '%s' between reads "
                        "(raw=%r); skipping write. url: %s",
                        ftag, lead_id, recheck_funnel, recheck_raw, lead_url,
                    )
                    stats["leads_raced"] += 1
                    _bump(target_funnel, "raced")
                    continue

                if recheck_action == "conflict":
                    log.info(
                        "%s Race-to-conflict on lead %s — funnel changed to '%s' between reads "
                        "(raw=%r); recording as conflict. url: %s",
                        ftag, lead_id, recheck_funnel, recheck_raw, lead_url,
                    )
                    stats["conflicts"] += 1
                    _bump(target_funnel, "conflicts")
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

                # recheck_action is "write" or "overwrite" — proceed with PUT
                try:
                    cli.update_lead(lead_id, {
                        f"custom.{funnel_name_field}": target_funnel,
                    })
                    overwrite_marker = " [OVERWRITE]" if recheck_action == "overwrite" else ""
                    log.info(
                        f"{_GREEN}%s   → wrote: lead %s funnel set to '%s' (was raw=%r){overwrite_marker}{_RESET}",
                        ftag, lead_id, target_funnel, recheck_raw,
                    )
                    stats["leads_updated"] += 1
                    _bump(target_funnel, "updated")
                    if recheck_action == "overwrite":
                        stats["leads_overwritten"] += 1
                except Exception as e:
                    log.warning("%s Failed to update lead %s: %s", ftag, lead_id, e)
                    stats["errors"] += 1
        elif action == "skip_already_set":
            stats["leads_skipped_already_set"] += 1
            _bump(target_funnel, "already_set")
            # One-line audit so the user can spot-check every matching lead in
            # the window, not just the ones that need writes.
            log.info(
                "%s Already set: lead %s '%s' — funnel=%r — url: %s",
                ftag, lead_id, display_name, current_funnel, lead_url,
            )
        elif action == "conflict":
            stats["conflicts"] += 1
            _bump(target_funnel, "conflicts")
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
                "%s Conflict on lead %s '%s'\n"
                "  url:             %s\n"
                "  current funnel:  %r  ← NOT overwriting\n"
                "  attempted funnel: %r\n"
                "  triggering contacts (%d):\n%s",
                ftag, lead_id, display_name, lead_url, current_funnel,
                target_funnel, len(contacts), _format_contacts(contacts),
            )

        # --- Check B: campaign monitoring (only for sheet-driven sources) ---
        # Simple config-driven channels (Instagram, X, Linkedin) have no
        # known-campaigns list, so we don't track missing campaigns for them.
        # Each funnel checks against its OWN tab's campaign list — so a
        # campaign known to the Webinar tab doesn't accidentally "satisfy"
        # a YouTube contact.
        for c in contacts:
            funnel = c["_funnel"]
            if funnel not in known_campaigns_by_funnel:
                continue  # not sheet-driven
            campaign = str(c.get(f"custom.{utm_campaign_field}") or "").strip()
            if campaign and campaign in known_campaigns_by_funnel[funnel]:
                continue
            # Prefix the key with the funnel so different funnels' missing
            # campaigns don't collide and the sheet entry is self-describing.
            key = f"[{funnel}] {campaign or '(blank)'}"
            entry = missing_funnels.setdefault(key, {
                "count": 0,
                "sample_lead_url": lead_url,
            })
            entry["count"] += 1

    stats["missing_campaigns"] = len(missing_funnels)
    stats["per_funnel"] = _format_per_funnel(by_funnel)

    # -------------------------------------------------------------------------
    # 4. Write reports
    # -------------------------------------------------------------------------
    log.info("=== Step 4: Writing reports ===")
    # Always call update_missing_funnels — passing {} clears resolved campaigns.
    sheets.update_missing_funnels(sheet, missing_funnels)
    sheets.append_conflicts(sheet, conflicts)

    stats["duration_sec"] = int(time.time() - start)

    # Only append a run-log row when something meaningful happened: an
    # actual write, a conflict that needs human review, or an error. Quiet
    # runs (all "already set") don't add noise to the sheet. Errors and
    # conflicts are kept because losing them silently is worse than noise.
    if stats["leads_updated"] > 0 or stats["errors"] > 0 or stats["conflicts"] > 0:
        sheets.append_run_log(sheet, stats)
    else:
        log.info(
            "Quiet run (no updates, no conflicts, no errors); skipping run-log append"
        )

    log.info(
        "=== Done in %ds | scanned=%d false_pos=%d processed=%d updated=%d "
        "(of which overwrites=%d) already_set=%d raced=%d conflicts=%d "
        "missing=%d errors=%d ===",
        stats["duration_sec"],
        stats["contacts_scanned"],
        stats["contacts_false_positive"],
        stats["leads_processed"],
        stats["leads_updated"],
        stats["leads_overwritten"],
        stats["leads_skipped_already_set"],
        stats["leads_raced"],
        stats["conflicts"],
        stats["missing_campaigns"],
        stats["errors"],
    )
    if stats["per_funnel"]:
        log.info("Per-funnel breakdown:\n%s", stats["per_funnel"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
