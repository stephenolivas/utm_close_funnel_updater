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
# Tabs to read for sheet-driven funnels. Each tab needs a 'Funnel Name'
# column plus utm_source/medium/campaign/content. Order doesn't matter.
# NOTE: Webinar/Meta/VSL are intentionally NOT read — those tabs contain
# rows describing TRAFFIC SOURCES for those funnels (e.g. utm_source=
# 'instagram' mapped to 'VSL'), which would override our channel-level
# mappings and cause spurious conflicts. They're handled by
# SIMPLE_SOURCE_MAPPINGS below until we work out the disambiguation.
SOURCE_TABS   = ["YouTube"]
MISSING_TAB   = "_Missing Funnels"
CONFLICTS_TAB = "_Conflicts"
RUN_LOG_TAB   = "_Run Log"

# The column header in the source tabs that carries the funnel name to write
# to Close. The fallback exists so we don't break if the YouTube tab still
# uses the old longer header name — remove the fallback once all tabs are
# renamed.
FUNNEL_NAME_HEADER          = "Funnel Name"
FUNNEL_NAME_HEADER_FALLBACK = "Funnel Name for Close"

# Required column headers in every source tab. Script aborts on any tab
# missing these. Both FUNNEL_NAME_HEADER variants are acceptable; the read
# code picks whichever is present.
REQUIRED_HEADERS = ["utm_source", "utm_medium", "utm_campaign", "utm_content"]

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
    "instagram":        "Instagram",
    "x":                "X",
    "twitter":          "X",
    "x-twitter":        "X",
    "linkedin":         "Linkedin",
    "li":               "Linkedin",
    "internal-webinar": "Internal Webinar",
    "meta":             "Meta Ads",
    "newsletter":       "VSL",
    "tiktok":           "TikTok",
    "ak-ig":            "Anthony IG",
    "anthony-x":        "Anthony X",
    "anthony-li":       "Anthony LinkedIn",
    "ltf":              "LTF - In-House",
}

# Some integrations stuff the entire UTM query string into the utm_source
# field, e.g. "linkedin&utm_medium=Kara&utm_campaign=vp_setter&...".
# We classify these by the part before the first '&' or '?'. Applies to every
# channel where we've observed the pattern.
MALFORMED_SOURCE_PREFIXES = {
    "youtube":          "YouTube",
    "instagram":        "Instagram",
    "linkedin":         "Linkedin",
    "twitter":          "X",
    "x-twitter":        "X",
    "internal-webinar": "Internal Webinar",
    "meta":             "Meta Ads",
    "newsletter":       "VSL",
    "tiktok":           "TikTok",
    "ak-ig":            "Anthony IG",
    "anthony-x":        "Anthony X",
    "anthony-li":       "Anthony LinkedIn",
    "ltf":              "LTF - In-House",
}

# -----------------------------------------------------------------------------
# Source + medium overrides
# -----------------------------------------------------------------------------
# Some channels split into "owned" vs "agency-managed" based on utm_medium.
# If a contact's utm_source matches an outer key AND utm_medium matches an
# inner key, the inner value wins over SIMPLE_SOURCE_MAPPINGS. Falls back to
# the regular source mapping if no medium override matches.
#
# Both keys are case-insensitive (normalized to lowercase + stripped).
SOURCE_MEDIUM_OVERRIDES = {
    "instagram": {
        "organic-social": "Anthony IG",
    },
}

# -----------------------------------------------------------------------------
# Overwrite policy
# -----------------------------------------------------------------------------
# Normally the script is fill-only: if the funnel field is already set we
# don't overwrite, we log a conflict. The two sets below introduce a narrow
# exception:
#
#   - If the lead's CURRENT funnel is in OVERRIDABLE_CURRENT_FUNNELS, we
#     are willing to overwrite it...
#   - ...UNLESS the funnel we're about to WRITE is in
#     NON_OVERRIDING_TARGET_FUNNELS, in which case it's treated as a
#     conflict (these are weak/ambient attribution signals that shouldn't
#     stomp on stronger ones).
#
# Exact string match, case-sensitive. Add / remove freely.
OVERRIDABLE_CURRENT_FUNNELS = {
    "Reactivation Email",
}

NON_OVERRIDING_TARGET_FUNNELS = {
    "Low Ticket Funnel",
    "LTF - Quiz Funnel",
}

# -----------------------------------------------------------------------------
# Direct funnel rewrites
# -----------------------------------------------------------------------------
# {current_funnel: target_funnel}
# Any lead whose current funnel matches a KEY gets its funnel updated to the
# VALUE, regardless of utm_source. Runs as its own pass BEFORE the utm_source
# scan, so a rewritten lead already has its new value by the time contact
# processing starts. Use for consolidation / renaming / retiring old funnels.
#
# Exact string match, case-sensitive.
FUNNEL_REWRITES = {
    "Low Ticket Funnel": "LTF - In-House",
}
