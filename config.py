"""Configuration: Close custom field IDs, sheet/tab names, and behavior tunables."""
import os

# -----------------------------------------------------------------------------
# Close CRM custom field IDs
# -----------------------------------------------------------------------------
# Note: "Funnel Name Deal (Opp)" is despite its name a LEAD-level custom field.
# UTM fields all live on the CONTACT object.
CLOSE_FIELDS = {
    "lead": {
        "funnel_name": "cf_xqDQE8fkPsWa0RNEve7hcaxKblCe6489XeZGRDzyPdX",
    },
    "contact": {
        "utm_source":   "cf_HA1ayKpXNvIKtmfTfLKWTZoEdBrpq5M35d19GinU5on",
        "utm_medium":   "cf_3csfRoal7yTIJBIBTZf0wJOVTypxE7nMyx6mq9Y0x5f",
        "utm_campaign": "cf_jnbd0xzUY3tuxzxiGxBs2hONuExeXMvAoTUM2R64Lq3",
        "utm_content":  "cf_R7o66i0XPycLQHlxOLbIqk6c6j3oB8CzxF3e3apI1hn",
        "utm_term":     "cf_xmkvth6khfF5h4PS6NYUYSeVfKR1UlSN9ssGTw3xHfj",
    },
}

# -----------------------------------------------------------------------------
# Master sheet config
# -----------------------------------------------------------------------------
SHEET_ID      = os.environ.get("MASTER_SHEET_ID", "")
SOURCE_TAB    = "YouTube"            # v1: only YouTube
MISSING_TAB   = "_Missing Funnels"
CONFLICTS_TAB = "_Conflicts"
RUN_LOG_TAB   = "_Run Log"

# The column header in the source tab that carries the funnel name to write
# to Close. Change this in one place if Marketing renames the column.
FUNNEL_NAME_HEADER = "Funnel Name for Close"

# Required column headers in the source tab. Script aborts if any are missing.
REQUIRED_HEADERS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", FUNNEL_NAME_HEADER]

# -----------------------------------------------------------------------------
# Behavior
# -----------------------------------------------------------------------------
# Only process contacts updated within this many days.
# 7 is sufficient given typical booking windows of 2–5 days.
LOOKBACK_DAYS = 7

# Abort the run if the source tab's row count drops by more than this fraction
# compared to the most recent successful run (catches broken IMPORTRANGE).
ROW_DROP_ABORT_THRESHOLD = 0.30

# DRY_RUN=true skips all Close writes; reads/reports still happen.
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Timezone used for all timestamps written to reports and the Python log
# output. Defaults to Pacific so it matches the GitHub Actions UI for the
# Eugene-based author. Override via REPORT_TIMEZONE env var if needed.
TIMEZONE = os.environ.get("REPORT_TIMEZONE", "America/Los_Angeles")

# Cell values that indicate the sheet didn't load properly.
INTEGRITY_FAIL_VALUES = {"#REF!", "#ERROR!", "#N/A", "Loading...", "#NAME?", "#VALUE!"}

# -----------------------------------------------------------------------------
# Simple source → funnel mappings (channels without per-campaign rules)
# -----------------------------------------------------------------------------
# For channels where every contact with this utm_source should get the same
# funnel name regardless of campaign. Sheet-driven channels (YouTube, later
# Webinar/Meta/VSL/Paid) override these if the same key appears in both.
# Keys MUST be lowercase. Values are the exact funnel name written to Close.
SIMPLE_SOURCE_MAPPINGS = {
    "instagram":  "Instagram",
    "x":          "X",
    "twitter":    "X",
    "x-twitter":  "X",
    "linkedin":   "LinkedIn",
    "li":         "LinkedIn",
}

# Some integrations stuff the entire UTM query string into the utm_source
# field, e.g. "linkedin&utm_medium=Kara&utm_campaign=vp_setter&...".
# We classify these by the part before the first '&' or '?'. Applies to every
# channel where we've observed the pattern.
MALFORMED_SOURCE_PREFIXES = {
    "youtube":   "YouTube",
    "instagram": "Instagram",
    "linkedin":  "LinkedIn",
    "twitter":   "X",
    "x-twitter": "X",
}
