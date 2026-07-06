import streamlit as st
import pandas as pd
import requests
import io
import time

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

# We match on this word appearing in the field's internal name OR its label,
# since JotForm auto-generates internal names per-form and they don't always match.
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


def find_location_field(items):
    """Given (key, field) pairs sorted in form order, return the field
    that is the REAL location dropdown.

    Many of these forms have two fields that both mention "location":
    one is often a plain free-text field (accepts anything someone
    types), the other is the actual dropdown/select widget used to
    route the notification email to the right site. Since email
    routing logic can't work off of freeform text, the field that has
    actual dropdown options defined in its setup is the reliable
    signal for "this is the real one" — not its name or position on
    the form.
    """
    candidates = [q for (k, q) in items
                  if LOCATION_MATCH_WORD in str(q.get('name', '')).strip().lower()
                  or LOCATION_MATCH_WORD in str(q.get('text', '')).strip().lower()]
    if not candidates:
        return None

    # Prefer whichever candidate actually has dropdown options defined.
    for q in candidates:
        if (q.get('options') or '').strip():
            return q

    # None have options (unusual) — fall back to the last matching field,
    # since on these forms the true selector tends to sit near the bottom,
    # next to the email-routing logic.
    return candidates[-1]


def get_location_field_name_and_options(form_id, api_key):
    """Return (internal_field_name, [list of valid dropdown options])
    for the real location field on this form.
    """
    questions = get_form_questions(form_id, api_key)
    try:
        items = sorted(questions.items(), key=lambda kv: int(kv[0]))
    except (ValueError, TypeError):
        items = list(questions.items())

    field = find_location_field(items)
    if not field:
        return None, []

    options_str = field.get('options', '') or ''
    options = [o.strip() for o in options_str.split('|') if o.strip()]
    return field.get('name'), options


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


def extract_row(sub, form_id, location_field_name):
    """Pull out the fields we care about from one submission.

    location_field_name is the exact internal field name of the real
    location dropdown for this form (determined once via
    get_location_field_name_and_options), so every submission is
    matched consistently instead of re-guessing per row.
    """
    answers = sub['answers']
    row = {
        'form_id': form_id,
        'form_name': FORM_ID_TO_NAME.get(form_id, form_id),
        'submission_id': sub['id'],
        'submitted_at': sub['created_at'],
        'location': None
    }

    if location_field_name:
        for k, v in answers.items():
            if v.get('name') == location_field_name:
                ans = v.get('answer', '')
                row['location'] = ans if isinstance(ans, str) else str(ans)
                break

    return row


def get_all_data(form_names, location_list, start_date, end_date, api_key, field_name_cache=None):
    all_rows = []
    form_ids = [FORM_NAME_TO_ID[name] for name in form_names]
    if field_name_cache is None:
        field_name_cache = {}
    for form_id in form_ids:
        if form_id not in field_name_cache:
            field_name, _ = get_location_field_name_and_options(form_id, api_key)
            field_name_cache[form_id] = field_name
        location_field_name = field_name_cache[form_id]

        submissions = get_submissions(form_id, api_key, start_date, end_date)
        for sub in submissions:
            row = extract_row(sub, form_id, location_field_name)
            if not location_list or row['location'] in location_list:
                all_rows.append(row)
    df_raw = pd.DataFrame(all_rows)
    return df_raw


def compute_period_targets(df_raw, start_date, end_date, selected_forms, selected_locations):
    summary_rows = []
    compliance_forms = [f for f in selected_forms if FORM_COMPLIANCE.get(f)]
    all_locations = selected_locations if selected_locations else sorted(df_raw['location'].dropna().unique())
    for form_name in compliance_forms:
        req = FORM_COMPLIANCE.get(form_name)
        if req is None:
            continue
        quota = req['quota']
        interval = req['interval']
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
        period_count = len(periods)
        for loc in all_locations:
            df_form_loc = df_raw[(df_raw['form_name'] == form_name) & (df_raw['location'] == loc)]
            actual = len(df_form_loc)
            target = period_count * quota
            percent_complete = 100 * actual / target if target else 0
            status = "Met" if actual >= target else f"Missing {target - actual}"
            summary_rows.append({
                "form_name": form_name,
                "location": loc,
                "interval": interval.capitalize(),
                "periods_in_range": period_count,
                "quota_per_period": quota,
                "target_total": target,
                "actual_total": actual,
                "percent_complete": round(percent_complete, 1),
                "status": status
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

# ---- Debug button: shows each form's actual field names/labels ----
# Use this if submissions are visible in JotForm but missing from the report.
# It tells you whether the "location" field is named consistently across forms.
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
            _, opts = get_location_field_name_and_options(form_id, api_key)
            if not opts:
                st.warning(
                    f"Could not find dropdown location options for "
                    f"'{FORM_ID_TO_NAME[form_id]}' — it may not use a "
                    f"dropdown/select field for location."
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

if st.button("Run Report"):
    if not api_key:
        st.error("API Key is required!")
    else:
        df_raw = get_all_data(selected_forms, selected_locations, start_date, end_date, api_key)
        if df_raw.empty:
            st.warning("No submissions found for this selection.")
        else:
            compliance_summary = compute_period_targets(df_raw, start_date, end_date, selected_forms, selected_locations)
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
