import streamlit as st
import pandas as pd
import requests
import io
import time
import html
import json
import copy
import gspread
from google.oauth2.service_account import Credentials

# -------- SETTINGS --------
FORM_COMPLIANCE = {
    "AWEstruck Arival/Departure":     None,
    "Daily Huddle":                   {"quota": 3, "interval": "daily"},
    "Valet Driver Assessment":        None,
    "Key Scrub":                      {"quota": 3, "interval": "daily"},
    "Lot Audit":                      {"quota": 3, "interval": "daily"},
    "Overnight Valet":                {"quota": 1, "interval": "daily"},
    "Parking/Work Area Hazard":       {"quota": 1, "interval": "weekly"},
    "Site Audit":                     {"quota": 1, "interval": "monthly"}
}
# Location-specific overrides for quota and/or interval, for locations
# whose requirements differ from the standard FORM_COMPLIANCE default
# above. Only list the locations that are DIFFERENT from the default —
# any location not listed here just uses the form's default quota.
#
# Structure: { form_name: { location_name: {"quota": N, "interval": "daily"/"weekly"/"monthly"} } }
# You can override just "quota", just "interval", or both — whatever key
# is omitted falls back to the form's default from FORM_COMPLIANCE.
#
# Example:
# LOCATION_COMPLIANCE_OVERRIDES = {
#     "Daily Huddle": {
#         "Downtown Garage": {"quota": 2},       # only needs 2/day instead of 3
#         "Airport Lot":     {"quota": 5},       # busier site, needs 5/day
#     },
# }
LOCATION_COMPLIANCE_OVERRIDES = {
    # "Daily Huddle": {
    #     "Downtown Garage": {"quota": 2},
    # },
}


def get_requirement_for(form_name, location, overrides=None):
    """Return the effective {"quota": ..., "interval": ...} requirement
    for this form/location, applying any location-specific override on
    top of the form's default from FORM_COMPLIANCE.

    `overrides` defaults to the hardcoded LOCATION_COMPLIANCE_OVERRIDES
    constant, but the UI passes in the live, in-app-editable version from
    st.session_state instead so users can adjust overrides without
    touching code.
    """
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    default = FORM_COMPLIANCE.get(form_name)
    if default is None:
        return None
    override = overrides.get(form_name, {}).get(location, {})
    return {
        "quota": override.get("quota", default["quota"]),
        "interval": override.get("interval", default["interval"]),
    }


# Which locations aren't open all 7 days a week. Any location NOT listed
# here is assumed open every day. Days are Python's weekday() numbering:
# 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun.
#
# This only affects DAILY-interval forms (Daily Huddle, Key Scrub, Lot
# Audit, Overnight Valet): the daily target is based on how many days the
# location is actually open in the date range, not every calendar day.
# Weekly/monthly quotas are unaffected — a location still needs to hit
# those regardless of which specific days it's open.
#
# Example:
# LOCATION_OPERATING_DAYS = {
#     "Downtown Garage": {0, 1, 2, 3, 4},        # 5-day: Mon-Fri, closed weekends
#     "Airport Lot":      {0, 1, 2, 3, 4, 5},    # 6-day: Mon-Sat, closed Sunday
# }
LOCATION_OPERATING_DAYS = {
    # "Downtown Garage": {0, 1, 2, 3, 4},
}

WEEKDAY_COLUMNS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPERATING_DAYS_HEADER = ["Location"] + WEEKDAY_COLUMNS


def get_operating_days_for(location, operating_days=None):
    """Return the set of weekday ints (0=Mon..6=Sun) this location is
    open on. Defaults to every day if the location isn't listed."""
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS
    return operating_days.get(location, set(range(7)))


def operating_days_to_df(operating_days):
    """Flatten the operating-days dict into a table for st.data_editor,
    one row per location with a checkbox column per weekday."""
    rows = []
    for loc, days in operating_days.items():
        row = {"Location": loc}
        for i, col in enumerate(WEEKDAY_COLUMNS):
            row[col] = i in days
        rows.append(row)
    return pd.DataFrame(rows, columns=OPERATING_DAYS_HEADER)


def _truthy(val):
    """Handle both real booleans (from the in-app editor) and the
    'TRUE'/'FALSE' strings gspread returns after a Google Sheets round trip."""
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() in ("TRUE", "1", "YES")


def df_to_operating_days(df):
    """Rebuild the operating-days dict from the edited table. A location
    with every day checked is dropped (that's just the default), and a
    location with every day UNCHECKED is dropped too (an empty row from
    the editor's 'add row' affordance, not a real 0-day location)."""
    operating_days = {}
    for _, row in df.iterrows():
        loc = row.get("Location")
        if not loc:
            continue
        days = {i for i, col in enumerate(WEEKDAY_COLUMNS) if _truthy(row.get(col))}
        if days and len(days) < 7:
            operating_days[loc] = days
    return operating_days


def overrides_to_df(overrides):
    """Flatten the nested overrides dict into a table for st.data_editor."""
    rows = []
    for form_name, locs in overrides.items():
        for loc, vals in locs.items():
            default = FORM_COMPLIANCE.get(form_name) or {}
            rows.append({
                "Form": form_name,
                "Location": loc,
                "Quota": vals.get("quota", default.get("quota")),
                "Interval": vals.get("interval", default.get("interval")),
            })
    if not rows:
        rows = [{"Form": None, "Location": None, "Quota": None, "Interval": None}][:0]
    return pd.DataFrame(rows, columns=["Form", "Location", "Quota", "Interval"])


def df_to_overrides(df):
    """Rebuild the nested overrides dict from the edited table. Rows with
    a missing Form, Location, or Quota are skipped (e.g. a blank row left
    over from the data editor's "add row" affordance)."""
    overrides = {}
    for _, row in df.iterrows():
        form_name = row.get("Form")
        loc = row.get("Location")
        quota = row.get("Quota")
        interval = row.get("Interval")
        if not form_name or not loc or pd.isna(quota):
            continue
        overrides.setdefault(form_name, {})[loc] = {
            "quota": int(quota),
            "interval": interval if interval in ("daily", "weekly", "monthly") else FORM_COMPLIANCE[form_name]["interval"],
        }
    return overrides


# ---- Google Sheets persistence for the overrides table ----
# Requires a Google Cloud service account with the Sheets API enabled,
# whose credentials are stored in Streamlit secrets under
# [gcp_service_account], and the target Google Sheet shared with that
# service account's email (found in the credentials as "client_email").
# ---- Google Sheets persistence for overrides + operating days ----
# Requires a Google Cloud service account with the Sheets API enabled,
# whose credentials are stored in Streamlit secrets under
# [gcp_service_account], and the target Google Sheet shared with that
# service account's email (found in the credentials as "client_email").
#
# Two tabs are used in the same spreadsheet:
#   - "Sheet1" (the default first tab)  -> quota overrides
#   - "Operating Days" (created automatically if missing) -> which days
#     each location is open
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
OVERRIDES_HEADER = ["Form", "Location", "Quota", "Interval"]


def get_gsheet_client():
    """Build an authenticated gspread client from the service account
    credentials stored in Streamlit secrets."""
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SHEETS_SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(sheet_url_or_key):
    """Open the target spreadsheet by URL or by key."""
    gc = get_gsheet_client()
    if sheet_url_or_key.startswith("http"):
        return gc.open_by_url(sheet_url_or_key)
    return gc.open_by_key(sheet_url_or_key)


def _get_or_create_worksheet(sh, title, rows=200, cols=10):
    """Return the named worksheet, creating it (blank) if it doesn't exist yet."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def load_overrides_from_sheet(sheet_url_or_key):
    """Read the overrides table from the sheet and return it as the
    nested overrides dict. Returns an empty dict if the sheet has no
    data rows yet."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = sh.sheet1
    records = ws.get_all_records()  # uses row 1 as the header
    if not records:
        return {}
    df = pd.DataFrame(records)
    for col in OVERRIDES_HEADER:
        if col not in df.columns:
            df[col] = None
    return df_to_overrides(df[OVERRIDES_HEADER])


def save_overrides_to_sheet(overrides, sheet_url_or_key):
    """Overwrite the sheet's contents with the current overrides table."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = sh.sheet1
    df = overrides_to_df(overrides)
    ws.clear()
    values = [OVERRIDES_HEADER] + df[OVERRIDES_HEADER].astype(object).where(pd.notna(df), "").values.tolist()
    ws.update(values)


def load_operating_days_from_sheet(sheet_url_or_key):
    """Read the 'Operating Days' tab and return it as the operating-days
    dict. Returns an empty dict (= everyone open 7 days) if the tab is
    missing or has no data rows yet."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, "Operating Days")
    records = ws.get_all_records()
    if not records:
        return {}
    df = pd.DataFrame(records)
    for col in OPERATING_DAYS_HEADER:
        if col not in df.columns:
            df[col] = False
    return df_to_operating_days(df[OPERATING_DAYS_HEADER])


def save_operating_days_to_sheet(operating_days, sheet_url_or_key):
    """Overwrite the 'Operating Days' tab with the current table."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, "Operating Days")
    df = operating_days_to_df(operating_days)
    ws.clear()
    values = [OPERATING_DAYS_HEADER] + df[OPERATING_DAYS_HEADER].values.tolist()
    ws.update(values)


FORM_ID_TO_NAME = {
    "250265807744158": "AWEstruck Arival/Departure",
    "242047162465151": "Daily Huddle",
    "230053977061151": "Valet Driver Assessment",
    "242746988998184": "Key Scrub",
    "223424048202040": "Lot Audit",
    "230404704539149": "Overnight Valet",
    "233176683727163": "Parking/Work Area Hazard",
    "222195371011142": "Site Audit"
}
FORM_NAME_TO_ID = {v: k for k, v in FORM_ID_TO_NAME.items()}

# Confirmed exact internal field names for the location field(s) on each
# form. Several forms have the location duplicated across two fields
# (e.g. a "pick from list" path vs. a "manual entry" path in the form's
# conditional logic) — only one of the two is actually filled in on any
# given submission, so we check both and use whichever is non-empty.
FORM_ID_TO_LOCATION_FIELDS = {
    "250265807744158": ["location125", "location"],   # AWEstruck Arival/Departure
    "242047162465151": ["location", "location60"],    # Daily Huddle
    "230053977061151": ["location172", "location"],   # Valet Driver Assessment
    "242746988998184": ["location73", "location1"],   # Key Scrub
    "223424048202040": ["location"],                  # Lot Audit
    "230404704539149": ["location", "location39"],    # Overnight Valet
    "233176683727163": ["location"],                  # Parking/Work Area Hazard
    "222195371011142": ["location74", "location"],    # Site Audit
}

# Kept for the diagnostic "Debug Location Field Setup" button below, which
# scans for anything mentioning "location" for troubleshooting purposes.
# It is no longer used to decide which field's answer to actually pull.
LOCATION_MATCH_WORD = "location"

# The single source of truth for valid location names, from Carly's master
# location list (exported from the sheet she maintains). This replaces
# pulling location options from each individual form's dropdown definition —
# we found different forms have DIFFERENT text for the same physical site
# (e.g. Lot Audit's dropdown said "TX - Courtyard Residence Inn Austin DT"
# while Daily Huddle's said "TX -  CY & RI Austin Downtown"), so per-form
# scraping was never going to give one consistent list.
#
# To update: replace this list from a fresh export of the master sheet.
MASTER_LOCATIONS = [
    "FL - Hotel Flor Tampa", "CA - Beverly Hills Marriott", "CA - Cameo Beverly Hills",
    "CA - Courtyard Santa Monica", "CA - Hampton Inn Santa Monica", "CA - Hilton Costa Mesa",
    "CA - Hilton Garden Inn Dana Point", "CO - Grand Hyatt Denver", "Corporate",
    "DC - Melrose Hotel", "DC - St. Regis Washington DC", "DC - The LINE DC",
    "DC - Westin Downtown", "FL - 4th Street Lot", "FL - 830 Brickell",
    "FL - 888 Brickell Dolce Gabanna", "FL - AC Hotel Clearwater Beach", "FL - AC Hotel St. Pete",
    "FL - AC Orlando Downtown", "FL - Andaz Miami Beach", "FL - Bellini",
    "FL - Courtyard Clearwater Beach", "FL - Courtyard Downtown St. Pete",
    "FL - Courtyard Downtown Tampa", "FL - Fifth Third Bank Center", "FL - HCC Ybor City Shuttle",
    "FL - Hilton St. Pete Bayfront", "FL - Hotel Haya Tampa", "FL - Hyatt House/Hyatt Place Tampa",
    "FL - Jacksonville DoubleTree", "FL - Juno & The Peacock", "FL - JW Marriott Clearwater",
    "FL - Kettler/Edge Lot", "FL - Miami Center 2", "FL - Mint House St. Pete",
    "FL - OLIVIA St. Pete", "FL - Renaissance Tampa", "FL - Ritz Carlton Sarasota",
    "FL - Rivergate Tower Tampa", "FL - Saddlebrook Resort", "FL - Sheraton Sand Key",
    "FL - SkyBeach Resort St. Pete", "FL - St. Regis Long Boat Key",
    "FL - The Vinoy Resort & Golf Club", "FL - Tru St. Pete Downtown", "FL - Westin Sarasota",
    "FL - Westin Tampa Waterside", "FL - Wynwood Plaza", "GA - Aloft Savannah",
    "GA - Fairfield Inn Savannah", "GA - Hampton Inn Savannah", "GA - HGI/HWS Atlanta Midtown",
    "GA - Holiday Inn Express Savannah", "GA - Ritz Carlton Atlanta", "GA - River Street Inn",
    "GA - SpringHill Atlanta", "GA - Studio Homes", "GA - Tempo Savannah",
    "GA - The Desoto Savannah", "GA - The Douglas Savannah", "GA - The Georgian Terrace",
    "GA - The Tess Atlanta", "IL - Waldorf Astoria Chicago", "KY - Cincinnati Marriott at RiverCenter",
    "LA - Ballys Hotel Casino", "LA - Cambria Hotel NOLA", "LA - Homewood NOLA",
    "LA - Hotel Tonnelle NOLA", "LA - Le Pavillon New Orleans", "LA - Mercantile Hotel",
    "LA - Ochsner Medical", "LA - Pontchartrain Hotel", "LA - Queen Baton Rouge",
    "LA - Virgin NOLA", "NC - Tempo/HWS Raleigh", "OH - Hotel Celare", "OH - The Summit Hotel",
    "Other", "SC - The Cooper Charleston", "TX -  CY RI Austin Downtown", "TX - Artisan Circle",
    "TX - DoubleTree Houston", "TX - Hotel Daphne Houston", "TX - Hotel Saint Augustine Houston",
    "TX - Intercontinental Houston Medical",
]

# Known non-canonical spellings found in actual submission/dropdown data
# that should be folded into the master list's name instead of counted as
# a separate location. Add to this as new mismatches turn up — same
# pattern as FORM_ID_TO_LOCATION_FIELDS: explicit, human-verified entries,
# no fuzzy-matching heuristics that could silently merge the wrong sites.
#
# This does NOT edit anything in JotForm or rewrite past submissions —
# it just normalizes the text at report time, so reports are accurate
# regardless of which spelling a given submission happened to use.
LOCATION_ALIASES = {
    "TX -  CY & RI Austin Downtown": "TX -  CY RI Austin Downtown",
    "TX - Courtyard Residence Inn Austin DT": "TX -  CY RI Austin Downtown",
}


def normalize_location(raw_value):
    """Map a raw submitted location string to its canonical master-list
    name, if a known alias applies. Falls back to the trimmed original
    value when there's no known alias (so unmapped/new locations still
    show up rather than silently vanishing).

    Also decodes HTML entities (e.g. "&amp;" -> "&") — JotForm's raw API
    answer text can come back HTML-encoded even when its own UI displays
    the decoded version, which silently breaks exact-string matching
    against the master list (found via "FL - The Vinoy Resort &amp; Golf
    Club" failing to match "FL - The Vinoy Resort & Golf Club")."""
    if raw_value is None:
        return None
    cleaned = html.unescape(raw_value.strip())
    return LOCATION_ALIASES.get(cleaned, cleaned)


def get_form_questions(form_id, api_key):
    """Fetch a form's field/question definitions (not submissions).

    Used to read the actual dropdown options configured on the location
    field, so we only ever show real, valid site names in the location
    filter — never write-in/free-text values someone typed into a
    submission.
    """
    url = f"https://api.jotform.com/form/{form_id}/questions"
    params = {'apiKey': api_key}
    max_retries = 3
    r = None
    for attempt in range(1, max_retries + 1):
        r = requests.get(url, params=params, timeout=60)
        if r.ok:
            break
        if r.status_code in (502, 503, 504) and attempt < max_retries:
            time.sleep(attempt * 2)
            continue
        break

    if not r.ok:
        form_label = FORM_ID_TO_NAME.get(form_id, form_id)
        st.error(
            f"JotForm API error while fetching field setup for '{form_label}' "
            f"(status {r.status_code}) after {max_retries} attempt(s): "
            f"{r.text[:500]}"
        )
        st.stop()

    return r.json()['content']


def get_location_options_for_form(form_id, api_key):
    """Return the list of valid location names as defined on this form's
    real location dropdown field(s) — using the confirmed field name(s)
    from FORM_ID_TO_LOCATION_FIELDS, not from submitted answers.

    Where a form has two location fields, options are collected from
    whichever of them actually has a dropdown list defined.
    """
    field_names = FORM_ID_TO_LOCATION_FIELDS.get(form_id, [])
    if not field_names:
        return []

    questions = get_form_questions(form_id, api_key)
    options = set()
    for k, q in questions.items():
        if q.get('name') in field_names:
            options_str = q.get('options', '') or ''
            for o in options_str.split('|'):
                o = o.strip()
                if o:
                    options.add(o)
    return sorted(options)


def get_submissions(form_id, api_key, start_date=None, end_date=None):
    all_submissions = []
    batch_size = 1000
    offset = 0
    max_retries = 3
    while True:
        url = f"https://api.jotform.com/form/{form_id}/submissions"
        params = {
            'apiKey': api_key,
            'limit': batch_size,
            'offset': offset
        }
        if start_date or end_date:
            filters = {}
            if start_date:
                filters["created_at:gt"] = f"{start_date}T00:00:00"
            if end_date:
                filters["created_at:lt"] = f"{end_date}T23:59:59"
            params["filter"] = str(filters).replace("'", '"')

        r = None
        for attempt in range(1, max_retries + 1):
            r = requests.get(url, params=params, timeout=60)
            if r.ok:
                break
            # 502/503/504 are usually transient server-side hiccups on
            # JotForm's end (server overloaded or slow) — worth a retry
            # with a short pause. Other errors (401, 403, 400) won't be
            # fixed by retrying, so fail immediately on those.
            if r.status_code in (502, 503, 504) and attempt < max_retries:
                time.sleep(attempt * 2)  # 2s, then 4s
                continue
            break

        if not r.ok:
            form_label = FORM_ID_TO_NAME.get(form_id, form_id)
            st.error(
                f"JotForm API error while fetching '{form_label}' "
                f"(status {r.status_code}) after {max_retries} attempt(s): "
                f"{r.text[:500]}"
            )
            st.stop()

        submissions = r.json()['content']
        all_submissions.extend(submissions)
        if len(submissions) < batch_size:
            break
        offset += batch_size
    return all_submissions


def extract_row(sub, form_id):
    """Pull out the fields we care about from one submission.

    Uses the confirmed field name(s) for this form's location field(s)
    (FORM_ID_TO_LOCATION_FIELDS). Where a form has two possible fields
    (a duplicated location field across conditional branches), we check
    each in order and use whichever one is actually filled in.
    """
    answers = sub['answers']
    row = {
        'form_id': form_id,
        'form_name': FORM_ID_TO_NAME.get(form_id, form_id),
        'submission_id': sub['id'],
        'submitted_at': sub['created_at'],
        'location': None
    }

    by_name = {v.get('name'): v.get('answer', '') for v in answers.values()}
    for field_name in FORM_ID_TO_LOCATION_FIELDS.get(form_id, []):
        val = by_name.get(field_name)
        if val not in (None, ''):
            val = val if isinstance(val, str) else str(val)
            row['location'] = normalize_location(val)
            break

    return row


def get_all_data(form_names, location_list, start_date, end_date, api_key):
    all_rows = []
    form_ids = [FORM_NAME_TO_ID[name] for name in form_names]
    for form_id in form_ids:
        submissions = get_submissions(form_id, api_key, start_date, end_date)
        for sub in submissions:
            row = extract_row(sub, form_id)
            if not location_list or row['location'] in location_list:
                all_rows.append(row)
    df_raw = pd.DataFrame(all_rows)
    return df_raw


def _period_count(interval, start_date, end_date, operating_days=None):
    """Number of compliance periods (days/weeks/months) in the date range
    for the given interval.

    For "daily" intervals, `operating_days` (a set of weekday ints,
    0=Mon..6=Sun) restricts the count to only the days the location is
    actually open — a 5-day location gets ~5/7 as many required days as
    a 7-day location over the same date range. Weekly/monthly intervals
    ignore `operating_days`: those quotas still apply regardless of which
    specific days the location is open.
    """
    if interval == "daily":
        periods = pd.date_range(start_date, end_date, freq='D')
        if operating_days is not None and len(operating_days) < 7:
            periods = [d for d in periods if d.weekday() in operating_days]
    elif interval == "weekly":
        periods = pd.date_range(start_date, end_date, freq='W-MON')
        if pd.to_datetime(start_date).weekday() != 0:
            periods = periods.insert(0, pd.to_datetime(start_date))
    elif interval == "monthly":
        periods = pd.period_range(start_date, end_date, freq='M')
    else:
        periods = []
    return len(periods)


def compute_period_targets(df_raw, start_date, end_date, selected_forms, selected_locations,
                            overrides=None, operating_days=None):
    """Build the compliance summary, one row per (form, location).

    Quota/interval requirements are resolved per (form, location) via
    get_requirement_for(), so locations listed in `overrides` (typically
    st.session_state['location_overrides'], edited live in the UI) get
    their own target instead of the form's default quota. Because
    interval can also differ per location, the period count is computed
    per row rather than once per form.

    `operating_days` (typically st.session_state['location_operating_days'])
    trims the daily-interval period count down to just the days each
    location is actually open, for locations that aren't open 7 days/week.
    """
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS
    summary_rows = []
    compliance_forms = [f for f in selected_forms if FORM_COMPLIANCE.get(f)]
    all_locations = selected_locations if selected_locations else sorted(df_raw['location'].dropna().unique())

    # Cache period counts by (interval, operating-days-set) so we don't
    # recompute the same date_range/period_range repeatedly. Weekly/monthly
    # don't depend on operating days, so they share one cache entry.
    period_count_cache = {}

    for form_name in compliance_forms:
        for loc in all_locations:
            req = get_requirement_for(form_name, loc, overrides)
            if req is None:
                continue
            quota = req['quota']
            interval = req['interval']
            loc_days = get_operating_days_for(loc, operating_days)

            cache_key = (interval, frozenset(loc_days)) if interval == "daily" else (interval, None)
            if cache_key not in period_count_cache:
                period_count_cache[cache_key] = _period_count(
                    interval, start_date, end_date,
                    loc_days if interval == "daily" else None
                )
            period_count = period_count_cache[cache_key]

            df_form_loc = df_raw[(df_raw['form_name'] == form_name) & (df_raw['location'] == loc)]
            actual = len(df_form_loc)
            target = period_count * quota
            percent_complete = 100 * actual / target if target else 0
            status = "Met" if actual >= target else f"Missing {target - actual}"
            is_override = loc in overrides.get(form_name, {})
            is_partial_week = interval == "daily" and len(loc_days) < 7
            summary_rows.append({
                "form_name": form_name,
                "location": loc,
                "interval": interval.capitalize(),
                "periods_in_range": period_count,
                "quota_per_period": quota,
                "target_total": target,
                "actual_total": actual,
                "percent_complete": round(percent_complete, 1),
                "status": status,
                "custom_quota": "Yes" if is_override else "No",
                "open_days_per_week": len(loc_days) if is_partial_week else 7
            })
    summary_df = pd.DataFrame(summary_rows)
    return summary_df


def leaderboard_with_badges(compliance_summary):
    if compliance_summary.empty:
        return pd.DataFrame(columns=['location', 'overall_percent', 'Badge', 'Rank'])
    leader = (compliance_summary.groupby('location')
              .agg(
                  forms_tracked=('form_name', 'count'),
                  total_target=('target_total', 'sum'),
                  total_actual=('actual_total', 'sum'),
                  overall_percent=('percent_complete', 'mean')
              ).reset_index()
              )
    leader = leader.sort_values('overall_percent', ascending=False)

    def badge_row(row):
        if row['overall_percent'] >= 100:
            return "🏅 Gold Star"
        elif row['overall_percent'] >= 90:
            return "🥈 Silver Star"
        elif row['overall_percent'] >= 75:
            return "🥉 Bronze Star"
        else:
            return "🚩 Needs Improvement"

    leader['Badge'] = leader.apply(badge_row, axis=1)
    leader['Rank'] = leader['overall_percent'].rank(method='min', ascending=False).astype(int)
    return leader


# --------- STREAMLIT UI ---------

st.title("JotForm Compliance Dashboard")

# ---- Blend "admin" sections into the background ----
# This targets EVERY st.expander on the page by its Streamlit testid, so it
# covers the Location list options, Location Quota Overrides, Location
# Operating Days, AND Debug Tools sections all at once — they all shrink
# down and go white-on-white so they don't draw attention during normal
# use, but stay fully clickable if you know where they are. NOTE: the
# color below (#ffffff) matches Streamlit's default light-theme
# background — if your app uses a dark theme or a custom background
# color, change the hex codes below to match, or these sections will be
# clearly visible instead of blended in.
st.markdown(
    """
    <style>
    [data-testid="stExpander"] {
        font-size: 0.65rem;
    }
    [data-testid="stExpander"] summary,
    [data-testid="stExpander"] summary * {
        color: #ffffff !important;
    }
    [data-testid="stExpander"] * {
        color: #ffffff !important;
        font-size: 0.65rem !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# Load API key from secrets (set after deploying to Streamlit Cloud)
api_key = st.secrets["API_KEY"] if "API_KEY" in st.secrets else st.text_input("Enter your JotForm API Key:", type="password")

filename = st.text_input("Excel filename (without .xlsx)", "JotForm_Compliance_Report") + ".xlsx"

form_names = list(FORM_NAME_TO_ID.keys())
selected_forms = st.multiselect("Select form(s):", options=form_names, default=form_names)

start_date = st.date_input("Start date")
end_date = st.date_input("End date")

# Location options now come from MASTER_LOCATIONS (Carly's master site
# list) rather than being scraped from each form's own dropdown setup.
# We found different forms used different text for the same physical site
# (e.g. "TX - Courtyard Residence Inn Austin DT" vs "TX -  CY & RI Austin
# Downtown" for the same hotel), so per-form scraping could never produce
# one consistent list. extract_row() normalizes submitted answers against
# this same list via LOCATION_ALIASES, so submissions and filter options
# always line up.
if 'locations' not in st.session_state:
    st.session_state['locations'] = sorted(MASTER_LOCATIONS)

locations = st.session_state['locations']
selected_locations = st.multiselect(
    "Select location(s):",
    options=locations,
    default=locations,
    key='selected_locations_widget'
)

with st.expander("Location list options"):
    st.caption(
        "The location list above comes from the hardcoded MASTER_LOCATIONS "
        "list in the code, not from JotForm. If the master list changes, "
        "update MASTER_LOCATIONS with a fresh export."
    )
    if st.button("Reset to full master list"):
        st.session_state['locations'] = sorted(MASTER_LOCATIONS)
        st.session_state['selected_locations_widget'] = sorted(MASTER_LOCATIONS)
        st.rerun()

# ---- Location Quota Overrides editor ----
# Seed session state from the hardcoded LOCATION_COMPLIANCE_OVERRIDES the
# first time the app runs. After that, the in-app table (or whatever was
# last loaded from the Google Sheet) is the source of truth for the rest
# of the session.
if 'location_overrides' not in st.session_state:
    st.session_state['location_overrides'] = copy.deepcopy(LOCATION_COMPLIANCE_OVERRIDES)

# The sheet URL/ID defaults to a value in secrets if you set one
# (st.secrets["overrides_sheet_url"]), so the team doesn't have to
# re-paste it every session — but it can still be overridden per-session
# in the text box below.
if 'overrides_sheet_url' not in st.session_state:
    st.session_state['overrides_sheet_url'] = st.secrets.get("overrides_sheet_url", "")

compliance_form_names = [f for f in FORM_NAME_TO_ID if FORM_COMPLIANCE.get(f)]

with st.expander("⚙️ Location Quota Overrides", expanded=False):
    st.caption(
        "Set a different quota (and, if needed, a different interval) for "
        "specific locations. Any location not listed here just uses the "
        "form's standard quota. Add a row, fill in Form/Location/Quota, "
        "and it takes effect on the next 'Run Report'."
    )

    edited_df = st.data_editor(
        overrides_to_df(st.session_state['location_overrides']),
        num_rows="dynamic",
        use_container_width=True,
        key="overrides_editor",
        column_config={
            "Form": st.column_config.SelectboxColumn(
                "Form", options=compliance_form_names, required=True
            ),
            "Location": st.column_config.SelectboxColumn(
                "Location", options=locations if locations else None, required=True
            ) if locations else st.column_config.TextColumn("Location", required=True),
            "Quota": st.column_config.NumberColumn(
                "Quota", min_value=0, step=1, required=True
            ),
            "Interval": st.column_config.SelectboxColumn(
                "Interval", options=["daily", "weekly", "monthly"], required=True
            ),
        }
    )
    st.session_state['location_overrides'] = df_to_overrides(edited_df)

    st.divider()
    st.markdown("**Google Sheet sync** (recommended — persists across sessions and app restarts)")
    st.session_state['overrides_sheet_url'] = st.text_input(
        "Google Sheet URL or ID",
        value=st.session_state['overrides_sheet_url'],
        help="The sheet must be shared (Editor access) with your service "
             "account's email — found as 'client_email' in your "
             "st.secrets['gcp_service_account'] credentials."
    )

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("⬇️ Load from Google Sheet"):
            if not st.session_state['overrides_sheet_url']:
                st.error("Enter a Google Sheet URL or ID first.")
            else:
                try:
                    st.session_state['location_overrides'] = load_overrides_from_sheet(
                        st.session_state['overrides_sheet_url']
                    )
                    st.success("Loaded overrides from the Google Sheet.")
                    st.rerun()
                except gspread.exceptions.SpreadsheetNotFound:
                    st.error(
                        "Sheet not found. Check the URL/ID and make sure it's "
                        "shared with the service account's client_email."
                    )
                except gspread.exceptions.APIError as e:
                    st.error(f"Google Sheets API error: {e}")
                except KeyError:
                    st.error(
                        "No 'gcp_service_account' credentials found in "
                        "st.secrets — add your service account JSON there first."
                    )
    with col_b:
        if st.button("⬆️ Save to Google Sheet"):
            if not st.session_state['overrides_sheet_url']:
                st.error("Enter a Google Sheet URL or ID first.")
            else:
                try:
                    save_overrides_to_sheet(
                        st.session_state['location_overrides'],
                        st.session_state['overrides_sheet_url']
                    )
                    st.success("Saved overrides to the Google Sheet.")
                except gspread.exceptions.SpreadsheetNotFound:
                    st.error(
                        "Sheet not found. Check the URL/ID and make sure it's "
                        "shared with the service account's client_email."
                    )
                except gspread.exceptions.APIError as e:
                    st.error(f"Google Sheets API error: {e}")
                except KeyError:
                    st.error(
                        "No 'gcp_service_account' credentials found in "
                        "st.secrets — add your service account JSON there first."
                    )

    with st.expander("Backup as a local file instead"):
        col_c, col_d = st.columns(2)
        with col_c:
            st.download_button(
                "Save overrides to file",
                data=json.dumps(st.session_state['location_overrides'], indent=2),
                file_name="location_quota_overrides.json",
                mime="application/json"
            )
        with col_d:
            uploaded_overrides = st.file_uploader(
                "Load overrides from file", type=["json"], key="overrides_uploader"
            )
            if uploaded_overrides is not None:
                try:
                    st.session_state['location_overrides'] = json.load(uploaded_overrides)
                    st.success("Overrides loaded. Expand this section to review them.")
                except (json.JSONDecodeError, ValueError):
                    st.error("That file doesn't look like a valid overrides JSON export.")

# ---- Location Operating Days editor ----
# For locations that aren't open 7 days a week (5-day or 6-day sites),
# this trims the DAILY-quota targets down to just the days they're
# actually open, so a 5-day lot isn't held to the same daily target as a
# 7-day lot. Weekly/monthly quotas are unaffected.
if 'location_operating_days' not in st.session_state:
    st.session_state['location_operating_days'] = copy.deepcopy(LOCATION_OPERATING_DAYS)

with st.expander("📅 Location Operating Days (5/6-day locations)", expanded=False):
    st.caption(
        "Only add a location here if it's NOT open all 7 days a week. "
        "Check the days it IS open (e.g. check Mon–Fri and leave Sat/Sun "
        "unchecked for a 5-day site). Locations left off this list are "
        "assumed open every day. This only affects daily-quota forms "
        "(Daily Huddle, Key Scrub, Lot Audit, Overnight Valet) — weekly "
        "and monthly quotas are unchanged."
    )

    days_edited_df = st.data_editor(
        operating_days_to_df(st.session_state['location_operating_days']),
        num_rows="dynamic",
        use_container_width=True,
        key="operating_days_editor",
        column_config={
            "Location": st.column_config.SelectboxColumn(
                "Location", options=locations if locations else None, required=True
            ) if locations else st.column_config.TextColumn("Location", required=True),
            **{day: st.column_config.CheckboxColumn(day, default=True) for day in WEEKDAY_COLUMNS}
        }
    )
    st.session_state['location_operating_days'] = df_to_operating_days(days_edited_df)

    st.divider()
    st.markdown("**Google Sheet sync** (uses a second tab, \"Operating Days\", in the same sheet as your quota overrides)")
    col_e, col_f = st.columns(2)
    with col_e:
        if st.button("⬇️ Load from Google Sheet", key="load_days_btn"):
            if not st.session_state.get('overrides_sheet_url'):
                st.error("Enter a Google Sheet URL or ID in the Location Quota Overrides section first.")
            else:
                try:
                    st.session_state['location_operating_days'] = load_operating_days_from_sheet(
                        st.session_state['overrides_sheet_url']
                    )
                    st.success("Loaded operating days from the Google Sheet.")
                    st.rerun()
                except gspread.exceptions.SpreadsheetNotFound:
                    st.error(
                        "Sheet not found. Check the URL/ID and make sure it's "
                        "shared with the service account's client_email."
                    )
                except gspread.exceptions.APIError as e:
                    st.error(f"Google Sheets API error: {e}")
                except KeyError:
                    st.error(
                        "No 'gcp_service_account' credentials found in "
                        "st.secrets — add your service account JSON there first."
                    )
    with col_f:
        if st.button("⬆️ Save to Google Sheet", key="save_days_btn"):
            if not st.session_state.get('overrides_sheet_url'):
                st.error("Enter a Google Sheet URL or ID in the Location Quota Overrides section first.")
            else:
                try:
                    save_operating_days_to_sheet(
                        st.session_state['location_operating_days'],
                        st.session_state['overrides_sheet_url']
                    )
                    st.success("Saved operating days to the Google Sheet.")
                except gspread.exceptions.SpreadsheetNotFound:
                    st.error(
                        "Sheet not found. Check the URL/ID and make sure it's "
                        "shared with the service account's client_email."
                    )
                except gspread.exceptions.APIError as e:
                    st.error(f"Google Sheets API error: {e}")
                except KeyError:
                    st.error(
                        "No 'gcp_service_account' credentials found in "
                        "st.secrets — add your service account JSON there first."
                    )

    with st.expander("Backup as a local file instead"):
        col_g, col_h = st.columns(2)
        with col_g:
            st.download_button(
                "Save operating days to file",
                data=json.dumps(
                    {loc: sorted(days) for loc, days in st.session_state['location_operating_days'].items()},
                    indent=2
                ),
                file_name="location_operating_days.json",
                mime="application/json"
            )
        with col_h:
            uploaded_days = st.file_uploader(
                "Load operating days from file", type=["json"], key="operating_days_uploader"
            )
            if uploaded_days is not None:
                try:
                    raw = json.load(uploaded_days)
                    st.session_state['location_operating_days'] = {
                        loc: set(days) for loc, days in raw.items()
                    }
                    st.success("Operating days loaded. Expand this section to review them.")
                except (json.JSONDecodeError, ValueError, AttributeError):
                    st.error("That file doesn't look like a valid operating-days JSON export.")

if st.button("Run Report"):
    if not api_key:
        st.error("API Key is required!")
    else:
        df_raw = get_all_data(selected_forms, selected_locations, start_date, end_date, api_key)
        if df_raw.empty:
            st.warning("No submissions found for this selection.")
        else:
            compliance_summary = compute_period_targets(
                df_raw, start_date, end_date, selected_forms, selected_locations,
                overrides=st.session_state.get('location_overrides', LOCATION_COMPLIANCE_OVERRIDES),
                operating_days=st.session_state.get('location_operating_days', LOCATION_OPERATING_DAYS)
            )
            leaderboard = leaderboard_with_badges(compliance_summary)

            st.subheader("Compliance Summary")
            st.dataframe(compliance_summary)

            st.subheader("Leaderboard")
            st.dataframe(leaderboard)

            st.subheader("Raw Data")
            st.dataframe(df_raw)

            submission_count = df_raw.groupby(['form_name', 'location']).size().reset_index(name='submission_count')

            # Excel export
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                compliance_summary.to_excel(writer, index=False, sheet_name='Compliance Summary')
                leaderboard.to_excel(writer, index=False, sheet_name='Leaderboard')
                df_raw.to_excel(writer, index=False, sheet_name='Raw Data')
                submission_count.to_excel(writer, index=False, sheet_name='Submission Count')
            st.download_button(
                label="Download Excel File",
                data=output.getvalue(),
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

# ---- Spacing before debug tools, so it's well out of the way ----
for _ in range(15):
    st.write("")

# ---- Debug tools ----
with st.expander("🔧 Debug Tools (click to expand)", expanded=False):
    if st.button("Debug: Location Match Check (for current form/date selection)"):
        if not api_key:
            st.error("API Key is required!")
        else:
            st.caption(
                "This shows every DISTINCT location value extract_row() actually "
                "produces for the selected forms/date range, ignoring the location "
                "filter — so we can catch near-invisible mismatches (stray spaces, "
                "lookalike dash characters, etc.) that make a real submission fail "
                "to match your selected location(s) exactly."
            )
            for form_id in [FORM_NAME_TO_ID[n] for n in selected_forms]:
                subs = get_submissions(form_id, api_key, start_date, end_date)
                st.write(f"### {FORM_ID_TO_NAME[form_id]} — {len(subs)} submission(s)")
                if not subs:
                    st.write("⚠️ No submissions returned at all for this form/date range")
                    continue
                extracted = [extract_row(s, form_id)['location'] for s in subs]
                counts = pd.Series(extracted).value_counts(dropna=False)
                rows = []
                for loc_val, count in counts.items():
                    display_val = loc_val if loc_val is not None else "(blank/None)"
                    exact_match = (loc_val in MASTER_LOCATIONS) if loc_val else False
                    rows.append({
                        "extracted_location": display_val,
                        "repr (shows hidden chars)": repr(loc_val),
                        "count": count,
                        "exact_match_to_master_list": exact_match,
                    })
                st.dataframe(pd.DataFrame(rows))

    if st.button("Debug Field Names"):
        if not api_key:
            st.error("API Key is required!")
        else:
            for form_id in [FORM_NAME_TO_ID[n] for n in selected_forms]:
                subs = get_submissions(form_id, api_key, start_date, end_date)
                st.write(f"### {FORM_ID_TO_NAME[form_id]}")
                st.write(f"{len(subs)} submission(s) returned for this date range.")
                if subs:
                    first = subs[0]
                    st.write(
                        f"Newest submission shown below — ID {first.get('id')}, "
                        f"submitted {first.get('created_at')}"
                    )
                    st.write({
                        v['name']: {'label': v.get('text'), 'answer': v.get('answer')}
                        for v in first['answers'].values()
                    })
                    st.write(
                        f"→ What extract_row() would record as location for this "
                        f"submission: **{extract_row(first, form_id)['location']}**"
                    )
                else:
                    st.write("⚠️ No submissions returned at all for this form/date range")


    if st.button("Debug Location Field Setup"):
        if not api_key:
            st.error("API Key is required!")
        else:
            for form_id in [FORM_NAME_TO_ID[n] for n in selected_forms]:
                st.write(f"### {FORM_ID_TO_NAME[form_id]}")
                questions = get_form_questions(form_id, api_key)
                try:
                    items = sorted(questions.items(), key=lambda kv: int(kv[0]))
                except (ValueError, TypeError):
                    items = list(questions.items())
                candidates = [
                    q for (k, q) in items
                    if LOCATION_MATCH_WORD in str(q.get('name', '')).strip().lower()
                    or LOCATION_MATCH_WORD in str(q.get('text', '')).strip().lower()
                ]
                if not candidates:
                    st.write("⚠️ No fields mentioning 'location' found at all.")
                    continue
                for q in candidates:
                    st.write({
                        'name': q.get('name'),
                        'text (label)': q.get('text'),
                        'type': q.get('type'),
                        'options': q.get('options'),
                    })
