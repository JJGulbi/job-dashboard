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
TIMEOUT       = 15
DELAY         = 1.5   # seconds between requests — be polite
SNIPPET_LEN   = 1500  # characters of job description to store

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

# ── Junk title filter ─────────────────────────────────────────────────────────

JUNK_TITLE_PATTERNS = [
    re.compile(r"^jobs?\s*(list|portal|overview|page|board)?$", re.I),
    re.compile(r"^(all\s+)?open\s+(positions|roles|vacancies|jobs)$", re.I),
    re.compile(r"unsolicited\s+appli", re.I),
    re.compile(r"initiative?bewerbung", re.I),
    re.compile(r"zum\s+job\s*portal", re.I),
    re.compile(r"job\s*portal$", re.I),
    re.compile(r"^stellenangebote$", re.I),
    re.compile(r"^offene\s+stellen$", re.I),
    re.compile(r"^karriere(seite)?$", re.I),
    re.compile(r"^(view\s+)?all\s+jobs?$", re.I),
    re.compile(r"^current\s+(openings|vacancies|opportunities)$", re.I),
    re.compile(r"^work\s+with\s+us$", re.I),
    re.compile(r"^join\s+(the\s+)?team$", re.I),
    re.compile(r"^(our\s+)?vacancies$", re.I),
    re.compile(r"^(our\s+)?careers?$", re.I),
]

def is_junk_title(title: str) -> bool:
    t = title.strip()
    return any(p.search(t) for p in JUNK_TITLE_PATTERNS)

# ── Location / language filter ────────────────────────────────────────────────

# Locations where local-language requirements are very likely and Janek
# doesn't speak the language — filter these out at source.
BLOCKED_LOCATION_PATTERNS = [
    re.compile(r"\bBangladesh\b", re.I),
    re.compile(r"\bDhaka\b", re.I),
    re.compile(r"\bKabul\b", re.I),
    re.compile(r"\bKampala\b", re.I),
    re.compile(r"\bAddis\s+Ababa\b", re.I),
    re.compile(r"\bKhartoum\b", re.I),
    re.compile(r"\bBamako\b", re.I),
    re.compile(r"\bConakry\b", re.I),
    re.compile(r"\bNiamey\b", re.I),
    re.compile(r"\bOuagadougou\b", re.I),
    re.compile(r"\bLome\b", re.I),
    re.compile(r"\bCotonou\b", re.I),
]

# Description text that strongly implies a local-language requirement
BLOCKED_LANGUAGE_PATTERNS = [
    re.compile(r"\b(fluent|proficient|native|required).{0,40}(Arabic|Bengali|Bangla|Dari|Pashto|Amharic|Swahili|Hausa|Yoruba|Somali|Tigrinya)\b", re.I),
    re.compile(r"\b(Arabic|Bengali|Bangla|Dari|Pashto|Amharic)\s+(language\s+)?(skills?\s+)?(required|essential|mandatory|necessary)\b", re.I),
]

def is_blocked_location(location: str) -> bool:
    return any(p.search(location) for p in BLOCKED_LOCATION_PATTERNS)

def has_blocked_language_req(text: str) -> bool:
    return any(p.search(text) for p in BLOCKED_LANGUAGE_PATTERNS)

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

def fetch_job_description(url: str) -> str:
    """
    Fetch the full text of an individual job posting page.
    Returns up to SNIPPET_LEN characters of the main content block.
    Falls back to empty string on any error.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Remove boilerplate
        for tag in soup.find_all(["nav", "footer", "header", "script", "style",
                                   "aside", "form", "noscript"]):
            tag.decompose()

        # Try common job-description container selectors first
        SELECTORS = [
            {"class": re.compile(r"job.?descri|position.?descri|vacancy.?descri|role.?descri", re.I)},
            {"id":    re.compile(r"job.?descri|position.?descri|vacancy.?descri", re.I)},
            {"class": re.compile(r"job.?detail|position.?detail|job.?content|posting.?content", re.I)},
            {"class": re.compile(r"content.?main|main.?content|entry.?content", re.I)},
        ]
        for sel in SELECTORS:
            el = soup.find(["div", "section", "article"], attrs=sel)
            if el:
                text = " ".join(el.get_text().split())
                if len(text) > 100:
                    return text[:SNIPPET_LEN]

        # Fallback: take the largest text block on the page
        blocks = soup.find_all(["div", "section", "article", "main"])
        if blocks:
            best = max(blocks, key=lambda b: len(b.get_text()))
            text = " ".join(best.get_text().split())
            return text[:SNIPPET_LEN]

        return ""
    except Exception:
        return ""

# ── ATS scrapers (use JSON APIs where available) ───────────────────────────────

def scrape_greenhouse(slug: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        jobs = []
        for j in r.json().get("jobs", []):
            # Greenhouse returns full HTML content — strip tags
            content_html = j.get("content", "") or ""
            snippet = BeautifulSoup(content_html, "html.parser").get_text()[:SNIPPET_LEN] if content_html else ""
            jobs.append({
                "title":    j["title"],
                "url":      j["absolute_url"],
                "location": j.get("location", {}).get("name", ""),
                "snippet":  snippet,
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
            snippet = (j.get("descriptionPlain") or "")[:SNIPPET_LEN]
            jobs.append({
                "title":    j["text"],
                "url":      j["hostedUrl"],
                "location": j.get("categories", {}).get("location", ""),
                "snippet":  snippet,
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
            if is_junk_title(text):
                continue
            if is_job_link(text, href):
                parent_text = clean_text(a.parent)
                location = extract_location(parent_text)
                if is_blocked_location(location):
                    continue
                jobs.append({
                    "title":    text,
                    "url":      href,
                    "location": location,
                    "snippet":  "",   # fetched below
                    "posted":   "",
                    "_fetch_description": True,
                })
                seen.add(href)
                if len(jobs) >= 40:
                    break
        return jobs
    except Exception as e:
        print(f"    [generic] {e}")
        return []

# ── Aggregator scrapers ────────────────────────────────────────────────────────

def scrape_gesine() -> list:
    """gesinesjobtipps.de — curated German civil society job board"""
    url = "https://www.gesinesjobtipps.de/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        soup = BeautifulSoup(r.text, "html.parser")
        jobs = []
        seen = set()

        for entry in soup.find_all(["article", "div"],
                                    class_=re.compile(r"post|entry|item|listing", re.I)):
            title_el = entry.find(re.compile(r"^h[1-4]$"))
            link_el  = entry.find("a", href=True)
            if title_el and link_el:
                title = clean_text(title_el)
                href  = link_el["href"]
                if href not in seen and len(title) > 5 and not is_junk_title(title):
                    jobs.append({"title": title, "url": href,
                                 "location": "", "snippet": "", "posted": ""})
                    seen.add(href)

        if not jobs:
            for a in soup.find_all("a", href=True):
                text = clean_text(a)
                href = a["href"]
                if (href.startswith("http") and href not in seen
                        and 20 < len(text) < 150 and not is_junk_title(text)):
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
            if href not in seen and is_job_link(text, href) and not is_junk_title(text):
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
            if href not in seen and is_job_link(text, href) and not is_junk_title(text):
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
            if "/jobs/" in href and len(text) > 8 and href not in seen and not is_junk_title(text):
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
            if "/jobs/" in href and len(text) > 8 and href not in seen and not is_junk_title(text):
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
            if href not in seen and is_job_link(text, href) and not is_junk_title(text):
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
            if href not in seen and is_job_link(text, href) and not is_junk_title(text):
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
            if "/vacancies/" in href and len(text) > 8 and href not in seen and not is_junk_title(text):
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
            if href not in seen and is_job_link(text, href) and not is_junk_title(text):
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
            if "/jobs/" in href and len(text) > 8 and href not in seen and not is_junk_title(text):
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
            if is_blocked_location(loc):
                continue
            snippet_html = f.get("body-html", "")
            snippet = BeautifulSoup(snippet_html, "html.parser").get_text()[:SNIPPET_LEN] if snippet_html else ""
            if has_blocked_language_req(snippet):
                continue
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
            if "/en/jobs/" in href and len(text) > 8 and href not in seen and not is_junk_title(text):
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
            # Skip junk titles (extra guard for ATS sources)
            if is_junk_title(j.get("title", "")):
                continue
            # Skip blocked locations
            if is_blocked_location(j.get("location", "")):
                continue

            # Fetch full description for generic scraper entries that need it
            snippet = j.get("snippet", "")
            if j.get("_fetch_description") and not snippet:
                print(f"    → fetching description: {j['title'][:60]}")
                snippet = fetch_job_description(j["url"])
                time.sleep(DELAY)

            # Skip if full description reveals a blocked language requirement
            if has_blocked_language_req(snippet):
                continue

            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", name),
                "org_tags":   org.get("tags", []),
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    snippet,
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
            if is_junk_title(j.get("title", "")):
                continue
            if is_blocked_location(j.get("location", "")):
                continue
            snippet = j.get("snippet", "")
            if has_blocked_language_req(snippet):
                continue
            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", source_name),
                "org_tags":   [],
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    snippet,
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
            if is_junk_title(j.get("title", "")):
                continue
            if is_blocked_location(j.get("location", "")):
                continue
            snippet = j.get("snippet", "")
            if has_blocked_language_req(snippet):
                continue
            jid = job_id(j["url"], j["title"])
            all_jobs.append({
                "id":         jid,
                "title":      j.get("title", ""),
                "org":        j.get("org", source_name),
                "org_tags":   [],
                "url":        j.get("url", ""),
                "location":   j.get("location", ""),
                "snippet":    snippet,
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
