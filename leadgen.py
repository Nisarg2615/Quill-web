#!/usr/bin/env python3
"""
leadgen.py - free lead-gen pipeline for a website-design service.

Stages:
  1. Discover  : OpenStreetMap (Nominatim + Overpass), no API key needed
  2. Audit     : fetch each business's site, flag fixable problems (optional PageSpeed)
  3. Contact   : scrape a public email from the site / contact page
  4. Score     : rank leads by "how much they need a new site" + contactability
  5. Draft     : personalised email per lead (Gemini free tier, or offline template)

Usage:
  python leadgen.py --niche plumbers --city "Austin, TX" --limit 100 --draft
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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

CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "unset")
HEADERS = {"User-Agent": f"leadgen/0.1 ({CONTACT_EMAIL})"}
OVERPASS = "https://overpass-api.de/api/interpreter"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
THIS_YEAR = datetime.date.today().year

# niche -> OSM tag. Use --tag key=value for anything not listed.
NICHES = {
    "plumbers": "craft=plumber",
    "electricians": "craft=electrician",
    "hvac": "craft=hvac",
    "roofers": "craft=roofer",
    "landscapers": "craft=gardener",
    "locksmiths": "craft=locksmith",
    "dentists": "amenity=dentist",
    "vets": "amenity=veterinary",
    "physios": "healthcare=physiotherapist",
    "salons": "shop=hairdresser",
    "barbers": "shop=hairdresser",
    "gyms": "leisure=fitness_centre",
    "restaurants": "amenity=restaurant",
    "cafes": "amenity=cafe",
    "lawyers": "office=lawyer",
    "accountants": "office=accountant",
    "realtors": "office=estate_agent",
    "driving_schools": "amenity=driving_school",
}

NICHE_DETAILS = {
    "plumbers": ("plumbing and heating", "plumbing repairs, drain clearing, boiler servicing and emergency call-outs"),
    "electricians": ("electrical services", "electrical repairs, rewiring, lighting installations and safety inspections"),
    "hvac": ("heating and air conditioning", "AC repairs, heating installations, ventilation and routine servicing"),
    "roofers": ("roofing services", "roof repairs, replacements, guttering and weatherproofing"),
    "landscapers": ("landscaping and garden care", "garden design, lawn care, patio installations and outdoor maintenance"),
    "locksmiths": ("locksmith services", "emergency lockouts, lock repairs, key cutting and security upgrades"),
    "dentists": ("dental care", "routine check-ups, teeth whitening, cosmetic treatments and emergency appointments"),
    "vets": ("veterinary care", "pet examinations, vaccinations, surgeries and wellness plans"),
    "physios": ("physiotherapy and rehabilitation", "injury recovery, joint treatments, sports physio and pain relief"),
    "salons": ("hair and beauty services", "hair styling, cuts, colouring and beauty treatments"),
    "barbers": ("barbering and grooming", "haircuts, beard styling, hot towel shaves and grooming"),
    "gyms": ("fitness training", "gym memberships, personal training, group fitness classes and wellness"),
    "restaurants": ("dining and hospitality", "table reservations, seasonal menus, private dining and events"),
    "cafes": ("cafe and coffee services", "specialty coffee, breakfast, lunch menus and takeaway orders"),
    "lawyers": ("legal services", "legal advice, consultations, dispute resolution and specialist representation"),
    "accountants": ("accounting and tax services", "tax returns, bookkeeping, payroll and business financial planning"),
    "realtors": ("real estate and property", "property listings, valuations, buyer consultations and rentals"),
    "driving_schools": ("driving instruction", "driving lessons, intensive courses, theory support and mock tests"),
}

SOCIAL = ("facebook.com", "instagram.com", "linktr.ee", "yelp.com", "tiktok.com",
          "twitter.com", "x.com", "business.site", "google.com")
BAD_EMAIL = ("example.", "sentry", "wixpress", "domain.com", "yourname", "email@",
             "godaddy", "schema.org", "@2x", "u003e")
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BOOK_WORDS = ("book now", "book online", "schedule", "appointment", "get a quote",
              "free quote", "request a quote", "contact us", "reservation")


# ---------------------------------------------------------------- 1. discover
def geocode(city):
    r = requests.get(NOMINATIM, params={"q": city, "format": "json", "limit": 1},
                     headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        sys.exit(f"Could not find '{city}' on OpenStreetMap.")
    s, n, w, e = map(float, data[0]["boundingbox"])
    return s, w, n, e  # Overpass order: south, west, north, east


def fetch_businesses(tag, bbox):
    k, v = tag.split("=", 1)
    s, w, n, e = bbox
    q = (f'[out:json][timeout:90];('
         f'node["{k}"="{v}"]({s},{w},{n},{e});'
         f'way["{k}"="{v}"]({s},{w},{n},{e}););out center tags;')
    r = requests.post(OVERPASS, data={"data": q}, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.json().get("elements", [])


def parse_element(el, niche):
    t = el.get("tags", {})
    name = t.get("name")
    if not name or t.get("brand") or t.get("brand:wikidata"):  # skip chains
        return None
    addr = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street"),
                                t.get("addr:city")) if x)
    lat = el.get("lat") or (el.get("center", {}).get("lat") if isinstance(el.get("center"), dict) else "")
    lon = el.get("lon") or (el.get("center", {}).get("lon") if isinstance(el.get("center"), dict) else "")
    return {
        "name": name,
        "niche": niche,
        "address": addr,
        "phone": t.get("phone") or t.get("contact:phone") or "",
        "website": t.get("website") or t.get("contact:website") or "",
        "osm_email": t.get("email") or t.get("contact:email") or "",
        "osm_id": f'{el.get("type")}/{el.get("id")}',
        "lat": str(lat) if lat else "",
        "lon": str(lon) if lon else "",
    }


# ------------------------------------------------------------------- 2. audit
def norm_url(u):
    u = u.strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u


def is_social(url):
    host = urlparse(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in SOCIAL)


def pagespeed_score(url):
    """Mobile performance score 0-100 or None. Free; API key optional (higher quota)."""
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if os.getenv("PAGESPEED_API_KEY"):
        params["key"] = os.getenv("PAGESPEED_API_KEY")
    try:
        r = requests.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                         params=params, timeout=90)
        score = r.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return int(score * 100)
    except Exception:
        return None


def audit_html(html, final_url, elapsed):
    """Pure function: returns (issues, need_points). Easy to test offline."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    issues, need = [], 0

    def flag(msg, pts):
        nonlocal need
        issues.append(msg)
        need += pts

    if not final_url.startswith("https://"):
        flag("No HTTPS - browsers label the site 'Not secure'", 20)
    if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
        flag("Not mobile-friendly (no responsive viewport)", 25)
    if not soup.find("meta", attrs={"name": re.compile("^description$", re.I)}):
        flag("No search-result description (hurts Google visibility)", 10)
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    if len(title) < 10:
        flag("Missing or very short page title (SEO)", 10)
    has_form = bool(soup.find("form"))
    if not has_form and not any(w in text for w in BOOK_WORDS):
        flag("No contact form or online booking / quote option", 15)
    if not soup.select('a[href^="tel:"]'):
        flag("Phone number isn't tap-to-call on mobile", 10)
    years = [int(y) for y in re.findall(
        r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", html, re.I)]
    if years and max(years) <= THIS_YEAR - 2:
        flag(f"Looks outdated (footer says {max(years)})", 15)
    if elapsed > 3:
        flag(f"Slow to respond ({elapsed:.1f}s)", 15)
    return issues, min(need, 100)


def clean_emails(found, site_host):
    out = []
    for e in found:
        el = e.lower().strip(".")
        if el.endswith(IMG_EXT) or any(b in el for b in BAD_EMAIL):
            continue
        if el not in out:
            out.append(el)
    host = site_host.lower().removeprefix("www.")
    out.sort(key=lambda e: 0 if e.split("@")[1].endswith(host) else 1)
    return out


def extract_emails(html):
    soup = BeautifulSoup(html, "html.parser")
    found = [a["href"][7:].split("?")[0] for a in soup.select('a[href^="mailto:"]')]
    found += EMAIL_RE.findall(soup.get_text(" "))
    return found


def get(url, timeout=12):
    t0 = time.time()
    r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    return r, time.time() - t0


def audit_lead(lead, use_pagespeed):
    site = norm_url(lead["website"])
    lead["issues"], lead["need"], lead["email"] = [], 0, lead["osm_email"]
    if not site:
        lead.update(issues=["No website"], need=100)
        return lead
    if is_social(site):
        lead.update(issues=["Only a social/listing page, no real website"], need=90)
        return lead
    try:
        r, elapsed = get(site)
        r.raise_for_status()
    except Exception:
        lead.update(issues=["Website is down or unreachable"], need=95)
        return lead

    issues, need = audit_html(r.text, r.url, elapsed)
    if use_pagespeed:
        ps = pagespeed_score(r.url)
        if ps is not None and ps < 50:
            issues.append(f"Poor mobile speed score ({ps}/100 on Google PageSpeed)")
            need = min(100, need + 20)
    lead["issues"], lead["need"] = issues, need

    if not lead["email"]:
        host = urlparse(r.url).netloc
        emails = clean_emails(extract_emails(r.text), host)
        if not emails:  # try up to 2 contact/about pages
            soup = BeautifulSoup(r.text, "html.parser")
            links = []
            for a in soup.find_all("a", href=True):
                if re.search(r"contact|about", a["href"], re.I):
                    u = urljoin(r.url, a["href"])
                    if urlparse(u).netloc == host and u not in links:
                        links.append(u)
            for u in links[:2]:
                try:
                    r2, _ = get(u, timeout=10)
                    emails = clean_emails(extract_emails(r2.text), host)
                    if emails:
                        break
                except Exception:
                    continue
        lead["email"] = emails[0] if emails else ""
    return lead


# --------------------------------------------------------------- 3. score/rank
def score(lead):
    no_site = not lead.get("website") or any(
        x in (lead.get("issues") or []) for x in ["No website", "Only a social"]
    )
    # No-website leads are highest priority (need=100 from audit)
    # We boost them further regardless of email availability
    lead["priority"] = round(
        lead["need"] * 0.7
        + (20 if lead["email"] else 0)
        + (10 if lead["phone"] else 0)
        + (10 if no_site else 0)      # extra boost for no-website leads
    )
    return lead


# ------------------------------------------------------------------- 4. draft
def footer():
    sender = os.getenv("SENDER_NAME", "Quillweb Team")
    title = os.getenv("SENDER_TITLE", "Website Design & Optimization")
    signature = f"{sender}\n{title}" if title else sender
    return (
        f"\n\nBest regards,\n\n"
        f"{signature}\n\n"
        f"—\n"
        f"If website improvements aren't a priority right now, feel free to reply \"no\" and we will respect your inbox and not follow up."
    )


def get_niche_details(niche):
    clean = niche.lower().replace(" ", "_")
    if clean in NICHE_DETAILS:
        return NICHE_DETAILS[clean]
    label = niche.replace("_", " ").title()
    return (f"{label} services", f"{label} services, customer enquiries and consultations")


def template_draft(lead, city=""):
    name = lead["name"]
    issues = lead.get("issues", [])
    niche = lead.get("niche", "business")
    city_name = city.split(",")[0].strip() if city else "your area"
    niche_label, niche_services = get_niche_details(niche)

    has_no_site = not lead.get("website") or (issues and (issues[0] == "No website" or issues[0].startswith("Only a social")))

    if has_no_site:
        subject = f"{name} - website & online enquiries"
        body = (
            f"Hi,\n\n"
            f"I recently came across {name} and was interested in the range of services you offer, particularly your {niche_services}.\n\n"
            f"I noticed that your business has built a strong reputation among customers, and I believe there's a strong opportunity to take that reputation further with a professional website.\n\n"
            f"I'd love to help you design and develop a modern, professional website for {name} that would:\n\n"
            f"**Attract more local customers:** Help people searching online for {niche_label} in {city_name} and surrounding areas discover your business.\n"
            f"**Generate more enquiries:** Include dedicated service pages, enquiry forms and prominent call buttons so potential customers can easily contact you for {niche_label}.\n"
            f"**Showcase your products and services:** Present your different services, specialties and expertise in an organised, professional way.\n"
            f"**Build customer confidence:** Display your existing customer reviews, experience and credentials to help potential customers feel confident choosing your business.\n"
            f"**Make contacting you easier:** Provide your location, business hours, contact details and directions in one convenient place.\n\n"
            f"My aim wouldn't just be to create a visually appealing website, but to build something that actively supports your business by helping you reach more potential customers and convert online searches into genuine enquiries.\n\n"
            f"I'd be happy to discuss some ideas for your website and even put together an initial design concept to show you what it could look like, with no obligation.\n\n"
            f"Would you be open to having a quick conversation about this?"
        )
    else:
        bullets = "\n".join(f"- {i}" for i in issues[:3]) if issues else "- A few technical improvements to boost visibility & speed"
        subject = f"Website ideas for {name}"
        body = (
            f"Hi,\n\n"
            f"I recently came across {name} and was interested in the range of services you offer, particularly your {niche_services}.\n\n"
            f"I noticed that your business has built a strong reputation in {city_name}. While reviewing your website, I noticed a few specific areas where small updates could significantly increase the number of enquiries you receive:\n\n"
            f"{bullets}\n\n"
            f"I'd love to help you refresh and modernise {name}'s website to:\n\n"
            f"**Attract more local customers:** Optimise your search visibility so potential clients searching in {city_name} and surrounding areas find {name} first.\n"
            f"**Generate more enquiries:** Include dedicated service pages, frictionless quote/booking forms and prominent tap-to-call buttons on mobile.\n"
            f"**Showcase your full services:** Present your range of services, credentials and customer testimonials in a modern, organised layout.\n"
            f"**Build customer confidence:** Highlight your reviews, ratings and experience so prospects immediately trust choosing you over competitors.\n"
            f"**Fast, mobile-friendly experience:** Ensure your website loads quickly and looks flawless across all smartphones, tablets and desktop computers.\n\n"
            f"My aim wouldn't just be to create a visually appealing website, but to build something that actively supports your business by turning online visitors into genuine enquiries.\n\n"
            f"I'd be happy to discuss some ideas for your website and even put together an initial design concept to show you what it could look like, with no obligation.\n\n"
            f"Would you be open to having a quick conversation about this?"
        )

    return subject, body + footer()


def gemini_draft(lead, city=""):
    key = os.getenv("GEMINI_API_KEY")
    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    if not key:
        return None
    city_name = city.split(",")[0].strip() if city else "the local area"
    niche_label, niche_services = get_niche_details(lead.get("niche", "business"))
    issues = lead.get("issues", [])
    has_no_site = not lead.get("website") or (issues and (issues[0] == "No website" or issues[0].startswith("Only a social")))
    
    if has_no_site:
        context_prompt = (
            f"'{lead['name']}' currently DOES NOT have a website (or only has a basic social page). "
            f"Pitch building and launching a brand-new, modern website from scratch so they can capture local customers who search online."
        )
    else:
        problems = "; ".join(issues[:3]) if issues else "modernization and mobile optimization"
        context_prompt = (
            f"'{lead['name']}' already has a website, but our audit found these technical issues: {problems}. "
            f"Pitch modernizing and refreshing their website to fix these issues and increase client bookings."
        )

    prompt = (
        f"Write a warm, high-converting professional cold email from a website design studio (Quillweb Team) "
        f"to '{lead['name']}', a local {niche_label} business in {city_name}. "
        f"They specialize in: {niche_services}.\n"
        f"Situation: {context_prompt}\n"
        f"Follow this exact structure:\n"
        f"1. Warm opening acknowledging their business and services.\n"
        f"2. Compliment their local reputation and note the opportunity for an upgraded website.\n"
        f"3. 5 bullet points starting with bold tags: **Attract more local customers:**, **Generate more enquiries:**, **Showcase your full services:**, **Build customer confidence:**, **Fast, mobile-friendly experience:**\n"
        f"4. Closing explaining the aim is not just aesthetics, but helping them reach more customers and convert searches into genuine enquiries.\n"
        f"5. Low-friction offer to put together an initial design concept with no obligation.\n"
        f"6. A one-line question asking if they would be open to a quick conversation.\n"
        f"Return ONLY valid JSON: {{\"subject\": \"...\", \"body\": \"...\"}} where body has no signature or footer."
    )

    # Retry up to 3 times in case of temporary rate limits or spikes
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": key}, timeout=60,
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"responseMimeType": "application/json"}})
            if r.status_code == 200:
                raw_text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                # Strip markdown code fences if Gemini wraps in ```json ... ```
                raw_text = raw_text.strip()
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("```", 2)[-1]  # strip opening fence + lang tag
                    if "```" in raw_text:
                        raw_text = raw_text[:raw_text.rfind("```")]
                data = json.loads(raw_text.strip())
                return data["subject"], data["body"].strip() + footer()
            elif r.status_code in (429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            else:
                print(f"  [Gemini] HTTP {r.status_code}: {r.text[:200]}")
                break
        except json.JSONDecodeError as e:
            print(f"  [Gemini] JSON parse error (attempt {attempt+1}): {e} | raw: {raw_text[:100]}")
            break
        except Exception as e:
            print(f"  [Gemini] Exception (attempt {attempt+1}): {e}")
            time.sleep(2)
            continue
    return None


def draft(lead, city=""):
    time.sleep(5)  # stay safely within Gemini free-tier rate limits (5s = ~12/min < 15/min limit)
    res = gemini_draft(lead, city)
    if res:
        return res[0], res[1], "Gemini AI"
    # Gemini failed — wait a bit and use template
    print(f"  [draft] Gemini unavailable for '{lead.get('name', '?')}', using offline template.")
    res_tmpl = template_draft(lead, city)
    return res_tmpl[0], res_tmpl[1], "Offline Template"


# ----------------------------------------------------------------------- main
def load_suppression(path="suppression.txt"):
    if not os.path.exists(path):
        return set()
    return {l.strip().lower() for l in open(path) if l.strip()}


def suppressed(email, sup):
    if not email:
        return False
    return email.lower() in sup or email.split("@")[-1].lower() in sup


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--niche", help=f"one of: {', '.join(NICHES)}")
    ap.add_argument("--tag", help="custom OSM tag, e.g. shop=florist")
    ap.add_argument("--city", required=True, help='e.g. "Austin, TX" or "Leeds, UK"')
    ap.add_argument("--limit", type=int, default=100, help="max businesses to audit")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--target", choices=["no-website", "has-website", "all"], default="no-website",
                    help="Target focus: no-website (build new site), has-website (redesign), or all")
    ap.add_argument("--pagespeed", action="store_true", help="add Google PageSpeed score (slow)")
    ap.add_argument("--draft", action="store_true", help="generate email drafts")
    ap.add_argument("--out", default="leads.csv")
    a = ap.parse_args()

    tag = a.tag or NICHES.get(a.niche or "")
    if not tag:
        sys.exit("Give --niche (see list in --help) or --tag key=value")
    niche = a.niche or tag

    print(f"[1/5] Discovering '{niche}' in {a.city} ...")
    bbox = geocode(a.city)
    time.sleep(1)
    leads = [x for x in (parse_element(e, niche) for e in fetch_businesses(tag, bbox)) if x]
    seen, uniq = set(), []
    for l in leads:  # de-dupe
        k = (l["name"].lower(), l["website"].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(l)

    # Sort leads based on chosen target strategy
    if a.target == "no-website":
        # Prioritize businesses without a website or only social, then those with contact info
        uniq.sort(key=lambda l: (bool(l["website"]) and not is_social(l["website"]), -(bool(l["phone"]) + bool(l["osm_email"]))))
    elif a.target == "has-website":
        uniq.sort(key=lambda l: (not bool(l["website"]) or is_social(l["website"]), -(bool(l["phone"]) + bool(l["osm_email"]))))
    else:
        uniq.sort(key=lambda l: -(bool(l["phone"]) + bool(l["osm_email"]) + (not bool(l["website"]))))

    leads = uniq[: a.limit]
    print(f"      {len(uniq)} independent businesses found, auditing {len(leads)}")

    print("[2/5] Auditing websites + [3/5] finding emails ...")
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        leads = list(ex.map(lambda l: audit_lead(l, a.pagespeed), leads))

    sup = load_suppression()
    # Keep no-email leads (no website prospects) — only suppress by email if they *have* one
    leads = [score(l) for l in leads if not (l["email"] and suppressed(l["email"], sup))]
    # Re-enforce target strategy in final sort (scoring can re-order)
    if a.target == "no-website":
        leads.sort(key=lambda l: (
            bool(l.get("website")) and not is_social(l.get("website", "")),  # no-site first
            -l["priority"]
        ))
    else:
        leads.sort(key=lambda l: -l["priority"])
    print(f"[4/5] Scored {len(leads)} leads "
          f"({sum(1 for l in leads if l['email'])} with an email)")

    if a.draft:
        email_leads = [l for l in leads if l.get("email")]
        no_email_leads = [l for l in leads if not l.get("email")]
        print(f"[5/5] Drafting emails for {len(email_leads)} leads with email (Gemini AI preferred) ...")
        for l in email_leads:
            l["subject"], l["body"], l["draft_source"] = draft(l, a.city)
        # No-email leads get template draft (useful as phone outreach reference)
        for l in no_email_leads:
            subj, body = template_draft(l, a.city)
            l["subject"], l["body"], l["draft_source"] = subj, body, "Offline Template"

    cols = ["priority", "name", "niche", "need", "email", "phone", "website",
            "address", "issues", "subject", "body", "draft_source", "lat", "lon", "osm_id"]
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for l in leads:
            row = dict(l, issues=" | ".join(l["issues"]))
            w.writerow(row)
    print(f"Done -> {a.out}")


if __name__ == "__main__":
    main()
