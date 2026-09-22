"""Morning job digest.

Finds junior (up to 2 years) full-stack / AI / front-end jobs, scores each one
against the CV, and emails the result to yourself. Jobs already sent are
remembered in data/seen_jobs.json so nothing repeats.
"""
import html
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic

EMAIL = "bmtehila@gmail.com"  # שולח ומקבל
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
LOCATION = os.environ.get("JOB_LOCATION", "Israel")
STRONG_MATCH = int(os.environ.get("STRONG_MATCH", "80"))  # סף "מתאים ממש"
SEEN_PATH = Path(__file__).parent.parent / "data" / "seen_jobs.json"  # data/seen_jobs.json בשורש ה-repo
MAX_SEEN = 2000

PROMPT = """You are a job-search agent. Today is {today}.

Find NEW, currently open job postings in {location} (remote roles that hire from {location} are fine) that meet ALL of these:
- Junior level: up to 2 years of experience required. Skip anything marked senior, lead, staff, or asking for 3+ years.
- Field: full-stack, AI / ML engineering, or front-end development.

Search widely: company career pages, LinkedIn, Drushim, AllJobs, Comeet, Greenhouse, Lever and similar boards.
Only include postings you actually saw in search results, with a direct link. Never invent a job or a URL.
Prefer postings from the last 7 days.

Also score each job from 0 to 100 for how well it fits this candidate's CV:

<cv>
{cv}
</cv>

Do NOT return any job whose URL appears in this already-sent list:
{seen}

Return ONLY a JSON array, with no other text and no code fences. Each item has these keys:
title, company, location, url,
category ("fullstack" or "ai" or "frontend"),
years_required (short string),
match_score (integer 0-100),
why_match (one short sentence in Hebrew naming the specific CV skills that match).
If you found nothing, return []."""


def load_seen():
    try:
        return list(json.loads(SEEN_PATH.read_text()))
    except Exception:
        return []


def save_seen(seen):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen[-MAX_SEEN:], indent=1))


def ask_claude(prompt):
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(8):  # web search can pause long turns
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 20}],
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    return "".join(b.text for b in resp.content if b.type == "text")


def parse_jobs(text):
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("No JSON array in model response:\n" + text[:500])
    data = json.loads(m.group(0))
    return [j for j in data if isinstance(j, dict) and j.get("url") and j.get("title")]


def esc(v):
    return html.escape(str(v or ""))


def card(j, show_score):
    url = html.escape(j["url"].strip(), quote=True)
    score = f' <span style="color:#666">· התאמה {esc(j.get("match_score"))}%</span>' if show_score else ""
    why = f'<div style="color:#333;margin-top:4px">{esc(j.get("why_match"))}</div>' if j.get("why_match") else ""
    return (
        '<div style="border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:8px 0">'
        f'<a href="{url}" style="font-size:16px;font-weight:bold;text-decoration:none">{esc(j["title"])}</a>{score}'
        f'<div style="color:#555">{esc(j.get("company"))} · {esc(j.get("location"))} · '
        f'{esc(j.get("category"))} · ניסיון: {esc(j.get("years_required"))}</div>'
        f"{why}</div>"
    )


def build_email(strong, others):
    parts = ['<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;text-align:right">']
    if strong:
        parts.append("<h2>⭐ מתאימות במיוחד לקורות החיים שלך</h2>")
        parts += [card(j, True) for j in strong]
    if others:
        parts.append("<h2>עוד משרות ג'וניור חדשות</h2>")
        parts += [card(j, True) for j in others]
    if not strong and not others:
        parts.append("<p>לא נמצאו היום משרות חדשות.</p>")
    parts.append("</div>")
    return "".join(parts)


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = EMAIL
    msg["To"] = EMAIL
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(EMAIL, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    cv = os.environ.get("CV_TEXT", "").strip()
    if not cv:
        sys.exit("Missing CV_TEXT secret")

    seen = load_seen()
    today = date.today().isoformat()
    prompt = PROMPT.format(
        today=today,
        location=LOCATION,
        cv=cv,
        seen=json.dumps(seen[-200:], ensure_ascii=False),
    )
    jobs = parse_jobs(ask_claude(prompt))

    known, new = set(seen), []
    for j in jobs:
        url = j["url"].strip()
        if not url.startswith("http") or url in known:
            continue
        known.add(url)
        j["url"] = url
        try:
            j["match_score"] = int(j.get("match_score") or 0)
        except (TypeError, ValueError):
            j["match_score"] = 0
        new.append(j)

    new.sort(key=lambda j: j["match_score"], reverse=True)
    strong = [j for j in new if j["match_score"] >= STRONG_MATCH]
    others = [j for j in new if j["match_score"] < STRONG_MATCH]

    if strong:
        subject = f"⭐ {len(strong)} התאמות חזקות · {len(new)} משרות חדשות · {today}"
    elif new:
        subject = f"{len(new)} משרות ג'וניור חדשות · {today}"
    else:
        subject = f"אין משרות חדשות היום · {today}"

    send_email(subject, build_email(strong, others))
    save_seen(seen + [j["url"] for j in new])  # שומרים רק אחרי שהמייל נשלח
    print(f"Sent: {len(strong)} strong, {len(others)} other")


if __name__ == "__main__":
    main()
