"""
EPGS JotForm Compliance Dashboard
=================================

Pulls submissions from JotForm, scores them against per-location quotas, and
reports compliance across every EPGS parking and valet site.

HOW THE PIECES FIT TOGETHER
---------------------------
    JotForm forms  ──►  JotForm API  ──►  extract_row()  ──►  compliance math
                                              ▲
    Google Sheet ─────────────────────────────┘
      Locations tab       master site list (the only list this app trusts)
      Aliases tab         submitted value -> master list name
      Overrides tab       per (form, location) quota + interval
      Operating Days tab  which days a site is open

Everything that changes site-to-site lives in the Google Sheet, NOT in this
file. There is deliberately no hardcoded location list here: if the sheet is
unreachable the app says so loudly instead of quietly scoring against a stale
copy, which is how sites silently went missing in earlier versions.

WHY ALIASES EXIST
-----------------
Six of the nine tracked forms take their location from JotForm's reusable
dropdown widget, which is fed by the master location table, so their values
match the master list exactly. Lot Audit and Parking/Work Area Hazard are
still on a frozen static dropdown and emit their own spellings
("FL - Embassy Downtown Tampa" where the master list says "FL - Embassy Tampa
Downtown"). Until those two forms are migrated, the Aliases tab is what keeps
their submissions from being silently dropped. The Location Health tab finds
the mismatches and writes the alias for you, so this no longer needs a code
change and a deploy.
"""

import streamlit as st
import pandas as pd
import requests
import io
import time
import html
import json
import re
import copy
import inspect
import os
import base64
import difflib
import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import gspread
from google.oauth2.service_account import Credentials

# Browser-tab icon: the EPGS pineapple, cut from the vertical logo. Falls back
# to the old emoji if the file is ever missing so a bad path can't stop the app.
PAGE_ICON_PATH = "assets/epgs-icon.png"

st.set_page_config(
    page_title="JotForm Compliance Dashboard",
    page_icon=PAGE_ICON_PATH if os.path.exists(PAGE_ICON_PATH) else "🚗",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Streamlit version shims
# ---------------------------------------------------------------------------
# Streamlit renamed `use_container_width` to `width="stretch"` in newer
# versions. Detect which one this install wants once, and splat FILL into any
# dataframe/data_editor call so the app works on either version.
#
# IMPORTANT: newer Streamlit builds (including 1.64, what this app runs on)
# keep `use_container_width` around as a *deprecated* alias for backward
# compatibility even after adding `width`. That means both names show up in
# the signature at once, so the previous check here (which required
# `use_container_width` to be ABSENT before trusting `width`) never fired and
# every dataframe/data_editor call kept using the deprecated kwarg — which is
# exactly what was flooding the deploy logs with
# "Please replace `use_container_width` with `width`" on every rerun. The fix
# is to only check whether `width` is supported at all.
try:
    _params = inspect.signature(st.dataframe).parameters
    FILL = {"width": "stretch"} if "width" in _params else {"use_container_width": True}
except Exception:
    FILL = {"use_container_width": True}

# Read-only tables also hide pandas' 0, 1, 2 row numbers, which mean nothing to
# anyone reading the report. Editable tables (Settings) keep plain FILL.
TABLE = {**FILL, "hide_index": True}

# Same idea for st.date_input's `format` argument, which only exists on
# Streamlit 1.30+. On older builds we leave it off rather than crash — the
# picker falls back to YYYY/MM/DD, but everything still works.
try:
    DATE_FMT = {"format": "MM/DD/YYYY"} \
        if "format" in inspect.signature(st.date_input).parameters else {}
except Exception:
    DATE_FMT = {}

# st.multiselect's `wrap` argument forces the selected-value chips to wrap
# onto multiple rows instead of collapsing behind an overflow icon when the
# widget is narrow or has many selections — exactly the Forms-box problem
# below. Confirmed present in 1.64.0 (this deploy's pinned version), so no
# Streamlit upgrade is needed for this; kept behind an inspect check anyway
# in case this ever runs on an older build that predates it, same defensive
# pattern as FILL and DATE_FMT above.
try:
    MULTISELECT_WRAP_KW = {"wrap": True} \
        if "wrap" in inspect.signature(st.multiselect).parameters else {}
except Exception:
    MULTISELECT_WRAP_KW = {}


# ===========================================================================
# SECTION 1 — THE FORM REGISTRY
# ===========================================================================
# EVERYTHING about a tracked form lives in ONE entry below. To add a form you
# add one block here and change nothing else in this file. To stop tracking a
# form you delete its block. The lookup tables the rest of the app uses are
# DERIVED from this registry further down, so they can never disagree with it.
#
# Each entry:
#   name              What the form is called everywhere in the app and in the
#                     Google Sheet. Changing this orphans any Overrides or
#                     Operating Days rows keyed on the old name.
#   compliance        None = tracked and reported but never scored.
#                     {"quota": N, "interval": "daily"|"weekly"|"monthly"}
#                     = required N times per period.
#   location_fields   The form's internal field name(s) for its location
#                     question, in priority order. First one carrying an
#                     answer wins. These are JotForm's API names, not the
#                     labels people see. See the note below on duplicates.
#   location_source   "reusable dropdown" = fed by the master location table
#                     in JotForm, so its values match the master list.
#                     "static list"       = a frozen list maintained by hand
#                     on the form, which drifts and needs aliases.
#   detail            None, or a block describing the two people the form
#                     names. Detail forms get their own table in the report
#                     and their own sheet in the Excel export.
#
# WHY THE SAME FIELD NAME APPEARS UNDER SEVERAL FORMS:
# "location" shows up in a lot of these lists and that is NOT a duplicate.
# JotForm field names are scoped to one form, so "location" on Lot Audit and
# "location" on Site Audit are two completely unrelated questions that happen
# to share a name. What matters is that no name repeats WITHIN a single list,
# and _validate_registry() below enforces exactly that.
#
# WHY MOST FORMS LIST TWO LOCATION FIELDS:
# Forms that moved to the reusable dropdown got a NEW question for it, and
# the old plain-dropdown question was hidden rather than deleted, because
# deleting it would have destroyed the location on every historical
# submission. So the widget field is listed first (what people fill in today)
# and the retired field second (what older submissions carry). One report can
# then span both eras.
#
# Field names below were read live off each rendered form on 2026-09-14/15.
FORMS = {
    "250265807744158": {
        # Spelling fixed from "Arival" on 2026-11-18. Safe because no Overrides
        # rows used the old name (checked the sheet) and this form has no quota.
        "name": "AWEstruck Arrival/Departure",
        "compliance": None,
        "location_fields": ["location125", "location"],
        "location_source": "reusable dropdown",
        # Names pulled the same way Valet Driver Assessment / Shuttle / Uniform
        # Issuance do. Confirmed live: "Team Member Name" is field "teamMember",
        # "Conducted by" is field "conductedBy", "Date Conducted on" is
        # "dateConducted3". This form has no separate title field for the
        # evaluator (no "Assessor's Title" equivalent), so second_title is
        # empty rather than pointing at a field that doesn't exist.
        "detail": {
            "title": "AWEstruck Evaluations",
            "sheet": "AWEstruck Evaluations",
            "noun": "evaluation",
            "person_label": "team member",
            "person_header": "Team member",
            "second_header": "Conducted by",
            "fields": {
                "person":        ["teamMember"],
                "second_person": ["conductedBy"],
                "second_title":  [],
                "detail_date":   ["dateConducted3"],
            },
        },
    },
    "242047162465151": {
        "name": "Daily Huddle",
        "compliance": {"quota": 3, "interval": "daily"},
        "location_fields": ["location60", "location"],
        "location_source": "reusable dropdown",
        "detail": None,
    },
    "242746988998184": {
        "name": "Key Scrub",                       # "Key Scrub & Key Security Audit"
        "compliance": {"quota": 3, "interval": "daily"},
        "location_fields": ["location73", "location1"],
        "location_source": "reusable dropdown",
        "detail": None,
    },
    "223424048202040": {
        "name": "Lot Audit",                       # "Lot Audit Form"
        "compliance": {"quota": 3, "interval": "daily"},
        "location_fields": ["location"],
        "location_source": "static list",
        "detail": None,
    },
    "230404704539149": {
        "name": "Overnight Valet",                 # "Overnight Valet Checklist"
        "compliance": {"quota": 1, "interval": "daily"},
        "location_fields": ["location", "location39"],
        "location_source": "reusable dropdown",    # the widget IS q36_location here
        "detail": None,
    },
    "233176683727163": {
        # The form's own instructions say DAILY ("we at Evolution are requiring
        # that this Audit be completed DAILY by the Property Leaders"). It is
        # still scored WEEKLY here, deliberately: switching it would rewrite
        # every historical compliance number. To switch, change interval to
        # "daily" and expect the history to move.
        "name": "Parking/Work Area Hazard",
        "compliance": {"quota": 1, "interval": "weekly"},
        "location_fields": ["location"],
        "location_source": "static list",
        "detail": None,
    },
    "222195371011142": {
        "name": "Site Audit",
        "compliance": {"quota": 1, "interval": "monthly"},
        "location_fields": ["location74", "location"],
        "location_source": "reusable dropdown",
        "detail": None,
    },
    "262024177345151": {
        # Replaces the retired Valet Driver Assessment (230053977061151),
        # which JotForm now reports as unavailable. This clone RENAMED the
        # team member field from teamMember51 to teamMember, which is why
        # names came back blank until both spellings were listed.
        "name": "Valet Driver Assessment",         # "Valet Driver Assessment as of 8/16/2026"
        "compliance": None,
        "location_fields": ["location"],
        # Converted to the reusable dropdown around 2026-09-16. Confirmed live
        # against a 2026-09-20 submission: the field name itself did NOT
        # change (still "location"), only its backing data source did, so
        # location_fields needed no edit — just this flag.
        "location_source": "reusable dropdown",
        "detail": {
            "title": "Driver Assessments",
            "sheet": "Driver Assessments",
            "noun": "assessment",
            "person_label": "team member",
            "person_header": "Team member",
            "second_header": "Assessor",
            "fields": {
                "person":        ["teamMember", "teamMember51"],
                "second_person": ["assessorsName"],
                "second_title":  ["assessorsTitle"],
                "detail_date":   ["date", "date403", "dateSection73"],
            },
        },
    },
    "231726764166159": {
        # Built from the same template as the OLD valet assessment, so it uses
        # that form's field names (teamMember51, dateSection73) rather than the
        # new valet form's.
        #
        # Location moved onto the reusable dropdown on 2026-09-20. Confirmed
        # live: the widget's field is named "typeA" (JotForm auto-names
        # reusable-dropdown widgets this way; it happens to collide with the
        # unrelated "typeA" field on other forms, which is fine — field names
        # are scoped per form). "schoolOr2" is kept second so any submissions
        # from before the conversion still resolve. There is also now a
        # "Site Email" widget field (name "siteEmail") alongside it, matching
        # the "Select Site Email" pattern on other reusable-dropdown forms;
        # it isn't read anywhere, same as those other forms' email fields.
        "name": "Shuttle Driver Assessment",       # "EPGS Shuttle Driver Assessment"
        "compliance": None,
        "location_fields": ["typeA", "schoolOr2"],
        "location_source": "reusable dropdown",
        "detail": {
            "title": "Shuttle Assessments",
            "sheet": "Shuttle Assessments",
            "noun": "assessment",
            "person_label": "team member",
            "person_header": "Team member",
            "second_header": "Assessor",
            "fields": {
                "person":        ["teamMember51", "teamMember"],
                "second_person": ["assessorsName"],
                "second_title":  ["assessorsTitle"],
                "detail_date":   ["dateSection73", "date"],
            },
        },
    },
    "222574939747169": {
        "name": "Uniform Issuance Agreement",
        "compliance": None,
        "location_fields": ["location47", "location"],
        "location_source": "reusable dropdown",
        "detail": {
            "title": "Uniform Issuance",
            "sheet": "Uniform Issuance",
            "noun": "issuance",
            "person_label": "employee",
            "person_header": "Employee",
            "second_header": "Supervisor",
            "fields": {
                "person":        ["employeeName"],
                "second_person": ["supervisorName"],
                "second_title":  [],
                "detail_date":   ["date", "date44"],
            },
        },
    },
}

REUSABLE_DROPDOWN = "reusable dropdown"
STATIC_LIST = "static list"


def _validate_registry(forms):
    """Catch registry mistakes at startup instead of halfway through a report.

    A typo here fails in the most expensive way possible: the report runs, the
    numbers look plausible, and a location quietly reads 0%. Better to refuse
    to start.
    """
    problems = []
    seen_names = {}
    for form_id, cfg in forms.items():
        label = cfg.get("name") or form_id

        if not str(form_id).isdigit():
            problems.append(f"{label}: form id '{form_id}' is not numeric.")
        if not cfg.get("name"):
            problems.append(f"{form_id}: missing 'name'.")
        elif cfg["name"] in seen_names:
            problems.append(
                f"'{cfg['name']}' is used by two forms ({seen_names[cfg['name']]} "
                f"and {form_id}). Names must be unique."
            )
        else:
            seen_names[cfg["name"]] = form_id

        fields = cfg.get("location_fields") or []
        if not fields:
            problems.append(f"{label}: 'location_fields' is empty.")
        dupes = {f for f in fields if fields.count(f) > 1}
        if dupes:
            problems.append(
                f"{label}: location_fields repeats {sorted(dupes)} within the same "
                f"form. The same field can't be checked twice."
            )

        source = cfg.get("location_source")
        if source not in (REUSABLE_DROPDOWN, STATIC_LIST):
            problems.append(
                f"{label}: location_source must be '{REUSABLE_DROPDOWN}' or "
                f"'{STATIC_LIST}', got {source!r}."
            )

        rule = cfg.get("compliance")
        if rule is not None:
            if rule.get("interval") not in ("daily", "weekly", "monthly"):
                problems.append(f"{label}: interval must be daily, weekly or monthly.")
            if not isinstance(rule.get("quota"), int) or rule["quota"] < 0:
                problems.append(f"{label}: quota must be a whole number, 0 or more.")

        detail = cfg.get("detail")
        if detail is not None:
            for key in ("title", "sheet", "noun", "person_label", "person_header",
                        "second_header", "fields"):
                if key not in detail:
                    problems.append(f"{label}: detail block is missing '{key}'.")
            for key in ("person", "second_person", "second_title", "detail_date"):
                if key not in detail.get("fields", {}):
                    problems.append(f"{label}: detail fields is missing '{key}'.")

    sheets = [c["detail"]["sheet"] for c in forms.values() if c.get("detail")]
    for sheet in set(sheets):
        if sheets.count(sheet) > 1:
            problems.append(f"Two detail forms both want the Excel sheet '{sheet}'.")
        if len(sheet) > 31:
            problems.append(f"Excel sheet name '{sheet}' is over 31 characters.")

    return problems


REGISTRY_PROBLEMS = _validate_registry(FORMS)


# ---------------------------------------------------------------------------
# Derived lookups. Nothing below is edited by hand.
# ---------------------------------------------------------------------------
FORM_ID_TO_NAME = {fid: cfg["name"] for fid, cfg in FORMS.items()}
FORM_NAME_TO_ID = {cfg["name"]: fid for fid, cfg in FORMS.items()}
FORM_COMPLIANCE = {cfg["name"]: cfg["compliance"] for cfg in FORMS.values()}
FORM_ID_TO_LOCATION_FIELDS = {fid: list(cfg["location_fields"]) for fid, cfg in FORMS.items()}
FORMS_ON_REUSABLE_DROPDOWN = {
    cfg["name"] for cfg in FORMS.values() if cfg["location_source"] == REUSABLE_DROPDOWN
}
DETAIL_FORMS = {cfg["name"]: cfg["detail"] for cfg in FORMS.values() if cfg.get("detail")}
DETAIL_FORM_IDS = {fid: cfg["name"] for fid, cfg in FORMS.items() if cfg.get("detail")}

# Used only by the "Location field setup" debug tool, which scans for anything
# mentioning "location". Not used to decide which field's answer to pull.
LOCATION_MATCH_WORD = "location"


# ===========================================================================
# SECTION 1B — SHUTTLE INSPECTIONS
# ===========================================================================
# Shuttle inspections are NOT one form. Each shuttle site has its own "Daily
# Shuttle Inspection" form in JotForm, so the site comes from WHICH form was
# submitted, not from a location question. That's why these live in their own
# map instead of the FORMS registry above, and why they get their own box in
# the report rather than a chip in the Forms picker: the box shows up on its
# own whenever the selected locations include one of these sites.
#
# Rule: one inspection per operating day (Operating Days tab). A day counts
# once however many inspections it has, so two on Monday don't cover Tuesday.
#
# Form id -> master-list location name. Ids confirmed in JotForm 2026-11-18.
# Left out on purpose: Sky Beach Resort (253445471578163, site isn't on the
# master list), "Shuttle Log - MOKE" and the JW Marriott SpringTime 2025
# employee shuttle (both 2025 one-offs). To add a site, add one line here.
SHUTTLE_INSPECTION_FORMS = {
    "230815808234152": "TX - Intercontinental Houston Medical",
    "250963780419162": "CA - Hilton Costa Mesa",                 # "Hilton Orange County, Costa Mesa"
    "231015075916148": "TX - DoubleTree Houston",                # "DoubleTree Houston Greenway Plaza"
    "231875297830163": "IL - Waldorf Astoria Chicago",           # house car shuttle
    "250586907706163": "FL - HCC Ybor City Shuttle",             # GEM shuttle
    "232084621604146": "FL - JW Marriott Clearwater",            # Moke shuttle
    "260815322799162": "FL - Westin Sarasota",
    "262708613052150": "NC - Hotel Ballast",                     # Wilmington, NC
}
SHUTTLE_SITE_TO_FORM = {site: fid for fid, site in SHUTTLE_INSPECTION_FORMS.items()}
SHUTTLE_SHEET = "Shuttle Inspections"


# ===========================================================================
# SECTION 2 — DETAIL FORM COLUMNS
# ===========================================================================
# Extra columns added to raw rows for detail forms only. Kept in one list so
# they are easy to strip back out of the general Raw Data sheet.
DETAIL_COLUMNS = ["person", "second_person", "second_title", "detail_date"]


# ===========================================================================
# SECTION 3 — LOCATION NAMES
# ===========================================================================
# There is NO hardcoded location list in this file, by design. The master list
# is the "Locations" tab of the Google Sheet, which is kept in step with the
# JotForm master location table. If the sheet cannot be read, the app reports
# the failure instead of falling back to a stale copy.
MASTER_LOCATIONS = []

# Seed aliases. The live alias map lives in the "Aliases" tab of the Google
# Sheet and is edited from the Location Health tab; anything there overrides
# and extends what is below. These stay only as a floor so a brand new sheet
# still resolves the mismatches we already know about.
#
# The Austin entries matter: the master list spells it "TX - CY RI Austin
# Downtown" with ONE space after the dash. Earlier versions of this file used
# two. Both spellings map to the master version so old and new data land in
# the same bucket.
SEED_ALIASES = {
    "TX -  CY RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX -  CY & RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX - CY & RI Austin Downtown": "TX - CY RI Austin Downtown",
    "TX - Courtyard Residence Inn Austin DT": "TX - CY RI Austin Downtown",
    "FL - Hilton Tampa": "FL - Hilton Tampa Downtown",
    "FL - Embassy Suites Tampa": "FL - Embassy Tampa Downtown",
    # What the two static-dropdown forms actually emit today, read straight
    # off their live forms. The older "Embassy Suites Tampa" guess never fired
    # because the form says "Embassy DOWNTOWN Tampa".
    "FL - Embassy Downtown Tampa": "FL - Embassy Tampa Downtown",
    "LA - Ballys Hotel & Casino": "LA - Ballys Hotel Casino",
}

# The alias map actually in force for this run. It is a module-level dict that
# the app REPLACES THE CONTENTS OF (never rebinds) once the sheet has loaded.
#
# Why it works this way: normalize_location() is called from extract_row(),
# deep inside the submission loop, and threading an alias map down through
# every call just to reach it would touch every signature in the file. Under
# Streamlit's one-pass execution model there is exactly one active alias map
# per run, so a module-level dict is the honest representation of that.
ACTIVE_ALIASES = dict(SEED_ALIASES)


def set_active_aliases(mapping):
    """Point normalize_location() at a new alias map, in place."""
    ACTIVE_ALIASES.clear()
    ACTIVE_ALIASES.update(mapping or {})


def normalize_location(raw_value):
    """Map a raw submitted location string to its canonical master-list name.

    Falls back to the trimmed original when no alias applies, so unmapped or
    brand-new locations still show up in the health report rather than
    vanishing.

    Also decodes HTML entities ("&amp;" -> "&"). JotForm's raw API answers can
    come back HTML-encoded even when its own UI displays them decoded, which
    silently breaks exact-string matching — found via "FL - The Vinoy Resort
    &amp; Golf Club" failing to match "FL - The Vinoy Resort & Golf Club".
    """
    if raw_value is None:
        return None
    cleaned = html.unescape(str(raw_value).strip())
    return ACTIVE_ALIASES.get(cleaned, cleaned)


def suggest_match(value, known_locations, cutoff=0.72):
    """Best-guess master-list name for an unmatched submitted value.

    Used only to PROPOSE an alias in the Location Health tab. Nothing is ever
    applied automatically: two genuinely different sites can read as similar,
    and silently merging them would corrupt the numbers in a way nobody would
    notice. A human confirms every alias.
    """
    if not value or not known_locations:
        return None
    matches = difflib.get_close_matches(value, list(known_locations), n=1, cutoff=cutoff)
    return matches[0] if matches else None


# ===========================================================================
# SECTION 4 — QUOTAS AND OPERATING DAYS
# ===========================================================================
# Per-location overrides of quota and/or interval. Only locations that DIFFER
# from the form default are listed; anything absent uses the form default.
# These live in the "Overrides" tab and load at session start — the constant
# below is only the starting seed when no sheet is reachable.
LOCATION_COMPLIANCE_OVERRIDES = {}


def get_requirement_for(form_name, location, overrides=None):
    """Quota + interval for one (form, location), or None if not applicable.

    Resolution order: a location-specific override, then the form default.
    A quota of 0 is meaningful and is NOT the same as missing — it means "this
    location is not required to do this form at all".
    """
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    base = FORM_COMPLIANCE.get(form_name)
    override = overrides.get(form_name, {}).get(location)
    if override is None:
        return dict(base) if base else None

    quota = override.get("quota") if isinstance(override, dict) else override
    if quota is None:
        return dict(base) if base else None
    interval = (override.get("interval") if isinstance(override, dict) else None) \
        or (base or {}).get("interval") or "daily"
    return {"quota": quota, "interval": interval}


# Which days each location is open, as a set of weekday ints (0=Mon..6=Sun).
# Locations absent from this map are assumed open all seven days. Lives in the
# "Operating Days" tab.
LOCATION_OPERATING_DAYS = {}

WEEKDAY_COLUMNS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPERATING_DAYS_HEADER = ["Location"] + WEEKDAY_COLUMNS


def get_operating_days_for(location, operating_days=None):
    """Weekday set for a location, defaulting to all seven days."""
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS
    return operating_days.get(location, {0, 1, 2, 3, 4, 5, 6})


def operating_days_to_df(operating_days):
    """Operating-days map -> editable DataFrame (one row per location)."""
    rows = []
    for loc in sorted(operating_days):
        days = operating_days[loc]
        row = {"Location": loc}
        row.update({name: (i in days) for i, name in enumerate(WEEKDAY_COLUMNS)})
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=OPERATING_DAYS_HEADER)
    return pd.DataFrame(rows, columns=OPERATING_DAYS_HEADER)


def _truthy(val):
    """Accept the many ways a spreadsheet can spell 'yes'."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"true", "yes", "y", "1", "x", "✓", "checked"}


def df_to_operating_days(df):
    """Editable DataFrame -> operating-days map.

    A row with every day checked is dropped rather than stored: "open 7 days"
    is already the default, and keeping those rows would bloat the sheet and
    imply a restriction that isn't there.
    """
    result = {}
    if df is None or df.empty:
        return result
    for _, row in df.iterrows():
        loc = row.get("Location")
        if loc is None or not str(loc).strip():
            continue
        loc = html.unescape(str(loc).strip())
        days = {i for i, name in enumerate(WEEKDAY_COLUMNS) if _truthy(row.get(name))}
        if days and len(days) < 7:
            result[loc] = days
    return result


def _parse_quota(val):
    """Spreadsheet cell -> int quota, or None when the cell is blank.

    Blank means "no override, use the form default". Zero means "not required".
    Those are different, so a blank cell must never quietly become a 0.
    """
    if val is None:
        return None
    text = str(val).strip()
    if text == "":
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def overrides_to_df(overrides):
    """Nested overrides map -> flat editable DataFrame."""
    rows = []
    for form_name, locs in sorted(overrides.items()):
        for loc, val in sorted(locs.items()):
            quota = val.get("quota") if isinstance(val, dict) else val
            interval = (val.get("interval") if isinstance(val, dict) else None) \
                or (FORM_COMPLIANCE.get(form_name) or {}).get("interval") or "daily"
            rows.append({"Form": form_name, "Location": loc,
                         "Quota": quota, "Interval": interval})
    if not rows:
        return pd.DataFrame(columns=OVERRIDES_HEADER)
    return pd.DataFrame(rows, columns=OVERRIDES_HEADER)


def df_to_overrides(df, collect_skipped=False):
    """Flat editable DataFrame -> nested overrides map.

    Rows with a blank quota are skipped, and when `collect_skipped` is set the
    reason is returned so the UI can say which rows were ignored and why —
    a silently dropped row here reads downstream as a compliance failure.
    """
    result, skipped = {}, []
    if df is None or df.empty:
        return (result, skipped) if collect_skipped else result

    for _, row in df.iterrows():
        form_name = row.get("Form")
        loc = row.get("Location")
        if not form_name or not loc:
            continue
        form_name = str(form_name).strip()
        loc = html.unescape(str(loc).strip())
        if not form_name or not loc:
            continue

        quota = _parse_quota(row.get("Quota"))
        if quota is None:
            skipped.append(
                f'Ignored "{form_name}" / "{loc}": the Quota cell is blank. '
                f"Enter 0 if this location isn't required to do this form."
            )
            continue

        interval = str(row.get("Interval") or "").strip().lower()
        if interval not in {"daily", "weekly", "monthly"}:
            interval = (FORM_COMPLIANCE.get(form_name) or {}).get("interval") or "daily"

        result.setdefault(form_name, {})[loc] = {"quota": quota, "interval": interval}

    return (result, skipped) if collect_skipped else result


# ===========================================================================
# SECTION 5 — GOOGLE SHEET PERSISTENCE
# ===========================================================================
# Requires a Google Cloud service account with the Sheets API enabled, whose
# credentials are in Streamlit secrets under [gcp_service_account], and the
# target sheet shared (Editor) with that service account's email — the
# "client_email" value in those credentials.
#
# Four tabs are used in the same spreadsheet:
#   "Locations"       master site list
#   "Aliases"         submitted value -> master list name
#   "Overrides"       per (form, location) quota + interval
#   "Operating Days"  which days each site is open
GOOGLE_SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

OVERRIDES_HEADER = ["Form", "Location", "Quota", "Interval"]
OVERRIDES_TAB = "Overrides"
OPERATING_DAYS_TAB = "Operating Days"
LOCATIONS_TAB = "Locations"
ALIASES_TAB = "Aliases"

# Extra header spellings are accepted on read so a rename in the sheet doesn't
# silently blank out the whole dashboard.
LOCATIONS_HEADER = "Location List"
LOCATIONS_HEADER_ALIASES = {"location list", "location", "locations", "location name", "site"}

ALIASES_HEADER = ["Submitted Value", "Maps To"]


def _secret(key, default=None):
    """Read one Streamlit secret, returning `default` if there are none.

    st.secrets raises StreamlitSecretNotFoundError as soon as it is touched
    when no secrets.toml exists ANYWHERE — and `.get()` is not exempt, because
    it parses the file before it can fall back to a default. So on a fresh
    clone, a Codespace, or a devcontainer with no secrets file, reading a
    secret at import time crashes the whole app with a traceback instead of
    rendering a page that could tell you what is missing.

    Every secret read goes through here so the app always loads and can
    explain itself. Missing configuration is a normal state, not a crash.
    """
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def has_secrets():
    """True if a secrets.toml was found at all. Used for setup guidance."""
    try:
        st.secrets.get("__probe__", None)
        return True
    except Exception:
        return False


def get_gsheet_client():
    """Authenticated gspread client from the service account in secrets.

    Raises a plain-language error rather than a traceback when the
    credentials block is missing, which is the usual state on a fresh local
    checkout.
    """
    creds_block = _secret("gcp_service_account")
    if not creds_block:
        # KeyError specifically: every call site already catches it and shows
        # "No 'gcp_service_account' credentials found in st.secrets." Raising
        # anything else would escape those handlers as a traceback.
        raise KeyError("gcp_service_account")
    creds_dict = dict(creds_block)
    creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SHEETS_SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(sheet_url_or_key):
    """Open the target spreadsheet by URL or by key."""
    gc = get_gsheet_client()
    if sheet_url_or_key.startswith("http"):
        return gc.open_by_url(sheet_url_or_key)
    return gc.open_by_key(sheet_url_or_key)


def _get_or_create_worksheet(sh, title, rows=500, cols=10):
    """Return the named worksheet, creating it blank if it doesn't exist."""
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


def _get_overrides_worksheet(sh):
    """Return the overrides tab.

    Looks for a tab literally named "Overrides" first and only falls back to
    the first tab if that doesn't exist. The old code always used sheet1,
    which broke the moment the tabs got reordered in the sheet.
    """
    try:
        return sh.worksheet(OVERRIDES_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return sh.sheet1


def _values_to_df(values, wanted_header):
    """Raw get_all_values() output -> DataFrame with exactly `wanted_header`.

    Matches header names case-insensitively and ignores extra unlabelled
    columns. Using get_all_values() instead of get_all_records() matters:
    get_all_records() chokes on a data column whose header cell is blank,
    which is exactly what happens when someone types a value into column E
    without labelling it.

    Returns (df, extra_column_note).
    """
    if len(values) < 2:
        return pd.DataFrame(columns=wanted_header), None

    header = [str(h).strip() for h in values[0]]
    idx = {}
    for want in wanted_header:
        for i, h in enumerate(header):
            if h.lower() == want.lower():
                idx[want] = i
                break

    rows = []
    for raw in values[1:]:
        if not any(str(c).strip() for c in raw):
            continue
        rows.append({w: (raw[i] if i < len(raw) else "") for w, i in idx.items()})

    df = pd.DataFrame(rows, columns=wanted_header) if rows \
        else pd.DataFrame(columns=wanted_header)

    note = None
    labelled = {h.lower() for h in header if h}
    unlabelled_with_data = any(
        i >= len(header) or not str(header[i]).strip()
        for raw in values[1:] for i in range(len(raw))
        if str(raw[i]).strip()
    )
    extra = labelled - {w.lower() for w in wanted_header}
    if extra or unlabelled_with_data:
        note = (
            f"The \"{', '.join(sorted(extra)) if extra else 'unlabelled'}\" column(s) "
            f"in this tab aren't read by the dashboard. Anything typed there is ignored."
        )
    return df, note


def load_locations_from_sheet(sheet_url_or_key):
    """Read the Locations tab and return the master location list.

    Sheet order is PRESERVED rather than re-sorted, so deliberate choices like
    putting "Corporate" and "Other" at the bottom survive.

    html.unescape() is applied for the same reason it is in
    normalize_location(): a name copy-pasted out of JotForm can arrive
    carrying "&amp;" instead of "&", which would then never match a real
    submission.
    """
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


def load_aliases_from_sheet(sheet_url_or_key):
    """Read the Aliases tab -> {submitted value: master list name}.

    Rows where either cell is blank are skipped, and a row mapping a value to
    itself is skipped too — it does nothing and only adds noise.
    """
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, ALIASES_TAB, rows=500, cols=2)
    values = ws.get_all_values()
    if len(values) < 2:
        return {}

    df, _ = _values_to_df(values, ALIASES_HEADER)
    result = {}
    for _, row in df.iterrows():
        src = html.unescape(str(row.get("Submitted Value") or "").strip())
        dst = html.unescape(str(row.get("Maps To") or "").strip())
        if src and dst and src != dst:
            result[src] = dst
    return result


def save_aliases_to_sheet(aliases, sheet_url_or_key):
    """Overwrite the Aliases tab with the current alias map."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, ALIASES_TAB, rows=500, cols=2)
    ws.clear()
    rows = [[src, dst] for src, dst in sorted(aliases.items())]
    ws.update([ALIASES_HEADER] + rows)


def load_overrides_from_sheet(sheet_url_or_key):
    """Read the Overrides tab -> (overrides map, warnings)."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_overrides_worksheet(sh)
    values = ws.get_all_values()
    df, note = _values_to_df(values, OVERRIDES_HEADER)
    overrides, skipped = df_to_overrides(df, collect_skipped=True)
    warnings = list(skipped)
    if note:
        warnings.append(note)
    return overrides, warnings


def save_overrides_to_sheet(overrides, sheet_url_or_key):
    """Overwrite the Overrides tab with the current overrides map."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_overrides_worksheet(sh)
    df = overrides_to_df(overrides)
    ws.clear()
    ws.update([OVERRIDES_HEADER] + df.astype(object).where(pd.notna(df), "").values.tolist())


def load_operating_days_from_sheet(sheet_url_or_key):
    """Read the Operating Days tab -> operating-days map."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, OPERATING_DAYS_TAB)
    values = ws.get_all_values()
    df, _ = _values_to_df(values, OPERATING_DAYS_HEADER)
    return df_to_operating_days(df)


def save_operating_days_to_sheet(operating_days, sheet_url_or_key):
    """Overwrite the Operating Days tab with the current map."""
    sh = _open_spreadsheet(sheet_url_or_key)
    ws = _get_or_create_worksheet(sh, OPERATING_DAYS_TAB)
    df = operating_days_to_df(operating_days)
    ws.clear()
    ws.update([OPERATING_DAYS_HEADER] + df.astype(object).where(pd.notna(df), "").values.tolist())


# ===========================================================================
# SECTION 6 — ANSWER PARSING
# ===========================================================================
def _full_name(answer):
    """Flatten a JotForm Full Name widget answer into one string.

    Handles both the dict form ({"first": "Kai", "last": "Campbell"}) and a
    plain string, in case the field is ever changed to a text box. Returns
    None for blank so it shows as empty rather than the word "None".
    """
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


# Dates are stored ISO (YYYY-MM-DD) everywhere internally, because that is
# what the JotForm API's date filters expect and what makes plain string
# sorting come out chronological. They convert to US MM/DD/YYYY only at the
# moment they are displayed or exported — so call to_us_dates() LAST, after
# any sorting or filtering, never on data still being computed on.
US_DATE_COLUMNS = {
    "submitted_at": "%m/%d/%Y %I:%M %p",
    "detail_date": "%m/%d/%Y",
}


def to_us_dates(df):
    """Copy of `df` with its date columns re-rendered MM/DD/YYYY."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for col, fmt in US_DATE_COLUMNS.items():
        if col in out.columns:
            parsed = pd.to_datetime(out[col], errors="coerce")
            out[col] = parsed.dt.strftime(fmt).where(parsed.notna(), out[col])
    return out


# ===========================================================================
# SECTION 7 — JOTFORM API
# ===========================================================================
class JotFormError(Exception):
    """Raised when JotForm can't be reached or returns an error."""


_APIKEY_IN_URL = re.compile(r"(apiKey=)[^&\s\"']+", re.IGNORECASE)


def _redact(text, api_key=None):
    """Strip the API key out of anything shown to a user.

    requests puts the full request URL into its exception messages, and that
    URL carries `apiKey=...`. Those messages are rendered on screen by
    st.error(), so without this the live key is displayed to anyone looking at
    the dashboard, and pasted into any screenshot of an outage.
    """
    out = str(text)
    if api_key:
        out = out.replace(str(api_key), "***")
    return _APIKEY_IN_URL.sub(r"\1***", out)


def _api_get(url, params, what):
    """GET with retries on transient failures.

    502/503/504 are usually momentary hiccups on JotForm's end and are worth
    retrying. 400/401/403 will never be fixed by retrying, so those break out
    immediately rather than making the user wait through three attempts.

    Every message raised from here is redacted — see _redact().
    """
    max_retries = 3
    key = params.get("apiKey") if isinstance(params, dict) else None
    r = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            if attempt < max_retries:
                time.sleep(attempt * 2)
                continue
            raise JotFormError(f"Couldn't reach JotForm: {_redact(e, key)}")
        if r.ok:
            return r
        if r.status_code in (502, 503, 504) and attempt < max_retries:
            time.sleep(attempt * 2)
            continue
        break

    body = _redact(r.text[:500], key) if r is not None else ""
    raise JotFormError(
        f"JotForm API error while fetching {what} "
        f"(status {r.status_code if r is not None else '?'}) after "
        f"{max_retries} attempt(s): {body}"
    )


def get_form_questions(form_id, api_key):
    """Fetch a form's field/question definitions (not submissions)."""
    r = _api_get(
        f"https://api.jotform.com/form/{form_id}/questions",
        {"apiKey": api_key},
        f"field setup for '{FORM_ID_TO_NAME.get(form_id, form_id)}'",
    )
    return r.json()["content"]


def get_submissions(form_id, api_key, start_date=None, end_date=None, label=None):
    """Page through every submission for one form in the date range.

    `label` names the form in error messages when it isn't in FORMS (the
    per-site shuttle inspection forms).
    """
    label = label or FORM_ID_TO_NAME.get(form_id, form_id)
    all_submissions = []
    batch_size = 1000
    offset = 0
    while True:
        params = {"apiKey": api_key, "limit": batch_size, "offset": offset}
        if start_date or end_date:
            filters = {}
            if start_date:
                filters["created_at:gt"] = f"{start_date}T00:00:00"
            if end_date:
                filters["created_at:lt"] = f"{end_date}T23:59:59"
            params["filter"] = json.dumps(filters)

        r = _api_get(
            f"https://api.jotform.com/form/{form_id}/submissions",
            params,
            f"'{label}'",
        )
        submissions = r.json()["content"]
        all_submissions.extend(submissions)
        if len(submissions) < batch_size:
            break
        offset += batch_size
    return all_submissions


def extract_row(sub, form_id):
    """One submission -> one flat row, using this form's confirmed fields.

    NOTE: this row deliberately does NOT include a resolved "location" key —
    only "location_raw". Alias resolution happens later, over a whole
    DataFrame, via apply_location_aliases() / resolve_location() below, so
    that an alias saved after a pull still applies to that (cached) pull
    without another trip to JotForm. Any call site that needs the
    alias-resolved value must go through one of those two, never index
    row["location"] on what this function returns.
    """
    answers = sub.get("answers", {})
    row = {
        "form_id": form_id,
        "form_name": FORM_ID_TO_NAME.get(form_id, form_id),
        "submission_id": sub.get("id"),
        "submitted_at": sub.get("created_at"),
        "location_raw": None,
    }
    # Present-but-empty on every form, so the DataFrame has a stable shape
    # whether or not a detail form is in the selection.
    row.update({col: None for col in DETAIL_COLUMNS})

    by_name = {v.get("name"): v.get("answer", "") for v in answers.values()}

    def _first(candidates):
        """First candidate API name that actually carries an answer."""
        for name in candidates:
            val = by_name.get(name)
            if val not in (None, "", {}):
                return val
        return None

    raw_location = _first(FORM_ID_TO_LOCATION_FIELDS.get(form_id, []))
    if raw_location is not None:
        raw_text = raw_location if isinstance(raw_location, str) else str(raw_location)
        # location_raw keeps exactly what the form sent, before aliasing. That
        # is what the Location Health tab needs in order to tell you which
        # form is emitting a bad value — the aliased version has by then
        # already covered the problem up.
        # ONLY the raw value is stored here. Aliases are applied later, by
        # apply_location_aliases(), and deliberately NOT at extraction time:
        # extracted rows are cached, and an alias saved after a pull has to
        # affect that pull. Aliasing here would bake the old mapping into the
        # cache, so saving an alias would appear to do nothing until the
        # cache expired.
        row["location_raw"] = html.unescape(raw_text.strip())

    if form_id in DETAIL_FORM_IDS:
        fields = DETAIL_FORMS[DETAIL_FORM_IDS[form_id]]["fields"]
        row["person"] = _full_name(_first(fields["person"]))
        row["second_person"] = _full_name(_first(fields["second_person"]))
        title = _first(fields["second_title"])
        row["second_title"] = html.unescape(str(title).strip()) if title not in (None, "") else None
        row["detail_date"] = _date_answer(_first(fields["detail_date"]))

    return row


BASE_RAW_COLUMNS = ["form_id", "form_name", "submission_id", "submitted_at",
                    "location_raw", "location"]


# How long a pull stays cached, and how many distinct pulls are kept. Twenty
# minutes is a compromise: long enough that clicking around the report, the
# Location Health tab and the Excel export costs one trip to JotForm, short
# enough that a report re-run after lunch reflects the morning's submissions.
# "Force fresh pull" on the Report tab bypasses it whenever that isn't enough.
PULL_CACHE_TTL_SECONDS = 20 * 60
PULL_CACHE_MAX_ENTRIES = 6

# Forms are fetched concurrently. Kept deliberately low: JotForm rate-limits,
# and the win from 4 to 10 workers is small compared to the risk of getting
# throttled mid-report. The slowest single form (Lot Audit, ~90k submissions)
# sets the floor no matter how many workers there are.
PULL_MAX_WORKERS = 4


def _fetch_form_rows(form_id, api_key, start_date, end_date):
    """Every submission for ONE form, already flattened into rows.

    Pure: no Streamlit calls, so it is safe to run in a worker thread.
    """
    return [extract_row(sub, form_id)
            for sub in get_submissions(form_id, api_key, start_date, end_date)]


@st.cache_data(ttl=PULL_CACHE_TTL_SECONDS, max_entries=PULL_CACHE_MAX_ENTRIES,
               show_spinner=False)
def fetch_rows_cached(form_names, start_date, end_date, _api_key):
    """Cached, concurrent pull of every selected form.

    `form_names` must be a TUPLE — Streamlit hashes the arguments to key the
    cache and a list is unhashable. `_api_key`'s leading underscore tells
    Streamlit not to hash it, which keeps the key out of the cache key; that
    means changing the API key alone does not invalidate the cache, which is
    fine because a changed key means new credentials for the same data.

    Errors are NOT cached: if a JotFormError escapes, Streamlit discards the
    entry, so a failed pull doesn't wedge a bad result in place for 20
    minutes.
    """
    all_rows = []
    with ThreadPoolExecutor(max_workers=min(PULL_MAX_WORKERS, max(len(form_names), 1))) as pool:
        futures = {
            pool.submit(_fetch_form_rows, FORM_NAME_TO_ID[name], _api_key,
                        start_date, end_date): name
            for name in form_names
        }
        for future in as_completed(futures):
            # .result() re-raises inside the main thread, so a JotFormError
            # still surfaces to the user as a normal error rather than
            # disappearing into a worker.
            all_rows.extend(future.result())

    if not all_rows:
        return pd.DataFrame(columns=BASE_RAW_COLUMNS + DETAIL_COLUMNS)
    return pd.DataFrame(all_rows)


def resolve_location(raw_value, aliases):
    """Alias-resolved location for ONE raw value.

    This is the single-row equivalent of apply_location_aliases() (which
    works over a whole DataFrame column at once). Use this wherever a row
    dict straight out of extract_row() needs its location resolved —
    extract_row() itself never sets a "location" key, only "location_raw",
    so indexing row["location"] on its return value is always a bug.
    """
    if raw_value is None:
        return None
    mapping = aliases or {}
    return mapping.get(raw_value, raw_value)


def apply_location_aliases(df, aliases):
    """Set the `location` column from `location_raw` using the current aliases.

    Split out from extraction so that aliases apply to CACHED pulls too. Save
    an alias on the Location Health tab, re-run, and the fix lands without
    another trip to JotForm.
    """
    if df is None:
        return df
    out = df.copy()
    if out.empty:
        out["location"] = pd.Series(dtype=object)
        return out
    mapping = aliases or {}
    out["location"] = out["location_raw"].map(
        lambda v: None if v is None else mapping.get(v, v)
    )
    return out


def get_all_data(form_names, start_date, end_date, api_key, aliases=None, force=False):
    """Every submission for the selected forms, UNFILTERED by location.

    Filtering by location happens later, in the report. Pulling unfiltered
    means one API round trip serves both the compliance report and the
    Location Health tab — and, more importantly, the rows whose location
    doesn't match are still here to be counted rather than thrown away inside
    the fetch loop, which is how mismatches stayed invisible for so long.
    """
    if force:
        fetch_rows_cached.clear()
    df = fetch_rows_cached(tuple(form_names), start_date, end_date, api_key)
    return apply_location_aliases(df, aliases if aliases is not None else ACTIVE_ALIASES)


def filter_to_locations(df_raw, location_list):
    """Rows whose resolved location is in the selection."""
    if df_raw is None or df_raw.empty or not location_list:
        return df_raw
    return df_raw[df_raw["location"].isin(location_list)].copy()


def build_detail_table(df_raw, form_name):
    """One row per submission for a detail form, showing the people named.

    No compliance scoring — these forms have no quota, so this is a
    visibility report only.
    """
    columns = ["detail_date", "submitted_at", "location", "person",
               "second_person", "second_title", "submission_id"]
    if df_raw is None or df_raw.empty or "form_name" not in df_raw.columns:
        return pd.DataFrame(columns=columns)
    df = df_raw[df_raw["form_name"] == form_name].copy()
    if df.empty:
        return pd.DataFrame(columns=columns)
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return (df[columns]
            .sort_values(["location", "person", "detail_date"], na_position="last")
            .reset_index(drop=True))


def detail_display_names(form_name):
    """Column rename map for one detail form's table.

    Applied at DISPLAY time, after to_us_dates(), never before — US_DATE_COLUMNS
    keys off the internal column names, so renaming first would silently skip
    the date formatting.
    """
    cfg = DETAIL_FORMS.get(form_name, {})
    return {
        "person": cfg.get("person_header", "Person"),
        "second_person": cfg.get("second_header", "Second person"),
        "second_title": "Title",
        "detail_date": "Date",
        "submitted_at": "Submitted",
        "location": "Location",
        "submission_id": "Submission ID",
    }


# ===========================================================================
# SECTION 8 — LOCATION HEALTH
# ===========================================================================
def build_location_health(df_all, known_locations):
    """Diagnose location matching for one pull of data.

    Returns three tables:

      unmatched  values a form actually submitted that are NOT on the master
                 list, with how many submissions each is costing, which forms
                 produce it, and a suggested master-list name. These are the
                 submissions being thrown away.
      silent     master-list locations that returned nothing at all. Some are
                 genuine compliance gaps; some are sites the form's dropdown
                 can't even produce.
      by_form    per-form matched/unmatched counts, so it is obvious which
                 forms are healthy and which are drifting.
    """
    unmatched_cols = ["Submitted value", "Submissions", "Forms",
                      "Suggested master name", "Hidden characters"]
    silent_cols = ["Location"]
    by_form_cols = ["Form", "Submissions", "Matched", "Unmatched",
                    "Blank location", "Reusable dropdown"]

    if df_all is None or df_all.empty:
        return (pd.DataFrame(columns=unmatched_cols),
                pd.DataFrame(columns=silent_cols),
                pd.DataFrame(columns=by_form_cols))

    known = set(known_locations or [])
    df = df_all.copy()
    df["_matched"] = df["location"].isin(known) & df["location"].notna()
    df["_blank"] = df["location"].isna() | (df["location"].astype(str).str.strip() == "")

    # ---- Unmatched values ----
    bad = df[~df["_matched"] & ~df["_blank"]]
    rows = []
    for value, group in bad.groupby("location", dropna=True):
        raw_values = sorted(set(group["location_raw"].dropna().astype(str)))
        rows.append({
            "Submitted value": value,
            "Submissions": len(group),
            "Forms": ", ".join(sorted(set(group["form_name"]))),
            "Suggested master name": suggest_match(value, known) or "",
            # repr() of the pre-alias value exposes trailing spaces and
            # lookalike dashes, which are invisible in a normal table and are
            # a real cause of mismatches.
            "Hidden characters": repr(raw_values[0]) if raw_values else "",
        })
    unmatched = pd.DataFrame(rows, columns=unmatched_cols)
    if not unmatched.empty:
        unmatched = unmatched.sort_values("Submissions", ascending=False).reset_index(drop=True)

    # ---- Locations that reported nothing ----
    reported = set(df[df["_matched"]]["location"].dropna())
    silent = pd.DataFrame(sorted(known - reported), columns=silent_cols)

    # ---- Per-form health ----
    form_rows = []
    for form_name, group in df.groupby("form_name"):
        form_rows.append({
            "Form": form_name,
            "Submissions": len(group),
            "Matched": int(group["_matched"].sum()),
            "Unmatched": int((~group["_matched"] & ~group["_blank"]).sum()),
            "Blank location": int(group["_blank"].sum()),
            "Reusable dropdown": "Yes" if form_name in FORMS_ON_REUSABLE_DROPDOWN else "No",
        })
    by_form = pd.DataFrame(form_rows, columns=by_form_cols)
    if not by_form.empty:
        by_form = by_form.sort_values("Unmatched", ascending=False).reset_index(drop=True)

    return unmatched, silent, by_form


# ===========================================================================
# SECTION 9 — COMPLIANCE MATH
# ===========================================================================
def _period_count(interval, start_date, end_date, operating_days=None):
    """Number of compliance periods (days/weeks/months) in the date range.

    For "daily", `operating_days` (weekday ints, 0=Mon..6=Sun) restricts the
    count to days the location is actually open — a 5-day location gets ~5/7
    as many required days as a 7-day one over the same range. Weekly and
    monthly ignore operating_days: those quotas apply regardless of which
    specific days a location is open.
    """
    if interval == "daily":
        periods = pd.date_range(start_date, end_date, freq="D")
        if operating_days is not None and len(operating_days) < 7:
            periods = [d for d in periods if d.weekday() in operating_days]
    elif interval == "weekly":
        periods = pd.date_range(start_date, end_date, freq="W-MON")
        if pd.to_datetime(start_date).weekday() != 0:
            periods = periods.insert(0, pd.to_datetime(start_date))
    elif interval == "monthly":
        periods = pd.period_range(start_date, end_date, freq="M")
    else:
        periods = []
    return len(periods)


def compute_period_targets(df_raw, start_date, end_date, selected_forms, selected_locations,
                           overrides=None, operating_days=None, all_known_locations=None):
    """Compliance summary, one row per (form, location).

    `all_known_locations` is the full master list. It is used when nothing is
    selected in the location filter so a location with ZERO submissions still
    appears at 0% instead of disappearing. An older version fell back to only
    the locations present in the returned data, which hid exactly the sites
    you most need to see.
    """
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    if operating_days is None:
        operating_days = LOCATION_OPERATING_DAYS

    if selected_locations:
        all_locations = list(selected_locations)
    elif all_known_locations:
        all_locations = list(all_known_locations)
    elif df_raw is not None and not df_raw.empty:
        all_locations = sorted(df_raw["location"].dropna().unique())
    else:
        all_locations = []

    summary_rows = []
    compliance_forms = [f for f in selected_forms if FORM_COMPLIANCE.get(f)]

    # Cache period counts by (interval, operating-days-set). Weekly and
    # monthly don't depend on operating days, so they share one cache entry.
    period_count_cache = {}

    for form_name in compliance_forms:
        df_form = df_raw[df_raw["form_name"] == form_name] if (df_raw is not None and not df_raw.empty) else df_raw
        counts = df_form["location"].value_counts().to_dict() \
            if (df_form is not None and not df_form.empty) else {}

        for loc in all_locations:
            req = get_requirement_for(form_name, loc, overrides)
            if req is None:
                continue
            quota = req["quota"]
            interval = req["interval"]
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
            # form". Older code scored that as 0% complete, which both looked
            # like a failure and dragged down the leaderboard average.
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
    """Rank locations by average completion across the forms they must do.

    Rows with a target of 0 ("Not required") are excluded from the average
    rather than counted as a free 100%, so a location can't climb the board by
    having fewer requirements.
    """
    cols = ["location", "forms_tracked", "total_target", "total_actual",
            "overall_percent", "Badge", "Rank"]
    if compliance_summary is None or compliance_summary.empty:
        return pd.DataFrame(columns=cols)

    scored = compliance_summary[compliance_summary["target_total"] > 0]
    if scored.empty:
        return pd.DataFrame(columns=cols)

    leader = (scored.groupby("location")
              .agg(forms_tracked=("form_name", "count"),
                   total_target=("target_total", "sum"),
                   total_actual=("actual_total", "sum"),
                   overall_percent=("percent_complete", "mean"))
              .reset_index())
    leader["overall_percent"] = leader["overall_percent"].round(1)
    leader = leader.sort_values("overall_percent", ascending=False)

    def badge_row(row):
        p = row["overall_percent"]
        # Plain words; the report colors the cell by badge (see tint_cells).
        if p >= 100:
            return "Gold Star"
        elif p >= 90:
            return "Silver Star"
        elif p >= 75:
            return "Bronze Star"
        return "Needs Improvement"

    leader["Badge"] = leader.apply(badge_row, axis=1)
    leader["Rank"] = leader["overall_percent"].rank(method="min", ascending=False).astype(int)
    return leader[cols]


# ---------------------------------------------------------------------------
# Shuttle inspections (see SECTION 1B)
# ---------------------------------------------------------------------------
# How far back to look for each site's most recent inspection. One query per
# site covers both the report range and this window, so "Last inspection" can
# show a date from before the report started (that's how a site that stopped
# inspecting weeks ago stands out).
SHUTTLE_LOOKBACK_DAYS = 365
SHUTTLE_COLUMNS = ["site", "form_id", "submitted_at"]


def shuttle_sites_for(location_list):
    """Shuttle sites among these locations, in the order given."""
    return [loc for loc in location_list or [] if loc in SHUTTLE_SITE_TO_FORM]


def _fetch_shuttle_site(form_id, api_key, start_date, end_date):
    """One shuttle site's inspections in the window. Thread-safe (no st.*)."""
    site = SHUTTLE_INSPECTION_FORMS[form_id]
    subs = get_submissions(form_id, api_key, start_date, end_date,
                           label=f"Daily Shuttle Inspection ({site})")
    return [{"site": site, "form_id": form_id, "submitted_at": s.get("created_at")}
            for s in subs]


@st.cache_data(ttl=PULL_CACHE_TTL_SECONDS, max_entries=PULL_CACHE_MAX_ENTRIES,
               show_spinner=False)
def fetch_shuttle_cached(sites, start_date, end_date, _api_key):
    """Inspections for these shuttle sites between the two dates.

    `sites` must be a TUPLE so Streamlit can hash it; see fetch_rows_cached
    for why _api_key carries a leading underscore.
    """
    rows = []
    with ThreadPoolExecutor(max_workers=min(PULL_MAX_WORKERS, max(len(sites), 1))) as pool:
        futures = [pool.submit(_fetch_shuttle_site, SHUTTLE_SITE_TO_FORM[site],
                               _api_key, start_date, end_date)
                   for site in sites]
        for future in as_completed(futures):
            rows.extend(future.result())
    return pd.DataFrame(rows, columns=SHUTTLE_COLUMNS)


def get_shuttle_data(sites, start_date, end_date, api_key, force=False):
    """Shuttle inspections from SHUTTLE_LOOKBACK_DAYS before the end date (or
    the start date, if earlier) through the end date."""
    if force:
        fetch_shuttle_cached.clear()
    end = pd.to_datetime(end_date).date()
    lookback = min(pd.to_datetime(start_date).date(),
                   end - datetime.timedelta(days=SHUTTLE_LOOKBACK_DAYS))
    return fetch_shuttle_cached(tuple(sites), lookback, end, api_key)


def build_shuttle_table(df, sites, start_date, end_date, operating_days=None):
    """One row per shuttle site: days inspected out of days open in the range.

    A day counts once however many inspections it has. Days the site is closed
    (Operating Days tab) aren't required. Sites with misses sort to the top.
    """
    start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    all_days = pd.date_range(start_date, end_date, freq="D")
    rows = []
    for site in sites:
        site_rows = df[df["site"] == site] if df is not None and not df.empty else None
        dates = (site_rows["submitted_at"].dropna().astype(str).str[:10]
                 if site_rows is not None and not site_rows.empty
                 else pd.Series(dtype=str))
        in_range = dates[(dates >= start_s) & (dates <= end_s)]
        open_days = get_operating_days_for(site, operating_days)
        required = {d.strftime("%Y-%m-%d") for d in all_days if d.weekday() in open_days}
        covered = len(required & set(in_range))
        missing = len(required) - covered
        if not required:
            status = "Not open in range"
        elif missing == 0:
            status = "Met"
        else:
            status = f"Missing {missing} day" + ("" if missing == 1 else "s")
        last = dates.max() if len(dates) else None
        rows.append({
            "Site": site,
            "Status": status,
            "Days inspected": f"{covered} of {len(required)}",
            "Coverage": round(100 * covered / len(required), 1) if required else 100.0,
            "Inspections": int(len(in_range)),
            "Last inspection": (pd.to_datetime(last).strftime("%m/%d/%Y") if last
                                else f"None in the past {SHUTTLE_LOOKBACK_DAYS // 30} months"),
            "_missing": missing,
            "_required": len(required),
            "_covered": covered,
        })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values(["_missing", "Site"], ascending=[False, True]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Report display: readable column names, colored status, plurals
# ---------------------------------------------------------------------------
# Internal column -> what people see, in the order they see it. Used for the
# on-screen tables AND the Excel sheets, so both read the same. Computation
# keeps using the internal names; this is applied last.
COMPLIANCE_DISPLAY = {
    "form_name": "Form",
    "location": "Location",
    "status": "Status",
    "percent_complete": "% Complete",
    "actual_total": "Submitted",
    "target_total": "Required",
    "interval": "Frequency",
    "quota_per_period": "Per period",
    "periods_in_range": "Periods",
    "custom_quota": "Custom quota",
    "open_days_per_week": "Days open/week",
}
LEADERBOARD_DISPLAY = {
    "Rank": "Rank",
    "location": "Location",
    "Badge": "Badge",
    "overall_percent": "Overall",
    "forms_tracked": "Forms tracked",
    "total_target": "Required",
    "total_actual": "Submitted",
}
RAW_DISPLAY = {
    "form_name": "Form",
    "submitted_at": "Submitted",
    "location_raw": "Location (as entered)",
    "location": "Location",
    "submission_id": "Submission ID",
    "form_id": "Form ID",
}


def for_display(df, mapping):
    """`df` reordered and renamed per `mapping`; empty-safe."""
    if df is None or df.empty:
        return pd.DataFrame(columns=list(mapping.values()))
    cols = [c for c in mapping if c in df.columns]
    return df[cols].rename(columns=mapping).reset_index(drop=True)


# Cell tints by status/badge. Semi-transparent so the theme's own text color
# stays readable on top in both Light and Dark mode. The words differ too, so
# color is never the only signal.
_TINT_GOOD = "background-color: rgba(7, 148, 100, 0.22)"
_TINT_BAD = "background-color: rgba(224, 104, 43, 0.24)"
_TINT_MID = "background-color: rgba(59, 130, 246, 0.18)"
_TINT_OK = "background-color: rgba(187, 131, 0, 0.20)"
_TINT_NONE = "background-color: rgba(128, 128, 128, 0.16)"


def _tint_for(value):
    v = str(value or "")
    if v in ("Met", "Gold Star"):
        return _TINT_GOOD
    if v.startswith("Missing") or v == "Needs Improvement":
        return _TINT_BAD
    if v == "Silver Star":
        return _TINT_MID
    if v == "Bronze Star":
        return _TINT_OK
    if v in ("Not required", "Not open in range"):
        return _TINT_NONE
    return ""


def tint_cells(df, columns, percent_columns=()):
    """A pandas Styler with status/badge cells tinted and % columns formatted."""
    styler = df.style
    apply = getattr(styler, "map", None) or styler.applymap
    present = [c for c in columns if c in df.columns]
    if present:
        styler = apply(_tint_for, subset=present)
    pct = [c for c in percent_columns if c in df.columns]
    if pct:
        styler = styler.format({c: "{:.1f}%" for c in pct}, na_rep="")
    return styler


def percent_bar(label):
    """A 0-100 progress-bar column labelled like 33.3%."""
    return st.column_config.ProgressColumn(label, min_value=0, max_value=100, format="%.1f%%")


DEFAULT_EXCEL_NAME = "JotForm_Compliance_Report"

# The server runs on UTC; "today" for the date pickers and shortcuts means
# today in Eastern time, where EPGS operates from.
LOCAL_TZ = ZoneInfo("America/New_York")


def dt_now_local():
    """Current date and time in Eastern time."""
    return datetime.datetime.now(LOCAL_TZ)


def button_row(key):
    """A container that lays its buttons out side by side.

    Uses st.container(horizontal=True) (Streamlit 1.46+). On an older build it
    falls back to a plain container, where the buttons stack instead.
    """
    try:
        return st.container(horizontal=True, gap="small", key=key)
    except TypeError:
        return st.container(key=key)


def plural(n, word):
    """'1 location', '2 locations'."""
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def format_date_range(start_date, end_date):
    """'Nov 17 to Nov 18, 2026', or 'Nov 18, 2026' for a single day."""
    a, b = pd.to_datetime(start_date), pd.to_datetime(end_date)
    if a == b:
        return f"{a:%b} {a.day}, {a.year}"
    if a.year == b.year:
        return f"{a:%b} {a.day} to {b:%b} {b.day}, {b.year}"
    return f"{a:%b} {a.day}, {a.year} to {b:%b} {b.day}, {b.year}"


# ===========================================================================
# SECTION 10 — BRANDING
# ===========================================================================
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
    # Deeper teal for anything with white text on it (buttons, tags). White
    # on the brand teal above reads at about 2.3:1; on this one it's about
    # 4.9:1, which passes the usual 4.5:1 readability bar. Picked 2026-11-18.
    # Must match primaryColor in .streamlit/config.toml.
    "deep":       "#007F78",
    "deep_hover": "#00665F",
    "teal_text":  "#007F78",   # teal text and outlines on the page background
    "tint":       "rgba(0, 127, 120, 0.08)",   # outline-button hover fill
    "ring":       "rgba(0, 127, 120, 0.40)",   # keyboard focus ring
    "logo_text":  "#4F4F57",   # the logo's charcoal wordmark
}

# Dark-mode partners for the BRAND colors that only work on a white page.
# Viewers can pick System, Light or Dark in the app's settings menu, and
# shade() below hands the CSS both values so the browser shows the right one
# for whichever theme is active. Values match Streamlit's own dark theme so
# the custom pieces blend in. Teal and white need no partner: they read fine
# on both backgrounds.
BRAND_DARK = {
    "slate":      "#EDEBE8",   # headings (Streamlit dark body text)
    "gray_text":  "#B8B3A8",   # body copy
    "teal_pale":  "#12302E",   # tints and fills (deep teal)
    "border":     "#4A4742",   # borders and dividers
    "gray_bg":    "#2D2B27",   # subtle backgrounds (Streamlit dark sidebar)
    "teal_text":  "#2CCFC4",   # deep teal is too dark to read on a dark page
    "tint":       "rgba(0, 192, 181, 0.14)",
    "ring":       "rgba(44, 207, 196, 0.50)",
    "logo_text":  "#EDEBE8",   # wordmark turns light so it shows on dark
}


def shade(name: str) -> str:
    """CSS color that is BRAND[name] in light mode and BRAND_DARK[name] in dark.

    Uses CSS light-dark(), which follows the color-scheme Streamlit sets on the
    app for the active theme.
    """
    return f"light-dark({BRAND[name]}, {BRAND_DARK[name]})"

# THE LOGO
# assets/epgs-vertical.svg is the vertical EPGS logo, converted from the
# brand PDF (assets/EPGS-VerticalLogo-Color.pdf) on 2026-11-18. It is drawn
# straight into the page as inline SVG rather than as an <img>, for one
# reason: Dark mode. Its charcoal wordmark paths carry the class
# "epgs-logo-text", and the CSS below recolors them with shade("logo_text"),
# so the words turn light on a dark background. An <img> can't be restyled by
# the page's CSS. The SVG has no <style> block of its own, so nothing in it
# can collide with the page's styles.
#
# Other paths are tried in order after it (raster fallbacks go through the
# old data-URI <img> route). If none exists the header shows the title alone
# instead of crashing.
LOGO_CANDIDATES = [
    "assets/epgs-vertical.svg",                      # current: vertical, inline SVG
    "assets/EPGS-HorizontalLogo-Color.svg",
    "assets/EPGS-HorizontalLogo-Color-50.png",       # raster, white background
]
LOGO_HEIGHT_PX = 88
LOGO_PATH = next((p for p in LOGO_CANDIDATES if os.path.exists(p)), None)


@st.cache_data(show_spinner=False)
def load_inline_svg(path: str) -> str:
    """An SVG file's markup with class="epgs-logo" on its root element."""
    with open(path, encoding="utf-8") as fh:
        svg = fh.read().strip()
    return svg.replace("<svg ", '<svg class="epgs-logo" ', 1)

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


st.markdown(
    f"""
    <style>
    /* ---- Headings ----
    Section headings (st.subheader = h3, "####" = h4) are a step smaller than
    Streamlit's defaults so the dashboard title leads. The .stApp prefix
    out-ranks Streamlit's own heading rules. */
    h1, h2, h3 {{ color: {shade('slate')}; }}
    .stApp [data-testid="stMarkdownContainer"] h3 {{ font-size: 1.3rem; }}
    .stApp [data-testid="stMarkdownContainer"] h4 {{ font-size: 1.1rem; }}
    .stApp [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h3 {{
        font-size: 1.05rem;
    }}
    .epgs-label {{ font-size: 0.875rem; margin: 0 0 0.25rem 0; }}

    /* ---- App header band ---- */
    .epgs-header {{
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 1.25rem;
        padding: 0.4rem 0 0.9rem 0;
        border-bottom: 3px solid {BRAND['teal']};
        margin-bottom: 1.2rem;
    }}
    .epgs-header .epgs-logo {{
        height: {LOGO_HEIGHT_PX}px;
        width: auto;
        flex: 0 0 auto;
        display: block;
    }}
    .epgs-logo .epgs-logo-text {{ fill: {shade('logo_text')}; }}
    .epgs-header .epgs-title {{
        font-size: 1.6rem;
        font-weight: 700;
        color: {shade('slate')};
        line-height: 1.1;
    }}
    .epgs-header .epgs-sub {{
        font-size: 0.85rem;
        color: {shade('gray_text')};
    }}

    /* Multiselect tags, checkboxes and the tab underline take their teal from
    primaryColor in .streamlit/config.toml. The selected tab's LABEL is set
    here because deep teal text is too dark to read in Dark mode. */
    [data-testid="stTab"][aria-selected="true"],
    [data-testid="stTab"][aria-selected="true"] p {{
        color: {shade('teal_text')};
    }}

    /* ---- Buttons ----
    Two styles. SOLID deep teal with white text for the main action in each
    place (anything with type="primary": Run Report, Download Excel File, the
    Save alias button). OUTLINE for everything else: no fill, deep teal
    border and text (a lighter teal in Dark mode so it stays readable).
    kind^= matches Streamlit's kind attribute, which is "primary" or
    "secondary" on regular and download buttons and "primaryFormSubmit" or
    "secondaryFormSubmit" inside forms. */
    .stButton > button p,
    .stDownloadButton > button p,
    .stFormSubmitButton > button p {{
        font-weight: 600;
    }}
    .stButton > button[kind^="primary"],
    .stDownloadButton > button[kind^="primary"],
    .stFormSubmitButton > button[kind^="primary"] {{
        background-color: {BRAND['deep']};
        border: 1px solid {BRAND['deep']};
        color: #FFFFFF;
    }}
    .stButton > button[kind^="primary"]:hover,
    .stButton > button[kind^="primary"]:active,
    .stDownloadButton > button[kind^="primary"]:hover,
    .stDownloadButton > button[kind^="primary"]:active,
    .stFormSubmitButton > button[kind^="primary"]:hover,
    .stFormSubmitButton > button[kind^="primary"]:active {{
        background-color: {BRAND['deep_hover']};
        border-color: {BRAND['deep_hover']};
        color: #FFFFFF;
    }}
    .stButton > button[kind^="secondary"],
    .stDownloadButton > button[kind^="secondary"],
    .stFormSubmitButton > button[kind^="secondary"] {{
        background-color: transparent;
        border: 1px solid {shade('teal_text')};
        color: {shade('teal_text')};
    }}
    .stButton > button[kind^="secondary"]:hover,
    .stButton > button[kind^="secondary"]:active,
    .stDownloadButton > button[kind^="secondary"]:hover,
    .stDownloadButton > button[kind^="secondary"]:active,
    .stFormSubmitButton > button[kind^="secondary"]:hover,
    .stFormSubmitButton > button[kind^="secondary"]:active {{
        background-color: {shade('tint')};
        border-color: {shade('teal_text')};
        color: {shade('teal_text')};
    }}

    /* ---- Focus state ----
    After a click Streamlit restyles a focused button in the theme's primary
    color, which would put teal text on a teal button. These keep each style's
    own colors and draw a soft teal ring instead. */
    .stButton > button[kind^="primary"]:focus,
    .stButton > button[kind^="primary"]:focus-visible,
    .stDownloadButton > button[kind^="primary"]:focus,
    .stDownloadButton > button[kind^="primary"]:focus-visible,
    .stFormSubmitButton > button[kind^="primary"]:focus,
    .stFormSubmitButton > button[kind^="primary"]:focus-visible {{
        background-color: {BRAND['deep']} !important;
        border-color: {BRAND['deep']} !important;
        color: #FFFFFF !important;
    }}
    .stButton > button[kind^="secondary"]:focus,
    .stButton > button[kind^="secondary"]:focus-visible,
    .stDownloadButton > button[kind^="secondary"]:focus,
    .stDownloadButton > button[kind^="secondary"]:focus-visible,
    .stFormSubmitButton > button[kind^="secondary"]:focus,
    .stFormSubmitButton > button[kind^="secondary"]:focus-visible {{
        background-color: transparent !important;
        border-color: {shade('teal_text')} !important;
        color: {shade('teal_text')} !important;
    }}
    .stButton > button:focus-visible,
    .stDownloadButton > button:focus-visible,
    .stFormSubmitButton > button:focus-visible {{
        box-shadow: 0 0 0 3px {shade('ring')} !important;
        outline: none !important;
    }}

    /* ---- KPI card row ----
    Lets the four summary cards wrap onto a second line when the page is
    narrow instead of squeezing them until the labels and numbers cut off. */
    .st-key-kpi_row [data-testid="stHorizontalBlock"] {{ flex-wrap: wrap; }}
    .st-key-kpi_row [data-testid="stColumn"] {{
        flex: 1 1 190px !important;
        min-width: 190px !important;
    }}

    /* ---- KPI metric cards ---- */
    [data-testid="stMetric"] {{
        background-color: {shade('teal_pale')};
        border-left: 4px solid {BRAND['teal']};
        border-radius: 4px;
        padding: 0.75rem 1rem;
    }}
    [data-testid="stMetricLabel"] {{ color: {shade('gray_text')}; }}
    [data-testid="stMetricValue"] {{ color: {shade('slate')}; }}

    /* ---- Sidebar ---- */
    [data-testid="stSidebar"] {{
        background-color: {shade('gray_bg')};
        border-right: 1px solid {shade('border')};
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

# st.logo() is deliberately NOT used: it shrinks the image to fit the
# sidebar's top corner, where a vertical logo's wording becomes unreadable.
_logo_html = ""
if LOGO_PATH:
    try:
        if LOGO_PATH.endswith(".svg"):
            _logo_html = load_inline_svg(LOGO_PATH)
        else:
            _logo_html = (
                f'<img class="epgs-logo" src="{load_logo_data_uri(LOGO_PATH)}" '
                f'alt="Evolution Parking &amp; Guest Services">'
            )
    except Exception:
        _logo_html = ""

st.markdown(
    f"""
    <div class="epgs-header">
      {_logo_html}
      <div>
        <div class="epgs-title">JotForm Compliance Dashboard</div>
        <div class="epgs-sub">Submission quotas across every EPGS parking and valet location</div>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ===========================================================================
# SECTION 11 — SESSION SETUP: CREDENTIALS, ADMIN GATE, SHEET LOAD
# ===========================================================================
api_key = _secret("API_KEY", "")

# Hides the admin tabs from everyone except whoever knows the password. Set it
# in Streamlit secrets as ADMIN_PASSWORD.
#
# HONEST LIMITS: this keeps settings out of reach of people using the
# dashboard normally, which is the actual goal. It is NOT a security boundary
# against someone determined — anyone who can open the app is talking to the
# same server, and the Google Sheet is protected by its own sharing settings,
# not by this. For real access control, restrict who can open the app at all
# via Streamlit Cloud's viewer settings.
#
# If ADMIN_PASSWORD isn't set, the app falls back to unlocking with ?admin=1
# on the URL — convenient, but anyone who sees you do it can repeat it.
ADMIN_PASSWORD = _secret("ADMIN_PASSWORD", "")


def _get_query_params():
    """st.query_params on modern Streamlit, experimental on older builds."""
    try:
        return {k: (v[0] if isinstance(v, list) else v) for k, v in st.query_params.items()}
    except Exception:
        try:
            return {k: (v[0] if isinstance(v, list) else v)
                    for k, v in st.experimental_get_query_params().items()}
        except Exception:
            return {}


if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = (
        not ADMIN_PASSWORD and str(_get_query_params().get("admin", "")) == "1"
    )

if "overrides_sheet_url" not in st.session_state:
    st.session_state["overrides_sheet_url"] = _secret("overrides_sheet_url", "")


def load_everything_from_sheet(ref, messages):
    """Populate session state from the Google Sheet. Returns True on success.

    Each tab is loaded independently so one bad tab doesn't take the others
    down with it.
    """
    ok = True
    try:
        locs = load_locations_from_sheet(ref)
        if locs:
            st.session_state["locations"] = locs
            st.session_state["locations_source"] = f'Google Sheet, "{LOCATIONS_TAB}" tab'
        else:
            ok = False
            messages.append(
                f'The "{LOCATIONS_TAB}" tab is empty. There is no built-in location '
                f"list to fall back on, so nothing can be scored until it's filled in."
            )
    except Exception as e:
        ok = False
        messages.append(f"Couldn't load the location list from the Google Sheet ({e}).")

    try:
        sheet_aliases = load_aliases_from_sheet(ref)
        merged = dict(SEED_ALIASES)
        merged.update(sheet_aliases)
        st.session_state["location_aliases"] = merged
        st.session_state["aliases_from_sheet"] = len(sheet_aliases)
    except Exception as e:
        messages.append(f"Couldn't load aliases ({e}). Using the built-in seed list only.")

    try:
        ovr, warn = load_overrides_from_sheet(ref)
        st.session_state["location_overrides"] = ovr
        messages.extend(warn)
    except Exception as e:
        messages.append(f"Couldn't load quota overrides ({e}).")

    try:
        st.session_state["location_operating_days"] = load_operating_days_from_sheet(ref)
    except Exception as e:
        messages.append(f"Couldn't load operating days ({e}).")

    return ok


# How long the Google Sheet data is trusted before this app automatically
# re-reads it, on top of the manual "⬇️ Reload from Google Sheet" button.
#
# Without this, the sheet was only ever read ONCE per browser session — the
# block below ran on the session's first script execution and then never
# again, because it's gated on a key that, once set, stays set for the rest
# of that session. Someone editing the "Locations", "Aliases", "Overrides"
# or "Operating Days" tab directly in the sheet had no way to reach an
# already-open tab short of clicking Reload by hand, which is exactly what
# was being reported: "the sheet is not automatically reloading." Ten
# minutes matches the spirit of PULL_CACHE_TTL_SECONDS (the same tradeoff
# for JotForm pulls, just for sheet data instead): frequent enough that an
# edit shows up within one coffee break, rare enough that it doesn't hammer
# the Sheets API on every rerun.
SHEET_CACHE_TTL_SECONDS = 10 * 60

if "sheet_loaded" not in st.session_state:
    st.session_state["sheet_loaded"] = True
    st.session_state["sheet_loaded_at"] = time.time()
    st.session_state["sheet_messages"] = []
    st.session_state["locations"] = list(MASTER_LOCATIONS)
    st.session_state["locations_source"] = "not loaded"
    st.session_state["location_aliases"] = dict(SEED_ALIASES)
    st.session_state["aliases_from_sheet"] = 0
    st.session_state["location_overrides"] = copy.deepcopy(LOCATION_COMPLIANCE_OVERRIDES)
    st.session_state["location_operating_days"] = copy.deepcopy(LOCATION_OPERATING_DAYS)

    _ref = st.session_state["overrides_sheet_url"]
    if _ref:
        load_everything_from_sheet(_ref, st.session_state["sheet_messages"])
    else:
        st.session_state["sheet_messages"].append(
            "No Google Sheet is configured, so there are no locations to report on. "
            "Set `overrides_sheet_url` in secrets, or paste the sheet URL in Settings."
        )
elif (time.time() - st.session_state.get("sheet_loaded_at", 0)) > SHEET_CACHE_TTL_SECONDS:
    # The periodic reload itself. Deliberately does NOT reset session state
    # to defaults first (unlike the first-load branch above) — if the sheet
    # is briefly unreachable, load_everything_from_sheet() only overwrites
    # each tab's session-state entry on that tab's own success, so the
    # session keeps whatever good data it already had instead of going
    # blank on a transient Sheets API hiccup.
    _ref = st.session_state.get("overrides_sheet_url")
    if _ref:
        _auto_messages = []
        load_everything_from_sheet(_ref, _auto_messages)
        st.session_state["sheet_messages"] = _auto_messages
    st.session_state["sheet_loaded_at"] = time.time()

# Streamlit won't let us write to a widget's session-state key after the
# widget has been created, so the buttons that change the location list set
# this flag and rerun. Applying it HERE, before the multiselect exists, is
# what makes Reload actually repopulate the picker.
if st.session_state.pop("_reload_location_widget", False):
    st.session_state["selected_locations_widget"] = list(st.session_state["locations"])

locations = st.session_state["locations"]

# Point the extractor at this session's alias map before anything is parsed.
set_active_aliases(st.session_state["location_aliases"])


def stashed_rows():
    """The last pull, with the CURRENT aliases applied.

    The pull itself is expensive and cached; aliasing is cheap and is redone
    on every render. That split is what lets an alias saved on the Location
    Health tab change the numbers straight away.
    """
    df = st.session_state.get("df_all")
    if df is None:
        return None
    return apply_location_aliases(df, st.session_state.get("location_aliases", {}))


def reload_from_sheet():
    """Re-read every tab and refresh the widgets that depend on them."""
    messages = []
    ref = st.session_state.get("overrides_sheet_url")
    if not ref:
        st.session_state["sheet_messages"] = ["No Google Sheet URL or ID is set."]
        return
    load_everything_from_sheet(ref, messages)
    st.session_state["sheet_messages"] = messages
    st.session_state["sheet_loaded_at"] = time.time()
    st.session_state["_reload_location_widget"] = True


# ---------------------------------------------------------------------------
# Sidebar: identity, admin gate, sheet status
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Data source")
    st.caption(f"Locations: **{st.session_state.get('locations_source')}** ({len(locations)})")
    st.caption(
        f"Aliases: **{len(st.session_state['location_aliases'])}** in force "
        f"({st.session_state.get('aliases_from_sheet', 0)} from the sheet)"
    )
    if st.button("🔄 Reload from Google Sheet", key="sidebar_reload"):
        reload_from_sheet()
        st.rerun()
    st.caption(
        f"Also auto-reloads every {SHEET_CACHE_TTL_SECONDS // 60} min in an open "
        f"tab. Use this button for an edit you need to see right away."
    )

    st.divider()
    st.markdown("### Admin")
    if st.session_state["is_admin"]:
        st.success("Admin tools unlocked")
        if st.button("Lock admin tools"):
            st.session_state["is_admin"] = False
            st.rerun()
    elif ADMIN_PASSWORD:
        _pw = st.text_input("Admin password", type="password", key="admin_pw")
        if st.button("Unlock"):
            if _pw == ADMIN_PASSWORD:
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    else:
        st.caption(
            "No ADMIN_PASSWORD is set in secrets, so admin tools unlock with "
            "`?admin=1` on the end of the URL. Set a password to lock them properly."
        )

is_admin = st.session_state["is_admin"]

if not api_key:
    api_key = st.sidebar.text_input("JotForm API Key", type="password")

# Sheet diagnostics are configuration problems a normal viewer can't act on,
# so they're admin-only — except a missing location list, which stops the
# whole dashboard and everyone needs to know about it.
if not has_secrets():
    st.warning(
        "**No secrets file found, so this app has nothing to connect to.** "
        "That is normal on a fresh clone, a Codespace or a devcontainer.\n\n"
        "Create `.streamlit/secrets.toml` next to `main.py` (it is gitignored) "
        "and copy `.streamlit/secrets.toml.example` into it, or set them under "
        "Settings → Secrets in Streamlit Cloud. Full walkthrough in "
        "`docs/STAGING.md`.",
        icon="🔑",
    )

if REGISTRY_PROBLEMS:
    st.error(
        "**The form registry at the top of `main.py` has a problem.** Reports "
        "may be wrong until it's fixed:\n\n"
        + "\n".join(f"- {p}" for p in REGISTRY_PROBLEMS)
    )

if not locations:
    st.error(
        "No locations are loaded, so nothing can be scored. "
        + ("Check the Settings tab." if is_admin else "Ask an admin to check the Google Sheet.")
    )
if is_admin:
    for msg in st.session_state.get("sheet_messages", []):
        st.info(msg, icon="ℹ️")


# ===========================================================================
# SECTION 12 — TABS
# ===========================================================================
TAB_LABELS = ["📊 Report", "🩺 Location Health"]
if is_admin:
    TAB_LABELS += ["⚙️ Settings", "🔧 Debug"]
_tabs = st.tabs(TAB_LABELS)
tab_report, tab_health = _tabs[0], _tabs[1]
tab_settings = _tabs[2] if is_admin else None
tab_debug = _tabs[3] if is_admin else None


# ---------------------------------------------------------------------------
# Shared controls (rendered inside the Report tab, used by Health and Debug)
# ---------------------------------------------------------------------------
with tab_report:
    st.markdown("#### What to pull")
    form_names = list(FORM_NAME_TO_ID.keys())
    # Full width, own row. `wrap` lets the chips flow onto several lines
    # instead of hiding behind an overflow icon when all forms are selected.
    selected_forms = st.multiselect(
        "Forms", options=form_names, default=form_names, key="selected_forms_widget",
        **MULTISELECT_WRAP_KW,
    )

    # ---- Dates ----
    # "Today" means today in Eastern time. The server runs on UTC, so a plain
    # date.today() (what the date pickers used to default to) rolled over to
    # tomorrow every evening after 7 or 8 pm Eastern.
    def _today():
        return dt_now_local().date()

    if "start_date_widget" not in st.session_state:
        st.session_state["start_date_widget"] = _today()
    if "end_date_widget" not in st.session_state:
        st.session_state["end_date_widget"] = _today()

    # Shortcut buttons set both pickers. Callbacks, for the same reason as the
    # location buttons below: a widget's own key can only be written before
    # the widget is created on the next run.
    def _set_dates(kind):
        t = _today()
        if kind == "yesterday":
            s = e = t - datetime.timedelta(days=1)
        elif kind == "last7":
            s, e = t - datetime.timedelta(days=6), t
        elif kind == "month":
            s, e = t.replace(day=1), t
        else:
            s = e = t
        st.session_state["start_date_widget"] = s
        st.session_state["end_date_widget"] = e

    st.markdown('<p class="epgs-label">Dates</p>', unsafe_allow_html=True)
    with button_row("date_shortcuts"):
        st.button("Today", key="dates_today_btn", on_click=_set_dates, args=("today",))
        st.button("Yesterday", key="dates_yesterday_btn", on_click=_set_dates, args=("yesterday",))
        st.button("Last 7 days", key="dates_last7_btn", on_click=_set_dates, args=("last7",))
        st.button("This month", key="dates_month_btn", on_click=_set_dates, args=("month",))

    d1, d2 = st.columns(2)
    with d1:
        start_date = st.date_input("Start date", key="start_date_widget", **DATE_FMT)
    with d2:
        end_date = st.date_input("End date", key="end_date_widget", **DATE_FMT)

    # A keyed multiselect ignores `default` on every rerun after the first, so
    # if this ever gets cleared it stays cleared. Seed it explicitly once.
    if "selected_locations_widget" not in st.session_state:
        st.session_state["selected_locations_widget"] = list(locations)

    selected_locations = st.multiselect(
        "Locations", options=locations, key="selected_locations_widget",
    )

    # These run as button CALLBACKS, which fire before the next script run
    # starts — i.e. before the multiselect above is re-created. That is the
    # only safe window in which Streamlit will let us write to a widget's own
    # session-state key. Writing inline raises StreamlitAPIException, and
    # calling st.rerun() inside a callback raises too.
    def _select_all_locations():
        st.session_state["selected_locations_widget"] = list(st.session_state["locations"])

    def _clear_locations():
        st.session_state["selected_locations_widget"] = []

    with button_row("location_buttons"):
        st.button("Select all", key="select_all_locs_btn", on_click=_select_all_locations)
        st.button("Clear", key="clear_locs_btn", on_click=_clear_locations)

    if not selected_locations:
        st.caption("No locations selected, so the report covers all of them.")
    st.caption(
        f"Shuttle inspections show up on their own when your locations include one "
        f"of the {len(SHUTTLE_INSPECTION_FORMS)} shuttle sites."
    )

    st.divider()
    r1, r2 = st.columns([1, 3], vertical_alignment="center")
    with r1:
        run_clicked = st.button("Run Report", type="primary", key="run_report_btn")
    with r2:
        force_fresh = st.checkbox(
            "Force fresh pull",
            key="force_fresh",
            help=f"A pull is reused for {PULL_CACHE_TTL_SECONDS // 60} minutes, so "
                 f"re-running the same forms and dates is instant. Tick this to go "
                 f"back to JotForm anyway, for example right after someone submits.",
        )

    # ---- Run ----
    if run_clicked:
        if not api_key:
            st.error("A JotForm API key is required.")
        elif start_date > end_date:
            st.error("Start date is after end date.")
        elif not selected_forms:
            st.error("Select at least one form.")
        else:
            try:
                with st.spinner(f"Pulling {plural(len(selected_forms), 'form')} from JotForm..."):
                    df_all = get_all_data(
                        selected_forms, start_date, end_date, api_key,
                        aliases=st.session_state["location_aliases"],
                        force=force_fresh,
                    )
                # Stashed so the Location Health tab can report on the same
                # pull instead of making its own round trip to the API.
                st.session_state["df_all"] = df_all
                st.session_state["report_context"] = {
                    "start_date": start_date,
                    "end_date": end_date,
                    "forms": list(selected_forms),
                    "locations": list(selected_locations),
                }
            except JotFormError as e:
                st.error(str(e))
                st.session_state.pop("df_all", None)

            # Shuttle inspections are a separate pull, only when the selection
            # includes a shuttle site. A failure here is shown inside the
            # shuttle box and never blocks the main report.
            _shuttle_sites = shuttle_sites_for(selected_locations or locations)
            st.session_state["shuttle_df"] = None
            st.session_state["shuttle_error"] = None
            if _shuttle_sites and "df_all" in st.session_state:
                st.session_state["report_context"]["shuttle_sites"] = _shuttle_sites
                try:
                    with st.spinner(f"Pulling shuttle inspections for "
                                    f"{plural(len(_shuttle_sites), 'site')}..."):
                        st.session_state["shuttle_df"] = get_shuttle_data(
                            _shuttle_sites, start_date, end_date, api_key, force=force_fresh,
                        )
                except JotFormError as e:
                    st.session_state["shuttle_error"] = str(e)

    # ---- Results ----
    # Aliases are re-applied on every render rather than trusted from the
    # stashed pull, so saving an alias on the Location Health tab updates the
    # numbers immediately, with no re-run and no second trip to JotForm.
    df_all = stashed_rows()
    if df_all is not None:
        ctx = st.session_state.get("report_context", {})
        ctx_locations = ctx.get("locations") or []
        ctx_start = ctx.get("start_date", start_date)
        ctx_end = ctx.get("end_date", end_date)
        ctx_forms = ctx.get("forms", selected_forms)
        operating_days = st.session_state.get("location_operating_days", LOCATION_OPERATING_DAYS)
        df_raw = filter_to_locations(df_all, ctx_locations) if ctx_locations else df_all

        compliance_summary = compute_period_targets(
            df_raw, ctx_start, ctx_end, ctx_forms, ctx_locations,
            overrides=st.session_state.get("location_overrides", LOCATION_COMPLIANCE_OVERRIDES),
            operating_days=operating_days,
            all_known_locations=locations,
        )
        leaderboard = leaderboard_with_badges(compliance_summary)

        unmatched_count = int((~df_all["location"].isin(locations) & df_all["location"].notna()).sum()) \
            if not df_all.empty else 0

        # ---- Build every table first, so the Download button can sit near the
        # top of the results. Display names are applied here and the Excel
        # sheets reuse the same tables, so both read the same.
        compliance_view = for_display(compliance_summary, COMPLIANCE_DISPLAY)
        leaderboard_view = for_display(leaderboard, LEADERBOARD_DISPLAY)

        detail_views = []   # (config, table, caption) for detail forms with rows
        detail_exports = {}
        for _detail_name, _cfg in DETAIL_FORMS.items():
            _details = build_detail_table(df_raw, _detail_name)
            _display = to_us_dates(_details).rename(columns=detail_display_names(_detail_name))
            detail_exports[_cfg["sheet"]] = _display
            if _details.empty:
                continue
            _caption = (
                f"{plural(len(_details), _cfg['noun'])} from "
                f"{plural(_details['person'].nunique(), _cfg['person_label'])} at "
                f"{plural(_details['location'].nunique(), 'location')}. Names are typed "
                f"in on the form, so one person can show up under more than one spelling."
            )
            detail_views.append((_cfg, _display, _caption))

        # The detail columns are blank on every other form, so they're
        # stripped from Raw Data. to_us_dates() runs last, after sorting and
        # filtering, so those used the underlying ISO values.
        raw_view = for_display(
            to_us_dates(df_raw) if df_raw is not None and not df_raw.empty else df_raw,
            RAW_DISPLAY,
        )

        shuttle_sites = ctx.get("shuttle_sites") or []
        shuttle_error = st.session_state.get("shuttle_error")
        shuttle_table = None
        if shuttle_sites and not shuttle_error and st.session_state.get("shuttle_df") is not None:
            shuttle_table = build_shuttle_table(
                st.session_state["shuttle_df"], shuttle_sites, ctx_start, ctx_end, operating_days,
            )
        shuttle_view = (shuttle_table.drop(columns=["_missing", "_required", "_covered"])
                        if shuttle_table is not None and not shuttle_table.empty else None)

        if df_raw is not None and not df_raw.empty:
            submission_count = (df_raw.groupby(["form_name", "location"]).size()
                                .reset_index(name="Submissions")
                                .rename(columns={"form_name": "Form", "location": "Location"}))
        else:
            submission_count = pd.DataFrame(columns=["Form", "Location", "Submissions"])

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            compliance_view.to_excel(writer, index=False, sheet_name="Compliance Summary")
            leaderboard_view.to_excel(writer, index=False, sheet_name="Leaderboard")
            raw_view.to_excel(writer, index=False, sheet_name="Raw Data")
            for _sheet_name, _table in detail_exports.items():
                _table.to_excel(writer, index=False, sheet_name=_sheet_name)
            if shuttle_view is not None:
                shuttle_view.to_excel(writer, index=False, sheet_name=SHUTTLE_SHEET)
            submission_count.to_excel(writer, index=False, sheet_name="Submission Count")

        # ---- Render ----
        st.divider()
        n_locations = len(ctx_locations) if ctx_locations else len(locations)
        st.markdown(
            f"**Results** · {format_date_range(ctx_start, ctx_end)} · "
            f"{plural(n_locations, 'location')} · {plural(len(ctx_forms), 'form')}"
        )

        if df_all.empty:
            st.warning(
                "No submissions were returned for this selection. The summary below "
                "still lists every expected location at 0%."
            )

        scored = compliance_summary[compliance_summary["target_total"] > 0] \
            if not compliance_summary.empty else compliance_summary
        with st.container(key="kpi_row"):
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Submissions pulled, all sites", f"{len(df_all):,}")
            m2.metric("Locations reported",
                      f"{compliance_summary['location'].nunique() if not compliance_summary.empty else 0}")
            m3.metric("Requirements met",
                      f"{(scored['status'] == 'Met').sum() if not scored.empty else 0} / {len(scored)}")
            m4.metric("Average completion",
                      f"{scored['percent_complete'].mean():.1f}%" if not scored.empty else "n/a")

        fn_col, dl_col = st.columns([2, 3], vertical_alignment="bottom")
        with fn_col:
            _name = st.text_input("Excel filename", value=DEFAULT_EXCEL_NAME, key="excel_filename")
        _name = (_name or "").strip() or DEFAULT_EXCEL_NAME
        if _name.lower().endswith(".xlsx"):
            _name = _name[:-5]
        with dl_col:
            st.download_button(
                label="Download Excel File",
                data=output.getvalue(),
                file_name=f"{_name}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                key="download_excel_btn",
            )

        if unmatched_count:
            st.warning(
                f"{plural(unmatched_count, 'submission')} in this pull "
                f"{'has' if unmatched_count == 1 else 'have'} a location that "
                f"isn't on the master list, so they are not counted anywhere below. "
                f"The Location Health tab shows which values and can fix them.",
                icon="⚠️",
            )

        st.subheader("Compliance Summary")
        st.dataframe(
            tint_cells(compliance_view, ["Status"], percent_columns=["% Complete"]),
            column_config={"% Complete": percent_bar("% Complete")},
            **TABLE,
        )

        st.subheader("Leaderboard")
        st.dataframe(
            tint_cells(leaderboard_view, ["Badge"], percent_columns=["Overall"]),
            column_config={"Overall": percent_bar("Overall")},
            **TABLE,
        )

        for _cfg, _display, _caption in detail_views:
            st.subheader(_cfg["title"])
            st.caption(_caption)
            st.dataframe(_display, **TABLE)

        if shuttle_sites:
            st.subheader("Shuttle Inspections")
            if shuttle_error:
                st.warning(f"Couldn't pull shuttle inspections: {shuttle_error}")
            elif shuttle_table is None or shuttle_table.empty:
                st.caption("No shuttle inspection data for this run. Try running the report again.")
            else:
                _req = shuttle_table[shuttle_table["_required"] > 0]
                _met = int((_req["_missing"] == 0).sum())
                st.caption(
                    f"One inspection per operating day at each shuttle site. "
                    f"{_met} of {plural(len(_req), 'site')} inspected every day "
                    f"({int(_req['_covered'].sum())} of {plural(int(_req['_required'].sum()), 'site-day')} "
                    f"covered). Last inspection can be before this date range."
                )
                st.dataframe(
                    tint_cells(shuttle_view, ["Status"], percent_columns=["Coverage"]),
                    column_config={"Coverage": percent_bar("Coverage")},
                    **TABLE,
                )

        with st.expander(f"Raw Data ({plural(len(raw_view), 'row')})"):
            st.dataframe(raw_view, **TABLE)


# ---------------------------------------------------------------------------
# Location Health
# ---------------------------------------------------------------------------
with tab_health:
    st.markdown("#### Location health")
    st.caption(
        "Reads the most recent report pull. Every submission whose location "
        "doesn't match the master list is counted here rather than silently "
        "discarded, and an alias can be saved straight to the Google Sheet "
        "without a code change or a deploy."
    )

    df_all = stashed_rows()
    if df_all is None:
        st.info("Run a report first, then come back. This tab reports on that same pull.")
    else:
        unmatched, silent, by_form = build_location_health(df_all, locations)

        h1, h2, h3 = st.columns(3)
        h1.metric("Submissions dropped", f"{int(unmatched['Submissions'].sum()) if not unmatched.empty else 0:,}")
        h2.metric("Values not matching", f"{len(unmatched)}")
        h3.metric("Sites with no activity", f"{len(silent)} / {len(locations)}")

        st.divider()
        st.markdown("##### Values that don't match the master list")
        if unmatched.empty:
            st.success("Every submitted location resolves to a master-list name.")
        else:
            st.caption(
                "Each of these is costing you submissions. Pick the master-list "
                "name it should map to and save the alias. The suggestion is a "
                "starting point, not a decision — confirm it before saving."
            )
            st.dataframe(unmatched, **TABLE)

            if is_admin and not locations:
                st.warning(
                    "The master location list is empty, so there is nothing to map "
                    "these values onto yet. Load the Locations tab first."
                )
            elif is_admin:
                with st.form("add_alias_form"):
                    a1, a2 = st.columns(2)
                    with a1:
                        bad_value = st.selectbox(
                            "Submitted value", options=list(unmatched["Submitted value"]),
                        )
                    with a2:
                        suggested = unmatched.loc[
                            unmatched["Submitted value"] == bad_value, "Suggested master name"
                        ]
                        suggested = suggested.iloc[0] if len(suggested) and suggested.iloc[0] else None
                        default_idx = locations.index(suggested) if suggested in locations else 0
                        maps_to = st.selectbox(
                            "Should count as", options=locations, index=default_idx,
                        )
                    submitted = st.form_submit_button("Save alias to Google Sheet", type="primary")

                if submitted:
                    ref = st.session_state.get("overrides_sheet_url")
                    if not ref:
                        st.error("No Google Sheet is configured, so there's nowhere to save this.")
                    elif bad_value == maps_to:
                        st.error("That value already matches. No alias needed.")
                    else:
                        try:
                            # The whole map is written back, seeds included, so
                            # the sheet is the complete picture of what's in
                            # force rather than a partial diff against code.
                            current = dict(st.session_state["location_aliases"])
                            current[bad_value] = maps_to
                            save_aliases_to_sheet(current, ref)
                            st.session_state["location_aliases"] = current
                            st.session_state["aliases_from_sheet"] = len(current)
                            set_active_aliases(current)
                            st.success(
                                f'Saved: "{bad_value}" now counts as "{maps_to}". '
                                f"Re-run the report to see it applied."
                            )
                        except gspread.exceptions.SpreadsheetNotFound:
                            st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                        except gspread.exceptions.APIError as e:
                            st.error(f"Google Sheets API error: {e}")
                        except KeyError:
                            st.error("No 'gcp_service_account' credentials found in st.secrets.")
            else:
                st.caption("Admin access is needed to save an alias.")

        st.divider()
        st.markdown("##### Per-form health")
        st.caption(
            "Forms on the reusable dropdown take their values from the master "
            "location table, so they shouldn't drift. A form with unmatched "
            "values and a 'No' in the last column is drifting because its "
            "dropdown is a frozen hand-maintained list."
        )
        st.dataframe(by_form, **TABLE)

        st.divider()
        st.markdown("##### Locations with no submissions in this range")
        st.caption(
            "Two different things look identical here: a site that genuinely "
            "isn't submitting, and a site whose name isn't in a form's dropdown "
            "at all, so nobody there can submit. Check the form's dropdown "
            "before treating one of these as a compliance failure."
        )
        st.dataframe(silent, **TABLE)


# ---------------------------------------------------------------------------
# Settings (admin)
# ---------------------------------------------------------------------------
if tab_settings is not None:
    with tab_settings:
        st.markdown("#### Settings")

        with st.expander("🔗 Google Sheet connection", expanded=not st.session_state.get("overrides_sheet_url")):
            st.session_state["overrides_sheet_url"] = st.text_input(
                "Google Sheet URL or ID",
                value=st.session_state["overrides_sheet_url"],
                help="The sheet must be shared (Editor access) with your service account's "
                     "email, found as 'client_email' in st.secrets['gcp_service_account'].",
            )
            st.caption(
                f'Tabs used: "{LOCATIONS_TAB}", "{ALIASES_TAB}", '
                f'"{OVERRIDES_TAB}", "{OPERATING_DAYS_TAB}". Missing tabs are created blank.'
            )
            if st.button("🔄 Reload everything from the sheet", key="settings_reload"):
                reload_from_sheet()
                st.rerun()

        # ---- Locations ----
        with st.expander("📍 Location list", expanded=False):
            st.caption(
                f"Loaded from **{st.session_state.get('locations_source')}**, "
                f"{len(locations)} locations. This list and the JotForm master "
                f"location table are meant to match each other. Edit here or "
                f'directly in the "{LOCATIONS_TAB}" tab.'
            )
            # dtype=object matters: pd.DataFrame({"Location": []}) infers
            # float64 on an empty list, and st.data_editor then refuses to pair
            # a text column with a float column. That is exactly the state a
            # fresh install is in before the sheet has loaded.
            loc_edited_df = st.data_editor(
                pd.DataFrame({"Location": pd.Series(locations, dtype=object)}),
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

            if st.button("⬆️ Save locations to Google Sheet", key="save_locs_btn"):
                if not st.session_state.get("overrides_sheet_url"):
                    st.error("Set the Google Sheet URL or ID first.")
                else:
                    try:
                        save_locations_to_sheet(_edited, st.session_state["overrides_sheet_url"])
                        st.session_state["locations"] = _edited
                        st.session_state["locations_source"] = f'Google Sheet, "{LOCATIONS_TAB}" tab'
                        st.session_state["_reload_location_widget"] = True
                        st.success(f"Saved {len(_edited)} locations.")
                        st.rerun()
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

        # ---- Aliases ----
        with st.expander("🔀 Location aliases", expanded=False):
            st.caption(
                "Maps what a form actually submits to the master-list name it "
                "should count as. Needed because Lot Audit and Parking/Work Area "
                "Hazard are still on frozen static dropdowns. Once those two "
                "forms move to the reusable dropdown, most of these become "
                "unnecessary. Editing here is the same as using the Location "
                "Health tab, just in bulk."
            )
            alias_df = pd.DataFrame(
                [{"Submitted Value": k, "Maps To": v}
                 for k, v in sorted(st.session_state["location_aliases"].items())],
                columns=ALIASES_HEADER,
            )
            alias_edited = st.data_editor(
                alias_df,
                num_rows="dynamic",
                key="aliases_editor",
                column_config={
                    "Submitted Value": st.column_config.TextColumn("Submitted Value", required=True),
                    "Maps To": st.column_config.SelectboxColumn(
                        "Maps To", options=locations, required=True
                    ) if locations else st.column_config.TextColumn("Maps To", required=True),
                },
                **FILL,
            )
            new_aliases = {}
            for _, row in alias_edited.iterrows():
                src = html.unescape(str(row.get("Submitted Value") or "").strip())
                dst = html.unescape(str(row.get("Maps To") or "").strip())
                if src and dst and src != dst:
                    new_aliases[src] = dst

            if st.button("⬆️ Save aliases to Google Sheet", key="save_aliases_btn"):
                if not st.session_state.get("overrides_sheet_url"):
                    st.error("Set the Google Sheet URL or ID first.")
                else:
                    try:
                        save_aliases_to_sheet(new_aliases, st.session_state["overrides_sheet_url"])
                        st.session_state["location_aliases"] = new_aliases
                        st.session_state["aliases_from_sheet"] = len(new_aliases)
                        set_active_aliases(new_aliases)
                        st.success(f"Saved {len(new_aliases)} aliases.")
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

        # ---- Quota overrides ----
        compliance_form_names = [f for f in FORM_NAME_TO_ID if FORM_COMPLIANCE.get(f)]
        with st.expander("⚙️ Location quota overrides", expanded=False):
            st.caption(
                "A different quota (and, if needed, interval) for specific "
                "locations. Anything not listed uses the form's standard quota. "
                "**Leave Quota blank and the row is ignored** — enter 0 to mean "
                "'this location doesn't have to do this form at all'."
            )
            edited_df = st.data_editor(
                overrides_to_df(st.session_state["location_overrides"]),
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
            st.session_state["location_overrides"] = _ovr
            for msg in _skipped:
                st.warning(msg)

            if st.button("⬆️ Save overrides to Google Sheet", key="save_ovr_btn"):
                if not st.session_state["overrides_sheet_url"]:
                    st.error("Set the Google Sheet URL or ID first.")
                else:
                    try:
                        save_overrides_to_sheet(
                            st.session_state["location_overrides"],
                            st.session_state["overrides_sheet_url"],
                        )
                        st.success("Saved overrides to the Google Sheet.")
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

            with st.expander("Backup as a local file instead"):
                cc, cd = st.columns(2)
                with cc:
                    st.download_button(
                        "Save overrides to file",
                        data=json.dumps(st.session_state["location_overrides"], indent=2),
                        file_name="location_quota_overrides.json",
                        mime="application/json",
                    )
                with cd:
                    uploaded_overrides = st.file_uploader(
                        "Load overrides from file", type=["json"], key="overrides_uploader"
                    )
                    if uploaded_overrides is not None:
                        try:
                            st.session_state["location_overrides"] = json.load(uploaded_overrides)
                            st.success("Overrides loaded. Expand this section to review them.")
                        except (json.JSONDecodeError, ValueError):
                            st.error("That file doesn't look like a valid overrides JSON export.")

        # ---- Operating days ----
        with st.expander("📅 Location operating days (5/6-day locations)", expanded=False):
            st.caption(
                "Only add a location here if it's NOT open all 7 days a week. "
                "Check the days it IS open. Locations left off are assumed open "
                "every day. This only affects daily-quota forms; weekly and "
                "monthly quotas are unchanged."
            )
            days_edited_df = st.data_editor(
                operating_days_to_df(st.session_state["location_operating_days"]),
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
            st.session_state["location_operating_days"] = df_to_operating_days(days_edited_df)

            if st.button("⬆️ Save operating days to Google Sheet", key="save_days_btn"):
                if not st.session_state.get("overrides_sheet_url"):
                    st.error("Set the Google Sheet URL or ID first.")
                else:
                    try:
                        save_operating_days_to_sheet(
                            st.session_state["location_operating_days"],
                            st.session_state["overrides_sheet_url"],
                        )
                        st.success("Saved operating days to the Google Sheet.")
                    except gspread.exceptions.SpreadsheetNotFound:
                        st.error("Sheet not found. Check the URL/ID and that it's shared with the service account.")
                    except gspread.exceptions.APIError as e:
                        st.error(f"Google Sheets API error: {e}")
                    except KeyError:
                        st.error("No 'gcp_service_account' credentials found in st.secrets.")

            with st.expander("Backup as a local file instead"):
                cg, ch = st.columns(2)
                with cg:
                    st.download_button(
                        "Save operating days to file",
                        data=json.dumps(
                            {loc: sorted(days) for loc, days
                             in st.session_state["location_operating_days"].items()},
                            indent=2,
                        ),
                        file_name="location_operating_days.json",
                        mime="application/json",
                    )
                with ch:
                    uploaded_days = st.file_uploader(
                        "Load operating days from file", type=["json"], key="operating_days_uploader"
                    )
                    if uploaded_days is not None:
                        try:
                            raw = json.load(uploaded_days)
                            st.session_state["location_operating_days"] = {
                                loc: set(days) for loc, days in raw.items()
                            }
                            st.success("Operating days loaded. Expand this section to review them.")
                        except (json.JSONDecodeError, ValueError, AttributeError):
                            st.error("That file doesn't look like a valid operating-days JSON export.")

        # ---- Reference ----
        with st.expander("📖 Tracked forms reference", expanded=False):
            st.caption(
                "Which forms are pulled, how they're scored, and where each one "
                "gets its location field. Forms not on the reusable dropdown are "
                "the ones that need aliases."
            )
            ref_rows = []
            for fid, fname in FORM_ID_TO_NAME.items():
                rule = FORM_COMPLIANCE.get(fname)
                ref_rows.append({
                    "Form": fname,
                    "Form ID": fid,
                    "Scored": f"{rule['quota']}x {rule['interval']}" if rule else "Not scored",
                    "Location field(s)": ", ".join(FORM_ID_TO_LOCATION_FIELDS.get(fid, [])),
                    "Reusable dropdown": "Yes" if fname in FORMS_ON_REUSABLE_DROPDOWN else "No",
                    "Names pulled": "Yes" if fname in DETAIL_FORMS else "No",
                })
            st.dataframe(pd.DataFrame(ref_rows), **TABLE)


# ---------------------------------------------------------------------------
# Debug (admin)
# ---------------------------------------------------------------------------
if tab_debug is not None:
    with tab_debug:
        st.markdown("#### Debug tools")
        st.caption(
            "These hit the JotForm API directly using the forms and dates "
            "selected on the Report tab."
        )

        _forms = st.session_state.get("selected_forms_widget", [])
        _start = st.session_state.get("report_context", {}).get("start_date")
        _end = st.session_state.get("report_context", {}).get("end_date")

        if st.button("Location match check"):
            if not api_key:
                st.error("A JotForm API key is required.")
            elif not _forms:
                st.error("Select at least one form on the Report tab.")
            else:
                st.caption(
                    "Every DISTINCT location value extract_row() produces for the "
                    "selected forms and dates, ignoring the location filter, so "
                    "near-invisible mismatches (stray spaces, lookalike dashes) "
                    "are visible."
                )
                try:
                    _current_aliases = st.session_state.get("location_aliases", {})
                    for form_id in [FORM_NAME_TO_ID[n] for n in _forms]:
                        subs = get_submissions(form_id, api_key, _start, _end)
                        st.write(f"### {FORM_ID_TO_NAME[form_id]} — {len(subs)} submission(s)")
                        if not subs:
                            st.write("⚠️ No submissions returned at all for this form and date range")
                            continue
                        # FIX: extract_row() only ever sets "location_raw", never
                        # "location" — that key is added later, over a whole
                        # DataFrame, by apply_location_aliases(). Indexing
                        # ["location"] here on a single extract_row() dict raised
                        # KeyError on every call. resolve_location() does the
                        # same alias lookup apply_location_aliases() does, just
                        # for one raw value at a time.
                        extracted = [
                            resolve_location(extract_row(s, form_id)["location_raw"], _current_aliases)
                            for s in subs
                        ]
                        counts = pd.Series(extracted).value_counts(dropna=False)
                        rows = []
                        for loc_val, count in counts.items():
                            rows.append({
                                "extracted_location": loc_val if loc_val is not None else "(blank/None)",
                                "repr (shows hidden chars)": repr(loc_val),
                                "count": count,
                                "in_current_location_list": (loc_val in locations) if loc_val else False,
                            })
                        st.dataframe(pd.DataFrame(rows), **TABLE)

                        unmatched = {v: int(c) for v, c in counts.items()
                                     if isinstance(v, str) and v and v not in locations}
                        if unmatched:
                            st.error(
                                f"❌ {sum(unmatched.values())} submission(s) across "
                                f"{len(unmatched)} name(s) are being DROPPED."
                            )
                            st.write(unmatched)
                        else:
                            st.success("✅ Every submitted location maps to the list.")
                        matched = {v for v in counts.index if isinstance(v, str) and v in locations}
                        silent = sorted(set(locations) - matched)
                        st.write(f"**Zero submissions ({len(silent)} of {len(locations)}):**")
                        st.write(silent or "None — every location reported.")
                except JotFormError as e:
                    st.error(str(e))

        if st.button("Field names"):
            if not api_key:
                st.error("A JotForm API key is required.")
            elif not _forms:
                st.error("Select at least one form on the Report tab.")
            else:
                try:
                    _current_aliases = st.session_state.get("location_aliases", {})
                    for form_id in [FORM_NAME_TO_ID[n] for n in _forms]:
                        subs = get_submissions(form_id, api_key, _start, _end)
                        st.write(f"### {FORM_ID_TO_NAME[form_id]}")
                        st.write(f"{len(subs)} submission(s) returned for this date range.")
                        if subs:
                            first = subs[0]
                            st.write(
                                f"Newest submission shown below — ID {first.get('id')}, "
                                f"submitted {first.get('created_at')}"
                            )
                            st.write({
                                v.get("name"): {"label": v.get("text"), "answer": v.get("answer")}
                                for v in first["answers"].values()
                            })
                            row = extract_row(first, form_id)
                            # FIX: same missing-key bug as "Location match check"
                            # above — row["location"] never exists on a raw
                            # extract_row() dict. Resolve it explicitly instead.
                            st.write({
                                "location recorded": resolve_location(row["location_raw"], _current_aliases),
                                "raw value before aliasing": row["location_raw"],
                                "person": row["person"],
                                "second person": row["second_person"],
                                "detail date": row["detail_date"],
                            })
                        else:
                            st.write("⚠️ No submissions returned at all for this form and date range")
                except JotFormError as e:
                    st.error(str(e))

        if st.button("Location field setup"):
            if not api_key:
                st.error("A JotForm API key is required.")
            elif not _forms:
                st.error("Select at least one form on the Report tab.")
            else:
                try:
                    for form_id in [FORM_NAME_TO_ID[n] for n in _forms]:
                        st.write(f"### {FORM_ID_TO_NAME[form_id]}")
                        questions = get_form_questions(form_id, api_key)
                        try:
                            items = sorted(questions.items(), key=lambda kv: int(kv[0]))
                        except (ValueError, TypeError):
                            items = list(questions.items())
                        candidates = [
                            q for (k, q) in items
                            if LOCATION_MATCH_WORD in str(q.get("name", "")).strip().lower()
                            or LOCATION_MATCH_WORD in str(q.get("text", "")).strip().lower()
                        ]
                        if not candidates:
                            st.write("⚠️ No fields mentioning 'location' found at all.")
                            continue
                        for q in candidates:
                            st.write({
                                "name": q.get("name"),
                                "text (label)": q.get("text"),
                                "type": q.get("type"),
                                "options": q.get("options"),
                            })
                except JotFormError as e:
                    st.error(str(e))
