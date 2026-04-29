#!/usr/bin/env python3
"""
Job dashboard scraper — runs daily via GitHub Actions.
Scrapes career pages, aggregators, and APIs.
Outputs data/jobs.json for the Claude artifact to consume.
"""

import requests
from bs4 import BeautifulSoup
import json
import hashlib
import re
import time
import os
from datetime import datetime
from urllib.parse import urljoin

# ── Config ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
TIMEOUT = 15
DELAY   = 1.5   # seconds between requests — be polite

# Keywords that help identify a link as a job listing
JOB_TITLE_SIGNALS = [
    "manager", "officer", "coordinator", "director", "analyst", "specialist",
    "consultant", "advisor", "associate", "lead", "researcher", "fellow",
    "head of", "project", "programme", "program", "advisor", "officer",
    "developer", "designer", "communications", "policy", "engagement",
    "facilitator", "organiser", "strategist", "partnership"
]
JOB_URL_SIGNALS = [
    "job", "vacancy", "position", "career", "opening", "role",
    "recruit", "posting", "opportunity", "hire", "apply"
]
SKIP_TEXT = {
    "home", "about", "contact", "news", "blog", "press", "login", "register",
    "privacy", "terms", "cookies", "donate", "subscribe", "newsletter",
    "imprint", "impressum", "search", "sitemap", "back", "more", "read more"
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def job_id(url: str, title: str) -> str:
    return hashlib.md5(f"{url}:{title}".encode()).hexdigest()[:12]

def abs_url(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    return urljoin(base, href)

def is_job_link(text: str, href: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if len(text) < 8 or len(text) > 160:
        return False
    if text.lower() in SKIP_TEXT:
        return False
    tl = text.lower()
    hl = href.lower()
    if any(s in hl for s in JOB_URL_SIGNALS):
        return True
    if any(s in tl for s in JOB_TITLE_SIGNALS):
        return True
    return False

def clean_text(el) -> str:
    return " ".join(el.get_text().split()) if el else ""

def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── ATS scrapers (use JSON APIs where available) ───────────────────────────────

def scrape_greenhouse(slug: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        jobs = []
        for j in r.json().get("jobs", []):
            jobs.append({
                "title":    j["title"],
                "url":      j["absolute_url"],
                "location": j.get("location", {}).get("name", ""),
                "snippet":  "",
                "posted":   j.get("updated_at", "")[:10],
            })
        return jobs
    except Exception as e:
        print(f"    [greenhouse:{slug}] {e}")
        return []

def scrape_lever(slug: str) -> list:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        jobs = []
        for j in r.json():
            ts = j.get("createdAt", 0)
            posted = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
            jobs.append({
                "title":    j["text"],
                "url":      j["hostedUrl"],
                "location": j.get("categories", {}).get("location", ""),
                "snippet":  (j.get("descriptionPlain") or "")[:250],
                "posted":   posted,
            })
        return jobs
    except Exception as e:
        print(f"    [lever:{slug}] {e}")
        return []

# ── Generic HTML scraper ───────────────────────────────────────────────────────

def scrape_generic(base_url: str) -> list:
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Remove clutter
        for tag in soup.find_all(["nav", "footer", "header", "script", "style"]):
            tag.decompose()

        seen, jobs = set(), []
        for a in soup.find_all("a", href=True):
            text = " ".join(a.get_text().split())
            href = abs_url(a["href"], base_url)
            if href in seen:
                continue
            if is_job_link(text, href):
                # Try to pick up a location hint from surrounding text
                parent_text = clean_text(a.parent)
                location = extract_location(parent_text)
                jobs.append({
                    "title":    text,
                    "url":      href,
                    "location": location,
                    "snippet":  "",
                    "posted":   "",
                })
                seen.add(href)
                if len(jobs) >= 40:
                    break
        return jobs
    except Exception as e:
        print(f"    [generic] {e}")
        return []

def extract_location(text: str) -> str:
    LOCATIONS = [
        "Berlin", "London", "Brussels", "Geneva", "Vienna", "New York",
        "Washington", "Amsterdam", "The Hague", "Paris", "Stockholm",
        "Nairobi", "remote", "Remote", "hybrid", "Hybrid", "global", "Global"
    ]
    for loc in LOCATIONS:
        if loc in text:
            return loc
    return ""

# ── Aggregator scrapers ────────────────────────────────────────────────────────

def scrape_gesine() -> list:
    """gesinesjobtipps.de — curated German civil society job board"""
    url = "https://www.gesinesjobtipps.de/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()

        # Gesine posts job listings as titled entries with external links
        for entry in soup.find_all(["article", "div"],
                                    class_=re.compile(r"post|entry|item|listing", re.I)):
            title_el = entry.find(re.compile(r"^h[1-4]$"))
            link_el  = entry.find("a", href=True)
            if title_el and link_el:
                title = clean_text(title_el)
                href  = link_el["href"]
                if href not in seen and len(title) > 5:
                    jobs.append({"title": title, "url": href,
                                 "location": "", "snippet": "", "posted": ""})
                    seen.add(href)

        # Fallback — grab any meaningful external link
        if not jobs:
            for a in soup.find_all("a", href=True):
                text = clean_text(a)
                href = a["href"]
                if (href.startswith("http") and href not in seen
                        and 20 < len(text) < 150):
                    jobs.append({"title": text, "url": href,
                                 "location": "", "snippet": "", "posted": ""})
                    seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [gesine] {e}")
        return []


def scrape_bundesverband() -> list:
    """Bundesverband Deutscher Stiftungen job board"""
    url = "https://www.stiftungen.org/service/jobs.html"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if href not in seen and is_job_link(text, href):
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [bundesverband] {e}")
        return []


def scrape_stiftungswelt() -> list:
    """Stiftungswelt.de job portal"""
    url = "https://www.stiftungswelt.de/stellenmarkt"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if href not in seen and is_job_link(text, href):
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [stiftungswelt] {e}")
        return []


def scrape_80k() -> list:
    """80,000 Hours job board"""
    url = "https://jobs.80000hours.org/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if "/jobs/" in href and len(text) > 8 and href not in seen:
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [80k] {e}")
        return []


def scrape_charityjob() -> list:
    """CharityJob UK"""
    url = "https://www.charityjob.co.uk/jobs?keywords=project+manager+programme"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if "/jobs/" in href and len(text) > 8 and href not in seen:
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [charityjob] {e}")
        return []


def scrape_nfp() -> list:
    """NFP Resourcing UK"""
    url = "https://www.nfpresourcing.co.uk/jobs/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if href not in seen and is_job_link(text, href):
                jobs.append({"title": text, "url": href,
                             "location": "UK", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [nfp] {e}")
        return []


def scrape_euractiv() -> list:
    """Euractiv job board — EU/Brussels policy jobs"""
    url = "https://jobs.euractiv.com/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if href not in seen and is_job_link(text, href):
                jobs.append({"title": text, "url": href,
                             "location": "Brussels", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [euractiv] {e}")
        return []


def scrape_unjobs() -> list:
    """UNjobs.org — aggregates all UN system vacancies"""
    url = "https://unjobs.org/themes/governance"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if "/vacancies/" in href and len(text) > 8 and href not in seen:
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [unjobs] {e}")
        return []


def scrape_impactjobs() -> list:
    """Impact.jobs — European social impact jobs"""
    url = "https://impactjobs.eu/jobs/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if href not in seen and is_job_link(text, href):
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [impactjobs] {e}")
        return []


def scrape_devex() -> list:
    """Devex — international development jobs (free search results)"""
    url = "https://www.devex.com/jobs/search?keywords=project+manager+governance+participation&page=1"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()
        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if "/jobs/" in href and len(text) > 8 and href not in seen:
                jobs.append({"title": text, "url": href,
                             "location": "", "snippet": "", "posted": ""})
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [devex] {e}")
        return []


# ── API sources ────────────────────────────────────────────────────────────────

def scrape_reliefweb() -> list:
    """ReliefWeb Jobs API — international development & humanitarian"""
    url    = "https://api.reliefweb.int/v1/jobs"
    params = {
        "appname": "job-dashboard-janek",
        "profile": "full",
        "preset":  "latest",
        "limit":   50,
        "query[value]": "project manager governance participation democracy civil society",
        "fields[include][]": ["title", "url", "date", "city", "country", "source", "body-html"],
    }
    try:
        r    = requests.get(url, params=params, timeout=TIMEOUT)
        jobs = []
        for item in r.json().get("data", []):
            f       = item.get("fields", {})
            sources = f.get("source", [{}])
            org     = sources[0].get("name", "Unknown") if sources else "Unknown"
            country = f.get("country", [{}])
            loc     = country[0].get("name", "") if country else ""
            snippet_html = f.get("body-html", "")
            snippet = BeautifulSoup(snippet_html, "html.parser").get_text()[:250] if snippet_html else ""
            jobs.append({
                "title":    f.get("title", ""),
                "url":      f.get("url", ""),
                "org":      org,
                "location": loc,
                "snippet":  snippet,
                "posted":   (f.get("date") or {}).get("created", "")[:10],
            })
        return jobs
    except Exception as e:
        print(f"    [reliefweb] {e}")
        return []


def scrape_idealist() -> list:
    """Idealist.org — NGO and non-profit jobs"""
    url = "https://www.idealist.org/en/jobs?q=project+manager+governance&type=JOB&radius=Anywhere"
    try:
        r    = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()

        for a in soup.find_all("a", href=True):
            text = clean_text(a)
            href = abs_url(a["href"], url)
            if "/en/jobs/" in href and len(text) > 8 and href not in seen:
                jobs.append({
                    "title":    text,
                    "url":      href,
                    "location": "",
                    "snippet":  "",
                    "posted":   "",
                })
                seen.add(href)
        return jobs
    except Exception as e:
        print(f"    [idealist] {e}")
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    root     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    orgs     = load_json(os.path.join(root, "scraper", "orgs.json"), [])
    seen_ids = set(load_json(os.path.join(root, "data", "seen_ids.json"), []))
    now      = datetime.utcnow().isoformat()
    all_jobs = []

    # ── Tier 1: direct career pages ──────────────────────────────────────────
    print(f"\n── Tier 1: {len(orgs)} career pages ──")
    for org in orgs:
        name = org["name"]
        url  = org["url"]
        print(f"  {name}")

        ats  = org.get("ats")
        slug = org.get("ats_slug", "")

        if ats == "greenhouse":
            raw = scrape_greenhouse(slug)
        elif ats == "lever":
            raw = scrape_lever(slug)
        else:
            raw = scrape_generic(url)

        for j in raw:
            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", name),
                "org_tags":   org.get("tags", []),
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    j.get("snippet", ""),
                "posted":     j.get("posted", ""),
                "source":     "direct",
                "is_new":     jid not in seen_ids,
                "scraped_at": now,
            })

        time.sleep(DELAY)

    # ── Tier 2: aggregators ───────────────────────────────────────────────────
    aggregators = [
        ("Gesine's Jobtipps",            scrape_gesine),
        ("Bundesverband",                scrape_bundesverband),
        ("Stiftungswelt",                scrape_stiftungswelt),
        ("80,000 Hours",                 scrape_80k),
        ("CharityJob",                   scrape_charityjob),
        ("NFP Resourcing",               scrape_nfp),
        ("Euractiv",                     scrape_euractiv),
        ("UNjobs",                       scrape_unjobs),
        ("Impact.jobs",                  scrape_impactjobs),
        ("Devex",                        scrape_devex),
    ]

    print(f"\n── Tier 2: {len(aggregators)} aggregators ──")
    for source_name, fn in aggregators:
        print(f"  {source_name}")
        for j in fn():
            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", source_name),
                "org_tags":   [],
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    j.get("snippet", ""),
                "posted":     j.get("posted", ""),
                "source":     source_name,
                "is_new":     jid not in seen_ids,
                "scraped_at": now,
            })
        time.sleep(DELAY)

    # ── Tier 3: APIs ──────────────────────────────────────────────────────────
    api_sources = [
        ("ReliefWeb", scrape_reliefweb),
        ("Idealist",  scrape_idealist),
    ]

    print(f"\n── Tier 3: {len(api_sources)} APIs ──")
    for source_name, fn in api_sources:
        print(f"  {source_name}")
        for j in fn():
            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", source_name),
                "org_tags":   [],
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    j.get("snippet", ""),
                "posted":     j.get("posted", ""),
                "source":     source_name,
                "is_new":     jid not in seen_ids,
                "scraped_at": now,
            })

    # ── Deduplicate & save ────────────────────────────────────────────────────
    seen_in_run = set()
    unique_jobs = []
    for j in all_jobs:
        if j["id"] not in seen_in_run and j["title"] and j["url"]:
            unique_jobs.append(j)
            seen_in_run.add(j["id"])

    # Update seen_ids (keep last 60 days worth by capping at 5000)
    all_seen = list(seen_ids | seen_in_run)[-5000:]
    save_json(os.path.join(root, "data", "seen_ids.json"), all_seen)

    output = {
        "scraped_at":  now,
        "total":       len(unique_jobs),
        "new_count":   sum(1 for j in unique_jobs if j["is_new"]),
        "jobs":        unique_jobs,
    }
    save_json(os.path.join(root, "data", "jobs.json"), output)

    print(f"\n✓ Done — {len(unique_jobs)} jobs ({output['new_count']} new) → data/jobs.json")


if __name__ == "__main__":
    main()
