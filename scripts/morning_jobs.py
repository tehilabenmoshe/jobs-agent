"""Morning job digest (free version).

1. Pulls OPEN jobs straight from companies' public Greenhouse / Lever boards
   (a closed job simply is not in the API, so there are no stale listings).
2. Filters in code: Israel, junior-level titles, field keywords, <= 2 years.
3. Scores fit against the CV: keyword overlap always, refined by a free Groq
   model when available (falls back silently if it is down / rate limited).
4. Emails a digest to yourself; sent jobs are remembered in data/seen_jobs.json.

Only the Python standard library is used - nothing to pip install.
"""
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

EMAIL = "bmtehila@gmail.com"  # שולח ומקבל
ROOT = Path(__file__).parent.parent
COMPANIES_PATH = ROOT / "companies.json"
SEEN_PATH = ROOT / "data" / "seen_jobs.json"
MAX_SEEN = 3000
MAX_YEARS = 2
STRONG_MATCH = int(os.environ.get("STRONG_MATCH", "70"))  # סף "מתאים ממש"
MAX_EMAIL = int(os.environ.get("MAX_EMAIL", "25"))  # מקסימום משרות במייל אחד
LLM_CANDIDATES = 12  # כמה משרות נשלחות למודל לדירוג (חוסך טוקנים)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = [m for m in [os.environ.get("GROQ_MODEL"), "llama-3.3-70b-versatile", "openai/gpt-oss-120b"] if m]

UA = {"User-Agent": "Mozilla/5.0 (compatible; jobs-agent/1.0)"}

# ---- מיקום / תפקיד / רמה -------------------------------------------------
LOCATION_WORDS = [
    "israel", "tel aviv", "tel-aviv", "herzliya", "haifa", "jerusalem", "ramat gan",
    "petah tikva", "petach tikva", "raanana", "ra'anana", "netanya", "rehovot",
    "beer sheva", "be'er sheva", "hod hasharon", "kfar saba", "yokneam", "rosh haayin",
    "givatayim", "bnei brak", "holon", "modiin", "ישראל", "תל אביב",
]
FIELD_RE = re.compile(
    r"full[\s-]?stack|front[\s-]?end|\bai\b|\bml\b|machine learning|\bllm\b|"
    r"software (engineer|developer)|web developer|\breact\b|application developer|"
    r"\bgenai\b",
    re.I,
)
SENIOR_RE = re.compile(
    r"senior|\bsr\b|\blead\b|\bstaff\b|principal|manager|director|\bhead\b|architect|"
    r"\bvp\b|expert|team leader|tech lead",
    re.I,
)
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years?|yrs?)", re.I)

SKILLS = [
    "react", "angular", "vue", "next.js", "nextjs", "typescript", "javascript", "node", "node.js",
    "express", "python", "django", "flask", "fastapi", "java", "c#", ".net", "go", "golang",
    "sql", "postgres", "postgresql", "mysql", "mongodb", "redis", "graphql", "rest", "html", "css",
    "tailwind", "aws", "gcp", "azure", "docker", "kubernetes", "git", "ci/cd", "linux",
    "machine learning", "deep learning", "pytorch", "tensorflow", "llm", "openai", "langchain",
    "rag", "nlp", "pandas", "numpy", "zoho", "deluge", "php", "laravel", "wordpress", "figma",
]


# ---- קבצים ---------------------------------------------------------------
def load_seen():
    try:
        return list(json.loads(SEEN_PATH.read_text()))
    except Exception:
        return []


def save_seen(seen):
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen[-MAX_SEEN:], indent=1))


def http_json(url, data=None, headers=None, timeout=40):
    h = dict(UA)
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---- שליפת משרות ---------------------------------------------------------
def fetch_greenhouse(c):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['token']}/jobs?content=true")
    for j in d.get("jobs", []):
        yield {
            "title": j.get("title", ""),
            "company": c["name"],
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "text": strip_html(j.get("content", "")),
        }


def fetch_lever(c, host="api.lever.co"):
    d = http_json(f"https://{host}/v0/postings/{c['token']}?mode=json")
    for j in d:
        extra = " ".join(
            strip_html(x.get("text", "") + " " + x.get("content", "")) for x in j.get("lists", [])
        )
        yield {
            "title": j.get("text", ""),
            "company": c["name"],
            "location": (j.get("categories") or {}).get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "text": strip_html(j.get("descriptionPlain", "")) + " " + extra,
        }


def fetch_all(companies):
    jobs, report = [], []
    for c in companies:
        src = c.get("source", "greenhouse")
        try:
            if src == "greenhouse":
                got = list(fetch_greenhouse(c))
            elif src == "lever":
                got = list(fetch_lever(c))
            elif src == "lever-eu":
                got = list(fetch_lever(c, "api.eu.lever.co"))
            else:
                raise ValueError(f"unknown source {src}")
            jobs += got
            report.append(f"  ok   {c['name']} ({src}): {len(got)} jobs")
        except urllib.error.HTTPError as e:
            report.append(f"  FAIL {c['name']} ({src}/{c['token']}): HTTP {e.code} - בדקי את ה-token")
        except Exception as e:
            report.append(f"  FAIL {c['name']} ({src}/{c.get('token')}): {e}")
    print("Boards:\n" + "\n".join(report))
    return jobs


# ---- סינון ---------------------------------------------------------------
def in_israel(loc):
    loc = (loc or "").lower()
    return any(w in loc for w in LOCATION_WORDS)


def required_years(text):
    """Smallest 'N years of experience' bar mentioned; None if not stated."""
    nums = []
    for m in YEARS_RE.finditer(text):
        ctx = text[max(0, m.start() - 70): m.end() + 70].lower()
        if "experience" in ctx or "ניסיון" in ctx:
            nums.append(int(m.group(1)))
    return min(nums) if nums else None


def category(title):
    t = title.lower()
    if re.search(r"full[\s-]?stack", t):
        return "fullstack"
    if re.search(r"front[\s-]?end|\breact\b", t):
        return "frontend"
    if re.search(r"\bai\b|\bml\b|machine learning|\bllm\b|\bgenai\b", t):
        return "ai"
    return "fullstack"


def prefilter(jobs):
    out = []
    for j in jobs:
        if not j["url"].startswith("http") or not in_israel(j["location"]):
            continue
        if not FIELD_RE.search(j["title"]) or SENIOR_RE.search(j["title"]):
            continue
        yrs = required_years(j["text"])
        if yrs is not None and yrs > MAX_YEARS:
            continue
        j["years"] = yrs
        j["category"] = category(j["title"])
        out.append(j)
    return out


# ---- דירוג ---------------------------------------------------------------
def skills_in(text):
    t = text.lower()
    found = set()
    for s in SKILLS:
        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", t):
            found.add(s)
    return found


def keyword_score(job, cv_skills):
    js = skills_in(job["title"] + " " + job["text"])
    if not js:
        return 30, []
    common = sorted(js & cv_skills)
    score = int(100 * len(common) / len(js))
    if job["category"] in ("fullstack", "frontend", "ai"):
        score = min(100, score + 10)
    return score, common


def llm_rank(cv, jobs):
    """Ask a free Groq model to score jobs. Returns {index: (score, why)} or {}."""
    key = os.environ.get("GROQ_API_KEY")
    if not key or not jobs:
        return {}
    items = [
        {"i": i, "title": j["title"], "company": j["company"], "snippet": j["text"][:450]}
        for i, j in enumerate(jobs)
    ]
    prompt = (
        "You match a junior developer's CV to job postings.\n\n"
        f"<cv>\n{cv[:5000]}\n</cv>\n\n"
        f"<jobs>\n{json.dumps(items, ensure_ascii=False)}\n</jobs>\n\n"
        "For every job give match_score (integer 0-100, how well the CV fits) and why_match "
        "(ONE short sentence in Hebrew naming the concrete CV skills that fit). "
        'Return ONLY a JSON array like [{"i":0,"match_score":75,"why_match":"..."}], no other text.'
    )
    for model in GROQ_MODELS:
        try:
            r = http_json(
                GROQ_URL,
                data={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": 2500,
                },
                headers={"Authorization": f"Bearer {key}"},
            )
            text = r["choices"][0]["message"]["content"]
            m = re.search(r"\[.*\]", text, re.S)
            arr = json.loads(m.group(0))
            out = {}
            for x in arr:
                out[int(x["i"])] = (max(0, min(100, int(x["match_score"]))), str(x.get("why_match", "")))
            print(f"Groq model {model}: scored {len(out)} jobs")
            return out
        except Exception as e:
            print(f"Groq model {model} failed: {e}")
    return {}


# ---- מכתב מקדים ----------------------------------------------------------
COVER_LETTER_MAX = 10  # כמה משרות (הכי מתאימות) יקבלו מכתב מקדים בכל מייל
COVER_PACE_SECONDS = 25  # מרווח בין בקשות - המכסה החינמית מוגבלת בטוקנים לדקה

LETTER_PROMPT = """Write a short cover letter (150-220 words) for the job below, from the candidate whose CV is given.

STRICT RULES:
- Use ONLY facts that appear in the CV. Never invent or inflate experience, employers, numbers, degrees, or skills.
- If the job asks for something the CV does not show, simply do not mention it.
- Be warm, professional and concrete: open with why this role/company, mention 2-3 specific things from the CV that match the job, close with a short call to action.
- No placeholders like [Company] - use the real company and role names given below.
- Write the letter in {lang}. Output only the letter itself, starting with the greeting. Sign with the candidate's name exactly as it appears in the CV.

<cv>
{cv}
</cv>

<job>
Title: {title}
Company: {company}
Description: {desc}
</job>"""


def cover_letter(cv, job):
    """Draft a cover letter with a free Groq model. Returns '' on any failure."""
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return ""
    lang = "Hebrew" if len(re.findall(r"[\u0590-\u05FF]", job["text"])) > 50 else "English"
    prompt = LETTER_PROMPT.format(
        cv=cv[:4500], title=job["title"], company=job["company"], desc=job["text"][:2500], lang=lang
    )
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                r = http_json(
                    GROQ_URL,
                    data={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.5,
                        "max_tokens": 700,
                    },
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=90,
                )
                return r["choices"][0]["message"]["content"].strip()
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    try:
                        wait = float(e.headers.get("retry-after") or 30)
                    except (TypeError, ValueError):
                        wait = 30
                    time.sleep(min(65, wait) + 1)
                    continue
                print(f"Cover letter {model}: HTTP {e.code}")
                break
            except Exception as e:
                print(f"Cover letter {model}: {e}")
                break
    return ""


# ---- מייל ----------------------------------------------------------------
def esc(v):
    return html.escape(str(v or ""))


def card(j):
    url = html.escape(j["url"].strip(), quote=True)
    yrs = "לא צוין" if j.get("years") is None else f"{j['years']}"
    why = f'<div style="color:#333;margin-top:4px">{esc(j.get("why_match"))}</div>' if j.get("why_match") else ""
    letter = ""
    if j.get("cover_letter"):
        letter = (
            '<div style="margin-top:8px;color:#666;font-size:13px">מכתב מקדים (טיוטה - כדאי לקרוא ולעדכן לפני ההגשה):</div>'
            '<div dir="auto" style="white-space:pre-wrap;background:#f7f7f7;border-radius:6px;padding:8px 10px;'
            f'margin-top:4px;font-size:14px">{esc(j["cover_letter"])}</div>'
        )
    return (
        '<div style="border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:8px 0">'
        f'<a href="{url}" style="font-size:16px;font-weight:bold;text-decoration:none">{esc(j["title"])}</a>'
        f' <span style="color:#666">· התאמה {esc(j.get("match_score"))}%</span>'
        f'<div style="color:#555">{esc(j.get("company"))} · {esc(j.get("location"))} · '
        f'{esc(j.get("category"))} · שנות ניסיון שנדרשו: {esc(yrs)}</div>'
        f"{why}{letter}</div>"
    )


def build_email(strong, others):
    parts = ['<div dir="rtl" style="font-family:Arial,sans-serif;max-width:640px;margin:auto;text-align:right">']
    if strong:
        parts.append("<h2>⭐ מתאימות במיוחד לקורות החיים שלך</h2>")
        parts += [card(j) for j in strong]
    if others:
        parts.append("<h2>עוד משרות ג'וניור חדשות</h2>")
        parts += [card(j) for j in others]
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


# ---- ראשי ----------------------------------------------------------------
def main():
    cv = os.environ.get("CV_TEXT", "").strip()
    if not cv:
        sys.exit("Missing CV_TEXT secret")

    companies = json.loads(COMPANIES_PATH.read_text(encoding="utf-8"))
    seen = load_seen()
    known = set(seen)
    today = date.today().isoformat()

    all_jobs = fetch_all(companies)
    cands = [j for j in prefilter(all_jobs) if j["url"] not in known]
    print(f"Fetched {len(all_jobs)} open jobs -> {len(cands)} new junior candidates")

    cv_skills = skills_in(cv)
    for j in cands:
        j["match_score"], common = keyword_score(j, cv_skills)
        j["why_match"] = ("כישורים משותפים: " + ", ".join(common[:6])) if common else ""

    cands.sort(key=lambda j: j["match_score"], reverse=True)
    top = cands[:LLM_CANDIDATES]
    for i, (score, why) in llm_rank(cv, top).items():
        if 0 <= i < len(top):
            top[i]["match_score"] = score
            if why:
                top[i]["why_match"] = why

    cands.sort(key=lambda j: j["match_score"], reverse=True)
    new = cands[:MAX_EMAIL]
    if os.environ.get("GROQ_API_KEY"):
        for n, j in enumerate(new[:COVER_LETTER_MAX]):
            if n:
                time.sleep(COVER_PACE_SECONDS)
            j["cover_letter"] = cover_letter(cv, j)
        print(f"Cover letters: {sum(1 for j in new if j.get('cover_letter'))}/{min(len(new), COVER_LETTER_MAX)}")

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
