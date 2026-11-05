"""Robust public job-posting scraper.

Strategy, cheapest and cleanest first:
1. Known applicant-tracking systems (Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, Workable) expose public JSON APIs; use them directly,
   including Greenhouse boards embedded in company career pages.
2. schema.org ``JobPosting`` JSON-LD, which most job sites embed for search engines.
3. Main-content extraction from the static HTML with boilerplate removed.
4. Optional headless rendering (``browser`` extra) for JavaScript-only pages, in
   a fresh throwaway context: no saved profile, cookies, or logins.

Everything returned is untrusted text; callers treat it as data only.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

MAX_FETCH_BYTES = 3_000_000
MAX_CHARS = 30_000
MIN_CHARS = 200
RETRY_STATUS = {429, 500, 502, 503, 504}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BOILERPLATE_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "header", "aside",
                    "form", "iframe", "button", "template", "dialog"]
BOILERPLATE_ATTR = re.compile(r"cookie|consent|gdpr|newsletter|share|social|breadcrumb|skip-link",
                              re.I)
BLOCK_TAGS = ["p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
              "tr", "table", "blockquote", "pre", "dd", "dt", "header", "main"]


class ScrapeError(ValueError):
    pass


# ---------------------------------------------------------------- utilities

def html_to_text(markup: str | Tag) -> str:
    """HTML to readable text that keeps headings, paragraphs and bullet lists."""
    soup = BeautifulSoup(markup, "html.parser") if isinstance(markup, str) else markup
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return clean(soup.get_text(""))


def clean(text: str) -> str:
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n(- *\n)+", "\n", text)  # empty bullets
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _maybe_html(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = htmllib.unescape(value) if "&lt;" in value else value
    return html_to_text(value) if "<" in value else clean(value)


def _compose(title: str = "", company: str = "", location: str = "", *body: str) -> str:
    head = [f"{label}: {value}" for label, value in
            (("Title", title), ("Company", company), ("Location", location)) if value]
    parts = ["\n".join(head)] + [b for b in body if b and b.strip()]
    return clean("\n\n".join(p for p in parts if p))


class Fetcher:
    """One pooled HTTP client with retries and a response-size cap."""

    def __init__(self, timeout: float = 20.0, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, retries: int = 2):
        self.client = client or httpx.Client(follow_redirects=True, timeout=timeout, headers=HEADERS,
                                             http2=False)
        self.sleep, self.retries = sleep, retries

    def get(self, url: str, **kwargs) -> tuple[str, str, str]:
        """Return (final_url, content_type, text)."""
        for attempt in range(self.retries + 1):
            try:
                with self.client.stream("GET", url, **kwargs) as response:
                    if response.status_code in RETRY_STATUS and attempt < self.retries:
                        self.sleep(self._delay(response, attempt))
                        continue
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size > MAX_FETCH_BYTES:
                            break
                    body = b"".join(chunks)
                    ctype = response.headers.get("content-type", "")
                    meta = re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
                    encoding = response.charset_encoding or (meta and meta.group(1).decode()) or "utf-8"
                    try:
                        text = body.decode(encoding, errors="replace")
                    except LookupError:  # unknown charset name
                        text = body.decode("utf-8", errors="replace")
                    return str(response.url), ctype, text
            except httpx.TransportError:
                if attempt >= self.retries:
                    raise
                self.sleep(1.0 * (attempt + 1))
        raise ScrapeError("request kept failing")  # pragma: no cover

    def json(self, url: str) -> Any:
        return json.loads(self.get(url, headers={"Accept": "application/json"})[2])

    @staticmethod
    def _delay(response: httpx.Response, attempt: int) -> float:
        try:
            return min(float(response.headers.get("retry-after", "")), 10.0)
        except ValueError:
            return 1.0 * (attempt + 1)

    def close(self):
        self.client.close()


# ------------------------------------------------------- ATS public APIs

def _greenhouse(f: Fetcher, board: str, job_id: str) -> str:
    data = f.json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
    return _compose(data.get("title", ""), data.get("company_name", "") or board,
                    (data.get("location") or {}).get("name", ""), _maybe_html(data.get("content")))


def _lever(f: Fetcher, host: str, company: str, job_id: str) -> str:
    api = "api.eu.lever.co" if ".eu." in host else "api.lever.co"
    data = f.json(f"https://{api}/v0/postings/{company}/{job_id}")
    cats = data.get("categories") or {}
    lists = [f"{item.get('text', '')}\n{_maybe_html(item.get('content'))}"
             for item in data.get("lists") or []]
    return _compose(data.get("text", ""), company, cats.get("location", ""),
                    _maybe_html(data.get("description")) or data.get("descriptionPlain", ""),
                    *lists, _maybe_html(data.get("additional")) or data.get("additionalPlain", ""))


def _ashby(f: Fetcher, org: str, job_id: str) -> str:
    data = f.json(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    for job in data.get("jobs") or []:
        if str(job.get("id", "")).lower() == job_id.lower():
            comp = (job.get("compensation") or {}).get("compensationTierSummary", "")
            return _compose(job.get("title", ""), org, job.get("location", ""),
                            _maybe_html(job.get("descriptionHtml")) or job.get("descriptionPlain", ""),
                            f"Compensation: {comp}" if comp else "")
    raise ScrapeError("ashby job not found")


def _workday(f: Fetcher, host: str, path: list[str]) -> str:
    # {tenant}.wdN.myworkdayjobs.com/[locale/]{site}/job/{...}
    if "job" not in path:
        raise ScrapeError("not a workday job URL")
    i = path.index("job")
    site = path[i - 1]
    tenant = host.split(".")[0]
    rest = "/".join(path[i + 1:])
    data = f.json(f"https://{host}/wday/cxs/{tenant}/{site}/job/{rest}")
    info = data.get("jobPostingInfo") or {}
    org = (data.get("hiringOrganization") or {}).get("name", "") or tenant
    return _compose(info.get("title", ""), org, info.get("location", ""),
                    _maybe_html(info.get("jobDescription")))


def _smartrecruiters(f: Fetcher, company: str, slug: str) -> str:
    job_id = re.match(r"\d+", slug)
    if not job_id:
        raise ScrapeError("no smartrecruiters id")
    data = f.json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{job_id.group()}")
    loc = data.get("location") or {}
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    body = [f"{s.get('title', '')}\n{_maybe_html(s.get('text'))}" for s in sections.values()
            if isinstance(s, dict) and s.get("text")]
    return _compose(data.get("name", ""), (data.get("company") or {}).get("name", "") or company,
                    ", ".join(v for v in (loc.get("city"), loc.get("region"), loc.get("country")) if v),
                    *body)


def _workable(f: Fetcher, account: str, code: str) -> str:
    data = f.json(f"https://apply.workable.com/api/v2/accounts/{account}/jobs/{code}")
    loc = data.get("location") or {}
    return _compose(data.get("title", ""), account,
                    ", ".join(v for v in (loc.get("city"), loc.get("country")) if v),
                    *(_maybe_html(data.get(k)) for k in ("description", "requirements", "benefits")))


def ats_text(f: Fetcher, url: str) -> str | None:
    """Text from a known ATS API, or None when the URL is not a recognized ATS posting."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    path = [p for p in u.path.split("/") if p]
    query = parse_qs(u.query)
    if host.endswith("greenhouse.io"):
        if "for" in query and "token" in query:  # embed/job_app?for=board&token=id
            return _greenhouse(f, query["for"][0], query["token"][0])
        if len(path) >= 3 and path[1] == "jobs":
            return _greenhouse(f, path[0], path[2])
    if host.endswith("lever.co") and host.startswith("jobs.") and len(path) >= 2:
        return _lever(f, host, path[0], path[1])
    if host == "jobs.ashbyhq.com" and len(path) >= 2:
        return _ashby(f, path[0], path[1])
    if host.endswith(".myworkdayjobs.com"):
        return _workday(f, host, path)
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and len(path) >= 2:
        return _smartrecruiters(f, path[0], path[1])
    if host == "apply.workable.com" and len(path) >= 3 and path[1] == "j":
        return _workable(f, path[0], path[2])
    return None


EMBEDDED_GREENHOUSE = re.compile(r"greenhouse\.io/embed/job_(?:board|app)[^\"'\s>]*?[?&]for=([\w-]+)")


def embedded_ats_text(f: Fetcher, url: str, page_html: str) -> str | None:
    """Company career pages often wrap a Greenhouse board (``?gh_jid=123``)."""
    gh_jid = parse_qs(urlparse(url).query).get("gh_jid")
    board = EMBEDDED_GREENHOUSE.search(page_html)
    if gh_jid and board:
        return _greenhouse(f, board.group(1), gh_jid[0])
    return None


# ------------------------------------------------------- generic HTML

def _walk_jsonld(node: Any):
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _place(node: Any) -> str:
    out = []
    for loc in node if isinstance(node, list) else [node]:
        if isinstance(loc, dict):
            addr = loc.get("address") or {}
            if isinstance(addr, dict):
                out.append(", ".join(str(addr[k]) for k in
                                     ("addressLocality", "addressRegion", "addressCountry")
                                     if isinstance(addr.get(k), str)))
            elif isinstance(addr, str):
                out.append(addr)
    return "; ".join(o for o in out if o)


def jsonld_text(soup: BeautifulSoup) -> str:
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            data = json.loads(script.string or script.get_text() or "", strict=False)
        except ValueError:
            continue
        for job in _walk_jsonld(data):
            org = job.get("hiringOrganization")
            org = org.get("name", "") if isinstance(org, dict) else (org if isinstance(org, str) else "")
            location = _place(job.get("jobLocation"))
            if job.get("jobLocationType") == "TELECOMMUTE":
                location = f"{location} (Remote)".strip()
            extras = []
            for key in ("employmentType", "datePosted", "validThrough"):
                if isinstance(job.get(key), (str, list)):
                    value = job[key] if isinstance(job[key], str) else ", ".join(map(str, job[key]))
                    extras.append(f"{key}: {value}")
            salary = job.get("baseSalary")
            if isinstance(salary, dict) and isinstance(salary.get("value"), dict):
                v = salary["value"]
                extras.append("Salary: " + " - ".join(
                    str(v[k]) for k in ("minValue", "maxValue", "value") if k in v)
                    + f" {salary.get('currency', '')} {v.get('unitText', '')}".rstrip())
            body = [_maybe_html(job.get(k)) for k in
                    ("description", "responsibilities", "qualifications", "skills",
                     "experienceRequirements", "educationRequirements")]
            text = _compose(str(job.get("title", "")), org, location, "\n".join(extras), *body)
            if len(text) >= MIN_CHARS:
                return text
    return ""


CONTENT_HINT = re.compile(r"job|posting|description|content|details|vacancy|career", re.I)


def main_text(soup: BeautifulSoup) -> str:
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()
    junk = []
    for tag in soup.find_all(True):
        if not tag.attrs or tag.name in ("html", "body", "main", "article"):
            continue
        marker = " ".join([tag.get("id") or "", *(tag.get("class") or [])])
        if tag.get("aria-hidden") == "true" or (marker.strip() and BOILERPLATE_ATTR.search(marker)):
            junk.append(tag)
    for tag in junk:
        if not tag.decomposed:
            tag.decompose()
    candidates = soup.find_all(["main", "article"]) + soup.find_all(attrs={"role": "main"})
    candidates += [t for t in soup.find_all(["div", "section"])
                   if CONTENT_HINT.search(" ".join([t.get("id") or "", *(t.get("class") or [])]))]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    best = ""
    for node in candidates:
        text = html_to_text(node)
        if len(text) > len(best):
            best = text
    body = soup.body or soup
    if len(best) < MIN_CHARS:
        best = html_to_text(body)
    return _compose(title, "", "", best) if title and title not in best[:300] else best


def meta_text(soup: BeautifulSoup) -> str:
    for attrs in ({"property": "og:description"}, {"name": "description"},
                  {"name": "twitter:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            title = soup.find("meta", attrs={"property": "og:title"})
            return _compose(title.get("content", "") if title else "", "", "", tag["content"])
    return ""


def html_page_text(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    text = jsonld_text(soup)
    if len(text) >= MIN_CHARS:
        return text
    text = main_text(soup)
    if len(text) >= MIN_CHARS:
        return text
    return max(text, meta_text(BeautifulSoup(page_html, "html.parser")), key=len)


# ------------------------------------------------------- headless fallback

def render_html(url: str, timeout_ms: int = 30_000) -> str | None:
    """Rendered HTML from a throwaway headless browser, or None if Playwright is missing."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=HEADERS["User-Agent"], accept_downloads=False)
            context.route("**/*", lambda route: route.abort()
                          if route.request.resource_type in ("image", "media", "font")
                          else route.continue_())
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass  # long-polling pages never go idle; use what has rendered
            return page.content()
        finally:
            browser.close()


# ------------------------------------------------------- entry point

def scrape(url: str, *, timeout: float = 20.0, fetcher: Fetcher | None = None,
           render: Callable[[str], str | None] | None = render_html) -> str:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("only http(s) job URLs are supported")
    f = fetcher or Fetcher(timeout)
    try:
        try:
            text = ats_text(f, url)
            if text and len(text) >= MIN_CHARS:
                return text[:MAX_CHARS]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            pass  # fall back to the page itself
        text = ""
        try:
            final_url, ctype, body = f.get(url)
            if "html" in ctype or "<html" in body[:2000].lower():
                try:
                    embedded = embedded_ats_text(f, final_url, body)
                    if embedded and len(embedded) >= MIN_CHARS:
                        return embedded[:MAX_CHARS]
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
                text = html_page_text(body)
            elif "text/plain" in ctype:
                text = clean(body)
        except httpx.HTTPStatusError as err:
            if err.response.status_code not in (401, 403) or render is None:
                raise  # 401/403 is often bot protection that a real browser passes
        if len(text) < MIN_CHARS and render is not None:
            try:
                rendered = render(url)
            except Exception:  # browser missing, crashed, or timed out
                rendered = None
            if rendered:
                text = max(text, html_page_text(rendered), key=len)
        if len(text) < MIN_CHARS:
            raise ValueError("page had too little text; it may need a login. "
                             "Use `jobagent capture` or paste the text instead.")
        return text[:MAX_CHARS]
    finally:
        if fetcher is None:
            f.close()
