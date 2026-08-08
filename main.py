import streamlit as st
import pandas as pd
import requests
import io
import time
import html
import json
import copy
import inspect
import os
import base64
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="JotForm Compliance Dashboard", layout="wide")

# ---------------------------------------------------------------------------
# Streamlit renamed `use_container_width` to `width="stretch"` in newer
# versions. Detect which one this install wants once, and splat FILL into
# any dataframe/data_editor call so the app works on either version.
# ---------------------------------------------------------------------------
try:
    _params = inspect.signature(st.dataframe).parameters
    FILL = {"width": "stretch"} if ("width" in _params and "use_container_width" not in _params) \
        else {"use_container_width": True}
except Exception:
    FILL = {"use_container_width": True}

# Same idea for st.date_input's `format` argument, which only exists on
# Streamlit 1.30+. On older builds we just leave it off rather than crash —
# the picker falls back to YYYY/MM/DD, but everything still works.
try:
    DATE_FMT = {"format": "MM/DD/YYYY"} \
        if "format" in inspect.signature(st.date_input).parameters else {}
except Exception:
    DATE_FMT = {}


# -------- SETTINGS --------
# NOTE ON THE HAZARD FORM: the Parking Area(s) & Workplace Hazard Inspection
# Report's own on-form instructions say inspections are required DAILY
# ("we at Evolution are requiring that this Audit be completed DAILY by the
# Property Leaders"). This dashboard still scores it WEEKLY, unchanged from
# before, because changing it silently would rewrite every historical
# compliance number. To switch it, change the line below to:
#     "Parking/Work Area Hazard": {"quota": 1, "interval": "daily"},
FORM_COMPLIANCE = {
    "AWEstruck Arival/Departure":     None,
    "Daily Huddle":                   {"quota": 3, "interval": "daily"},
    "Valet Driver Assessment":        None,
    "NEW Valet Driver Assessment":    None,
    "Key Scrub":                      {"quota": 3, "interval": "daily"},
    "Lot Audit":                      {"quota": 3, "interval": "daily"},
    "Overnight Valet":                {"quota": 1, "interval": "daily"},
    "Parking/Work Area Hazard":       {"quota": 1, "interval": "weekly"},
    "Site Audit":                     {"quota": 1, "interval": "monthly"},
}

# Location-specific overrides for quota and/or interval. Only list the
# locations that are DIFFERENT from the form's default — any location not
# listed here just uses the form's default quota above.
#
# In practice these now live in the "Overrides" tab of the Google Sheet and
# are loaded at session start. This constant is only the starting seed /
# fallback when no sheet is reachable.
LOCATION_COMPLIANCE_OVERRIDES = {}


def get_requirement_for(form_name, location, overrides=None):
    """Return the effective {"quota": ..., "interval": ...} requirement for
    this form/location, applying any location-specific override on top of
    the form's default from FORM_COMPLIANCE."""
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


# Which locations aren't open all 7 days a week. Any location NOT listed here
# is assumed open every day. Days use Python's weekday() numbering:
# 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun.
#
# This only affects DAILY-interval forms: the daily target is based on how
# many days the location is actually open in the range, not every calendar
# day. Weekly/monthly quotas are unaffected.
LOCATION_OPERATING_DAYS = {}

WEEKDAY_COLUMNS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPERATING_DAYS_HEADER = ["Location"] + WEEKDAY_COLUMNS


def get_operating_days_for(location, operating_days=None):
    """Return the set of weekday ints (0=Mon..6=Sun) this location is open
    on. Defaults to every day if the location isn't listed."""
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS
    return operating_days.get(location, set(range(7)))


def operating_days_to_df(operating_days):
    """Flatten the operating-days dict into a table for st.data_editor."""
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
    return str(val).strip().upper() in ("TRUE", "1", "YES", "X", "Y")


def df_to_operating_days(df):
    """Rebuild the operating-days dict from the edited table. A location with
    every day checked is dropped (that's just the default), and a location
    with every day UNCHECKED is dropped too (an empty row from the editor's
    'add row' affordance, not a real 0-day location)."""
    operating_days = {}
    for _, row in df.iterrows():
        loc = row.get("Location")
        if not loc:
            continue
        days = {i for i, col in enumerate(WEEKDAY_COLUMNS) if _truthy(row.get(col))}
        if days and len(days) < 7:
            operating_days[str(loc).strip()] = days
    return operating_days


def _parse_quota(val):
    """Turn a quota cell into an int, or None if it's genuinely blank.

    IMPORTANT: 0 is a REAL, meaningful quota ("this location doesn't have to
    do this form"), so this must distinguish 0 from blank. A plain truthiness
    check would wrongly treat 0 as missing.

    Also note Google Sheets hands back "" for an empty cell, NOT NaN — the
    old pd.isna() check let "" through and then crashed on int("")."""
    if val is None or isinstance(val, bool):
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "n/a", "-"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


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
    return pd.DataFrame(rows, columns=["Form", "Location", "Quota", "Interval"])


def df_to_overrides(df, collect_skipped=False):
    """Rebuild the nested overrides dict from the edited/loaded table.

    Rows that can't be used are collected into `skipped` with a plain-English
    reason instead of vanishing silently — the old version dropped blank-quota
    rows with no indication, so a location you thought you'd configured would
    quietly fall back to the form's default quota.

    Interval matching is case-insensitive: the Google Sheet stores "Daily"
    but the compliance math expects "daily"."""
    overrides = {}
    skipped = []
    for _, row in df.iterrows():
        form_name = row.get("Form")
        loc = row.get("Location")
        form_name = str(form_name).strip() if form_name is not None else ""
        loc = str(loc).strip() if loc is not None else ""
        if not form_name or form_name.lower() == "nan":
            continue
        if not loc or loc.lower() == "nan":
            continue

        default = FORM_COMPLIANCE.get(form_name)
        if default is None:
            skipped.append(
                f"**{form_name}** / {loc} — that form has no compliance rule "
                f"(or the name doesn't match a tracked form), so this row is ignored."
            )
            continue

        quota = _parse_quota(row.get("Quota"))
        if quota is None:
            skipped.append(
                f"**{form_name}** / {loc} — Quota cell is blank, so this location "
                f"is using the form's default of {default['quota']} per {default['interval']}. "
                f"Enter 0 if it isn't required at all."
            )
            continue

        interval = str(row.get("Interval") or "").strip().lower()
        if interval not in ("daily", "weekly", "monthly"):
            interval = default["interval"]

        overrides.setdefault(form_name, {})[loc] = {"quota": quota, "interval": interval}

    return (overrides, skipped) if collect_skipped else overrides


# ---------------------------------------------------------------------------
# Google Sheets persistence
# ---------------------------------------------------------------------------
# Requires a Google Cloud service account with the Sheets API enabled, whose
# credentials are stored in Streamlit secrets under [gcp_service_account],
# and the target sheet shared (Editor) with that service account's email
# (found in the credentials as "client_email").
#
# Three tabs are used in the same spreadsheet:
#   - "Overrides"      -> quota overrides per form/location
#   - "Operating Days" -> which days each location is open
#   - "Locations"      -> the master location list (created if missing)
# ---------------------------------------------------------------------------
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
OVERRIDES_HEADER = ["Form", "Location", "Quota", "Interval"]
OVERRIDES_TAB = "Overrides"
OPERATING_DAYS_TAB = "Operating Days"
LOCATIONS_TAB = "Locations"

# The Locations tab's header cell. Extra spellings are accepted on read so a
# rename in the sheet doesn't silently blank out the whole dashboard.
LOCATIONS_HEADER = "Location List"
LOCATIONS_HEADER_ALIASES = {"location list", "location", "locations", "location name", "site"}


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


def _get_or_create_worksheet(sh, title, rows=500, cols=10):
    """Return the named worksheet, creating it (blank) if it doesn't exist."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def _get_overrides_worksheet(sh):
    """Return the overrides tab. Looks for a tab literally named "Overrides"
    first, and only falls back to the first tab if that doesn't exist — the
    old code always used sheet1, which breaks the moment the tabs get
    reordered in the sheet."""
    try:
        return sh.worksheet(OVERRIDES_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return sh.sheet1


def _values_to_df(values, wanted_header):
    """Turn raw get_all_values() output into a DataFrame with exactly the
    columns in `wanted_header`, matching header names case-insensitively and
    ignoring any extra unlabelled columns.

    Returns (df, extra_column_note). Using get_all_values() instead of
    get_all_records() matters: get_all_records() chokes on a data column
    whose header cell is blank, which is exactly what happens when someone
    types an extra value into column E without labelling it."""
    if len(values) < 2:
        return pd.DataFrame(columns=wanted_header), None

    header = [str(h).strip() for h in values[0]]
    idx = {}
    for want in wanted_header:
        for i, h in enumerate(header):
            if h.lower() == want.lower():
                idx[want] = i
                break

    # Flag any column that actually holds data but isn't one we recognise.
    known = set(idx.values())
    extra = []
    for i in range(max((len(r) for r in values), default=0)):
        if i in known:
            continue
        if any(i < len(r) and str(r[i]).strip() for r in values[1:]):
            label = header[i] if i < len(header) and header[i] else f"column {chr(65 + i)}"
            extra.append(label)
    note = ", ".join(extra) if extra else None

    rows = []
    for raw in values[1:]:
        row = {}
        for want in wanted_header:
            i = idx.get(want)
            row[want] = raw[i] if (i is not None and i < len(raw)) else ""
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
    return pd.DataFrame(rows, columns=wanted_header), note


def load_overrides_from_sheet(sheet_url_or_key):
    """Read the Overrides tab. Returns (overrides_dict, warnings_list)."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_overrides_worksheet(sh)
    df, extra_note = _values_to_df(ws.get_all_values(), OVERRIDES_HEADER)
    warnings = []
    if extra_note:
        warnings.append(
            f"The Overrides tab has extra data in {extra_note} that this dashboard "
            f"ignores. If those numbers are days-open-per-week, they belong in the "
            f"\"{OPERATING_DAYS_TAB}\" tab instead."
        )
    if df.empty:
        return {}, warnings
    overrides, skipped = df_to_overrides(df, collect_skipped=True)
    return overrides, warnings + skipped


def save_overrides_to_sheet(overrides, sheet_url_or_key):
    """Overwrite the Overrides tab with the current overrides table."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_overrides_worksheet(sh)
    df = overrides_to_df(overrides)
    ws.clear()
    body = df[OVERRIDES_HEADER].astype(object).where(pd.notna(df), "").values.tolist()
    ws.update([OVERRIDES_HEADER] + body)


def load_operating_days_from_sheet(sheet_url_or_key):
    """Read the Operating Days tab. Returns an empty dict (= everyone open 7
    days) if the tab is missing or has no data rows yet."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, OPERATING_DAYS_TAB)
    df, _ = _values_to_df(ws.get_all_values(), OPERATING_DAYS_HEADER)
    if df.empty:
        return {}
    return df_to_operating_days(df)


def save_operating_days_to_sheet(operating_days, sheet_url_or_key):
    """Overwrite the Operating Days tab with the current table."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, OPERATING_DAYS_TAB)
    df = operating_days_to_df(operating_days)
    ws.clear()
    ws.update([OPERATING_DAYS_HEADER] + df[OPERATING_DAYS_HEADER].values.tolist())


def load_locations_from_sheet(sheet_url_or_key):
    """Read the Locations tab and return the master location list.

    Sheet order is PRESERVED rather than re-sorted, so deliberate choices
    like putting "Corporate" and "Other" at the bottom of the list survive.

    html.unescape() is applied here for the same reason it's applied in
    normalize_location(): a name copy-pasted out of JotForm can arrive
    carrying "&amp;" instead of "&", which would then never match a real
    submission."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, LOCATIONS_TAB, rows=500, cols=2)
    values = ws.get_all_values()
    if len(values) < 2:
        return []

    header = [str(h).strip() for h in values[0]]
    col_idx = 0
    for i, h in enumerate(header):
        if h.lower() in LOCATIONS_HEADER_ALIASES:
            col_idx = i
            break

    names, seen = [], set()
    for raw in values[1:]:
        if col_idx >= len(raw):
            continue
        cleaned = html.unescape(str(raw[col_idx]).strip())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            names.append(cleaned)
    return names


def save_locations_to_sheet(locations, sheet_url_or_key):
    """Overwrite the Locations tab with the current location list."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, LOCATIONS_TAB, rows=500, cols=2)
    ws.clear()
    ws.update([[LOCATIONS_HEADER]] + [[loc] for loc in locations])


# ---------------------------------------------------------------------------
# Form identity — every ID below verified against live JotForm
# ---------------------------------------------------------------------------
FORM_ID_TO_NAME = {
    "250265807744158": "AWEstruck Arival/Departure",
    "242047162465151": "Daily Huddle",
    "230053977061151": "Valet Driver Assessment",        # "EPGS Valet Driver Assessment"
    "242746988998184": "Key Scrub",                      # "Key Scrub & Key Security Audit"
    "223424048202040": "Lot Audit",                      # "Lot Audit Form"
    "230404704539149": "Overnight Valet",                # "Overnight Valet Checklist"
    "233176683727163": "Parking/Work Area Hazard",       # "Parking Area(s) & Workplace Hazard Inspection Report"
    "222195371011142": "Site Audit",
    "262024177345151": "NEW Valet Driver Assessment",    # "Clone of Test 1 - Valet Driver Assessment"
}
FORM_NAME_TO_ID = {v: k for k, v in FORM_ID_TO_NAME.items()}

# Confirmed exact internal field names for the location field(s) on each form.
# Several forms have the location duplicated across two fields (a "pick from
# list" path vs. a "manual entry" path in the form's conditional logic) — only
# one is filled in on any given submission, so we check both in order and use
# whichever is non-empty.
#
# UNVERIFIED: the NEW Valet Driver Assessment (262024177345151) is a clone and
# its field names were copied from the old form. Its live form definition shows
# only ONE Location question, not two. Run "Debug Field Names" against it with
# a real submission before trusting its location data.
FORM_ID_TO_LOCATION_FIELDS = {
    "250265807744158": ["location125", "location"],   # AWEstruck Arival/Departure
    "242047162465151": ["location", "location60"],    # Daily Huddle
    "230053977061151": ["location172", "location"],   # Valet Driver Assessment
    "242746988998184": ["location73", "location1"],   # Key Scrub
    "223424048202040": ["location"],                  # Lot Audit
    "230404704539149": ["location", "location39"],    # Overnight Valet
    "233176683727163": ["location"],                  # Parking/Work Area Hazard
    "222195371011142": ["location74", "location"],    # Site Audit
    "262024177345151": ["location172", "location"],   # NEW Valet Driver Assessment (UNVERIFIED)
}

# Used only by the diagnostic "Debug Location Field Setup" button, which scans
# for anything mentioning "location" for troubleshooting. Not used to decide
# which field's answer to actually pull.
LOCATION_MATCH_WORD = "location"

# Fallback location list, used ONLY when the Google Sheet is unreachable or
# its Locations tab is still empty. The live list is the "Locations" tab of
# the overrides sheet — edit it there, not here. This copy will drift out of
# date over time, and that's fine; it exists so the dashboard never starts
# with an empty location filter.
MASTER_LOCATIONS = [
    "CA - Beverly Hills Marriott", "CA - Cameo Beverly Hills", "CA - Courtyard Santa Monica",
    "CA - Hampton Inn Santa Monica", "CA - Hilton Costa Mesa", "CA - Hilton Garden Inn Dana Point",
    "CO - Grand Hyatt Denver", "DC - Melrose Hotel", "DC - St. Regis Washington DC",
    "DC - The LINE DC", "DC - Westin Downtown", "FL - 4th Street Lot", "FL - 830 Brickell",
    "FL - 888 Brickell Dolce Gabanna", "FL - AC Hotel Clearwater Beach", "FL - AC Hotel St. Pete",
    "FL - AC Orlando Downtown", "FL - Andaz Miami Beach", "FL - Bellini",
    "FL - Courtyard Clearwater Beach", "FL - Courtyard Downtown St. Pete",
    "FL - Courtyard Downtown Tampa", "FL - Embassy Tampa Downtown", "FL - Fifth Third Bank Center",
    "FL - HCC Ybor City Shuttle", "FL - Hilton St. Pete Bayfront", "FL - Hilton Tampa Downtown",
    "FL - Hotel Flor Tampa", "FL - Hotel Haya Tampa", "FL - Hyatt House/Hyatt Place Tampa",
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
    "GA - The Tess Atlanta", "IL - Waldorf Astoria Chicago",
    "KY - Cincinnati Marriott at RiverCenter", "LA - Ballys Hotel Casino",
    "LA - Cambria Hotel NOLA", "LA - Homewood NOLA", "LA - Hotel Tonnelle NOLA",
    "LA - Le Pavillon New Orleans", "LA - Mercantile Hotel", "LA - Ochsner Medical",
    "LA - Pontchartrain Hotel", "LA - Queen Baton Rouge", "LA - Virgin NOLA",
    "NC - Tempo/HWS Raleigh", "OH - Hotel Celare", "OH - The Summit Hotel",
    "SC - The Cooper Charleston", "TX - Artisan Circle", "TX - CY RI Austin Downtown",
    "TX - DoubleTree Houston", "TX - Hotel Daphne Houston",
    "TX - Hotel Saint Augustine Houston", "TX - Intercontinental Houston Medical",
    "Corporate", "Other",
]

# Known non-canonical spellings found in real submission data that should be
# folded into the master list's name instead of counted as a separate
# location. Explicit, human-verified entries only — no fuzzy matching that
# could silently merge two genuinely different sites.
#
# This does NOT edit anything in JotForm or rewrite past submissions. It just
# normalizes the text at report time.
#
# The Austin entries matter: the master list in the Google Sheet spells it
# "TX - CY RI Austin Downtown" with ONE space after the dash. Earlier versions
# of this file used TWO spaces. Both spellings are mapped to the sheet's
# version so old and new data land in the same bucket.
LOCATION_ALIASES = {
    "TX -  CY RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX -  CY & RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX - CY & RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX - Courtyard Residence Inn Austin DT": "TX - CY RI Austin Downtown",
}


def normalize_location(raw_value):
    """Map a raw submitted location string to its canonical master-list name,
    if a known alias applies. Falls back to the trimmed original value when
    there's no known alias, so unmapped/new locations still show up rather
    than silently vanishing.

    Also decodes HTML entities ("&amp;" -> "&"). JotForm's raw API answer text
    can come back HTML-encoded even when its own UI displays the decoded
    version, which silently breaks exact-string matching against the master
    list — found via "FL - The Vinoy Resort &amp; Golf Club" failing to match
    "FL - The Vinoy Resort & Golf Club"."""
    if raw_value is None:
        return None
    cleaned = html.unescape(str(raw_value).strip())
    return LOCATION_ALIASES.get(cleaned, cleaned)


# ---------------------------------------------------------------------------
# Valet Driver Assessment detail fields
# ---------------------------------------------------------------------------
# This form captures WHO was assessed and WHO did the assessing, which no
# other tracked form does. Field names confirmed directly from JotForm's
# field output (same approach as FORM_ID_TO_LOCATION_FIELDS — no guessing).
#
# teamMember51 and assessorsName are JotForm "Full Name" widgets, so their
# answers arrive as {"first": ..., "last": ...} dicts, not strings.
# assessorsTitle is a dropdown, so it's already a clean string.
#
# IMPORTANT: these names are verified for the OLD form (230053977061151)
# ONLY. The NEW Valet Driver Assessment (262024177345151) is a clone with a
# rebuilt question set — its equivalent fields are almost certainly named
# differently, so it is deliberately NOT wired up here. Run "Debug Field
# Names" against it and add a second entry below once confirmed.
VALET_ASSESSMENT_FORM_NAME = "Valet Driver Assessment"
VALET_ASSESSMENT_FORM_ID = FORM_NAME_TO_ID[VALET_ASSESSMENT_FORM_NAME]

VALET_ASSESSMENT_FIELDS = {
    "team_member":     "teamMember51",
    "assessor":        "assessorsName",
    "assessor_title":  "assessorsTitle",
    "assessment_date": "dateSection73",
}

# Extra columns added to raw rows for this form only. Kept in one list so
# they're easy to strip back out of the general Raw Data sheet.
ASSESSMENT_COLUMNS = ["team_member", "assessor", "assessor_title", "assessment_date"]


def _full_name(answer):
    """Flatten a JotForm Full Name widget answer into one string.

    Handles both the dict form ({"first": "Kai", "last": "Campbell"}) and a
    plain string, in case the field is ever changed to a text box. Returns
    None for blank/missing so it shows as empty rather than the word "None"."""
    if answer in (None, ""):
        return None
    if isinstance(answer, dict):
        parts = [str(answer.get(k, "")).strip()
                 for k in ("prefix", "first", "middle", "last", "suffix")]
        joined = " ".join(p for p in parts if p)
    else:
        joined = str(answer).strip()
    joined = " ".join(html.unescape(joined).split())  # collapse double spaces
    return joined or None


def _date_answer(answer):
    """Flatten a JotForm Date widget answer to a plain YYYY-MM-DD string."""
    if answer in (None, ""):
        return None
    if isinstance(answer, dict):
        dt = answer.get("datetime")
        if dt:
            return str(dt).split(" ")[0]
        y, m, d = answer.get("year"), answer.get("month"), answer.get("day")
        if y and m and d:
            return f"{y}-{str(m).zfill(2)}-{str(d).zfill(2)}"
        return None
    return str(answer).strip() or None


# Dates are stored ISO (YYYY-MM-DD) everywhere internally, because that's what
# the JotForm API's date filters expect and what makes plain string sorting
# come out in true chronological order. They get converted to US MM/DD/YYYY
# only at the moment they're displayed or exported — so call to_us_dates()
# LAST, after any sorting or filtering, never on data still being computed on.
US_DATE_COLUMNS = {
    "submitted_at": "%m/%d/%Y %I:%M %p",
    "assessment_date": "%m/%d/%Y",
}


def to_us_dates(df):
    """Return a copy of `df` with its date columns re-rendered MM/DD/YYYY.

    A value that can't be parsed as a date is left exactly as JotForm sent
    it rather than being blanked out, so bad data stays visible instead of
    silently turning into an empty cell."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for col, fmt in US_DATE_COLUMNS.items():
        if col not in out.columns:
            continue
        parsed = pd.to_datetime(out[col], errors="coerce")
        out[col] = parsed.dt.strftime(fmt).where(parsed.notna(), out[col])
    return out


# ---------------------------------------------------------------------------
# JotForm API
# ---------------------------------------------------------------------------
class JotFormError(Exception):
    """Raised when the JotForm API can't be reached or returns an error, so
    the UI layer can show a clean message instead of a stack trace."""


def get_form_questions(form_id, api_key):
    """Fetch a form's field/question definitions (not submissions)."""
    url = f"https://api.jotform.com/form/{form_id}/questions"
    params = {'apiKey': api_key}
    max_retries = 3
    r = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            if attempt < max_retries:
                time.sleep(attempt * 2)
                continue
            raise JotFormError(f"Couldn't reach JotForm: {e}")
        if r.ok:
            break
        if r.status_code in (502, 503, 504) and attempt < max_retries:
            time.sleep(attempt * 2)
            continue
        break

    if r is None or not r.ok:
        form_label = FORM_ID_TO_NAME.get(form_id, form_id)
        raise JotFormError(
            f"JotForm API error while fetching field setup for '{form_label}' "
            f"(status {r.status_code if r is not None else '?'}) after "
            f"{max_retries} attempt(s): {r.text[:500] if r is not None else ''}"
        )
    return r.json()['content']


def get_submissions(form_id, api_key, start_date=None, end_date=None):
    """Page through every submission for one form in the date range."""
    all_submissions = []
    batch_size = 1000
    offset = 0
    max_retries = 3
    while True:
        url = f"https://api.jotform.com/form/{form_id}/submissions"
        params = {'apiKey': api_key, 'limit': batch_size, 'offset': offset}
        if start_date or end_date:
            filters = {}
            if start_date:
                filters["created_at:gt"] = f"{start_date}T00:00:00"
            if end_date:
                filters["created_at:lt"] = f"{end_date}T23:59:59"
            params["filter"] = json.dumps(filters)

        r = None
        for attempt in range(1, max_retries + 1):
            try:
                r = requests.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt < max_retries:
                    time.sleep(attempt * 2)
                    continue
                raise JotFormError(f"Couldn't reach JotForm: {e}")
            if r.ok:
                break
            # 502/503/504 are usually transient hiccups on JotForm's end and
            # are worth a retry. 401/403/400 won't be fixed by retrying.
            if r.status_code in (502, 503, 504) and attempt < max_retries:
                time.sleep(attempt * 2)
                continue
            break

        if r is None or not r.ok:
            form_label = FORM_ID_TO_NAME.get(form_id, form_id)
            raise JotFormError(
                f"JotForm API error while fetching '{form_label}' "
                f"(status {r.status_code if r is not None else '?'}) after "
                f"{max_retries} attempt(s): {r.text[:500] if r is not None else ''}"
            )

        submissions = r.json()['content']
        all_submissions.extend(submissions)
        if len(submissions) < batch_size:
            break
        offset += batch_size
    return all_submissions


def extract_row(sub, form_id):
    """Pull out the fields we care about from one submission, using the
    confirmed field name(s) for this form's location field(s)."""
    answers = sub.get('answers', {})
    row = {
        'form_id': form_id,
        'form_name': FORM_ID_TO_NAME.get(form_id, form_id),
        'submission_id': sub.get('id'),
        'submitted_at': sub.get('created_at'),
        'location': None,
    }
    # Present-but-empty on every form, so the DataFrame has a stable shape
    # whether or not the Valet Driver Assessment is in the selection.
    row.update({col: None for col in ASSESSMENT_COLUMNS})

    by_name = {v.get('name'): v.get('answer', '') for v in answers.values()}
    for field_name in FORM_ID_TO_LOCATION_FIELDS.get(form_id, []):
        val = by_name.get(field_name)
        if val not in (None, ''):
            row['location'] = normalize_location(val if isinstance(val, str) else str(val))
            break

    if form_id == VALET_ASSESSMENT_FORM_ID:
        row['team_member'] = _full_name(by_name.get(VALET_ASSESSMENT_FIELDS['team_member']))
        row['assessor'] = _full_name(by_name.get(VALET_ASSESSMENT_FIELDS['assessor']))
        title = by_name.get(VALET_ASSESSMENT_FIELDS['assessor_title'])
        row['assessor_title'] = html.unescape(str(title).strip()) if title not in (None, '') else None
        row['assessment_date'] = _date_answer(by_name.get(VALET_ASSESSMENT_FIELDS['assessment_date']))

    return row


BASE_RAW_COLUMNS = ['form_id', 'form_name', 'submission_id', 'submitted_at', 'location']


def get_all_data(form_names, location_list, start_date, end_date, api_key):
    all_rows = []
    for name in form_names:
        form_id = FORM_NAME_TO_ID[name]
        for sub in get_submissions(form_id, api_key, start_date, end_date):
            row = extract_row(sub, form_id)
            if not location_list or row['location'] in location_list:
                all_rows.append(row)
    if not all_rows:
        return pd.DataFrame(columns=BASE_RAW_COLUMNS + ASSESSMENT_COLUMNS)
    return pd.DataFrame(all_rows)


def build_assessment_details(df_raw):
    """One row per Valet Driver Assessment submission, showing who was
    assessed and who assessed them. No compliance scoring — this form has no
    quota, so this is a visibility report only."""
    columns = ["assessment_date", "submitted_at", "location", "team_member",
               "assessor", "assessor_title", "submission_id"]
    if df_raw.empty or 'form_name' not in df_raw.columns:
        return pd.DataFrame(columns=columns)
    df = df_raw[df_raw['form_name'] == VALET_ASSESSMENT_FORM_NAME].copy()
    if df.empty:
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return (df[columns]
            .sort_values(["location", "team_member", "assessment_date"], na_position="last")
            .reset_index(drop=True))


# ---------------------------------------------------------------------------
# Compliance math
# ---------------------------------------------------------------------------
def _period_count(interval, start_date, end_date, operating_days=None):
    """Number of compliance periods (days/weeks/months) in the date range.

    For "daily", `operating_days` (a set of weekday ints, 0=Mon..6=Sun)
    restricts the count to only the days the location is actually open — a
    5-day location gets ~5/7 as many required days as a 7-day location over
    the same range. Weekly/monthly ignore operating_days: those quotas apply
    regardless of which specific days the location is open."""
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
                           overrides=None, operating_days=None, all_known_locations=None):
    """Build the compliance summary, one row per (form, location).

    `all_known_locations` is the full master location list. It's used when
    nothing is selected in the location filter, so that a location with ZERO
    submissions still appears in the report at 0% instead of disappearing.
    The old version fell back to only the locations present in the returned
    data, which hid exactly the sites you most need to see."""
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS

    if selected_locations:
        all_locations = list(selected_locations)
    elif all_known_locations:
        all_locations = list(all_known_locations)
    else:
        all_locations = sorted(df_raw['location'].dropna().unique())

    summary_rows = []
    compliance_forms = [f for f in selected_forms if FORM_COMPLIANCE.get(f)]

    # Cache period counts by (interval, operating-days-set). Weekly/monthly
    # don't depend on operating days, so they share one cache entry.
    period_count_cache = {}

    for form_name in compliance_forms:
        df_form = df_raw[df_raw['form_name'] == form_name] if not df_raw.empty else df_raw
        counts = df_form['location'].value_counts().to_dict() if not df_form.empty else {}

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

            actual = counts.get(loc, 0)
            target = period_count * quota

            # A quota of 0 means "this location isn't required to do this
            # form". The old code scored that as 0% complete, which both
            # looked like a failure and dragged down the leaderboard average.
            if target > 0:
                percent_complete = round(100 * actual / target, 1)
                status = "Met" if actual >= target else f"Missing {target - actual}"
            else:
                percent_complete = 100.0
                status = "Not required"

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
                "percent_complete": percent_complete,
                "status": status,
                "custom_quota": "Yes" if is_override else "No",
                "open_days_per_week": len(loc_days) if is_partial_week else 7,
            })
    return pd.DataFrame(summary_rows)


def leaderboard_with_badges(compliance_summary):
    """Rank locations by average completion across the forms they're actually
    required to do. Rows with a target of 0 ("Not required") are excluded from
    the average rather than counted as a free 100%, so a location can't climb
    the board by having fewer requirements."""
    cols = ['location', 'forms_tracked', 'total_target', 'total_actual',
            'overall_percent', 'Badge', 'Rank']
    if compliance_summary.empty:
        return pd.DataFrame(columns=cols)

    scored = compliance_summary[compliance_summary['target_total'] > 0]
    if scored.empty:
        return pd.DataFrame(columns=cols)

    leader = (scored.groupby('location')
              .agg(forms_tracked=('form_name', 'count'),
                   total_target=('target_total', 'sum'),
                   total_actual=('actual_total', 'sum'),
                   overall_percent=('percent_complete', 'mean'))
              .reset_index())
    leader['overall_percent'] = leader['overall_percent'].round(1)
    leader = leader.sort_values('overall_percent', ascending=False)

    def badge_row(row):
        p = row['overall_percent']
        if p >= 100:
            return "🏅 Gold Star"
        elif p >= 90:
            return "🥈 Silver Star"
        elif p >= 75:
            return "🥉 Bronze Star"
        return "🚩 Needs Improvement"

    leader['Badge'] = leader.apply(badge_row, axis=1)
    leader['Rank'] = leader['overall_percent'].rank(method='min', ascending=False).astype(int)
    return leader[cols]


# ---------------------------------------------------------------------------
# STREAMLIT UI
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Company logo
# ---------------------------------------------------------------------------
# Commit the logo into an "assets" folder in the repo root, next to main.py:
#     jotformdashboard/
#       main.py
#       assets/EPGS-HorizontalLogo-Color.svg
#       .streamlit/config.toml
#
# Either the SVG or the PNG works — whichever is present is used, SVG first.
# The SVG is preferred because it's vector (sharp on high-DPI screens) and
# genuinely transparent. The PNG has an alpha channel but no transparent
# pixels, so its background is solid white: fine on the light theme this app
# pins in config.toml, but it would show as a white box on a dark background.
#
# WHY THE IMAGE IS EMBEDDED AS A DATA URI RATHER THAN PASSED TO st.image():
# st.image() routes bitmaps through PIL, which doesn't read SVG, so support
# for .svg paths varies by Streamlit version. Base64-encoding the file into
# an <img> tag sidesteps that entirely and renders crisply at any size on
# any version. It also keeps the SVG's internal <style> block (.st0/.st1)
# scoped inside the image, where it can't collide with the page's own CSS
# classes — which inlining the raw <svg> markup would risk.
#
# If neither file is found the app renders the title alone instead of
# crashing, so a bad filename degrades quietly rather than taking the
# dashboard down.
LOGO_CANDIDATES = [
    "assets/EPGS-HorizontalLogo-Color.svg",     # preferred: vector, transparent
    "assets/EPGS-HorizontalLogo-Color-50.png",  # fallback: raster, white bg
]
LOGO_HEIGHT_PX = 64          # logo is ~2.8:1, so 64px tall ≈ 181px wide

LOGO_PATH = next((p for p in LOGO_CANDIDATES if os.path.exists(p)), None)

_LOGO_MIME = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


@st.cache_data(show_spinner=False)
def load_logo_data_uri(path: str) -> str:
    """Read an image off disk and return it as a base64 data URI."""
    mime = _LOGO_MIME.get(os.path.splitext(path)[1].lower(), "image/png")
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


if LOGO_PATH:
    try:
        _logo_uri = load_logo_data_uri(LOGO_PATH)
        st.markdown(
            f'<img src="{_logo_uri}" '
            f'alt="Evolution Parking &amp; Guest Services" '
            f'style="height:{LOGO_HEIGHT_PX}px;margin-bottom:0.75rem;">',
            unsafe_allow_html=True,
        )
    except Exception:
        pass

    # st.logo() pins the logo above the sidebar on Streamlit 1.35+. It's
    # wrapped because older builds don't have it at all.
    if hasattr(st, "logo"):
        try:
            st.logo(LOGO_PATH)
        except Exception:
            pass

st.title("JotForm Compliance Dashboard")

# ---------------------------------------------------------------------------
# Evolution brand palette
# ---------------------------------------------------------------------------
# Pulled from the computed styles on Evolution's own site, so these are the
# colors actually rendered in production rather than a guess. Change a value
# here and it propagates through every rule in the CSS block below.
BRAND = {
    "teal":       "#00C0B5",   # primary brand color
    "teal_dark":  "#00938B",   # hover / active state
    "teal_pale":  "#C8F0EE",   # tints and fills
    "slate":      "#313C48",   # headings
    "gray_text":  "#616365",   # body copy
    "sand":       "#E2E2D2",   # warm neutral
    "border":     "#E2E2E2",   # borders and dividers
    "gray_bg":    "#EFEFEF",   # subtle backgrounds
}

# The admin/settings expanders are made small and low-contrast so they don't
# draw attention during normal use, but stay fully readable and clickable.
# Opacity is used rather than a hardcoded text color so this works the same
# on light and dark themes.
st.markdown(
    f"""
    <style>
    [data-testid="stExpander"] summary {{ font-size: 0.8rem; opacity: 0.55; }}
    [data-testid="stExpander"] summary:hover {{ opacity: 1; }}

    /* ---- Headings ---- */
    h1, h2, h3 {{ color: {BRAND['slate']}; }}
    h1 {{ border-bottom: 3px solid {BRAND['teal']}; padding-bottom: 0.3rem; }}

    /* ---- Primary button (Run Report) ---- */
    .stButton > button[kind="primary"] {{
        background-color: {BRAND['teal']};
        border-color: {BRAND['teal']};
        color: #FFFFFF;
    }}
    .stButton > button[kind="primary"]:hover,
    .stButton > button[kind="primary"]:focus {{
        background-color: {BRAND['teal_dark']};
        border-color: {BRAND['teal_dark']};
        color: #FFFFFF;
    }}

    /* ---- Secondary buttons ---- */
    .stButton > button[kind="secondary"] {{
        border-color: {BRAND['border']};
        color: {BRAND['slate']};
    }}
    .stButton > button[kind="secondary"]:hover {{
        border-color: {BRAND['teal']};
        color: {BRAND['teal_dark']};
    }}

    /* ---- KPI metric cards ---- */
    [data-testid="stMetric"] {{
        background-color: {BRAND['teal_pale']};
        border-left: 4px solid {BRAND['teal']};
        border-radius: 4px;
        padding: 0.75rem 1rem;
    }}
    [data-testid="stMetricLabel"] {{ color: {BRAND['gray_text']}; }}
    [data-testid="stMetricValue"] {{ color: {BRAND['slate']}; }}

    /* ---- Multiselect chips (selected forms / locations) ---- */
    [data-baseweb="tag"] {{
        background-color: {BRAND['teal']} !important;
        color: #FFFFFF !important;
    }}

    /* ---- Sidebar ---- */
    [data-testid="stSidebar"] {{
        background-color: {BRAND['gray_bg']};
        border-right: 1px solid {BRAND['border']};
    }}

    /* ---- Spinner + progress ---- */
    .stSpinner > div {{ border-top-color: {BRAND['teal']} !important; }}
    [data-testid="stProgressBar"] > div > div > div {{
        background-color: {BRAND['teal']};
    }}
    </style>
    """,
    unsafe_allow_html=True,
)

api_key = st.secrets["API_KEY"] if "API_KEY" in st.secrets \
    else st.text_input("Enter your JotForm API Key:", type="password")

# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------
# Hides the Location list, Quota Overrides, Operating Days and Debug Tools
# sections from everyone except whoever knows the admin password.
#
# Set the password in Streamlit secrets:
#     ADMIN_PASSWORD = "something-only-you-know"
#
# HONEST LIMITS OF THIS: it keeps the settings out of reach of people using
# the dashboard normally, which is the actual goal. It is NOT a security
# boundary against someone determined — anyone who can open the app is still
# talking to the same server, and the underlying Google Sheet is protected by
# its own sharing settings, not by this. For real access control, restrict who
# can open the app at all via Streamlit Cloud's viewer settings.
#
# If ADMIN_PASSWORD isn't set, the app falls back to unlocking with ?admin=1
# on the end of the URL — convenient, but anyone who sees you do it can repeat
# it, so set the password once you're past testing.
ADMIN_PASSWORD = st.secrets.get("ADMIN_PASSWORD", "")


def _get_query_params():
    """st.query_params on modern Streamlit, experimental_get_query_params on
    older builds. Returns a plain dict of str -> str."""
    try:
        return {k: (v[0] if isinstance(v, list) else v) for k, v in st.query_params.items()}
    except Exception:
        try:
            return {k: (v[0] if isinstance(v, list) else v)
                    for k, v in st.experimental_get_query_params().items()}
        except Exception:
            return {}


if 'is_admin' not in st.session_state:
    st.session_state['is_admin'] = (
        not ADMIN_PASSWORD and str(_get_query_params().get("admin", "")) == "1"
    )

with st.sidebar:
    st.markdown("### Admin")
    if st.session_state['is_admin']:
        st.success("Admin tools unlocked")
        if st.button("Lock admin tools"):
            st.session_state['is_admin'] = False
            st.rerun()
    elif ADMIN_PASSWORD:
        _pw = st.text_input("Admin password", type="password", key="admin_pw")
        if st.button("Unlock"):
            if _pw == ADMIN_PASSWORD:
                st.session_state['is_admin'] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    else:
        st.caption(
            "No ADMIN_PASSWORD is set in secrets, so admin tools unlock with "
            "`?admin=1` on the end of the URL. Set a password to lock them properly."
        )

is_admin = st.session_state['is_admin']

# The sheet URL/ID defaults to st.secrets["overrides_sheet_url"] if set, so
# nobody has to re-paste it every session.
if 'overrides_sheet_url' not in st.session_state:
    st.session_state['overrides_sheet_url'] = st.secrets.get("overrides_sheet_url", "")

# ---- Load everything from the Google Sheet once per session ----
if 'sheet_loaded' not in st.session_state:
    st.session_state['sheet_loaded'] = True
    st.session_state['sheet_messages'] = []
    st.session_state['locations'] = sorted(MASTER_LOCATIONS)
    st.session_state['locations_source'] = "built-in fallback list"
    st.session_state['location_overrides'] = copy.deepcopy(LOCATION_COMPLIANCE_OVERRIDES)
    st.session_state['location_operating_days'] = copy.deepcopy(LOCATION_OPERATING_DAYS)

    _ref = st.session_state['overrides_sheet_url']
    if _ref:
        try:
            _locs = load_locations_from_sheet(_ref)
            if _locs:
                st.session_state['locations'] = _locs
                st.session_state['locations_source'] = f"Google Sheet → {LOCATIONS_TAB} tab"
            else:
                st.session_state['sheet_messages'].append(
                    f"The \"{LOCATIONS_TAB}\" tab is empty — using the built-in "
                    f"fallback list of {len(MASTER_LOCATIONS)} locations."
                )
        except Exception as e:
            st.session_state['sheet_messages'].append(
                f"Couldn't load the location list from the Google Sheet ({e}). "
                f"Using the built-in fallback list."
            )
        try:
            _ovr, _warn = load_overrides_from_sheet(_ref)
            st.session_state['location_overrides'] = _ovr
            st.session_state['sheet_messages'].extend(_warn)
        except Exception as e:
            st.session_state['sheet_messages'].append(f"Couldn't load quota overrides ({e}).")
        try:
            st.session_state['location_operating_days'] = load_operating_days_from_sheet(_ref)
        except Exception as e:
            st.session_state['sheet_messages'].append(f"Couldn't load operating days ({e}).")

# Streamlit won't let us write to a widget's session-state key after the
# widget has been created, so the buttons below set this flag and rerun.
# Applying it HERE — before the multiselect exists — is what makes
# "Reset"/"Reload" actually repopulate the location picker.
if st.session_state.pop('_reload_location_widget', False):
    st.session_state['selected_locations_widget'] = list(st.session_state['locations'])

locations = st.session_state['locations']

filename = st.text_input("Excel filename (without .xlsx)", "JotForm_Compliance_Report") + ".xlsx"

form_names = list(FORM_NAME_TO_ID.keys())
selected_forms = st.multiselect("Select form(s):", options=form_names, default=form_names)

col_d1, col_d2 = st.columns(2)
with col_d1:
    start_date = st.date_input("Start date", **DATE_FMT)
with col_d2:
    end_date = st.date_input("End date", **DATE_FMT)

# ---- Location picker ----
# A keyed multiselect ignores `default` on every rerun after the first, so if
# this ever gets cleared it stays cleared. Seed it explicitly the first time.
if 'selected_locations_widget' not in st.session_state:
    st.session_state['selected_locations_widget'] = list(locations)

selected_locations = st.multiselect(
    "Select location(s):",
    options=locations,
    key='selected_locations_widget',
)

if not selected_locations:
    st.warning(
        "No locations are selected, so the report will cover **all** locations. "
        "Click **Select all locations** below if that isn't what you meant."
    )

# These run as button CALLBACKS, which fire before the next script run
# starts — i.e. before the multiselect above has been re-created. That's the
# only safe window in which Streamlit will let us write to a widget's own
# session-state key. Writing to it inline (after the widget exists) raises
# StreamlitAPIException, and calling st.rerun() inside a callback raises too,
# so neither of those belongs here.
def _select_all_locations():
    st.session_state['selected_locations_widget'] = list(st.session_state['locations'])


def _clear_locations():
    st.session_state['selected_locations_widget'] = []


col_s1, col_s2, _ = st.columns([1, 1, 3])
with col_s1:
    st.button("Select all locations", key="select_all_locs_btn", on_click=_select_all_locations)
with col_s2:
    st.button("Clear locations", key="clear_locs_btn", on_click=_clear_locations)

# Sheet-loading diagnostics (blank quotas, unlabelled columns, fallback list
# in use) are configuration problems, not something a normal viewer can act
# on — so they're admin-only too.
if is_admin:
    for msg in st.session_state.get('sheet_messages', []):
        st.info(msg, icon="ℹ️")

if is_admin:
    # ---- Location list editor ----
    with st.expander("📍 Location list", expanded=False):
        st.caption(
            f"Currently loaded from **{st.session_state.get('locations_source')}** — "
            f"{len(locations)} locations. Add or edit rows below, then click "
            f"**Save to Google Sheet** so the change sticks for everyone. You can "
            f"also edit the \"{LOCATIONS_TAB}\" tab directly in Google Sheets."
        )

        loc_edited_df = st.data_editor(
            pd.DataFrame({"Location": locations}),
            num_rows="dynamic",
            key="locations_editor",
            column_config={"Location": st.column_config.TextColumn("Location", required=True)},
            **FILL,
        )
        _edited, _seen = [], set()
        for v in loc_edited_df["Location"].tolist():
            if v is None:
                continue
            c = html.unescape(str(v).strip())
            if c and c not in _seen:
                _seen.add(c)
                _edited.append(c)

        lc1, lc2, lc3 = st.columns(3)
        with lc1:
            if st.button("⬆️ Save to Google Sheet", key="save_locs_btn"):
                if not st.session_state.get('overrides_sheet_url'):
                    st.error("Enter a Google Sheet URL or ID in the Quota Overrides section first.")
                else:
                    try:
                        save_locations_to_sheet(_edited, st.session_state['overrides_sheet_url'])
                        st.session_state['locations'] = _edited
                        st.session_state['locations_source'] = f"Google Sheet → {LOCATIONS_TAB} tab"
                        st.session_state['_reload_location_widget'] = True
                        st.success(f"Saved {len(_edited)} locations.")
                        st.rerun()
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")
        with lc2:
            if st.button("⬇️ Reload from Google Sheet", key="load_locs_btn"):
                if not st.session_state.get('overrides_sheet_url'):
                    st.error("Enter a Google Sheet URL or ID in the Quota Overrides section first.")
                else:
                    try:
                        fetched = load_locations_from_sheet(st.session_state['overrides_sheet_url'])
                        if fetched:
                            st.session_state['locations'] = fetched
                            st.session_state['locations_source'] = f"Google Sheet → {LOCATIONS_TAB} tab"
                            st.session_state['_reload_location_widget'] = True
                            st.rerun()
                        else:
                            st.warning(f"The \"{LOCATIONS_TAB}\" tab is empty — nothing to load.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
        with lc3:
            if st.button("↩️ Reset to built-in list", key="reset_locs_btn"):
                st.session_state['locations'] = sorted(MASTER_LOCATIONS)
                st.session_state['locations_source'] = "built-in fallback list"
                st.session_state['_reload_location_widget'] = True
                st.rerun()

compliance_form_names = [f for f in FORM_NAME_TO_ID if FORM_COMPLIANCE.get(f)]

if is_admin:
    # ---- Location Quota Overrides editor ----
    with st.expander("⚙️ Location Quota Overrides", expanded=False):
        st.caption(
            "Set a different quota (and, if needed, a different interval) for "
            "specific locations. Any location not listed here uses the form's "
            "standard quota. **Leave Quota blank and the row is ignored** — enter "
            "0 to mean 'this location doesn't have to do this form at all'."
        )

        edited_df = st.data_editor(
            overrides_to_df(st.session_state['location_overrides']),
            num_rows="dynamic",
            key="overrides_editor",
            column_config={
                "Form": st.column_config.SelectboxColumn("Form", options=compliance_form_names, required=True),
                "Location": st.column_config.SelectboxColumn(
                    "Location", options=locations, required=True
                ) if locations else st.column_config.TextColumn("Location", required=True),
                "Quota": st.column_config.NumberColumn("Quota", min_value=0, step=1),
                "Interval": st.column_config.SelectboxColumn(
                    "Interval", options=["daily", "weekly", "monthly"]
                ),
            },
            **FILL,
        )
        _ovr, _skipped = df_to_overrides(edited_df, collect_skipped=True)
        st.session_state['location_overrides'] = _ovr
        for msg in _skipped:
            st.warning(msg)

        st.divider()
        st.markdown("**Google Sheet sync** (persists across sessions and app restarts)")
        st.session_state['overrides_sheet_url'] = st.text_input(
            "Google Sheet URL or ID",
            value=st.session_state['overrides_sheet_url'],
            help="The sheet must be shared (Editor access) with your service account's "
                 "email — found as 'client_email' in st.secrets['gcp_service_account'].",
        )

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("⬇️ Load from Google Sheet", key="load_ovr_btn"):
                if not st.session_state['overrides_sheet_url']:
                    st.error("Enter a Google Sheet URL or ID first.")
                else:
                    try:
                        _ovr, _warn = load_overrides_from_sheet(st.session_state['overrides_sheet_url'])
                        st.session_state['location_overrides'] = _ovr
                        st.session_state['sheet_messages'] = _warn
                        st.success("Loaded overrides from the Google Sheet.")
                        st.rerun()
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")
        with col_b:
            if st.button("⬆️ Save to Google Sheet", key="save_ovr_btn"):
                if not st.session_state['overrides_sheet_url']:
                    st.error("Enter a Google Sheet URL or ID first.")
                else:
                    try:
                        save_overrides_to_sheet(
                            st.session_state['location_overrides'],
                            st.session_state['overrides_sheet_url'],
                        )
                        st.success("Saved overrides to the Google Sheet.")
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

        with st.expander("Backup as a local file instead"):
            col_c, col_d = st.columns(2)
            with col_c:
                st.download_button(
                    "Save overrides to file",
                    data=json.dumps(st.session_state['location_overrides'], indent=2),
                    file_name="location_quota_overrides.json",
                    mime="application/json",
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

if is_admin:
    # ---- Location Operating Days editor ----
    with st.expander("📅 Location Operating Days (5/6-day locations)", expanded=False):
        st.caption(
            "Only add a location here if it's NOT open all 7 days a week. Check the "
            "days it IS open (e.g. Mon–Fri checked, Sat/Sun unchecked for a 5-day "
            "site). Locations left off this list are assumed open every day. This "
            "only affects daily-quota forms — weekly and monthly quotas are unchanged."
        )

        days_edited_df = st.data_editor(
            operating_days_to_df(st.session_state['location_operating_days']),
            num_rows="dynamic",
            key="operating_days_editor",
            column_config={
                "Location": st.column_config.SelectboxColumn(
                    "Location", options=locations, required=True
                ) if locations else st.column_config.TextColumn("Location", required=True),
                **{day: st.column_config.CheckboxColumn(day, default=True) for day in WEEKDAY_COLUMNS},
            },
            **FILL,
        )
        st.session_state['location_operating_days'] = df_to_operating_days(days_edited_df)

        st.divider()
        st.markdown(f"**Google Sheet sync** (uses the \"{OPERATING_DAYS_TAB}\" tab in the same sheet)")
        col_e, col_f = st.columns(2)
        with col_e:
            if st.button("⬇️ Load from Google Sheet", key="load_days_btn"):
                if not st.session_state.get('overrides_sheet_url'):
                    st.error("Enter a Google Sheet URL or ID in the Quota Overrides section first.")
                else:
                    try:
                        st.session_state['location_operating_days'] = load_operating_days_from_sheet(
                            st.session_state['overrides_sheet_url']
                        )
                        st.success("Loaded operating days from the Google Sheet.")
                        st.rerun()
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")
        with col_f:
            if st.button("⬆️ Save to Google Sheet", key="save_days_btn"):
                if not st.session_state.get('overrides_sheet_url'):
                    st.error("Enter a Google Sheet URL or ID in the Quota Overrides section first.")
                else:
                    try:
                        save_operating_days_to_sheet(
                            st.session_state['location_operating_days'],
                            st.session_state['overrides_sheet_url'],
                        )
                        st.success("Saved operating days to the Google Sheet.")
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

        with st.expander("Backup as a local file instead"):
            col_g, col_h = st.columns(2)
            with col_g:
                st.download_button(
                    "Save operating days to file",
                    data=json.dumps(
                        {loc: sorted(days) for loc, days in st.session_state['location_operating_days'].items()},
                        indent=2,
                    ),
                    file_name="location_operating_days.json",
                    mime="application/json",
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

# ---- Run Report ----
if st.button("Run Report", type="primary"):
    if not api_key:
        st.error("API Key is required!")
    elif start_date > end_date:
        st.error("Start date is after end date.")
    else:
        try:
            with st.spinner("Pulling submissions from JotForm..."):
                df_raw = get_all_data(selected_forms, selected_locations, start_date, end_date, api_key)
        except JotFormError as e:
            st.error(str(e))
            df_raw = None

        if df_raw is not None:
            compliance_summary = compute_period_targets(
                df_raw, start_date, end_date, selected_forms, selected_locations,
                overrides=st.session_state.get('location_overrides', LOCATION_COMPLIANCE_OVERRIDES),
                operating_days=st.session_state.get('location_operating_days', LOCATION_OPERATING_DAYS),
                all_known_locations=locations,
            )
            leaderboard = leaderboard_with_badges(compliance_summary)

            if df_raw.empty:
                st.warning(
                    "No submissions were returned for this selection. The summary "
                    "below still lists every expected location at 0%."
                )

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Submissions pulled", f"{len(df_raw):,}")
            m2.metric("Locations reported", f"{compliance_summary['location'].nunique() if not compliance_summary.empty else 0}")
            scored = compliance_summary[compliance_summary['target_total'] > 0] if not compliance_summary.empty else compliance_summary
            m3.metric("Requirements met", f"{(scored['status'] == 'Met').sum() if not scored.empty else 0} / {len(scored)}")
            m4.metric("Average completion", f"{scored['percent_complete'].mean():.1f}%" if not scored.empty else "—")

            st.subheader("Compliance Summary")
            st.dataframe(compliance_summary, **FILL)

            st.subheader("Leaderboard")
            st.dataframe(leaderboard, **FILL)

            assessment_details = build_assessment_details(df_raw)
            if not assessment_details.empty:
                st.subheader("Driver Assessments")
                st.caption(
                    f"{len(assessment_details):,} assessment(s) — "
                    f"{assessment_details['team_member'].nunique()} distinct team member "
                    f"name(s) across {assessment_details['location'].nunique()} location(s). "
                    "Names are free text on the form, so the same person can appear "
                    "under more than one spelling."
                )
                st.dataframe(to_us_dates(assessment_details), **FILL)

            st.subheader("Raw Data")
            # The four assessment columns are blank on every other form, so
            # they're stripped here to keep the general Raw Data sheet clean.
            # to_us_dates() is applied last, once sorting and filtering are
            # done, so the underlying ISO values were used for all of that.
            df_raw_display = to_us_dates(df_raw.drop(
                columns=[c for c in ASSESSMENT_COLUMNS if c in df_raw.columns]
            ))
            assessment_export = to_us_dates(assessment_details)
            st.dataframe(df_raw_display, **FILL)

            if not df_raw.empty:
                submission_count = df_raw.groupby(['form_name', 'location']).size().reset_index(name='submission_count')
            else:
                submission_count = pd.DataFrame(columns=['form_name', 'location', 'submission_count'])

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                compliance_summary.to_excel(writer, index=False, sheet_name='Compliance Summary')
                leaderboard.to_excel(writer, index=False, sheet_name='Leaderboard')
                df_raw_display.to_excel(writer, index=False, sheet_name='Raw Data')
                assessment_export.to_excel(writer, index=False, sheet_name='Driver Assessments')
                submission_count.to_excel(writer, index=False, sheet_name='Submission Count')
            st.download_button(
                label="Download Excel File",
                data=output.getvalue(),
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

if is_admin:
    # ---- Debug tools ----
    st.write("")
    st.write("")
    with st.expander("🔧 Debug Tools", expanded=False):
        if st.button("Debug: Location Match Check (for current form/date selection)"):
            if not api_key:
                st.error("API Key is required!")
            else:
                st.caption(
                    "Every DISTINCT location value extract_row() actually produces for "
                    "the selected forms/date range, ignoring the location filter — so "
                    "near-invisible mismatches (stray spaces, lookalike dashes) that "
                    "make a real submission fail to match are visible."
                )
                try:
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
                            rows.append({
                                "extracted_location": loc_val if loc_val is not None else "(blank/None)",
                                "repr (shows hidden chars)": repr(loc_val),
                                "count": count,
                                "in_current_location_list": (loc_val in locations) if loc_val else False,
                            })
                        st.dataframe(pd.DataFrame(rows), **FILL)
                except JotFormError as e:
                    st.error(str(e))

        if st.button("Debug Field Names"):
            if not api_key:
                st.error("API Key is required!")
            else:
                try:
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
                                v.get('name'): {'label': v.get('text'), 'answer': v.get('answer')}
                                for v in first['answers'].values()
                            })
                            st.write(
                                f"→ What extract_row() records as location for this "
                                f"submission: **{extract_row(first, form_id)['location']}**"
                            )
                        else:
                            st.write("⚠️ No submissions returned at all for this form/date range")
                except JotFormError as e:
                    st.error(str(e))

        if st.button("Debug Location Field Setup"):
            if not api_key:
                st.error("API Key is required!")
            else:
                try:
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
                except JotFormError as e:
                    st.error(str(e))
