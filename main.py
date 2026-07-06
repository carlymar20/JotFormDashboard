import streamlit as st
import pandas as pd
import requests
import io
import time
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


def _open_overrides_sheet(sheet_url_or_key):
    """Open the target spreadsheet by URL or by key, and return its
    first worksheet."""
    gc = get_gsheet_client()
    if sheet_url_or_key.startswith("http"):
        sh = gc.open_by_url(sheet_url_or_key)
    else:
        sh = gc.open_by_key(sheet_url_or_key)
    return sh.sheet1


def load_overrides_from_sheet(sheet_url_or_key):
    """Read the overrides table from the sheet and return it as the
    nested overrides dict. Returns an empty dict if the sheet has no
    data rows yet."""
    ws = _open_overrides_sheet(sheet_url_or_key)
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
    ws = _open_overrides_sheet(sheet_url_or_key)
    df = overrides_to_df(overrides)
    ws.clear()
    values = [OVERRIDES_HEADER] + df[OVERRIDES_HEADER].astype(object).where(pd.notna(df), "").values.tolist()
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
            row['location'] = val if isinstance(val, str) else str(val)
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


def _period_count(interval, start_date, end_date):
    """Number of compliance periods (days/weeks/months) in the date range
    for the given interval."""
    if interval == "daily":
        periods = pd.date_range(start_date, end_date, freq='D')
    elif interval == "weekly":
        periods = pd.date_range(start_date, end_date, freq='W-MON')
        if pd.to_datetime(start_date).weekday() != 0:
            periods = periods.insert(0, pd.to_datetime(start_date))
    elif interval == "monthly":
        periods = pd.period_range(start_date, end_date, freq='M')
    else:
        periods = []
    return len(periods)


def compute_period_targets(df_raw, start_date, end_date, selected_forms, selected_locations, overrides=None):
    """Build the compliance summary, one row per (form, location).

    Quota/interval requirements are resolved per (form, location) via
    get_requirement_for(), so locations listed in `overrides` (typically
    st.session_state['location_overrides'], edited live in the UI) get
    their own target instead of the form's default quota. Because
    interval can also differ per location, the period count is computed
    per row rather than once per form.
    """
    if overrides is None:
        overrides = LOCATION_COMPLIANCE_OVERRIDES
    summary_rows = []
    compliance_forms = [f for f in selected_forms if FORM_COMPLIANCE.get(f)]
    all_locations = selected_locations if selected_locations else sorted(df_raw['location'].dropna().unique())

    # Cache period counts by interval so we don't recompute the same
    # date_range/period_range repeatedly for every location.
    period_count_cache = {}

    for form_name in compliance_forms:
        for loc in all_locations:
            req = get_requirement_for(form_name, loc, overrides)
            if req is None:
                continue
            quota = req['quota']
            interval = req['interval']

            if interval not in period_count_cache:
                period_count_cache[interval] = _period_count(interval, start_date, end_date)
            period_count = period_count_cache[interval]

            df_form_loc = df_raw[(df_raw['form_name'] == form_name) & (df_raw['location'] == loc)]
            actual = len(df_form_loc)
            target = period_count * quota
            percent_complete = 100 * actual / target if target else 0
            status = "Met" if actual >= target else f"Missing {target - actual}"
            is_override = loc in overrides.get(form_name, {})
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
                "custom_quota": "Yes" if is_override else "No"
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

# Load API key from secrets (set after deploying to Streamlit Cloud)
api_key = st.secrets["API_KEY"] if "API_KEY" in st.secrets else st.text_input("Enter your JotForm API Key:", type="password")

filename = st.text_input("Excel filename (without .xlsx)", "JotForm_Compliance_Report") + ".xlsx"

form_names = list(FORM_NAME_TO_ID.keys())
selected_forms = st.multiselect("Select form(s):", options=form_names, default=form_names)

start_date = st.date_input("Start date")
end_date = st.date_input("End date")

# For the location picker: pull the REAL dropdown options straight from
# each form's field setup, not from what people have actually submitted.
# This avoids picking up write-in/free-text values that don't match the
# official list of site names.
if st.button("Load Locations"):
    if not api_key:
        st.error("API Key is required!")
    else:
        all_options = set()
        for form_id in [FORM_NAME_TO_ID[n] for n in selected_forms]:
            opts = get_location_options_for_form(form_id, api_key)
            if not opts:
                st.warning(
                    f"Could not find dropdown location options for "
                    f"'{FORM_ID_TO_NAME[form_id]}' — check its confirmed "
                    f"field name(s) in FORM_ID_TO_LOCATION_FIELDS."
                )
            all_options.update(opts)
        locations = sorted(all_options)
        if not locations:
            st.warning("No location options found for these forms.")
        st.session_state['locations'] = locations
        # Reset the selection to match the freshly loaded options, so we never
        # end up with a stale default that no longer matches the option list.
        st.session_state['selected_locations'] = locations

locations = st.session_state.get('locations', [])
selected_locations = st.multiselect(
    "Select location(s):",
    options=locations,
    default=st.session_state.get('selected_locations', locations),
    key='selected_locations_widget'
)

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
                overrides=st.session_state.get('location_overrides', LOCATION_COMPLIANCE_OVERRIDES)
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
# Styled to be small and blend into the background so it doesn't draw
# attention during normal use, but still fully clickable if you know it's
# there. NOTE: the color below (#ffffff) matches Streamlit's default
# light-theme background — if your app uses a dark theme or a custom
# background color, change the hex codes below to match.
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

with st.expander("🔧 Debug Tools (click to expand)", expanded=False):
    if st.button("Debug Field Names"):
        if not api_key:
            st.error("API Key is required!")
        else:
            for form_id in [FORM_NAME_TO_ID[n] for n in selected_forms]:
                subs = get_submissions(form_id, api_key, start_date, end_date)
                st.write(f"### {FORM_ID_TO_NAME[form_id]}")
                if subs:
                    st.write({v['name']: v.get('text') for v in subs[0]['answers'].values()})
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
