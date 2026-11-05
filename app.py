import os
import csv
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
import streamlit as st
import pandas as pd

# Load environment variables
from leadgen import (
    _load_env, NICHES, geocode, fetch_businesses, parse_element,
    audit_lead, score, load_suppression, suppressed, draft, is_social, norm_url
)
import sender

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
        df = pd.read_csv(filepath)
        # Ensure standard columns exist
        for col in ["priority", "name", "niche", "need", "email", "phone", "website",
                    "address", "issues", "subject", "body", "draft_source", "lat", "lon", "osm_id"]:
            if col not in df.columns:
                df[col] = ""
        return df
    except Exception as e:
        st.error(f"Error loading {filepath}: {e}")
        return pd.DataFrame()


def save_leads_df(df, filepath="leads.csv"):
    df.to_csv(filepath, index=False, encoding="utf-8")


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
        enable_draft = st.checkbox("Generate Personalized Drafts", value=True)
        worker_threads = st.slider("Audit Worker Threads:", 2, 12, 6)

    run_scrape = st.button("🔍 Find & Audit Leads", type="primary", use_container_width=True)

    st.divider()
    st.subheader("📬 Mailer Status")
    sender_state = sender.load_state()
    sent_today_count = sender.sent_today(sender_state)
    eff_cap = sender.effective_cap(sender_state, 30)
    st.metric("Sent Today", f"{sent_today_count} / {eff_cap}")
    st.metric("Total Contacted", len(sender_state))
    st.caption("Powered by Gmail Direct SMTP")


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

            if enable_draft:
                status_text.info(f"✍️ Step 4/4: Drafting personalized emails with Gemini AI...")
                progress_bar.progress(85)
                for l in scored_leads:
                    if l["email"]:
                        subj, bdy, src = draft(l, city_input)
                        l["subject"], l["body"], l["draft_source"] = subj, bdy, src
                    else:
                        l["subject"], l["body"], l["draft_source"] = "", "", ""

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

df = load_leads_df()

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
    tab_leads, tab_outreach, tab_editor = st.tabs(["📋 Filter & Explore Leads", "🚀 Outreach & Sending", "✏️ Email Draft Editor"])

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
                    elif src:
                        st.markdown('<span class="badge-offline">📄 Offline Template</span>', unsafe_allow_html=True)
                    st.caption(f"Priority Score: **{row['priority']}**")

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
                        with st.expander(f"To: {j['email']} ({j['name']}) - {j['subject']}"):
                            st.text(j["body"])

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
                            msg = sender.build_message(j["email"], j["subject"], j["body"], j["in_reply_to"], j["references"])
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

    # --- TAB 3: Email Draft Editor ---
    with tab_editor:
        st.subheader("✏️ Inspect & Edit Email Drafts Before Sending")
        email_leads = df[df["email"].notna() & (df["email"] != "")]
        if email_leads.empty:
            st.info("No leads with emails available to edit.")
        else:
            selected_lead_name = st.selectbox("Select Business to Review Draft:", email_leads["name"].tolist())
            lead_row = df[df["name"] == selected_lead_name].iloc[0]

            col_info1, col_info2 = st.columns([1, 1])
            with col_info1:
                st.write(f"**Business:** {lead_row['name']}")
                st.write(f"**Email:** `{lead_row['email']}`")
                st.write(f"**Draft Source:** `{lead_row.get('draft_source', 'Offline Template')}`")
            with col_info2:
                st.write(f"**Audited Issues:** {lead_row.get('issues', 'None')}")
                st.write(f"**Website:** {lead_row.get('website', 'None')}")

            st.divider()
            new_subject = st.text_input("Email Subject:", value=str(lead_row.get("subject", "")))
            new_body = st.text_area("Email Body:", value=str(lead_row.get("body", "")), height=350)

            if st.button("💾 Save Changes to CSV", type="primary"):
                df.loc[df["name"] == selected_lead_name, "subject"] = new_subject
                df.loc[df["name"] == selected_lead_name, "body"] = new_body
                save_leads_df(df)
                st.success(f"Changes saved for {selected_lead_name}!")

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

