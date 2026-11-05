# leadgen - free lead pipeline

## Setup
    pip install -r requirements.txt
    export CONTACT_EMAIL="you@gmail.com"        # sent in User-Agent (OSM asks for this)
    export SENDER_NAME="Your Name"
    export POSTAL_ADDRESS="Your address"         # required in US cold email (CAN-SPAM)
    export GEMINI_API_KEY="..."                  # optional, free key from aistudio.google.com
    export PAGESPEED_API_KEY="..."               # optional

## Run
    python leadgen.py --niche plumbers --city "Austin, TX" --limit 100 --draft
    python leadgen.py --tag shop=florist --city "Leeds, UK" --limit 50

Output: `leads.csv`, sorted best lead first (priority, need score, email, issues, subject, body).

## Opt-outs
Put emails or domains (one per line) in `suppression.txt`; they're skipped on every run.

## Notes
- OSM coverage is patchier than Google Maps; try several cities/niches.
- Many leads won't have a public email. Those still appear in the CSV (phone/contact form).
- Review every draft before sending. UK: only email limited companies (Ltd/PLC).
- Without GEMINI_API_KEY, drafts use the built-in offline template.

# sender.py - send via Gmail

## One-time setup
1. Create a NEW Gmail account for outreach (never your WNS/college email).
2. Turn on 2-Step Verification, then create an App Password (Google Account -> Security).
3. Export:
       export GMAIL_ADDRESS="outreach@gmail.com"
       export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

## Daily routine
    python sender.py send            # dry run: preview what would go out
    python sender.py send --live     # send (new emails + due follow-ups)
    python sender.py replies         # pull replies / opt-outs / bounces from inbox
    python sender.py status

Run `replies` BEFORE `send --live` each day so replied/opted-out leads are never followed up.

## Built-in safeguards
- Dry-run by default; `--live` required to send.
- Cap ramps automatically: 5 on day 1, +3/day, up to `--daily-cap` (default 30).
- 60-180s random delay between emails (`--delay MIN MAX`).
- Follow-up 1 after 3 days, follow-up 2 after 7 days, in the same thread; stops on any reply.
- Replies starting with "no"/"stop"/"unsubscribe" etc. and bounces go to `suppression.txt`.
  Other replies are marked "replied" (stops follow-ups) - answer those yourself.
- State is in `state.json`; don't delete it or leads may be emailed twice.
