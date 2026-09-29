"""Tailor the CV to one specific job (on demand).

Run from the 'Tailor CV' workflow with a job URL (or pasted job text).
A free Groq model rewrites the CV so the parts that really match the job come
first and use the job's own wording. It does NOT invent experience: anything
the job asks for that the CV does not show is listed separately as a gap.

The result is emailed to you (in the body + as an attached .md file).
"""
import html
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from morning_jobs import EMAIL, GROQ_MODELS, GROQ_URL, UA, esc, http_json, strip_html

MAX_CV = 6000
MAX_JOB = 3500

PROMPT = """You are a careful CV editor. Tailor the CV below to the job posting.

STRICT RULES:
- Use ONLY facts that already appear in the CV. Never invent or inflate experience, employers, dates, degrees, projects, numbers, or skills.
- You may reorder sections/bullets, tighten wording, put the most relevant experience first, and use the job posting's own terminology WHEN the CV genuinely supports it.
- Keep the CV in the same language as the original CV. Keep it to the same length or shorter.
- If the job asks for something the CV does not show, do NOT add it to the CV. List it as a gap instead.

<cv>
{cv}
</cv>

<job>
{job}
</job>

Output EXACTLY in this format, with these two marker lines and nothing before the first marker:
===CV===
(the full tailored CV, in Markdown)
===GAPS===
(in Hebrew, short: 1. estimated match before and after tailoring, as percentages - say it is an estimate; 2. requirements from the job that the CV does not show; 3. two or three honest suggestions for closing them, e.g. a small project or course)"""


def fetch_job_text(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read().decode("utf-8", errors="replace")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        title = strip_html(m.group(1))
    raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    return title, strip_html(raw)


def ask_groq(prompt):
    key = os.environ["GROQ_API_KEY"]
    last = None
    for model in GROQ_MODELS:
        for attempt in range(2):
            try:
                r = http_json(
                    GROQ_URL,
                    data={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 3000,
                    },
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=120,
                )
                print(f"Model used: {model}")
                return r["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last = f"{model}: HTTP {e.code}"
                print("Groq error:", last)
                if e.code == 429 and attempt == 0:
                    time.sleep(65)  # מגבלת טוקנים לדקה - מחכים ומנסים שוב
                    continue
                break
            except Exception as e:
                last = f"{model}: {e}"
                print("Groq error:", last)
                break
    sys.exit(f"All Groq models failed. Last error: {last}")


def split_output(text):
    m = re.search(r"===CV===(.*?)===GAPS===(.*)", text, re.S)
    if not m:
        return text.strip(), ""
    return m.group(1).strip(), m.group(2).strip()


def send(subject, cv_md, gaps, job_ref):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = EMAIL
    msg["To"] = EMAIL
    body = (
        '<div dir="rtl" style="font-family:Arial,sans-serif;max-width:680px;margin:auto;text-align:right">'
        f"<p>המשרה: {esc(job_ref)}</p>"
        "<h2>מה חסר ואיך אפשר להתקדם</h2>"
        f'<div style="white-space:pre-wrap">{esc(gaps) or "לא התקבל ניתוח."}</div>'
        "<h2>קורות החיים המותאמים</h2>"
        f'<div style="white-space:pre-wrap;border:1px solid #ddd;border-radius:8px;padding:10px">{esc(cv_md)}</div>'
        "<p style='color:#666'>כדאי לקרוא ולוודא שכל פרט נכון לפני ששולחים - זו טיוטה שנוצרה על ידי מודל.</p>"
        "</div>"
    )
    msg.attach(MIMEText(body, "html", "utf-8"))
    att = MIMEText(cv_md, "plain", "utf-8")
    att.add_header("Content-Disposition", "attachment", filename="tailored_cv.md")
    msg.attach(att)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(EMAIL, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def main():
    cv = os.environ.get("CV_TEXT", "").strip()
    if not cv:
        sys.exit("Missing CV_TEXT secret")
    url = os.environ.get("JOB_URL", "").strip()
    pasted = os.environ.get("JOB_TEXT", "").strip()

    title, job = "", pasted
    if not job:
        if not url:
            sys.exit("Give a job_url or paste job_text when running the workflow")
        title, job = fetch_job_text(url)
    if len(job) < 300:
        sys.exit(
            "Could not read enough of the job page (it may load with JavaScript). "
            "Run again and paste the job description into job_text."
        )

    prompt = PROMPT.format(cv=cv[:MAX_CV], job=job[:MAX_JOB])
    cv_md, gaps = split_output(ask_groq(prompt))
    ref = url or "טקסט שהודבק"
    send("קורות חיים מותאמים למשרה" + (f": {title[:60]}" if title else ""), cv_md, gaps, ref)
    print("Sent tailored CV")


if __name__ == "__main__":
    main()
