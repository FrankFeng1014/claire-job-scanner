"""各招聘系统（ATS）的读取模块。

每个函数接收 (session, ats_id)，返回统一格式的职位列表：
    {"job_id", "title", "location", "url", "posted"}
读取失败时抛出异常，由 main.py 记录为该雇主"读取失败"。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

TIMEOUT = 25


def _job(job_id, title, location, url, posted=""):
    return {
        "job_id": str(job_id),
        "title": (title or "").strip(),
        "location": (location or "").strip(),
        "url": url or "",
        "posted": posted or "",
    }


def _ms_to_date(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


# ---------- Greenhouse ----------
def greenhouse(s, board):
    r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(_job(j.get("id"), j.get("title"), (j.get("location") or {}).get("name"),
                        j.get("absolute_url"), (j.get("updated_at") or "")[:10]))
    return out


# ---------- Lever ----------
def lever(s, company):
    r = s.get(f"https://api.lever.co/v0/postings/{company}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        out.append(_job(j.get("id"), j.get("text"), loc, j.get("hostedUrl"), _ms_to_date(j.get("createdAt"))))
    return out


# ---------- Ashby ----------
def ashby(s, org):
    r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = j.get("location") or ""
        extra = [x.get("location", "") for x in (j.get("secondaryLocations") or []) if isinstance(x, dict)]
        if extra:
            loc = "; ".join([loc] + extra)
        out.append(_job(j.get("id"), j.get("title"), loc, j.get("jobUrl"), (j.get("publishedAt") or "")[:10]))
    return out


# ---------- SmartRecruiters ----------
def smartrecruiters(s, company):
    out, offset = [], 0
    while True:
        r = s.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                  params={"limit": 100, "offset": offset}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        for j in d.get("content", []):
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            if loc.get("remote"):
                loc_s += " (Remote)"
            out.append(_job(j.get("id"), j.get("name"), loc_s,
                            f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                            (j.get("releasedDate") or "")[:10]))
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 2000:
            break
    return out


# ---------- Workable ----------
def workable(s, account):
    r = s.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append(_job(j.get("shortcode"), j.get("title"), loc, j.get("url") or j.get("application_url"),
                        j.get("published_on") or ""))
    return out


# ---------- Workday ----------
# ats_id 格式：tenant/wdN/site，例如 tmx/wd3/TMX_Careers
WORKDAY_SEARCHES = ["", "Toronto", "Mississauga", "Ottawa", "Ontario"]


def workday(s, ats_id):
    tenant, wd, site = ats_id.split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    seen, out = set(), []

    def page(search, offset):
        r = s.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": search},
                   headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    first = page("", 0)
    total = first.get("total", 0)
    # 职位少（≤400）就全部翻完；职位多的大公司改用城市关键词搜索，避免翻几千条
    searches = [""] if total <= 400 else WORKDAY_SEARCHES[1:]
    for search in searches:
        offset = 0
        data = first if search == "" else page(search, 0)
        while True:
            for j in data.get("jobPostings", []):
                path = j.get("externalPath") or ""
                if path in seen:
                    continue
                seen.add(path)
                jid = (j.get("bulletFields") or [path])[0]
                out.append(_job(jid, j.get("title"), j.get("locationsText"),
                                f"{base}/en-US/{site}{path}", j.get("postedOn") or ""))
            offset += 20
            if offset >= data.get("total", total) or offset >= 400:
                break
            data = page(search, offset)
    return out


# ---------- 普通网页（博物馆、剧团等静态招聘页） ----------
class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            t = re.sub(r"\s+", " ", "".join(self._text)).strip()
            if t:
                self.links.append((t, self._href))
            self._href = None


JOBLIKE = re.compile(
    r"analyst|coordinator|associate|assistant|accountant|officer|administrator|specialist|"
    r"manager|clerk|intern|graduate|new grad|program|development|finance|operations|"
    r"posting|job|career|opportunit", re.I)
NAV = re.compile(r"^(careers?|jobs?|join us|work with us|opportunities|volunteer|apply now|learn more|read more)$", re.I)


def page(s, url):
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    p = _LinkParser()
    p.feed(r.text)
    out = []
    for text, href in p.links:
        if len(text) < 8 or len(text) > 140 or NAV.match(text) or not JOBLIKE.search(text):
            continue
        full = urljoin(url, href or "")
        out.append(_job(full, text, "", full))
    # 去重
    uniq = {j["job_id"]: j for j in out}
    return list(uniq.values())


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "workday": workday,
    "page": page,
}
