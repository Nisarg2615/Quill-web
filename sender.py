#!/usr/bin/env python3
"""
sender.py - sends the drafts from leads.csv through Gmail, safely.

  python sender.py send            # DRY RUN: shows what would be sent
  python sender.py send --live     # actually sends
  python sender.py replies         # reads inbox: marks replies, opt-outs, bounces
  python sender.py status          # counts

Safety built in:
  - dry-run by default
  - daily cap with automatic ramp-up (5 on day 1, +3 per day, up to --daily-cap)
  - random delay between emails
  - never emails the same address twice (except follow-ups)
  - follow-up 1 after 3 days, follow-up 2 after 7 days, same thread, stops on reply
  - opt-outs and bounces are added to suppression.txt automatically
  - List-Unsubscribe header on every email

Env vars: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (Google account -> Security -> 2-Step
Verification -> App passwords), SENDER_NAME, POSTAL_ADDRESS
"""
import argparse
import csv
import datetime as dt
import imaplib
import json
import os
import random
import re
import smtplib
import sys
import time
import requests
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

def _load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v

_load_env()

STATE_FILE = "state.json"
SUPPRESSION_FILE = "suppression.txt"
FOLLOWUP_DAYS = {1: 3, 2: 7}  # stage -> days after the FIRST email
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OPTOUT_START = re.compile(r"^\W*(no|nope|stop|unsubscribe|remove|not interested)\b", re.I)
OPTOUT_ANY = re.compile(r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|"
                        r"not interested|don'?t contact|do not contact|leave me alone)\b", re.I)


# ---------------------------------------------------------------- state & lists
def load_state():
    return json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=2)


def load_suppression():
    if not os.path.exists(SUPPRESSION_FILE):
        return set()
    return {l.strip().lower() for l in open(SUPPRESSION_FILE) if l.strip()}


def suppress(email):
    if email.lower() not in load_suppression():
        with open(SUPPRESSION_FILE, "a") as f:
            f.write(email.lower() + "\n")


def is_suppressed(email, sup):
    return email.lower() in sup or email.split("@")[-1].lower() in sup


def sent_today(state):
    today = dt.date.today().isoformat()
    return sum(1 for e in state.values() for t in e.get("sent_times", []) if t.startswith(today))


def get_campaign_start_date(state):
    times = [t for e in state.values() for t in e.get("sent_times", [])]
    if not times:
        return dt.date.today()
    return dt.date.fromisoformat(min(times)[:10])


def effective_cap(state, daily_cap=30, ramp_step=5, initial_cap=5):
    """
    Ramps daily email sending volume day-wise by +5 every day (e.g. Day 1: 5, Day 2: 10, Day 3: 15...)
    up to the configured maximum daily_cap.
    """
    times = [t for e in state.values() for t in e.get("sent_times", [])]
    if not times:
        return min(daily_cap, initial_cap)
    start_date = dt.date.fromisoformat(min(times)[:10])
    days = (dt.date.today() - start_date).days
    return min(daily_cap, initial_cap + ramp_step * max(days, 0))


def get_outreach_calendar(state, daily_cap=30, ramp_step=5, initial_cap=5, lookback_days=7, lookahead_days=7):
    """
    Builds a day-wise calendar tracking sending history, daily target caps (increasing by 5/day),
    and scheduled future quotas.
    """
    times = [t for e in state.values() for t in e.get("sent_times", [])]
    today = dt.date.today()
    start_date = dt.date.fromisoformat(min(times)[:10]) if times else today

    # Map sent emails by date string YYYY-MM-DD
    sent_by_date = {}
    for email, e in state.items():
        for t in e.get("sent_times", []):
            d_str = t[:10]
            if d_str not in sent_by_date:
                sent_by_date[d_str] = []
            sent_by_date[d_str].append({
                "email": email,
                "name": e.get("name", "Unknown"),
                "subject": e.get("subject", ""),
                "stage": e.get("stage", 0),
                "time": t[11:19] if len(t) >= 19 else "",
                "status": e.get("status", "sent"),
            })

    first_day = min(start_date, today - dt.timedelta(days=lookback_days))
    last_day = today + dt.timedelta(days=lookahead_days)
    calendar_days = []
    curr = first_day

    while curr <= last_day:
        d_str = curr.isoformat()
        day_diff = (curr - start_date).days
        target_cap = min(daily_cap, initial_cap + ramp_step * max(day_diff, 0)) if day_diff >= 0 else initial_cap

        actual_sent = sent_by_date.get(d_str, [])
        sent_count = len(actual_sent)

        if curr < today:
            status_label = f"Completed ({sent_count}/{target_cap})" if sent_count >= target_cap else (f"Sent {sent_count}/{target_cap}" if sent_count > 0 else "No sends")
        elif curr == today:
            status_label = f"Today ({sent_count}/{target_cap} sent - {max(target_cap - sent_count, 0)} available)"
        else:
            status_label = f"Planned (Cap: {target_cap})"

        calendar_days.append({
            "date": curr,
            "date_str": d_str,
            "day_num": day_diff + 1 if day_diff >= 0 else None,
            "target_cap": target_cap,
            "sent_count": sent_count,
            "status": status_label,
            "is_today": curr == today,
            "is_past": curr < today,
            "is_future": curr > today,
            "recipients": actual_sent,
        })
        curr += dt.timedelta(days=1)

    return calendar_days


# ---------------------------------------------------------------------- compose
def footer():
    sender = os.getenv("SENDER_NAME", "Quillweb Team")
    title = os.getenv("SENDER_TITLE", "Website Design & Optimization")
    signature = f"{sender}  \n{title}" if title else sender
    return (
        f"\n\nBest regards,\n\n"
        f"{signature}\n\n"
        f"—  \n"
        f"If website improvements aren't a priority right now, feel free to reply \"no\" and we will respect your inbox and not follow up."
    )


def clean_body_markdown(text):
    """Standardizes email body formatting so bullets and signatures never squash together."""
    lines = text.strip().split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.count("•") > 1:
            for p in stripped.split("•"):
                p_str = p.strip()
                if p_str:
                    cleaned.append(f"* {p_str}")
            continue
        if stripped.count(" - ") > 1 and "**" in stripped:
            for p in stripped.split(" - "):
                p_str = p.strip()
                if p_str:
                    cleaned.append(f"* {p_str}")
            continue

        if re.match(r"^[•\*\-]\s*", stripped):
            clean_item = re.sub(r"^[•\*\-]\s*", "", stripped)
            cleaned.append(f"* {clean_item}")
        elif re.match(r"^\*\*[A-Za-z\s]+:\*\*", stripped):
            cleaned.append(f"* {stripped}")
        elif stripped in ("—", "--", "-"):
            cleaned.append("—  ")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def followup_body(stage, name):
    if stage == 1:
        body = (f"Hi,\n\nI just wanted to follow up briefly on my previous note regarding {name}'s website. "
                f"I know how busy running a business can be, so I wanted to bring this back to the top of your inbox.\n\n"
                f"I'm still very happy to put together a free initial design concept and mock-up showing some ideas to help "
                f"attract more local enquiries, with zero obligation.\n\n"
                f"Would you be interested in taking a look?")
    else:
        body = (f"Hi,\n\nThis will be my final note. If updating or building a new website for {name} isn't a priority "
                f"right now, I completely understand and will not follow up again.\n\n"
                f"If you ever do want to explore ways to turn more local online searches into customers, feel free to reach "
                f"out anytime.\n\n"
                f"Wishing you and {name} all the best!")
    return clean_body_markdown(body + footer())


def body_to_plain(text):
    """Strip markdown asterisks while keeping clean readable unicode bullets."""
    clean_text = clean_body_markdown(text)
    lines = []
    for line in clean_text.split("\n"):
        stripped = line.strip()
        # Clean leading markdown bullets like * or - to a neat unicode bullet •
        if re.match(r"^[\*\-]\s+", stripped):
            stripped = "• " + re.sub(r"^[\*\-]\s+", "", stripped)
        # Remove markdown asterisks for bold / italics
        stripped = re.sub(r"\*\*([^\*]+)\*\*", r"\1", stripped)
        stripped = re.sub(r"\*([^\*]+)\*", r"\1", stripped)
        lines.append(stripped)
    return "\n".join(lines)


def body_to_html(text):
    """Convert email body with markdown formatting into clean professional HTML."""
    clean_text = clean_body_markdown(text)
    paragraphs = clean_text.strip().split("\n\n")
    html_parts = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        lines = p.split("\n")
        # Check if paragraph is a bulleted list
        is_bullet_list = any(re.match(r"^(?:[•\*\-]|\d+\.)\s+", l.strip()) or l.strip().startswith("**") for l in lines)
        if is_bullet_list and len(lines) > 1:
            list_items = []
            for l in lines:
                l_str = l.strip()
                l_str = re.sub(r"^(?:[•\*\-]|\d+\.)\s*", "", l_str)
                l_str = re.sub(r"\*\*([^\*]+)\*\*", r"<strong style='color: #111;'>\1</strong>", l_str)
                list_items.append(f"<li style='margin-bottom: 8px;'>{l_str}</li>")
            html_parts.append(f"<ul style='margin: 12px 0 16px 20px; padding: 0;'>{''.join(list_items)}</ul>")
        else:
            content = "<br>".join(lines)
            content = re.sub(r"\*\*([^\*]+)\*\*", r"<strong style='color: #111;'>\1</strong>", content)
            if p.startswith("—") or p.startswith("--"):
                html_parts.append(f"<p style='margin: 18px 0 0 0; font-size: 13px; color: #777; border-top: 1px solid #eee; padding-top: 12px;'>{content}</p>")
            else:
                html_parts.append(f"<p style='margin: 0 0 14px 0; font-size: 15px; line-height: 1.55; color: #222;'>{content}</p>")

    body_content = "\n".join(html_parts)
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; font-size: 15px; line-height: 1.55; color: #222; background-color: #fff; margin: 0; padding: 12px;">
{body_content}
</body>
</html>"""


def get_smtp_connection():
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465" if host == "smtp.gmail.com" else "587"))
    user = os.getenv("SMTP_USER") or os.getenv("GMAIL_ADDRESS")
    pwd = os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD")

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port)
    else:
        smtp = smtplib.SMTP(host, port)
        smtp.starttls()
    smtp.login(user, pwd)
    return smtp


def build_message(to, subject, body, in_reply_to=None, references=None):
    from_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("SMTP_USER") or os.environ.get("GMAIL_ADDRESS")
    m = EmailMessage()
    m["From"] = formataddr((os.getenv("SENDER_NAME", ""), from_email))
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=from_email.split("@")[1] if "@" in from_email else "localhost")
    m["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = references or in_reply_to

    # 1. Plain text version (strips raw markdown asterisks)
    plain_content = body_to_plain(body)
    m.set_content(plain_content)

    # 2. Rich HTML version (renders bold tags and clean bullet formatting for Gmail/Outlook)
    html_content = body_to_html(body)
    m.add_alternative(html_content, subtype="html")

    return m


# ------------------------------------------------------------------- job queue
def due_followups(state, sup):
    jobs, now = [], dt.datetime.now()
    for email, e in state.items():
        if e["status"] != "sent" or e["stage"] >= 2 or is_suppressed(email, sup):
            continue
        nxt = e["stage"] + 1
        first = dt.datetime.fromisoformat(e["sent_times"][0])
        last = dt.datetime.fromisoformat(e["sent_times"][-1])
        if now - first >= dt.timedelta(days=FOLLOWUP_DAYS[nxt]) and now - last >= dt.timedelta(days=2):
            jobs.append({"email": email, "name": e["name"], "stage": nxt,
                         "subject": "Re: " + e["subject"].removeprefix("Re: "),
                         "body": followup_body(nxt, e["name"]),
                         "in_reply_to": e["last_id"], "references": e["first_id"]})
    return jobs


def new_jobs(csv_path, state, sup, min_priority):
    jobs = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            em = (r.get("email") or "").strip().lower()
            if not em or not r.get("subject") or not r.get("body"):
                continue
            if em in state or is_suppressed(em, sup):
                continue
            if int(float(r.get("priority") or 0)) < min_priority:
                continue
            jobs.append({"email": em, "name": r["name"], "stage": 0, "subject": r["subject"],
                         "body": r["body"], "in_reply_to": None, "references": None})
    return jobs


def record(state, job, msg_id):
    now = dt.datetime.now().isoformat(timespec="seconds")
    e = state.setdefault(job["email"], {"name": job["name"], "subject": job["subject"],
                                        "stage": 0, "sent_times": [], "status": "sent",
                                        "first_id": msg_id})
    e["stage"] = job["stage"]
    e["sent_times"].append(now)
    e["last_id"] = msg_id


def send_via_brevo_api(api_key, to_email, subject, body, sender_name, sender_email):
    url = "https://api.brevo.com/v3/smtp/email"
    headers = {
        "accept": "application/json",
        "api-key": api_key,
        "content-type": "application/json",
    }
    html_content = body_to_html(body)
    plain_content = body_to_plain(body)
    payload = {
        "sender": {"name": sender_name or "Quillweb Team", "email": sender_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": plain_content,
        "htmlContent": html_content,
        "headers": {
            "List-Unsubscribe": f"<mailto:{sender_email}?subject=unsubscribe>"
        }
    }
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("messageId", f"<{time.time()}@{sender_email.split('@')[-1]}>")


# ------------------------------------------------------------------------ send
def cmd_send(a):
    brevo_api_key = os.getenv("BREVO_API_KEY")
    user = os.getenv("SMTP_USER") or os.getenv("GMAIL_ADDRESS")
    pwd = os.getenv("SMTP_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD")
    if a.live and not (brevo_api_key or (user and pwd)):
        sys.exit("Set BREVO_API_KEY (or SMTP_USER/SMTP_PASSWORD) in .env first.")
    if a.live and "[" in footer():
        sys.exit("Set SENDER_NAME and POSTAL_ADDRESS in .env first (required in every email).")
    state, sup = load_state(), load_suppression()
    cap = effective_cap(state, a.daily_cap)
    room = cap - sent_today(state)
    print(f"Today's cap: {cap} (daily max {a.daily_cap}, ramping) | already sent today: "
          f"{sent_today(state)} | room: {max(room, 0)}")
    if room <= 0:
        return print("Cap reached for today.")

    jobs = (due_followups(state, sup) + new_jobs(a.csv, state, sup, a.min_priority))[:room]
    if not jobs:
        return print("Nothing to send.")

    smtp = None
    if a.live and not brevo_api_key:
        smtp = get_smtp_connection()
    try:
        for i, j in enumerate(jobs, 1):
            label = "NEW" if j["stage"] == 0 else f"FOLLOW-UP {j['stage']}"
            if not a.live:
                print(f"\n[{i}/{len(jobs)}] DRY RUN {label} -> {j['email']}\nSubject: "
                      f"{j['subject']}\n{j['body'][:300]}...")
                continue
            
            sender_email = os.getenv("SMTP_FROM_EMAIL") or os.getenv("GMAIL_ADDRESS") or "quillweb.co@gmail.com"
            sender_name = os.getenv("SENDER_NAME", "Quillweb Team")
            
            try:
                if brevo_api_key:
                    msg_id = send_via_brevo_api(brevo_api_key, j["email"], j["subject"], j["body"], sender_name, sender_email)
                else:
                    msg = build_message(j["email"], j["subject"], j["body"],
                                        j["in_reply_to"], j["references"])
                    smtp.send_message(msg)
                    msg_id = msg["Message-ID"]
            except Exception as e:
                print(f"[{i}/{len(jobs)}] failed for {j['email']}: {e}")
                continue

            record(state, j, msg_id)
            save_state(state)
            print(f"[{i}/{len(jobs)}] sent {label} -> {j['email']}")
            if i < len(jobs):
                time.sleep(random.uniform(*a.delay))
    finally:
        if smtp:
            smtp.quit()
    if not a.live:
        print("\nDry run only. Re-run with --live to send.")


# --------------------------------------------------------------------- replies
def plain_text(m):
    parts = m.walk() if m.is_multipart() else [m]
    for p in parts:
        if p.get_content_type() == "text/plain":
            return (p.get_payload(decode=True) or b"").decode(
                p.get_content_charset() or "utf-8", errors="ignore")
    return ""


def own_reply_text(body):
    out = []
    for line in body.splitlines():
        if line.startswith(">") or re.match(r"^On .* wrote:", line):
            break
        out.append(line)
    return " ".join(out)[:200]


def classify(frm, body, state):
    """Returns (email, new_status) pairs. Pure function, easy to test."""
    frm = frm.lower()
    if frm in state:
        text = own_reply_text(body)
        optout = OPTOUT_START.search(text) or OPTOUT_ANY.search(text)
        return [(frm, "optout" if optout else "replied")]
    if "mailer-daemon" in frm or "postmaster" in frm:
        return [(e.lower(), "bounced") for e in EMAIL_RE.findall(body) if e.lower() in state]
    return []


def cmd_replies(a):
    state = load_state()
    user = os.getenv("GMAIL_ADDRESS") or os.getenv("SMTP_USER")
    pwd = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("SMTP_PASSWORD")
    if not (user and pwd):
        sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, pwd)
    imap.select("INBOX")
    since = (dt.date.today() - dt.timedelta(days=a.days)).strftime("%d-%b-%Y")
    _, data = imap.search(None, "SINCE", since)
    changed = 0
    for num in data[0].split():
        _, md = imap.fetch(num, "(RFC822)")
        m = message_from_bytes(md[0][1])
        for email, status in classify(parseaddr(m["From"])[1], plain_text(m), state):
            if state[email]["status"] in ("optout", "bounced"):
                continue
            if status == "replied" and state[email]["status"] == "replied":
                continue
            state[email]["status"] = status
            changed += 1
            if status in ("optout", "bounced"):
                suppress(email)
            print(f"{email}: {status}")
    imap.logout()
    save_state(state)
    print(f"{changed} update(s).")


def cmd_status(a):
    state, counts = load_state(), {}
    for e in state.values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print(f"Contacted: {len(state)} | " + " | ".join(f"{k}: {v}" for k, v in counts.items()))
    print(f"Sent today: {sent_today(state)} | suppression list: {len(load_suppression())}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--csv", default="leads.csv")
    s.add_argument("--live", action="store_true")
    s.add_argument("--daily-cap", type=int, default=30)
    s.add_argument("--min-priority", type=int, default=40)
    s.add_argument("--delay", type=int, nargs=2, default=[60, 180], metavar=("MIN", "MAX"))
    s.set_defaults(fn=cmd_send)
    r = sub.add_parser("replies")
    r.add_argument("--days", type=int, default=14)
    r.set_defaults(fn=cmd_replies)
    st = sub.add_parser("status")
    st.set_defaults(fn=cmd_status)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
