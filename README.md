# Close Funnel Sync

Reads the **UTM Master Sheet** and sets the **Funnel Name** on Close CRM leads
based on the UTMs attached to their contacts.

**v1 scope:** YouTube tab only. Other tabs (Webinar, Meta, VSL, Paid) come later
with mostly config changes.

---

## What it does, in five sentences

1. Reads the `YouTube` tab of the master sheet to learn (a) which `utm_source`
   values map to which Funnel Name, and (b) the catalog of campaigns Marketing
   has documented.
2. Searches Close for **contacts** with `utm_source = youtube` updated in the
   last 30 days, then groups them by their parent **lead**.
3. For each lead, looks at its current Funnel Name and — if empty — writes the
   target funnel (e.g. `"YouTube"`). Never overwrites an existing value.
4. Independently checks every contact's `utm_campaign` against the documented
   catalog; anything not in the sheet gets logged to `_Missing Funnels` for
   Marketing to review.
5. Appends a row to `_Run Log` with timing and counters every run.

---

## Setup

### 1. Repo secrets

Set these three in **Settings → Secrets and variables → Actions**:

| Secret | What |
|--------|------|
| `CLOSE_API_KEY` | Generate at https://app.close.com/settings/developer/api-keys |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The entire JSON contents of the service account key |
| `MASTER_SHEET_ID` | The ID from the master sheet URL: `docs.google.com/spreadsheets/d/`**`<ID>`**`/edit` |

### 2. Google service account

1. Google Cloud Console → create a project (or pick an existing one)
2. Enable the **Google Sheets API**
3. Create a service account, then create a JSON key for it
4. Paste the entire JSON into the `GOOGLE_SERVICE_ACCOUNT_JSON` secret
5. **Share the master sheet** with the service account's email (Editor access).
   The email looks like `name@project-id.iam.gserviceaccount.com`.

### 3. First run

Trigger manually with dry-run on first:

- **Actions tab → Close Funnel Sync — YouTube → Run workflow**
- Toggle `dry_run: true`
- Verify the `_Run Log`, `_Missing Funnels`, and `_Conflicts` tabs are
  populated correctly without anything being written to Close
- Then run again with `dry_run: false`

---

## Tabs the script reads/writes

| Tab | Direction | Purpose |
|-----|-----------|---------|
| `YouTube` | Read | UTM-to-funnel rules — Marketing maintains |
| `_Missing Funnels` | Read + rewrite | Accumulated unknown campaigns (drops when resolved) |
| `_Conflicts` | Append-only | Leads whose current funnel disagrees with expected |
| `_Run Log` | Append-only | One row per run with stats |

The leading underscore on the report tabs keeps them visually separate from
Marketing's tabs.

---

## Safety rails

- **Integrity check:** the source tab is scanned for `#REF!`, `#ERROR!`,
  `Loading...`, `#N/A`, etc. before anything else. Any sentinel = abort.
- **Row-count baseline:** if the source tab loses more than 30% of its rows
  vs. the most recent successful run, abort. Catches a broken IMPORTRANGE or
  revoked sharing.
- **Fill-only writes:** the script will never overwrite an existing Funnel
  Name. Disagreements go to `_Conflicts`.
- **Rate limiting:** 0.5s sleep between every Close call + automatic retry on
  429s using the `Retry-After` header.
- **Idempotent:** safe to re-run. Leads already correctly set are skipped.
- **Dry run:** `workflow_dispatch` with `dry_run: true` reads everything and
  writes reports, but skips the actual Close PUT calls.

---

## Behavior notes worth knowing

- **Funnel Name lives on the Lead**, despite the field being called "Funnel
  Name Deal (Opp)". The script writes to the Lead object.
- **UTMs live on the Contact.** A single lead can have multiple contacts. The
  rule: if **any** contact on a lead has `utm_source = youtube`, the lead's
  funnel becomes `YouTube` (if currently empty).
- **utm_medium is ignored for matching.** Variance is fine.
- **Casing is normalized** — `YouTube`, `youtube`, `Youtube` all match.
- **Last-touch / first-touch attribution** is not implemented in v1. Any
  contact wins.

---

## Reports

### `_Missing Funnels`

| campaign | count | first_seen | last_seen | sample_lead_url |
|----------|-------|------------|-----------|-----------------|

- One row per campaign value seen in Close but not present in the source tab.
- Count accumulates across runs.
- `first_seen` is preserved when the campaign reappears.
- When Marketing adds a rule for a campaign, the row **drops off** on the next
  run. The list is always "what still needs attention."

### `_Conflicts`

| timestamp | lead_id | lead_url | current_funnel_name | attempted_funnel_name | contact_id | utm_source | utm_campaign |
|-----------|---------|----------|---------------------|-----------------------|------------|------------|--------------|

- Append-only. One row per conflict event.
- Use this to spot leads whose Funnel Name was already set to something other
  than what the UTMs suggest. Review and decide manually whether to update.

### `_Run Log`

| timestamp | duration_sec | dry_run | source_rows | contacts_scanned | leads_processed | leads_updated | leads_skipped_already_set | conflicts | missing_campaigns | errors | notes |
|-----------|--------------|---------|-------------|------------------|-----------------|---------------|---------------------------|-----------|-------------------|--------|-------|

---

## Adding more tabs later

The matcher is sheet-driven. Adding Webinar / Meta / VSL / Paid is mostly:

1. Ensure each tab has standardized columns: `utm_source`, `utm_medium`,
   `utm_campaign`, `utm_content`, `Funnel Name`
2. Change `SOURCE_TAB` in `src/config.py` to a list `SOURCE_TABS`
3. In `src/main.py`, loop over tabs and union their rules into the source map
4. Loop the contact-search step over every `utm_source` value in the merged map

The reporting logic doesn't need to change.

---

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CLOSE_API_KEY="..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat service-account.json)"
export MASTER_SHEET_ID="..."
export DRY_RUN=true   # always for local testing

python3 -u -m src.main
```

`service-account.json` is gitignored. Don't commit it.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Workflow fails on first step with "Tab 'YouTube' not found" | Tab renamed; update `SOURCE_TAB` in `config.py` |
| `Integrity check failed: ... contains error sentinel '#REF!'` | An IMPORTRANGE broke; check sharing on the source sheet |
| `Row count dropped from N to M (>30% drop)` | Sheet partially loaded, or a source sheet stopped sharing |
| `403` or `gspread.exceptions.APIError` on read | Service account doesn't have access to the master sheet; share it with the SA email |
| `401` from Close | `CLOSE_API_KEY` secret is missing or revoked |
| No leads updated, but contacts_scanned > 0 | All contacts either older than 30 days or their leads already have a Funnel Name |
