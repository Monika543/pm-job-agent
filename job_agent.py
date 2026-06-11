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
]

# Companies whose Lever boards to check: jobs.lever.co/<slug>
LEVER_COMPANIES = [
    "zepto", "dream11", "porter", "atlan", "netomi", "plum",
]

MAX_AGE_DAYS = 30          # drop jobs older than this from the file
OUTPUT = "data/jobs.json"

# Pretty display names for slugs (optional)
COMPANY_NAMES = {
    "razorpaysoftwareprivatelimited": "Razorpay",
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
