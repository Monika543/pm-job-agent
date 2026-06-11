"""
PM Job Agent — runs on GitHub Actions every few hours.
Fetches Product Manager / Product Growth roles (India + Remote) from sources
that allow programmatic access, merges with history, writes data/jobs.json.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone, timedelta

import requests

# ----------------- CONFIG (edit freely) -----------------

# Title must match at least one of these (case-insensitive)
INCLUDE_PATTERNS = [
    r"product manager",
    r"product growth",
    r"growth product",
    r"\bapm\b",
    r"product lead",
    r"head of product\b",
    r"director of product\b",
    r"director,? product management",
    r"product director\b",
    r"\bproduct owner\b",
    r"\bgpm\b",
]

# Reject titles containing these (avoids project manager / production noise)
EXCLUDE_PATTERNS = [
    r"project manager",
    r"production",
    r"product marketing",     # remove this line if you want PMM roles too
    r"product designer",
    r"product analyst",       # remove if you want analyst roles
]

# Locations that qualify (substring match, lowercase)
LOCATION_KEYWORDS = [
    "india", "remote", "anywhere", "worldwide", "global",
    "bangalore", "bengaluru", "mumbai", "delhi", "gurgaon", "gurugram",
    "noida", "hyderabad", "pune", "chennai", "ncr",
]

# Companies whose Greenhouse boards to check.
# Find the slug in the careers URL: boards.greenhouse.io/<slug>
GREENHOUSE_COMPANIES = [
    "postman", "razorpaysoftwareprivatelimited", "rippling", "gusto",
    "coinbase", "stripe", "gitlab", "doola", "vimeo", "reddit",
    # — sector: fintech / lending / retail tech (verified live) —
    "slice",        # fintech lending
    "zenoti",       # vertical SaaS/ERP
]

# Companies whose Lever boards to check: jobs.lever.co/<slug>
LEVER_COMPANIES = [
    "zepto", "dream11", "porter", "atlan", "netomi", "plum",
    # — sector (verified live) —
    "cred",         # fintech / lending
    "epifi",        # fintech (Fi Money)
    "meesho",       # retail tech / e-commerce
]

# Ashby boards: jobs.ashbyhq.com/<slug>
ASHBY_COMPANIES = [
    "navi",         # lending / fintech (verified live)
]

# SmartRecruiters boards: careers.smartrecruiters.com/<slug>
SMARTRECRUITERS_COMPANIES = [
    "lendingkart",  # SME lending (verified live)
    "kredx",        # supply chain finance / invoice discounting (verified live)
    "deskera",      # ERP (verified live)
]

# Careers pages WITHOUT a public ATS API — watched best-effort.
# The agent fetches the page (with a JS-rendering fallback), scans for PM
# keywords, and emits a "check this page" alert when they appear.
WATCH_PAGES = {
    # — supply chain finance / lending —
    "CredAble":  "https://credable.in/current-openings/",
    "Cashflo":   "https://www.cashflo.io/company/careers",
    "Vayana":    "https://www.vayana.com/careers/",
    "Mintifi":   "https://www.mintifi.com/careers",
    "Yubi":      "https://www.go-yubi.com/careers/",
    "Oxyzo":     "https://www.oxyzo.in/careers",
    "Jupiter":   "https://jupiter.money/careers/",
    "Perfios":   "https://perfios.ai/careers/",
    # — ERP / business software —
    "Zoho":      "https://www.zoho.com/careers/",
    "Tally":     "https://tallysolutions.com/career/",
    "Darwinbox": "https://darwinbox.com/careers",
    "Increff":   "https://www.increff.com/careers/",
    # — retail tech / B2B commerce —
    "Udaan":     "https://www.udaan.com/careers",
    "Bizongo":   "https://www.bizongo.com/careers",
    "Zetwerk":   "https://www.zetwerk.com/careers/",
    "Moglix":    "https://www.moglix.com/careers",
    "Jumbotail": "https://www.jumbotail.com/careers",
    "DeHaat":    "https://www.dehaat.com/careers",
    "ElasticRun": "https://www.elastic.run/careers",
    "Ninjacart": "https://www.ninjacart.com/careers/",
}

MAX_AGE_DAYS = 30          # drop jobs older than this from the file
OUTPUT = "data/jobs.json"

# Pretty display names for slugs (optional)
COMPANY_NAMES = {
    "razorpaysoftwareprivatelimited": "Razorpay",
    "epifi": "Fi Money",
    "kredx": "KredX",
    "credable": "CredAble",
    "dehaat": "DeHaat",
    "gitlab": "GitLab",
    "remoteok": "RemoteOK",
}


def pretty_company(slug: str) -> str:
    return COMPANY_NAMES.get(slug.lower(), slug.replace("-", " ").title())

# ---------------------------------------------------------

INCLUDE_RE = [re.compile(p, re.I) for p in INCLUDE_PATTERNS]
EXCLUDE_RE = [re.compile(p, re.I) for p in EXCLUDE_PATTERNS]
HEADERS = {"User-Agent": "Mozilla/5.0 (job-agent; personal job alerts)"}


def title_matches(title: str) -> bool:
    if not title:
        return False
    if any(p.search(title) for p in EXCLUDE_RE):
        return False
    return any(p.search(title) for p in INCLUDE_RE)


def location_matches(location: str) -> bool:
    loc = (location or "").lower()
    if not loc:
        return True  # unknown location: keep, let the human judge
    return any(k in loc for k in LOCATION_KEYWORDS)


def job_id(url: str, title: str, company: str) -> str:
    return hashlib.md5(f"{url}|{title}|{company}".encode()).hexdigest()[:16]


def make_job(title, company, location, url, source, salary=None, posted=None, tags=None):
    return {
        "id": job_id(url, title, company),
        "title": title.strip(),
        "company": company.strip(),
        "location": (location or "Not specified").strip(),
        "url": url,
        "source": source,
        "salary": salary,
        "posted": posted,
        "tags": tags or [],
    }


# ----------------- SOURCES -----------------

def fetch_remoteok():
    jobs = []
    try:
        r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=30)
        for item in r.json():
            if not isinstance(item, dict) or "position" not in item:
                continue
            title = item.get("position", "")
            if not title_matches(title):
                continue
            jobs.append(make_job(
                title=title,
                company=item.get("company", "Unknown"),
                location=item.get("location") or "Remote",
                url=item.get("url", ""),
                source="RemoteOK",
                salary=(f"${item['salary_min']:,}–${item['salary_max']:,}"
                        if item.get("salary_min") and item.get("salary_max") else None),
                posted=item.get("date", "")[:10] or None,
                tags=(item.get("tags") or [])[:3],
            ))
    except Exception as e:
        print(f"[remoteok] failed: {e}")
    return jobs


def fetch_remotive():
    jobs = []
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": "product manager", "limit": 50},
            headers=HEADERS, timeout=30,
        )
        for item in r.json().get("jobs", []):
            title = item.get("title", "")
            if not title_matches(title):
                continue
            loc = item.get("candidate_required_location", "Remote")
            if not location_matches(loc):
                continue
            jobs.append(make_job(
                title=title,
                company=item.get("company_name", "Unknown"),
                location=loc,
                url=item.get("url", ""),
                source="Remotive",
                salary=item.get("salary") or None,
                posted=(item.get("publication_date") or "")[:10] or None,
                tags=[item.get("category", "Product")][:3],
            ))
    except Exception as e:
        print(f"[remotive] failed: {e}")
    return jobs


def fetch_greenhouse(slug):
    jobs = []
    try:
        r = requests.get(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return jobs
        for item in r.json().get("jobs", []):
            title = item.get("title", "")
            loc = (item.get("location") or {}).get("name", "")
            if not title_matches(title) or not location_matches(loc):
                continue
            jobs.append(make_job(
                title=title, company=pretty_company(slug),
                location=loc, url=item.get("absolute_url", ""),
                source="Greenhouse",
                posted=(item.get("updated_at") or "")[:10] or None,
            ))
    except Exception as e:
        print(f"[greenhouse:{slug}] failed: {e}")
    return jobs


def fetch_lever(slug):
    jobs = []
    try:
        r = requests.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return jobs
        for item in r.json():
            title = item.get("text", "")
            loc = (item.get("categories") or {}).get("location", "")
            if not title_matches(title) or not location_matches(loc):
                continue
            jobs.append(make_job(
                title=title, company=pretty_company(slug),
                location=loc, url=item.get("hostedUrl", ""),
                source="Lever",
                posted=(datetime.fromtimestamp(item["createdAt"] / 1000, tz=timezone.utc)
                        .strftime("%Y-%m-%d") if item.get("createdAt") else None),
            ))
    except Exception as e:
        print(f"[lever:{slug}] failed: {e}")
    return jobs


def fetch_ashby(slug):
    jobs = []
    try:
        r = requests.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return jobs
        for item in r.json().get("jobs", []):
            title = item.get("title", "")
            loc = item.get("location", "")
            if not title_matches(title) or not location_matches(loc):
                continue
            jobs.append(make_job(
                title=title, company=pretty_company(slug),
                location=loc or ("Remote" if item.get("isRemote") else ""),
                url=item.get("jobUrl") or item.get("applyUrl", ""),
                source="Ashby",
                posted=(item.get("publishedAt") or "")[:10] or None,
            ))
    except Exception as e:
        print(f"[ashby:{slug}] failed: {e}")
    return jobs


def fetch_smartrecruiters(slug):
    jobs = []
    try:
        r = requests.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            headers=HEADERS, timeout=20,
        )
        if r.status_code != 200:
            return jobs
        for item in r.json().get("content", []):
            title = item.get("name", "")
            loc_obj = item.get("location") or {}
            loc = ", ".join(filter(None, [loc_obj.get("city"), loc_obj.get("country")]))
            if loc_obj.get("remote"):
                loc = (loc + " · Remote").strip(" ·")
            if not title_matches(title) or not location_matches(loc):
                continue
            jobs.append(make_job(
                title=title, company=pretty_company(slug),
                location=loc,
                url=f"https://jobs.smartrecruiters.com/{slug}/{item.get('id','')}",
                source="SmartRecruiters",
                posted=(item.get("releasedDate") or "")[:10] or None,
            ))
    except Exception as e:
        print(f"[smartrecruiters:{slug}] failed: {e}")
    return jobs


def fetch_page_text(url):
    """Fetch a careers page; fall back to Jina Reader for JS-rendered pages."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200 and len(r.text) > 5000:
            return r.text
    except Exception:
        pass
    # Fallback: Jina Reader renders JS pages and returns text (free, rate-limited)
    try:
        r = requests.get(f"https://r.jina.ai/{url}", headers=HEADERS, timeout=45)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except Exception:
        pass
    return ""


def watch_career_pages():
    """Best-effort watcher for custom careers pages without an ATS API.

    Scans page text for PM title patterns. When found, emits one alert item
    per company linking to the page. The job's id is derived from the matched
    titles, so the alert re-fires as NEW only when the set of roles changes.
    """
    jobs = []
    for company, url in WATCH_PAGES.items():
        text = fetch_page_text(url)
        if not text:
            print(f"[watch:{company}] page unreachable")
            continue
        # Strip tags crudely; we only need visible-ish text
        visible = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        visible = re.sub(r"<[^>]+>", " ", visible)

        matches = set()
        for line in re.split(r"[\n\r]+", visible):
            line = line.strip()
            if 5 < len(line) < 120 and title_matches(line):
                matches.add(line)

        if matches:
            titles = sorted(matches)[:5]
            jobs.append(make_job(
                title=f"PM role(s) spotted: {titles[0]}" + (f" (+{len(titles)-1} more)" if len(titles) > 1 else ""),
                company=company,
                location="See careers page",
                url=url,
                source="Page watcher",
                tags=["careers page", "verify on site"],
            ))
            print(f"[watch:{company}] {len(matches)} PM mention(s)")
    return jobs




# ----------------- MAIN -----------------

def main():
    now = datetime.now(timezone.utc)

    fresh = []
    fresh += fetch_remoteok()
    fresh += fetch_remotive()
    for slug in GREENHOUSE_COMPANIES:
        fresh += fetch_greenhouse(slug)
    for slug in LEVER_COMPANIES:
        fresh += fetch_lever(slug)
    for slug in ASHBY_COMPANIES:
        fresh += fetch_ashby(slug)
    for slug in SMARTRECRUITERS_COMPANIES:
        fresh += fetch_smartrecruiters(slug)
    fresh += watch_career_pages()

    print(f"Fetched {len(fresh)} matching jobs this run")

    # Merge with previous file to preserve first_seen timestamps
    previous = {}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT) as f:
                for j in json.load(f).get("jobs", []):
                    previous[j["id"]] = j
        except Exception:
            pass

    merged = {}
    for job in fresh:
        if job["id"] in previous:
            job["first_seen"] = previous[job["id"]].get("first_seen", now.isoformat())
        else:
            job["first_seen"] = now.isoformat()
            print(f"  NEW: {job['title']} @ {job['company']}")
        merged[job["id"]] = job

    # Keep recently-seen previous jobs that didn't appear this run
    # (a source may have hiccuped; don't lose them immediately)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    for jid, job in previous.items():
        if jid in merged:
            continue
        try:
            seen = datetime.fromisoformat(job.get("first_seen", "").replace("Z", "+00:00"))
        except Exception:
            continue
        if seen > cutoff:
            merged[jid] = job

    jobs = sorted(merged.values(), key=lambda j: j.get("first_seen", ""), reverse=True)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump({
            "generated_at": now.isoformat(),
            "count": len(jobs),
            "jobs": jobs,
        }, f, indent=2)

    print(f"Wrote {len(jobs)} jobs to {OUTPUT}")


if __name__ == "__main__":
    main()
