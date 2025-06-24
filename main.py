import streamlit as st
import pandas as pd
import requests

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
LOCATION_FIELD = "location"

def get_submissions(form_id, api_key, start_date=None, end_date=None):
    all_submissions = []
    batch_size = 1000
    offset = 0
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
        r = requests.get(url, params=params)
        r.raise_for_status()
        submissions = r.json()['content']
        all_submissions.extend(submissions)
        if len(submissions) < batch_size:
            break
        offset += batch_size
    return all_submissions

def extract_row(sub, form_id, location_field):
    answers = sub['answers']
    row = {
        'form_id': form_id,
        'form_name': FORM_ID_TO_NAME.get(form_id, form_id),
        'submission_id': sub['id'],
        'submitted_at': sub['created_at'],
        'location': None
    }
    for k, v in answers.items():
        if v['name'] == location_field:
            row['location'] = v.get('answer', '')
    return row

def get_all_data(form_names, location_list, start_date, end_date, api_key):
    all_rows = []
    form_ids = [FORM_NAME_TO_ID[name] for name in form_names]
    for form_id in form_ids:
        submissions = get_submissions(form_id, api_key, start_date, end_date)
        for sub in submissions:
            row = extract_row(sub, form_id, LOCATION_FIELD)
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
            status = "Met" if actual >= target else f"Missing {target-actual}"
            summary_rows.append({
                "form_name": form_name,
                "location": loc,
                "interval": interval.capitalize(),
                "periods_in_range": period_count,
                "quota_per_period": quota,
                "target_total": target,
                "actual_total": actual,
                "percent_complete": round(percent_complete,1),
                "status": status
            })
    summary_df = pd.DataFrame(summary_rows)
    return summary_df

def leaderboard_with_badges(compliance_summary):
    if compliance_summary.empty:
        return pd.DataFrame(columns=['location','overall_percent','Badge','Rank'])
    leader = (compliance_summary.groupby('location')
              .agg(
                  forms_tracked=('form_name','count'),
                  total_target=('target_total','sum'),
                  total_actual=('actual_total','sum'),
                  overall_percent=('percent_complete','mean')
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

# For the location picker, fetch after forms/dates picked
if st.button("Load Locations"):
    if not api_key:
        st.error("API Key is required!")
    else:
        df_raw_temp = get_all_data(selected_forms, [], start_date, end_date, api_key)
        if df_raw_temp.empty:
            st.warning("No data found for these selections.")
            locations = []
        else:
            locations = sorted([loc for loc in df_raw_temp['location'].dropna().unique() if str(loc).strip() != ""])
        st.session_state['locations'] = locations

locations = st.session_state.get('locations', [])
selected_locations = st.multiselect("Select location(s):", options=locations, default=locations)

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
            import io
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
