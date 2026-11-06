import os
import csv
import re
import time
import datetime as dt
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd

# ── Inject Streamlit Cloud secrets into os.environ BEFORE any other imports ──
# This must happen here, before leadgen is imported, because leadgen.py reads
# os.environ at module level when it loads. st.secrets is available immediately
# in the main script but NOT inside a function called during module import.
_EXPECTED_KEYS = [
    "GEMINI_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
    "CONTACT_EMAIL", "SENDER_NAME", "SENDER_TITLE",
    "PAGESPEED_API_KEY", "BREVO_API_KEY", "GEMINI_MODEL",
]
for _k in _EXPECTED_KEYS:
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:
        pass

import importlib
import leadgen
import sender
importlib.reload(leadgen)
importlib.reload(sender)

from leadgen import (
    _load_env, NICHES, geocode, fetch_businesses, parse_element,
    audit_lead, score, load_suppression, suppressed, draft, is_social, norm_url,
    clean_body_markdown
)

_load_env()

st.set_page_config(
    page_title="Quillweb Lead Engine",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for clean UI styling
st.markdown("""
<style>
    .metric-card {
        background-color: #1e212b;
        padding: 15px;
        border-radius: 10px;
        border-left: 5px solid #4CAF50;
    }
    .badge-gemini {
        background-color: #1a73e8;
        color: white;
        padding: 3px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .badge-offline {
        background-color: #f2994a;
        color: white;
        padding: 3px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
    .badge-no-web {
        background-color: #eb5757;
        color: white;
        padding: 3px 8px;
        border-radius: 12px;
        font-size: 0.8rem;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# --- Helper Functions ---
def load_leads_df(filepath="leads.csv"):
    if not os.path.exists(filepath):
        return pd.DataFrame()
    try:
        # Load all columns as string/object to avoid dtype mismatch when setting text values
        df = pd.read_csv(filepath, dtype=str).fillna("")
        # Ensure standard columns exist
        for col in ["priority", "name", "niche", "need", "email", "phone", "website",
                    "address", "issues", "subject", "body", "draft_source", "image_path", "lat", "lon", "osm_id"]:
            if col not in df.columns:
                df[col] = ""
            else:
                df[col] = df[col].astype(str)
        return df
    except Exception as e:
        st.error(f"Error loading {filepath}: {e}")
        return pd.DataFrame()


def save_leads_df(df, filepath="leads.csv"):
    df.to_csv(filepath, index=False, encoding="utf-8")
    # Also keep in session state so the UI always reflects latest data without disk read
    st.session_state["leads_df"] = df.copy()


def get_maps_url(row):
    name = str(row.get("name", ""))
    addr = str(row.get("address", ""))
    lat = str(row.get("lat", "")).strip()
    lon = str(row.get("lon", "")).strip()
    if lat and lon and lat != "nan" and lon != "nan":
        return f"https://www.google.com/maps?q={lat},{lon}"
    query = f"{name} {addr}".strip()
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"


# --- Sidebar: Controls & Settings ---
with st.sidebar:
    st.title("🚀 Quillweb Engine")
    st.markdown("Automated Local Lead Gen & Cold Outreach")
    st.divider()

    st.subheader("🎯 Lead Discovery")
    niche_options = list(NICHES.keys())
    selected_niche = st.selectbox("Select Business Niche:", niche_options, index=niche_options.index("dentists") if "dentists" in niche_options else 0)
    city_input = st.text_input("Target City / Region:", value="Austin, TX")
    limit_input = st.slider("Max businesses to audit:", min_value=5, max_value=100, value=25, step=5)
    
    target_focus = st.radio(
        "Target Strategy:",
        ["🔥 No Website / Social Only (Build from Scratch)", "⚡ Has Website (Audit & Redesign)", "🌐 All (Balanced Mix)"],
        index=0
    )

    with st.expander("⚙️ Advanced Options"):
        custom_tag = st.text_input("Custom OSM Tag (optional):", placeholder="e.g. shop=florist")
        enable_pagespeed = st.checkbox("Google PageSpeed Score (Slows down audit)", value=False)
        auto_draft_ai = st.checkbox("Auto-generate AI Drafts during audit (Uses Gemini quota)", value=False)
        worker_threads = st.slider("Audit Worker Threads:", 2, 12, 6)

    run_scrape = st.button("🔍 Find & Audit Leads", type="primary", use_container_width=True)

    st.divider()
    st.subheader("📬 Mailer Status")
    sender_state = sender.load_state()
    sent_today_count = sender.sent_today(sender_state)
    eff_cap = sender.effective_cap(sender_state, 30)
    start_d = sender.get_campaign_start_date(sender_state)
    campaign_day = max((dt.date.today() - start_d).days + 1, 1) if sender_state else 1
    st.metric("Sent Today", f"{sent_today_count} / {eff_cap}", delta=f"Day {campaign_day} (+5/day ramp)")
    st.metric("Total Contacted", len(sender_state))
    st.caption("Daily cap auto-ramps by +5 emails every day")

    st.divider()
    # ── Live API key status indicator ──
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    gmail_addr = os.environ.get("GMAIL_ADDRESS", "")
    if gemini_key:
        st.success(f"🤖 Gemini API: ✅ Loaded (`...{gemini_key[-6:]}`)")
    else:
        st.error("🤖 Gemini API: ❌ NOT loaded — add GEMINI_API_KEY to Secrets")
    if gmail_addr:
        st.success(f"📧 Gmail: ✅ `{gmail_addr}`")
    else:
        st.warning("📧 Gmail: ❌ GMAIL_ADDRESS not set")


# --- Pipeline Execution ---
if run_scrape:
    if not city_input.strip():
        st.error("Please enter a target city!")
    else:
        tag = custom_tag.strip() or NICHES.get(selected_niche)
        niche = selected_niche if not custom_tag.strip() else custom_tag.strip()
        progress_bar = st.progress(0)
        status_text = st.empty()

        try:
            status_text.info(f"📍 Step 1/4: Discovering '{niche}' in {city_input} on OpenStreetMap...")
            progress_bar.progress(15)
            bbox = geocode(city_input)
            raw_elements = fetch_businesses(tag, bbox)
            parsed_leads = [x for x in (parse_element(e, niche) for e in raw_elements) if x]

            # De-duplicate
            seen, unique_leads = set(), []
            for l in parsed_leads:
                key = (l["name"].lower(), l["website"].lower())
                if key not in seen:
                    seen.add(key)
                    unique_leads.append(l)

            # Sort based on target strategy chosen by user
            no_site_fn = lambda l: not l["website"] or is_social(norm_url(l["website"]))
            if target_focus.startswith("🔥"):
                # No-website first, then by phone/email availability
                unique_leads.sort(key=lambda l: (not no_site_fn(l), -(bool(l["phone"]) + bool(l["osm_email"]))))
            elif target_focus.startswith("⚡"):
                # Has-website first
                unique_leads.sort(key=lambda l: (no_site_fn(l), -(bool(l["phone"]) + bool(l["osm_email"]))))
            else:
                # Balanced: interleave no-website and has-website
                no_web = [l for l in unique_leads if no_site_fn(l)]
                has_web = [l for l in unique_leads if not no_site_fn(l)]
                unique_leads = []
                for a, b in zip(no_web, has_web):
                    unique_leads += [a, b]
                unique_leads += no_web[len(has_web):] + has_web[len(no_web):]

            leads_to_audit = unique_leads[:limit_input]
            no_site_count = sum(1 for l in leads_to_audit if no_site_fn(l))
            has_site_count = len(leads_to_audit) - no_site_count
            st.success(f"Found {len(unique_leads)} businesses. Auditing {len(leads_to_audit)} — 🔥 {no_site_count} without website, 💻 {has_site_count} with website.")

            status_text.info(f"🌐 Step 2/4: Auditing websites & scraping public emails ({len(leads_to_audit)} leads)...")
            progress_bar.progress(40)
            with ThreadPoolExecutor(max_workers=worker_threads) as ex:
                audited = list(ex.map(lambda l: audit_lead(l, enable_pagespeed), leads_to_audit))

            status_text.info("⚖️ Step 3/4: Scoring & filtering opt-outs...")
            progress_bar.progress(70)
            sup = load_suppression()
            # Keep no-email leads — only suppress by email if they *have* one
            scored_leads = [score(l) for l in audited if not (l["email"] and suppressed(l["email"], sup))]
            # Re-enforce target sort after scoring (scoring can reshuffle)
            if target_focus.startswith("🔥"):
                scored_leads.sort(key=lambda l: (
                    not no_site_fn(l),  # no-website leads first
                    -l["priority"]
                ))
            else:
                scored_leads.sort(key=lambda l: -l["priority"])

            if auto_draft_ai:
                status_text.info(f"✍️ Step 4/4: Drafting personalized emails with Gemini AI...")
                progress_bar.progress(85)
                _load_env()
                for l in scored_leads:
                    subj, bdy, src = draft(l, city_input)
                    l["subject"], l["body"], l["draft_source"] = subj, bdy, src
            else:
                for l in scored_leads:
                    l["subject"] = l.get("subject", "")
                    l["body"] = l.get("body", "")
                    l["draft_source"] = l.get("draft_source", "")

            # Save to leads.csv
            cols = ["priority", "name", "niche", "need", "email", "phone", "website",
                    "address", "issues", "subject", "body", "draft_source", "lat", "lon", "osm_id"]
            with open("leads.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for l in scored_leads:
                    row = dict(l, issues=" | ".join(l["issues"]))
                    w.writerow(row)

            progress_bar.progress(100)
            status_text.success("🎉 Lead generation & audit complete! Results updated below.")
            time.sleep(1)
            st.rerun()

        except Exception as e:
            st.error(f"Error during scrape: {e}")


# --- Main Dashboard ---
st.header("📊 Lead Intelligence & Outreach Dashboard")

# Use session_state as source of truth so button updates show immediately on rerun
if "leads_df" not in st.session_state:
    st.session_state["leads_df"] = load_leads_df()
# Always sync from disk if session just started or CSV is newer (e.g. after scrape)
_disk_df = load_leads_df()
if not _disk_df.empty and (st.session_state["leads_df"].empty or len(_disk_df) != len(st.session_state["leads_df"])):
    st.session_state["leads_df"] = _disk_df

df = st.session_state["leads_df"]

if df.empty:
    st.info("👋 No leads generated yet. Use the sidebar on the left to start your first search!")
else:
    # Top Metrics Bar
    total_leads = len(df)
    with_email = len(df[df["email"].notna() & (df["email"] != "")])
    has_website = len(df[df["website"].notna() & (df["website"] != "") & (~df["issues"].str.contains("No website", na=False))])
    no_website = total_leads - has_website
    gemini_drafted = len(df[df["draft_source"] == "Gemini AI"])

    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Leads", total_leads)
    col2.metric("Has Website", has_website)
    col3.metric("No Website (High Need 🔥)", no_website)
    col4.metric("With Contact Email", with_email)
    col5.metric("Gemini AI Drafted 🤖", gemini_drafted)

    st.divider()

    # Tabs for Organized Navigation
    tab_leads, tab_outreach, tab_calendar, tab_editor = st.tabs([
        "📋 Filter & Explore Leads",
        "🚀 Outreach & Sending",
        "📅 Outreach Calendar & Schedule (+5/day)",
        "✏️ Email Draft Editor"
    ])

    # --- TAB 1: Filter & Explore Leads ---
    with tab_leads:
        filter_col1, filter_col2 = st.columns([2, 1])
        with filter_col1:
            segment = st.radio(
                "Filter Segment:",
                ["All Leads", "💻 Has Website", "🔥 No Website / Social Only", "✉️ Has Email (Ready to Send)"],
                horizontal=True
            )
        with filter_col2:
            search_query = st.text_input("🔍 Search by business name or email:", "")

        filtered_df = df.copy()
        if segment == "💻 Has Website":
            filtered_df = filtered_df[filtered_df["website"].notna() & (filtered_df["website"] != "") & (~filtered_df["issues"].str.contains("No website", na=False))]
        elif segment == "🔥 No Website / Social Only":
            filtered_df = filtered_df[filtered_df["website"].isna() | (filtered_df["website"] == "") | (filtered_df["issues"].str.contains("No website|Only a social", na=False))]
        elif segment == "✉️ Has Email (Ready to Send)":
            filtered_df = filtered_df[filtered_df["email"].notna() & (filtered_df["email"] != "")]

        if search_query.strip():
            q = search_query.strip().lower()
            filtered_df = filtered_df[filtered_df["name"].str.lower().str.contains(q, na=False) | filtered_df["email"].str.lower().str.contains(q, na=False)]

        st.caption(f"Showing {len(filtered_df)} of {len(df)} leads")

        # Interactive Lead Cards / Table
        for idx, row in filtered_df.iterrows():
            with st.container():
                c1, c2, c3, c4 = st.columns([3, 3, 2, 2])
                with c1:
                    st.markdown(f"**{row['name']}**")
                    st.caption(f"📍 {row['address'] if pd.notna(row['address']) else 'Address not listed'}")
                    maps_url = get_maps_url(row)
                    st.markdown(f"[📍 Check on Google Maps]({maps_url})", unsafe_allow_html=True)

                with c2:
                    web = str(row.get("website", "")).strip()
                    if web and web != "nan":
                        if not web.startswith("http"):
                            web = "http://" + web
                        st.markdown(f"🔗 [Visit Website]({web})")
                    else:
                        st.markdown('<span class="badge-no-web">❌ No Website</span>', unsafe_allow_html=True)
                    
                    issues = str(row.get("issues", ""))
                    if issues and issues != "nan":
                        st.caption(f"⚠️ {issues[:90]}..." if len(issues) > 90 else f"⚠️ {issues}")

                with c3:
                    em = str(row.get("email", "")).strip()
                    ph = str(row.get("phone", "")).strip()
                    if em and em != "nan":
                        st.markdown(f"✉️ `{em}`")
                    else:
                        st.caption("No email found")
                    if ph and ph != "nan":
                        st.caption(f"📞 {ph}")

                with c4:
                    src = str(row.get("draft_source", "")).strip()
                    if src == "Gemini AI":
                        st.markdown('<span class="badge-gemini">🤖 Gemini AI</span>', unsafe_allow_html=True)
                    elif src == "Offline Template":
                        st.markdown('<span class="badge-offline">📄 Offline Template</span>', unsafe_allow_html=True)
                    else:
                        st.caption("No draft generated")

                    st.caption(f"Priority Score: **{row['priority']}**")
                    img_p = str(row.get("image_path", "")).strip()
                    if img_p and img_p != "nan" and os.path.exists(img_p):
                        st.markdown('<span style="background-color: #0284c7; color: white; padding: 2px 7px; border-radius: 10px; font-size: 0.75rem; font-weight: 600;">🎨 Mockup Attached</span>', unsafe_allow_html=True)

                    # On-demand AI draft generation button for this lead
                    btn_label = "✨ Generate AI Draft" if not src else "🔄 Regenerate with AI"
                    if st.button(btn_label, key=f"btn_draft_{idx}"):
                        if not os.environ.get("GEMINI_API_KEY"):
                            st.error("❌ GEMINI_API_KEY not found in Streamlit Secrets!")
                        else:
                            with st.spinner(f"Calling Gemini AI for {row['name']}..."):
                                lead_dict = row.to_dict()
                                if isinstance(lead_dict.get("issues"), str):
                                    lead_dict["issues"] = [i.strip() for i in lead_dict["issues"].split("|") if i.strip()]
                                subj, bdy, d_src = draft(lead_dict, city_input)
                            df["subject"] = df["subject"].astype(str)
                            df["body"] = df["body"].astype(str)
                            df["draft_source"] = df["draft_source"].astype(str)
                            df.loc[df["name"] == row["name"], "subject"] = str(subj)
                            df.loc[df["name"] == row["name"], "body"] = clean_body_markdown(str(bdy))
                            df.loc[df["name"] == row["name"], "draft_source"] = str(d_src)
                            save_leads_df(df)
                            st.success(f"✅ Generated via **{d_src}** for {row['name']}!")
                            st.rerun()

                # Preview generated email right on the card if present
                curr_subj = str(row.get("subject", "")).strip()
                curr_body = str(row.get("body", "")).strip()
                if curr_subj and curr_subj != "nan" and curr_body and curr_body != "nan":
                    with st.expander(f"✉️ Preview Email: {curr_subj[:60]}..."):
                        st.markdown(f"**Subject:** {curr_subj}")
                        st.markdown("---")
                        st.markdown(clean_body_markdown(curr_body))

                st.divider()

    # --- TAB 2: Outreach & Sending ---
    with tab_outreach:
        st.subheader("📬 Outreach Control Room")
        st.markdown("Safely send personalized cold emails directly through your configured Gmail account.")

        col_stat1, col_stat2, col_stat3 = st.columns(3)
        col_stat1.metric("Today's Send Room", f"{max(eff_cap - sent_today_count, 0)} available")
        col_stat2.metric("Daily Cap (Ramping)", eff_cap)
        col_stat3.metric("Suppression Count", len(sender.load_suppression()))

        st.divider()

        st.write("### Actions")
        act_col1, act_col2, act_col3 = st.columns(3)

        with act_col1:
            if st.button("👀 Preview Outreach (Dry Run)", use_container_width=True):
                state = sender.load_state()
                sup = sender.load_suppression()
                cap = sender.effective_cap(state, 30)
                room = max(cap - sender.sent_today(state), 0)
                jobs = (sender.due_followups(state, sup) + sender.new_jobs("leads.csv", state, sup, 30))[:room]
                if not jobs:
                    st.info("No new emails due to send today.")
                else:
                    st.success(f"Previewing {len(jobs)} emails scheduled to go out:")
                    for j in jobs:
                        has_img = bool(j.get("image_path") and os.path.exists(str(j.get("image_path"))))
                        with st.expander(f"To: {j['email']} ({j['name']}) - {j['subject']}" + (" [🎨 Mockup Attached]" if has_img else "")):
                            st.markdown(sender.body_to_plain(j["body"]))

        with act_col2:
            confirm_send = st.checkbox("Confirm I want to send live emails")
            if st.button("🚀 Send Live Outreach", type="primary", disabled=not confirm_send, use_container_width=True):
                st.warning("Sending sequence initiated in background... Check Gmail sent folder!")
                # Execute live sender
                state = sender.load_state()
                sup = sender.load_suppression()
                cap = sender.effective_cap(state, 30)
                room = max(cap - sender.sent_today(state), 0)
                jobs = (sender.due_followups(state, sup) + sender.new_jobs("leads.csv", state, sup, 30))[:room]

                if not jobs:
                    st.info("No leads available or daily cap reached.")
                else:
                    progress_send = st.progress(0)
                    smtp = sender.get_smtp_connection()
                    try:
                        for idx, j in enumerate(jobs):
                            msg = sender.build_message(
                                j["email"], j["subject"], j["body"],
                                j.get("in_reply_to"), j.get("references"),
                                image_path=j.get("image_path"),
                                name=j.get("name", "")
                            )
                            smtp.send_message(msg)
                            sender.record(state, j, msg["Message-ID"])
                            sender.save_state(state)
                            st.success(f"[{idx+1}/{len(jobs)}] Sent -> {j['email']}")
                            progress_send.progress((idx + 1) / len(jobs))
                            if idx < len(jobs) - 1:
                                time.sleep(15)  # Shorter UI pause
                    finally:
                        smtp.quit()
                    st.balloons()
                    st.rerun()

        with act_col3:
            if st.button("📥 Check Inbox Replies", use_container_width=True):
                with st.spinner("Checking Gmail inbox for replies and opt-outs..."):
                    class MockArgs:
                        days = 14
                    sender.cmd_replies(MockArgs())
                st.success("Inbox sync complete! State and suppression lists updated.")
                st.rerun()

    # --- TAB 3: Outreach Calendar & Schedule (+5/day ramp) ---
    with tab_calendar:
        st.subheader("📅 Day-Wise Outreach Calendar & Sending Status")
        st.markdown(
            "Track daily email volumes, historical outreach logs, and the automatic **+5 emails/day** safe-ramping schedule."
        )

        cal_state = sender.load_state()
        start_d = sender.get_campaign_start_date(cal_state)
        today_date = dt.date.today()
        campaign_day_num = max((today_date - start_d).days + 1, 1) if cal_state else 1
        today_target_cap = sender.effective_cap(cal_state, 30)
        sent_today_val = sender.sent_today(cal_state)
        remaining_sends = max(today_target_cap - sent_today_val, 0)

        c_stat1, c_stat2, c_stat3, c_stat4 = st.columns(4)
        c_stat1.metric("Campaign Day", f"Day {campaign_day_num}", help=f"Campaign started on {start_d.isoformat()}")
        c_stat2.metric("Today's Target Cap", f"{today_target_cap} emails", delta="+5 from yesterday")
        c_stat3.metric("Sent Today", f"{sent_today_val} / {today_target_cap}", delta=f"{remaining_sends} available")
        c_stat4.metric("Total Contacted", len(cal_state), help="Unique businesses emailed")

        st.divider()

        # Day-by-day table
        st.write("### 📈 14-Day Outreach Schedule & Status")
        cal_days = sender.get_outreach_calendar(cal_state, daily_cap=30, ramp_step=5, lookback_days=4, lookahead_days=9)
        table_rows = []
        for d in cal_days:
            prefix = "👉 (TODAY) " if d["is_today"] else ""
            table_rows.append({
                "Date": prefix + d["date_str"],
                "Campaign Day": f"Day {d['day_num']}" if d.get("day_num") else "—",
                "Daily Target Cap": f"{d['target_cap']} emails",
                "Sent Volume": d["sent_count"],
                "Status": d["status"],
            })
        st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)

        # Bar chart showing Target Cap vs Actual Sent
        chart_df = pd.DataFrame({
            "Date": [d["date_str"][5:] for d in cal_days],
            "Sent": [d["sent_count"] for d in cal_days],
            "Target Cap": [d["target_cap"] for d in cal_days],
        }).set_index("Date")
        st.bar_chart(chart_df)

        st.divider()

        # Interactive Day Inspector
        st.write("### 🔍 Inspect Contacts by Date")
        all_dates = sorted(list({t[:10] for e in cal_state.values() for t in e.get("sent_times", [])}), reverse=True)
        if not all_dates:
            all_dates = [today_date.isoformat()]
        elif today_date.isoformat() not in all_dates:
            all_dates.insert(0, today_date.isoformat())

        chosen_date = st.selectbox("Select Date to View Activity:", all_dates, index=0)
        day_contacts = []
        for em, e in cal_state.items():
            for t in e.get("sent_times", []):
                if t.startswith(chosen_date):
                    day_contacts.append({
                        "Time": t[11:19] if len(t) >= 19 else t[11:16],
                        "Business Name": e.get("name", "Unknown"),
                        "Email Address": em,
                        "Subject Line": e.get("subject", ""),
                        "Stage": "Initial (Stage 0)" if e.get("stage", 0) == 0 else f"Follow-up {e.get('stage')}",
                        "Status": e.get("status", "sent"),
                    })

        if day_contacts:
            st.success(f"Found {len(day_contacts)} emails sent on {chosen_date}:")
            st.dataframe(pd.DataFrame(day_contacts), use_container_width=True, hide_index=True)
        else:
            st.info(f"No emails recorded for {chosen_date}. Today's capacity allows sending up to {today_target_cap} emails!")

    # --- TAB 4: Email Draft Editor ---
    with tab_editor:
        st.subheader("✏️ Inspect & Edit Email Drafts Before Sending")
        selectable_leads = df[df["name"].notna() & (df["name"] != "")]
        if selectable_leads.empty:
            st.info("No leads available to edit.")
        else:
            selected_lead_name = st.selectbox("Select Business to Review Draft:", selectable_leads["name"].tolist())
            lead_row = df[df["name"] == selected_lead_name].iloc[0]

            col_info1, col_info2 = st.columns([1, 1])
            with col_info1:
                st.write(f"**Business:** {lead_row['name']}")
                em_val = str(lead_row.get('email', '')).strip()
                st.write(f"**Email:** `{em_val}`" if em_val and em_val != 'nan' else "**Email:** *(No email - phone/offline pitch)*")
                st.write(f"**Draft Source:** `{lead_row.get('draft_source', 'No draft yet')}`")
            with col_info2:
                st.write(f"**Audited Issues:** {lead_row.get('issues', 'None')}")
                st.write(f"**Website:** {lead_row.get('website', 'None')}")

            st.divider()
            new_subject = st.text_input("Email Subject:", value=str(lead_row.get("subject", "")))
            new_body = st.text_area("Email Body (Text Editor):", value=clean_body_markdown(str(lead_row.get("body", ""))), height=260)

            # --- Mockup Image Attachment Section ---
            st.markdown("##### 🎨 Design Concept Mockup / Screenshot Attachment (Optional)")
            curr_img_path = str(lead_row.get("image_path", "")).strip()
            img_attached = bool(curr_img_path and curr_img_path != "nan" and os.path.exists(curr_img_path))

            mock_col1, mock_col2 = st.columns([1, 1])
            with mock_col1:
                if img_attached:
                    st.success(f"🖼️ Mockup Attached: `{os.path.basename(curr_img_path)}`")
                    st.image(curr_img_path, caption=f"Concept Preview for {selected_lead_name}", use_container_width=True)
                    if st.button("🗑️ Detach Mockup", key=f"del_img_{selected_lead_name}"):
                        df["image_path"] = df["image_path"].astype(str)
                        df.loc[df["name"] == selected_lead_name, "image_path"] = ""
                        save_leads_df(df)
                        st.info("Mockup detached.")
                        st.rerun()
                else:
                    st.info("No mockup attached. You can attach a website redesign mockup, homepage wireframe, or concept graphic. It will be embedded inline in the HTML email.")

            with mock_col2:
                uploaded_mockup = st.file_uploader(
                    "Upload Design Mockup / Screenshot (PNG, JPG, WEBP):",
                    type=["png", "jpg", "jpeg", "webp"],
                    key=f"uploader_{selected_lead_name}"
                )
                if uploaded_mockup is not None:
                    os.makedirs("attachments", exist_ok=True)
                    clean_slug = re.sub(r"[^a-zA-Z0-9_-]", "_", selected_lead_name).lower()[:30]
                    ext = uploaded_mockup.name.split(".")[-1].lower()
                    save_path = os.path.join("attachments", f"{clean_slug}_mockup.{ext}")
                    with open(save_path, "wb") as f_out:
                        f_out.write(uploaded_mockup.getbuffer())
                    df["image_path"] = df["image_path"].astype(str)
                    df.loc[df["name"] == selected_lead_name, "image_path"] = save_path
                    save_leads_df(df)
                    st.success(f"✅ Mockup attached! Embedded inline in outreach.")
                    st.rerun()

            st.divider()

            # Live Rendered Email Preview (Exact recipient view with styling, bullets & mockup)
            st.markdown("##### 👁️ Live Formatted Email Preview (Exact Recipient View)")
            clean_preview_body = clean_body_markdown(str(new_body or ""))
            preview_img = str(df.loc[df["name"] == selected_lead_name, "image_path"].iloc[0]).strip()
            has_preview_img = bool(preview_img and preview_img != "nan" and os.path.exists(preview_img))
            try:
                html_preview = sender.body_to_html(
                    clean_preview_body,
                    image_path=preview_img if has_preview_img else None,
                    lead_name=str(selected_lead_name)
                )
            except Exception:
                import importlib
                importlib.reload(sender)
                try:
                    html_preview = sender.body_to_html(clean_preview_body)
                except Exception:
                    html_preview = f"<pre style='font-family: inherit;'>{clean_preview_body}</pre>"
            components.html(html_preview, height=450, scrolling=True)

            btn_save_col, btn_ai_col = st.columns([1, 1])
            with btn_save_col:
                if st.button("💾 Save Changes to CSV", type="primary", use_container_width=True):
                    df["subject"] = df["subject"].astype(str)
                    df["body"] = df["body"].astype(str)
                    df.loc[df["name"] == selected_lead_name, "subject"] = str(new_subject)
                    df.loc[df["name"] == selected_lead_name, "body"] = clean_body_markdown(str(new_body))
                    save_leads_df(df)
                    st.success(f"Changes saved for {selected_lead_name}!")
                    st.rerun()

            with btn_ai_col:
                if st.button("✨ (Re)Generate Draft with Gemini AI", use_container_width=True):
                    if not os.environ.get("GEMINI_API_KEY"):
                        st.error("❌ GEMINI_API_KEY not found in Streamlit Secrets!")
                    else:
                        with st.spinner(f"Drafting personalized pitch for {selected_lead_name} with Gemini..."):
                            lead_dict = lead_row.to_dict()
                            if isinstance(lead_dict.get("issues"), str):
                                lead_dict["issues"] = [i.strip() for i in lead_dict["issues"].split("|") if i.strip()]
                            subj, bdy, d_src = draft(lead_dict, city_input)
                            clean_bdy = clean_body_markdown(bdy)
                            df["subject"] = df["subject"].astype(str)
                            df["body"] = df["body"].astype(str)
                            df["draft_source"] = df["draft_source"].astype(str)
                            df.loc[df["name"] == selected_lead_name, "subject"] = str(subj)
                            df.loc[df["name"] == selected_lead_name, "body"] = str(clean_bdy)
                            df.loc[df["name"] == selected_lead_name, "draft_source"] = str(d_src)
                            save_leads_df(df)
                        st.success(f"✅ Generated via {d_src}!")
                        st.rerun()

    st.divider()
    # Download CSV Button
    csv_bytes = df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Download leads.csv",
        data=csv_bytes,
        file_name="leads.csv",
        mime="text/csv",
        use_container_width=True
    )

