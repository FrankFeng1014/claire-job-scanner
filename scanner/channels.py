"""名单以外的招聘渠道：发现名单里没有的雇主。

每个渠道函数返回职位列表，每个职位比普通职位多几个字段：
    company        雇主名称（渠道上显示的）
    company_site   雇主网站（有的话，用来自动找招聘页）
    apply_url      申请链接（有的话，用来自动识别招聘系统）

渠道：
    workinculture  Work In Culture（安省艺术文化行业招聘网站，公开 WordPress 接口）
    jobbank        Job Bank（加拿大联邦政府招聘网站，遵守它 robots.txt 要求的每 5 秒一次）
    adzuna         Adzuna 招聘聚合网站的官方免费接口（需要在仓库 Secrets 里设置
                   ADZUNA_APP_ID 和 ADZUNA_APP_KEY，没设置就自动跳过）

不抓取 LinkedIn、Indeed、Glassdoor 等禁止程序读取的网站。
"""
from __future__ import annotations

import html as _html
import os
import re
import time

from .adapters import TIMEOUT, _clean, _job

# Job Bank 搜索词：覆盖 Claire 的两个方向（财务/分析 与 艺术行政）
JOBBANK_QUERIES = [
    "analyst", "financial analyst", "accounting", "audit", "junior", "entry level", "new graduate",
    "coordinator", "administrative assistant", "research assistant", "economist", "gallery", "museum",
    "arts administrator",
]
JOBBANK_PAGES = 2          # 每个搜索词读前几页（每页 25 个，按日期排序）
JOBBANK_DELAY = 5          # Job Bank 的 robots.txt 要求每次请求间隔 5 秒

ADZUNA_QUERIES = ["analyst", "junior accountant", "entry level finance", "coordinator arts",
                  "research assistant", "new graduate"]
ADZUNA_WHERE = ["Toronto", "Ottawa"]


# ---------- Work In Culture ----------
# 地点用的是 Wikidata 编号
WIC_PLACES = {"q172": "Toronto, ON", "q1930": "Ottawa, ON", "q50816": "Mississauga, ON",
              "q44198": "Brampton, ON", "q44013": "Markham, ON", "q34109": "Gatineau, QC"}
WIC_ONTARIO = "q1904"


def workinculture(s, pages=2):
    out = []
    for page_no in range(1, pages + 1):
        r = s.get("https://workinculture.ca/wp-json/wp/v2/job-listings",
                  params={"per_page": 100, "page": page_no}, timeout=TIMEOUT)
        if page_no > 1 and r.status_code == 400:      # 没有更多页
            break
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            raise ValueError("Work In Culture 返回的不是列表")
        for it in items:
            meta = it.get("meta") or {}
            classes = " ".join(it.get("class_list") or []).lower()
            places = re.findall(r"si_place-(q\d+)", classes)
            loc = next((WIC_PLACES[p] for p in places if p in WIC_PLACES), "")
            if not loc and places and WIC_ONTARIO not in places:
                loc = "Outside Ontario"
            if meta.get("_remote_position") in (1, "1", True):
                loc = (loc + "; Remote, Canada").strip("; ")
            title = _html.unescape(_clean((it.get("title") or {}).get("rendered", "")))
            kind = re.findall(r"job-type-([\w-]+)", classes)
            if kind and kind[0] not in ("full-time",):
                title_for_filter = f"{title} ({kind[0].replace('-', ' ')})"
            else:
                title_for_filter = title
            j = _job(it.get("id"), title_for_filter, loc, it.get("link"), (it.get("date") or "")[:10])
            j.update(company=_html.unescape(meta.get("_company_name") or "").strip(),
                     company_site=meta.get("_company_website") or "",
                     apply_url=meta.get("_application") or "")
            out.append(j)
        if len(items) < 100:
            break
    return out


# ---------- Job Bank ----------
def jobbank(s, queries=None, pages=JOBBANK_PAGES, delay=JOBBANK_DELAY):
    out, seen, first = [], set(), True
    for q in queries or JOBBANK_QUERIES:
        for page_no in range(1, pages + 1):
            if not first:
                time.sleep(delay)
            first = False
            r = s.get("https://www.jobbank.gc.ca/jobsearch/jobsearch",
                      params={"searchstring": q, "locationstring": "", "fprov": "ON", "sort": "D", "page": page_no},
                      timeout=TIMEOUT)
            r.raise_for_status()
            arts = re.split(r'<article id="article-', r.text)[1:]
            for a in arts:
                jid = re.match(r"(\d+)", a)
                if not jid or jid.group(1) in seen:
                    continue
                seen.add(jid.group(1))
                title = re.search(r'class="noctitle">(.*?)</span>', a, re.S)
                biz = re.search(r'class="business">(.*?)</li>', a, re.S)
                loc = re.search(r'class="location">.*?</span>\s*(?:<span[^>]*>.*?</span>)?\s*([^<]+?)\s*</li>', a, re.S)
                date = re.search(r'class="date">\s*([^<]+?)\s*</li>', a, re.S)
                location = _clean(loc.group(1)) if loc else ""
                location = re.sub(r"\s*\(ON\)\s*$", ", ON", location)
                j = _job(jid.group(1), _clean(title.group(1)) if title else "", location,
                         f"https://www.jobbank.gc.ca/jobsearch/jobposting/{jid.group(1)}", _clean(date.group(1)) if date else "")
                j.update(company=_clean(biz.group(1)) if biz else "", company_site="", apply_url="")
                out.append(j)
            if len(arts) < 25:
                break
    return out


# ---------- Adzuna（可选，需要免费的接口密钥） ----------
def adzuna(s):
    app_id, key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not key:
        return None          # 没有设置密钥：跳过（不算失败）
    out, seen = [], set()
    for where in ADZUNA_WHERE:
        for q in ADZUNA_QUERIES:
            r = s.get("https://api.adzuna.com/v1/api/jobs/ca/search/1",
                      params={"app_id": app_id, "app_key": key, "results_per_page": 50, "what": q,
                              "where": where, "max_days_old": 7, "sort_by": "date",
                              "content-type": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            for x in (r.json() or {}).get("results") or []:
                jid = str(x.get("id"))
                if jid in seen:
                    continue
                seen.add(jid)
                j = _job(jid, x.get("title"), (x.get("location") or {}).get("display_name", ""),
                         x.get("redirect_url"), (x.get("created") or "")[:10])
                j.update(company=((x.get("company") or {}).get("display_name") or "").strip(),
                         company_site="", apply_url="")
                out.append(j)
            time.sleep(1)
    return out


CHANNELS = {
    "workinculture": ("Work In Culture", workinculture),
    "jobbank": ("Job Bank", jobbank),
    "adzuna": ("Adzuna", adzuna),
}


# ---------- 雇主名称比对 ----------
_SUFFIX = re.compile(r"\b(inc|incorporated|ltd|limited|ltee|ltée|corp|corporation|co|company|llp|lp|llc|plc|"
                     r"canada|canadian|the|of|and|group|holdings|ulc|s\.?e\.?n\.?c\.?r\.?l\.?)\b\.?", re.I)


def norm_name(name: str) -> str:
    import unicodedata
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"\(.*?\)", " ", n).replace("&", " and ")
    n = _SUFFIX.sub(" ", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def match_employer(company: str, index):
    """index: [(norm_name, emp)]。返回匹配到的雇主或 None。"""
    n = norm_name(company)
    if not n:
        return None
    for en, emp in index:
        if en and en == n:
            return emp
    for en, emp in index:
        if not en or len(en) < 5 or len(n) < 5:
            continue
        a, b = (en, n) if len(en) <= len(n) else (n, en)
        # 短的名字是长名字开头的完整单词，例如 "kroll" 与 "kroll consulting"
        if b == a or b.startswith(a + " "):
            return emp
    return None


# 不加入名单的“雇主”：招聘中介、移民中介、匿名发布
SKIP_COMPANY = re.compile(r"staffing|recruit|hire\b|hiring|personnel|placement|employment agency|immigration|"
                          r"confidential|anonymous|withheld|not disclosed|^n/?a$|talent solutions|"
                          r"nuhire|manpower|randstad|adecco|kelly services|hays\b|robert walters|spherion", re.I)


# 只有这类职位才会让一家“名单外”的雇主自动加入名单（贴合 Claire 的经济学 + 艺术史背景）；
# 普通前台、文员、记账、行政助理等职位仍会出现在简报里，但不会把雇主加入名单
CORE_ROLE = re.compile(r"analyst|accountant|accounting|audit|finance|financial|econom|research|consult|"
                       r"investment|risk|compliance|tax|treasury|valuation|actuar|underwrit|"
                       r"gallery|museum|curat|registrar|collection|exhibit|archiv|arts?\b|cultur|"
                       r"program (coordinator|officer|associate)|development (coordinator|officer|associate)|"
                       r"fundrais|philanthrop|donor|grant|marketing|communications|strategy|operations|"
                       r"procurement|purchasing|supply chain|business analyst|graduate|trainee|associate", re.I)
NOT_CORE = re.compile(r"receptionist|clerk|bookkeep|office (administrator|assistant|manager)|"
                      r"administrative assistant|business development officer|sales|dental|driver", re.I)


def employer_id(name, taken):
    base = re.sub(r"[^a-z0-9]+", "-", norm_name(name) or name.lower()).strip("-")[:50] or "employer"
    i, cand = 1, base
    while cand in taken:
        i += 1
        cand = f"{base}-{i}"
    return cand
