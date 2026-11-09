"""各招聘系统（ATS）的读取模块。

每个函数接收 (session, ats_id)，返回 AdapterResult——它就是一个职位列表（list 的子类，
旧代码按列表使用不受影响），另外带两个属性：
    complete  True  = 确认读全了这个招聘板，可以据此判断“职位已下架”
              False = 只读到一部分 / 无法确认完整（普通网页、分页中断、超过上限等），
                      只用于发现新职位，绝不用来判定下架
    note      中文说明（为什么不完整）
每个职位为 {"job_id", "title", "location", "url", "posted"}。
整体读取失败时抛出异常；接口返回的数据结构不对时抛出 AdapterDataError。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

TIMEOUT = 25


class AdapterDataError(ValueError):
    """接口返回了无法识别的数据结构（不是网络错误）。"""


class AdapterResult(list):
    def __init__(self, jobs=(), complete=True, note=""):
        super().__init__(jobs)
        self.complete = complete
        self.note = note


def _dedupe(jobs):
    """按 job_id 去重，保留第一次出现的。"""
    seen, out = set(), []
    for j in jobs:
        if j["job_id"] in seen:
            continue
        seen.add(j["job_id"])
        out.append(j)
    return out


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
    d = r.json()
    if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
        raise AdapterDataError("Greenhouse 返回中没有 jobs 列表")
    out = []
    for j in d["jobs"]:
        out.append(_job(j.get("id"), j.get("title"), (j.get("location") or {}).get("name"),
                        j.get("absolute_url"), (j.get("updated_at") or "")[:10]))
    return AdapterResult(_dedupe(out))


# ---------- Lever ----------
def lever(s, company):
    r = s.get(f"https://api.lever.co/v0/postings/{company}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, list):
        raise AdapterDataError("Lever 返回的不是职位列表")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        out.append(_job(j.get("id"), j.get("text"), loc, j.get("hostedUrl"), _ms_to_date(j.get("createdAt"))))
    return AdapterResult(_dedupe(out))


# ---------- Ashby ----------
def ashby(s, org):
    r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
        raise AdapterDataError("Ashby 返回中没有 jobs 列表")
    out = []
    for j in d["jobs"]:
        loc = j.get("location") or ""
        extra = [x.get("location", "") for x in (j.get("secondaryLocations") or []) if isinstance(x, dict)]
        if extra:
            loc = "; ".join([loc] + extra)
        out.append(_job(j.get("id"), j.get("title"), loc, j.get("jobUrl"), (j.get("publishedAt") or "")[:10]))
    return AdapterResult(_dedupe(out))


# ---------- SmartRecruiters ----------
def smartrecruiters(s, company):
    out, offset, total, complete, note = [], 0, None, True, ""
    while True:
        try:
            r = s.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                      params={"limit": 100, "offset": offset}, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
        except Exception as e:  # noqa: BLE001
            if offset == 0:
                raise
            complete, note = False, f"第{offset // 100 + 1}页读取失败：{type(e).__name__}"
            break
        if not isinstance(d, dict) or not isinstance(d.get("content"), list):
            if offset == 0:
                raise AdapterDataError("SmartRecruiters 返回中没有 content 列表")
            complete, note = False, "中途返回数据结构异常"
            break
        if total is None:
            total = d.get("totalFound", 0) or 0
        for j in d["content"]:
            loc = j.get("location") or {}
            loc_s = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
            if loc.get("remote"):
                loc_s += " (Remote)"
            out.append(_job(j.get("id"), j.get("name"), loc_s,
                            f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                            (j.get("releasedDate") or "")[:10]))
        offset += 100
        if offset >= total:
            break
        if not d["content"]:
            complete, note = False, f"接口报告 {total} 个职位，但第{offset // 100}页为空"
            break
        if offset >= SMARTRECRUITERS_MAX:
            complete, note = False, f"职位超过 {SMARTRECRUITERS_MAX} 个，只读了前 {SMARTRECRUITERS_MAX} 个"
            break
    out = _dedupe(out)
    if complete and total is not None and len(out) < total:
        complete, note = False, f"接口报告 {total} 个职位，实际只读到 {len(out)} 个"
    return AdapterResult(out, complete, note)


SMARTRECRUITERS_MAX = 2000


# ---------- Workable ----------
def workable(s, account):
    r = s.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}", timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
        raise AdapterDataError("Workable 返回中没有 jobs 列表")
    out = []
    for j in d["jobs"]:
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append(_job(j.get("shortcode"), j.get("title"), loc, j.get("url") or j.get("application_url"),
                        j.get("published_on") or ""))
    return AdapterResult(_dedupe(out))


# ---------- Workday ----------
# ats_id 格式：tenant/wdN/site，例如 tmx/wd3/TMX_Careers
#
# 读取策略（按顺序）：
# 1. 职位总数 ≤ WORKDAY_FULL_MAX：完整分页读完整个招聘板 → complete=True。
# 2. 总数更多：优先用 Workday 自带的“地点”筛选（facet）只读 Toronto/GTA/Ottawa/Ontario 的职位，
#    分页读完 → 新职位能发现，但整板不完整 → complete=False（不判定下架）。
# 3. 找不到地点筛选：退回关键词搜索 → complete=False，并在 note 里写明。
# 任何一页中途失败、空页提前出现、总数和实际不符，都会标记为不完整，不会静默截断。
WORKDAY_PAGE = 20
WORKDAY_FULL_MAX = 1000          # 完整分页的上限（50 页）
WORKDAY_SCOPED_MAX = 1000        # 地点筛选后的分页上限
WORKDAY_DELAY = 0.25             # 每页之间稍停，避免触发限流
WORKDAY_SEARCHES = ["Toronto", "Mississauga", "Ottawa", "Ontario"]
WORKDAY_LOC_WORDS = re.compile(
    r"toronto|mississauga|ottawa|ontario|markham|vaughan|oakville|brampton|north york|"
    r"etobicoke|scarborough|richmond hill|concord|burlington|kanata|canada", re.I)


def _wd_location_facets(first):
    """从第一页的 facets 中找出“地点”筛选里属于目标地区的选项。返回 (参数名, [id...]) 或 None。"""
    facets = first.get("facets") or []
    best = None
    stack = list(facets)
    while stack:
        f = stack.pop()
        if not isinstance(f, dict):
            continue
        param = f.get("facetParameter") or ""
        values = f.get("values") or []
        # 嵌套的 facet（如 locationMainGroup 下再分组）
        stack.extend(v for v in values if isinstance(v, dict) and v.get("facetParameter"))
        if "location" not in param.lower() and "country" not in param.lower():
            continue
        ids = [v.get("id") for v in values
               if isinstance(v, dict) and v.get("id") and WORKDAY_LOC_WORDS.search(v.get("descriptor") or "")]
        # 优先选更细的城市级筛选（排除只有 "Canada" 一项的国家级）
        if ids and (best is None or len(ids) > len(best[1])):
            best = (param, ids)
    return best


def workday(s, ats_id):
    tenant, wd, site = ats_id.split("/")
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs = {}
    notes = []

    def fetch(offset, search="", facets=None):
        r = s.post(api, json={"appliedFacets": facets or {}, "limit": WORKDAY_PAGE, "offset": offset,
                              "searchText": search},
                   headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or not isinstance(d.get("jobPostings"), list):
            raise AdapterDataError("Workday 返回中没有 jobPostings 列表")
        return d

    def add(postings):
        for j in postings:
            path = j.get("externalPath") or ""
            if not path or path in jobs:
                continue
            # 用 externalPath 当职位编号：它在招聘板内唯一。bulletFields 在有些公司里不是唯一编号
            # （例如放的是地点或发布时间），用它会让不同职位“撞号”。
            jobs[path] = _job(path, j.get("title"), j.get("locationsText"),
                              f"{base}/en-US/{site}{path}", j.get("postedOn") or "")

    def paginate(first, cap, search="", facets=None):
        """从已取到的第一页开始翻完。返回 (是否读全, 说明)。"""
        total = first.get("total") or 0     # Workday 只在第一页给 total
        add(first["jobPostings"])
        offset = WORKDAY_PAGE
        while offset < total:
            if offset >= cap:
                return False, f"共 {total} 个，超过上限只读了前 {cap} 个"
            time.sleep(WORKDAY_DELAY)
            try:
                d = fetch(offset, search, facets)
            except Exception as e:  # noqa: BLE001
                return False, f"第 {offset // WORKDAY_PAGE + 1} 页读取失败（{type(e).__name__}），已读 {offset} 个"
            if not d["jobPostings"]:
                return False, f"报告共 {total} 个，但第 {offset // WORKDAY_PAGE + 1} 页为空"
            add(d["jobPostings"])
            offset += WORKDAY_PAGE
        return True, ""

    first = fetch(0)
    total = first.get("total") or 0
    if total <= WORKDAY_FULL_MAX:
        ok, why = paginate(first, WORKDAY_FULL_MAX)
        if ok and len(jobs) < total:
            # 去重后少于 total：可能接口有重复或总数不准，保守处理
            if total - len(jobs) > max(2, total // 50):
                ok, why = False, f"报告共 {total} 个，去重后只有 {len(jobs)} 个"
        return AdapterResult(jobs.values(), ok, why)

    # 大型招聘板：只读目标地区
    loc = _wd_location_facets(first)
    if loc:
        param, ids = loc
        try:
            scoped_first = fetch(0, facets={param: ids})
            ok, why = paginate(scoped_first, WORKDAY_SCOPED_MAX, facets={param: ids})
            notes.append(f"招聘板共 {total} 个职位，按地点筛选只读目标地区"
                         + ("" if ok else f"；{why}"))
            return AdapterResult(jobs.values(), False, "；".join(notes))
        except Exception as e:  # noqa: BLE001
            notes.append(f"地点筛选失败（{type(e).__name__}），改用关键词搜索")
    else:
        notes.append("找不到地点筛选，改用关键词搜索")
    for kw in WORKDAY_SEARCHES:
        try:
            d = fetch(0, kw)
        except Exception as e:  # noqa: BLE001
            notes.append(f"关键词 {kw} 读取失败（{type(e).__name__}）")
            continue
        ok, why = paginate(d, 400, kw)
        if not ok:
            notes.append(f"关键词 {kw}：{why}")
    notes.insert(0, f"招聘板共 {total} 个职位，只读了部分")
    return AdapterResult(jobs.values(), False, "；".join(notes))


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


# 普通网页上的链接很多是导航、新闻，只保留“像职位名”的：必须含一个岗位名词
JOBLIKE = re.compile(
    r"\b(analyst|coordinator|associate|assistant|accountant|officer|administrator|specialist|"
    r"manager|clerk|trainee|graduate|new grad|controller|bookkeeper|producer|registrar|"
    r"representative|advisor|consultant|director|technician|educator|curator|intern)\b", re.I)
NAV = re.compile(
    r"^(careers?|jobs?|join us|work with us|opportunities|volunteer|apply now|learn more|read more)$|"
    r"overview|learn about|talent community|our people|meet |statement|indicators|news|press|"
    r"blog|podcast|report|event|webinar|subscribe|sign up|log ?in|privacy|cookie", re.I)


def _jsonld_jobs(html, base_url):
    """从网页里的 JSON-LD（Google 招聘格式 JobPosting）读出职位。很多招聘页都有。"""
    import json as _json
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = _json.loads(block.strip())
        except Exception:  # noqa: BLE001
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
                continue
            if not isinstance(d, dict):
                continue
            if "@graph" in d:
                stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
            if d.get("@type") != "JobPosting":
                continue
            out.append(_job(d.get("url") or d.get("identifier") or d.get("title"), _clean(d.get("title")),
                            _ld_location(d), urljoin(base_url, d.get("url") or ""), (d.get("datePosted") or "")[:10]))
    return out


def _ld_location(d):
    locs = d.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts = []
    for l in locs:
        a = (l or {}).get("address") or {} if isinstance(l, dict) else {}
        if isinstance(a, dict):
            parts.append(", ".join(x for x in [a.get("addressLocality"), a.get("addressRegion"),
                                               a.get("addressCountry") if isinstance(a.get("addressCountry"), str)
                                               else ""] if x))
    if not parts and str(d.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(p for p in parts if p)


def page(s, url):
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    p = _LinkParser()
    p.feed(r.text)
    out = _jsonld_jobs(r.text, url)
    for text, href in p.links:
        if len(text) < 8 or len(text) > 140 or NAV.match(text) or not JOBLIKE.search(text):
            continue
        full = urljoin(url, href or "")
        # 去掉每次访问都会变的会话编号（如 Job Bank 的 ;jsessionid=...），否则同一职位会被当成新职位
        full = re.sub(r";jsessionid=[^?#]*", "", full, flags=re.I)
        out.append(_job(full, text, "", full))
    # 普通网页无法确认是否读全（可能有 JS 加载、分页、改版），一律标记为不完整
    return AdapterResult(_dedupe(out), False, "普通网页：无法确认是否读全，只用于发现新职位")


# ======================================================================
# 以下是 2026-11 新增的招聘系统。都经过 GitHub Actions 实际联网验证过接口格式。
# ======================================================================
def _clean(t):
    import html as _h
    t = _h.unescape(re.sub(r"<[^>]+>", " ", t or ""))
    return re.sub(r"\s+", " ", t).strip()


def _root(url):
    m = re.match(r"(https?://[^/]+)", url.strip())
    if not m:
        raise ValueError(f"不是网址：{url}")
    return m.group(1)


# ---------- SAP SuccessFactors 招聘网站（jobs.xxx.com/search/?q= 这种） ----------
# ats_id 填网站根地址，例如 https://jobs.bce.ca 、https://careers.deloitte.ca
SF_PAGE = 25
SF_MAX = 2500
SF_DELAY = 0.3
_SF_TOTAL = re.compile(r"Results\s*<b>\s*\d+\s*[–-]\s*\d+\s*</b>\s*of\s*<b>\s*([\d,]+)", re.I)


def successfactors(s, root, known=None):
    root = root.rstrip("/")
    first = s.get(f"{root}/search/", params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                                              "startrow": 0}, timeout=TIMEOUT)
    first.raise_for_status()
    if "jobTitle-link" not in first.text and "data-row" not in first.text:
        try:
            res = _successfactors_unify(s, root)
            if res:
                return res
        except Exception:  # noqa: BLE001
            pass
        return _successfactors_feed(s, root, known)
    m = _SF_TOTAL.search(first.text)
    total = int(m.group(1).replace(",", "")) if m else None
    jobs, html, start, complete, note = [], first.text, 0, True, ""
    while True:
        rows = _sf_rows(html, root)
        jobs.extend(rows)
        start += SF_PAGE
        if total is None:
            complete, note = (len(rows) < SF_PAGE), ("" if len(rows) < SF_PAGE else "找不到职位总数，只读了第一页")
            break
        if start >= total:
            break
        if start >= SF_MAX:
            complete, note = False, f"共 {total} 个，超过上限只读了前 {SF_MAX} 个"
            break
        time.sleep(SF_DELAY)
        try:
            r = s.get(f"{root}/search/", params={"q": "", "sortColumn": "referencedate", "sortDirection": "desc",
                                                  "startrow": start}, timeout=TIMEOUT)
            r.raise_for_status()
            html = r.text
        except Exception as e:  # noqa: BLE001
            complete, note = False, f"第 {start // SF_PAGE + 1} 页读取失败（{type(e).__name__}）"
            break
        if "jobTitle-link" not in html:
            complete, note = False, f"报告共 {total} 个，但第 {start // SF_PAGE + 1} 页为空"
            break
    jobs = _dedupe(jobs)
    if not jobs:
        return _successfactors_feed(s, root, known)
    if complete and total and total - len(jobs) > max(2, total // 50):
        complete, note = False, f"报告共 {total} 个，去重后只有 {len(jobs)} 个"
    return AdapterResult(jobs, complete, note)


def _successfactors_feed(s, root, known=None):
    """最后的办法：SuccessFactors 网站的 /sitemap.xml 其实是最新职位的 RSS（通常只有最近 20 个左右）。"""
    r = s.get(f"{root}/sitemap.xml", timeout=TIMEOUT)
    if (not r.ok or ("<item>" not in r.text and "<loc>" not in r.text)) and _root(root) != root:
        root = _root(root)                     # 网站在子路径下（如 /jobsatcity）时，网站地图在根目录
        r = s.get(f"{root}/sitemap.xml", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        t = re.search(r"<title>(.*?)</title>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        if not t or not link:
            continue
        title = _clean(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1)))
        m = re.match(r"(.*)\(([^()]*)\)\s*$", title)
        title, loc = (m.group(1).strip(), m.group(2).strip()) if m else (title, "")
        url = _clean(link.group(1))
        jid = re.search(r"/(\d{5,})/?", url)
        out.append(_job(jid.group(1) if jid else url, title, loc, url))
    if not out and "<loc>" in r.text:
        # 有的网站 /sitemap.xml 是普通网站地图：按网站地图读取
        res = sitemap(s, f"{root}/sitemap.xml", known=known)
        return AdapterResult(res, False, "按网站地图读取（不完整）")
    if not out and "<rss" not in r.text:
        raise AdapterDataError("SuccessFactors 网站上找不到职位列表")
    return AdapterResult(_dedupe(out), False, "只读到最新职位的 RSS（不完整）")


def _sf_rows(html, root):
    out = []
    for chunk in html.split('class="data-row')[1:]:
        m = re.search(r'<a[^>]*href="([^"]+)"[^>]*class="jobTitle-link"[^>]*>(.*?)</a>|'
                      r'<a[^>]*class="jobTitle-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        href, title = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        loc = re.search(r'class="jobLocation"[^>]*>(.*?)</span>', chunk, re.S)
        date = re.search(r'class="jobDate"[^>]*>(.*?)</span>', chunk, re.S)
        jid = re.search(r"/(\d{5,})/?", href)
        out.append(_job(jid.group(1) if jid else href, _clean(title), _clean(loc.group(1)) if loc else "",
                        urljoin(root + "/", href), _clean(date.group(1)) if date else ""))
    return out


def _successfactors_unify(s, root):
    """新版 SuccessFactors 页面（结果由网页脚本加载），直接调用它背后的接口。"""
    jobs, page_no, total, complete, note = [], 0, None, True, ""
    while True:
        r = s.post(f"{root}/services/recruiting/v1/jobs",
                   json={"locale": "en_US", "pageNumber": page_no, "sortBy": "", "keywords": "", "location": "",
                         "facetFilters": {}, "brand": "", "skills": [], "categoryId": 0, "alertId": "",
                         "rcmCandidateId": ""},
                   headers={"Accept": "application/json"}, timeout=TIMEOUT)
        if page_no == 0:
            r.raise_for_status()
        elif not r.ok:
            complete, note = False, f"第 {page_no + 1} 页读取失败（HTTP {r.status_code}）"
            break
        d = r.json()
        if not isinstance(d, dict) or not isinstance(d.get("jobSearchResult"), list):
            if page_no == 0:
                raise AdapterDataError("SuccessFactors 接口返回中没有 jobSearchResult")
            complete, note = False, "中途返回数据结构异常"
            break
        if total is None:
            total = int(d.get("totalJobs") or 0)
        batch = d["jobSearchResult"]
        for x in batch:
            j = (x or {}).get("response") or {}
            locs = []
            for k, v in j.items():
                if "location" in k.lower() or k == "mfield1":
                    locs.extend(v if isinstance(v, list) else [v])
            title = j.get("unifiedStandardTitle") or j.get("title") or ""
            url_title = j.get("unifiedUrlTitle") or j.get("urlTitle") or ""
            jobs.append(_job(j.get("id"), title, "; ".join(str(x) for x in locs if x),
                             f"{root}/job/{url_title}/{j.get('id')}-en_US/", j.get("unifiedStandardStart") or ""))
        page_no += 1
        if not batch or len(jobs) >= total:
            break
        if len(jobs) >= SF_MAX:
            complete, note = False, f"共 {total} 个，超过上限只读了前 {SF_MAX} 个"
            break
        time.sleep(SF_DELAY)
    jobs = _dedupe(jobs)
    if complete and total and len(jobs) < total - max(2, total // 50):
        complete, note = False, f"报告共 {total} 个，实际只读到 {len(jobs)} 个"
    return AdapterResult(jobs, complete, note)


# ---------- Phenom 招聘网站（jobs.rbc.com/ca/en、jobs.bmo.com/ca/en 等） ----------
# ats_id 填带语言路径的地址，例如 https://jobs.rbc.com/ca/en
PHENOM_PAGE = 50
PHENOM_MAX = 3000


def _slug(t):
    return re.sub(r"[^A-Za-z0-9]+", "-", t or "").strip("-")


def phenom(s, base):
    base = base.rstrip("/")
    r = s.get(f"{base}/search-results", timeout=TIMEOUT)
    r.raise_for_status()
    h = r.text
    ref = re.search(r'"refNum"\s*:\s*"([^"]+)"', h)
    if not ref:
        raise AdapterDataError("页面里找不到 Phenom 的 refNum")
    loc = re.search(r'"locale"\s*:\s*"([a-z]{2}_[A-Z]{2})"', h)
    locale = loc.group(1) if loc else "en_US"
    ctry = re.search(r'"country"\s*:\s*"([a-z]{2})"', h)
    country = ctry.group(1) if ctry else locale[-2:].lower()
    jobs, start, total, complete, note = [], 0, None, True, ""
    while True:
        body = {"lang": locale.lower(), "deviceType": "desktop", "country": country, "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "Most recent", "subsearch": "", "from": start, "jobs": True,
                "counts": True, "all_fields": ["category", "country", "state", "city", "type"],
                "size": PHENOM_PAGE, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                "pageId": "page20", "siteType": "external", "keywords": "", "global": True,
                "selected_fields": {}, "locationData": {}, "refNum": ref.group(1)}
        try:
            w = s.post(_root(base) + "/widgets", json=body, timeout=TIMEOUT)
            w.raise_for_status()
            d = (w.json() or {}).get("refineSearch") or {}
        except Exception as e:  # noqa: BLE001
            if start == 0:
                raise
            complete, note = False, f"第 {start // PHENOM_PAGE + 1} 页读取失败（{type(e).__name__}）"
            break
        batch = (d.get("data") or {}).get("jobs")
        if not isinstance(batch, list):
            if start == 0:
                raise AdapterDataError("Phenom 接口返回中没有 jobs 列表")
            complete, note = False, "中途返回数据结构异常"
            break
        if total is None:
            total = int(d.get("totalHits") or 0)
        for j in batch:
            jid = j.get("jobId") or j.get("reqId") or j.get("jobSeqNo")
            loc_s = j.get("location") or j.get("cityStateCountry") or "; ".join(j.get("multi_location") or [])
            jobs.append(_job(jid, j.get("title"), loc_s, f"{base}/job/{jid}/{_slug(j.get('title'))}",
                             (j.get("postedDate") or "")[:10]))
        start += PHENOM_PAGE
        if not batch or start >= total:
            break
        if start >= PHENOM_MAX:
            complete, note = False, f"共 {total} 个，超过上限只读了前 {PHENOM_MAX} 个"
            break
        time.sleep(0.3)
    jobs = _dedupe(jobs)
    if complete and total and len(jobs) < total - max(2, total // 50):
        complete, note = False, f"报告共 {total} 个，实际只读到 {len(jobs)} 个"
    return AdapterResult(jobs, complete, note)


# ---------- Oracle 招聘云（xxx.fa.xx.oraclecloud.com/hcmUI/CandidateExperience） ----------
# ats_id 填 主机/站点编号，例如 ehtc.fa.ca2.oraclecloud.com/CX_1
ORACLE_PAGE = 25
ORACLE_MAX = 2500
_COUNTRY_ONLY = re.compile(r"^\s*(canada|united states|usa|us|ca)\s*$", re.I)


def oracle(s, ats_id):
    host, site = ats_id.rsplit("/", 1)
    host = host.replace("https://", "").rstrip("/")
    jobs, offset, total, complete, note = [], 0, None, True, ""
    while True:
        url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
               f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
               f"limit={ORACLE_PAGE},offset={offset},sortBy=POSTING_DATES_DESC")
        try:
            r = s.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
        except Exception as e:  # noqa: BLE001
            if offset == 0:
                raise
            complete, note = False, f"第 {offset // ORACLE_PAGE + 1} 页读取失败（{type(e).__name__}）"
            break
        if not items or not isinstance(items[0].get("requisitionList"), list):
            if offset == 0:
                raise AdapterDataError("Oracle 接口返回中没有 requisitionList")
            break
        if total is None:
            total = int(items[0].get("TotalJobsCount") or 0)
        batch = items[0]["requisitionList"]
        for j in batch:
            locs = [j.get("PrimaryLocation") or ""] + [x.get("Name", "") for x in (j.get("secondaryLocations") or [])
                                                       if isinstance(x, dict)]
            locs = [x for x in locs if x and not _COUNTRY_ONLY.match(x)] or \
                   [x for x in [j.get("PrimaryLocation")] if x]
            jobs.append(_job(j.get("Id"), j.get("Title"), "; ".join(locs),
                             f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
                             j.get("PostedDate") or ""))
        offset += ORACLE_PAGE
        if not batch or offset >= total:
            break
        if offset >= ORACLE_MAX:
            complete, note = False, f"共 {total} 个，超过上限只读了前 {ORACLE_MAX} 个"
            break
        time.sleep(0.3)
    jobs = _dedupe(jobs)
    if complete and total and len(jobs) < total - max(2, total // 50):
        complete, note = False, f"报告共 {total} 个，实际只读到 {len(jobs)} 个"
    return AdapterResult(jobs, complete, note)


# ---------- Avature（emplois.bnc.ca/en_CA/careers 这种 /careers/SearchJobs） ----------
AVATURE_PAGE = 20
AVATURE_MAX = 2000


def avature(s, base):
    base = re.sub(r"/searchjobs/?$", "", base.rstrip("/"), flags=re.I)
    jobs, offset, total, complete, note = [], 0, None, True, ""
    while True:
        try:
            r = s.get(f"{base}/SearchJobs", params={"jobRecordsPerPage": AVATURE_PAGE, "jobOffset": offset},
                      timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            if offset == 0:
                raise
            complete, note = False, f"第 {offset // AVATURE_PAGE + 1} 页读取失败（{type(e).__name__}）"
            break
        h = r.text
        if total is None:
            m = re.search(r"\bof\s+([\d,]+)\s*(?:</[^>]+>\s*)?result", h, re.I)
            total = int(m.group(1).replace(",", "")) if m else None
        batch = []
        for m in re.finditer(r'<a[^>]+href="([^"]*/JobDetail/[^"]*?/(\d+))"[^>]*>(.*?)</a>', h, re.S):
            title = _clean(m.group(3))
            if not title:
                continue
            after = h[m.end():m.end() + 900]
            loc = re.search(r'<td[^>]*>(.*?)</td>|class="[^"]*location[^"]*"[^>]*>(.*?)</', after, re.S | re.I)
            loc_s = _clean((loc.group(1) or loc.group(2)) if loc else "")
            batch.append(_job(m.group(2), title, loc_s, urljoin(base + "/", m.group(1))))
        batch = _dedupe(batch)
        jobs.extend(batch)
        offset += AVATURE_PAGE
        if not batch:
            if offset == AVATURE_PAGE:
                raise AdapterDataError("Avature 页面里找不到职位链接")
            break
        if total is None:
            complete, note = (len(batch) < AVATURE_PAGE), "找不到职位总数"
            if len(batch) < AVATURE_PAGE:
                break
        elif offset >= total:
            break
        if offset >= AVATURE_MAX:
            complete, note = False, f"超过上限只读了前 {AVATURE_MAX} 个"
            break
        time.sleep(0.3)
    jobs = _dedupe(jobs)
    if complete and total and len(jobs) < total - max(2, total // 50):
        complete, note = False, f"报告共 {total} 个，实际只读到 {len(jobs)} 个"
    return AdapterResult(jobs, complete, note)


# ---------- Radancy / TMP（careers.blackrock.com/search-jobs 这种） ----------
RADANCY_PAGE = 100
RADANCY_MAX_PAGES = 30


def radancy(s, root):
    root = _root(root)
    jobs, page_no, pages, complete, note = [], 1, None, True, ""
    while True:
        try:
            r = s.get(f"{root}/search-jobs/results", params={
                "ActiveFacetID": 0, "CurrentPage": page_no, "RecordsPerPage": RADANCY_PAGE, "Keywords": "",
                "Location": "", "SearchResultsModuleName": "Search Results",
                "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0,
                "SearchType": 5}, headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                timeout=TIMEOUT)
            r.raise_for_status()
            h = (r.json() or {}).get("results") or ""
        except Exception as e:  # noqa: BLE001
            if page_no == 1:
                raise
            complete, note = False, f"第 {page_no} 页读取失败（{type(e).__name__}）"
            break
        if pages is None:
            m = re.search(r'data-total-pages="(\d+)"', h)
            pages = int(m.group(1)) if m else 1
        batch = []
        for m in re.finditer(r'<a\s([^>]*)>(.*?)</a>', h, re.S):
            attrs, inner = m.group(1), m.group(2)
            href = re.search(r'href="([^"]*/job/[^"]+)"', attrs)
            jid = re.search(r'data-job-id="(\d+)"', attrs)
            if not href or not jid:
                continue
            t = re.search(r"<h2[^>]*>(.*?)</h2>", inner, re.S)
            loc = re.search(r'class="job-location"[^>]*>(.*?)</span>', inner, re.S)
            dt = re.search(r'class="job-date-posted"[^>]*>(.*?)</span>', inner, re.S)
            batch.append(_job(jid.group(1), _clean(t.group(1)) if t else _clean(inner),
                              _clean(loc.group(1)) if loc else "", urljoin(root + "/", href.group(1)),
                              _clean(dt.group(1)) if dt else ""))
        if not batch:
            if page_no == 1:
                raise AdapterDataError("Radancy 返回中找不到职位")
            complete, note = False, f"第 {page_no} 页为空"
            break
        jobs.extend(batch)
        page_no += 1
        if page_no > pages:
            break
        if page_no > RADANCY_MAX_PAGES:
            complete, note = False, f"共 {pages} 页，只读了前 {RADANCY_MAX_PAGES} 页"
            break
        time.sleep(0.3)
    return AdapterResult(_dedupe(jobs), complete, note)


# ---------- Amazon（amazon.jobs，只读加拿大） ----------
def amazon(s, country="CAN"):
    jobs, offset, total, complete, note = [], 0, None, True, ""
    while True:
        r = s.get("https://www.amazon.jobs/en/search.json",
                  params={"normalized_country_code[]": country or "CAN", "result_limit": 100, "offset": offset,
                          "sort": "recent"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
            raise AdapterDataError("Amazon 返回中没有 jobs 列表")
        if total is None:
            total = int(d.get("hits") or 0)
        for j in d["jobs"]:
            posted = j.get("posted_date") or ""
            try:
                posted = datetime.strptime(re.sub(r"\s+", " ", posted), "%B %d, %Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
            jobs.append(_job(j.get("id_icims") or j.get("id"), j.get("title"),
                             j.get("normalized_location") or j.get("location"),
                             "https://www.amazon.jobs" + (j.get("job_path") or ""), posted))
        offset += 100
        if not d["jobs"] or offset >= total:
            break
        if offset >= 2000:
            complete, note = False, "超过 2000 个，只读了前 2000 个"
            break
        time.sleep(0.3)
    return AdapterResult(_dedupe(jobs), complete, note)


# ---------- JazzHR（xxx.applytojob.com） ----------
def jazzhr(s, account):
    url = f"https://{account}.applytojob.com/apply"
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    h = r.text
    out = []
    for m in re.finditer(r'<a[^>]+href="(https?://[\w-]+\.applytojob\.com/apply/(\w+)/[^"]*)"[^>]*>(.*?)</a>', h, re.S):
        title = _clean(m.group(3))
        if not title or title.lower() in ("apply", "apply now", "view job"):
            continue
        after = h[m.end():m.end() + 800]
        loc = re.search(r'fa-map-marker[^>]*>\s*</i>\s*([^<]+)', after)
        out.append(_job(m.group(2), title, _clean(loc.group(1)) if loc else "", m.group(1)))
    if not out and "applytojob" not in h:
        raise AdapterDataError("JazzHR 页面结构异常")
    return AdapterResult(_dedupe(out))


# ---------- BambooHR（xxx.bamboohr.com/careers） ----------
def bamboohr(s, account):
    r = s.get(f"https://{account}.bamboohr.com/careers/list", headers={"Accept": "application/json"},
              timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict) or not isinstance(d.get("result"), list):
        raise AdapterDataError("BambooHR 返回中没有 result 列表")
    out = []
    for j in d["result"]:
        loc = j.get("location") or {}
        loc_s = ", ".join(x for x in [loc.get("city"), loc.get("state")] if x) if isinstance(loc, dict) else str(loc)
        if j.get("isRemote"):
            loc_s = (loc_s + " (Remote)").strip()
        out.append(_job(j.get("id"), j.get("jobOpeningName"), loc_s,
                        f"https://{account}.bamboohr.com/careers/{j.get('id')}"))
    return AdapterResult(_dedupe(out))


# ---------- UKG / UltiPro（recruiting.ultipro.com/租户/JobBoard/编号） ----------
# ats_id 填 主机/租户/JobBoard编号，例如 recruiting.ultipro.ca/TVO5000TVOO/5ff80775-069d-4e87-9adc-94455820b5c9
def ultipro(s, ats_id):
    host, tenant, board = ats_id.replace("https://", "").split("/")[:3]
    base = f"https://{host}/{tenant}/JobBoard/{board}"
    jobs, skip, total, complete, note = [], 0, None, True, ""
    while True:
        r = s.post(f"{base}/JobBoardView/LoadSearchResults", json={"opportunitySearch": {
            "Top": 50, "Skip": skip, "QueryString": "", "Filters": [],
            "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}]}},
            headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        if not isinstance(d, dict) or not isinstance(d.get("opportunities"), list):
            raise AdapterDataError("UltiPro 返回中没有 opportunities 列表")
        if total is None:
            total = int(d.get("totalCount") or 0)
        for j in d["opportunities"]:
            locs = []
            for l in j.get("Locations") or []:
                a = (l or {}).get("Address") or {}
                st = a.get("State") or {}
                locs.append(", ".join(x for x in [a.get("City"), st.get("Code") if isinstance(st, dict) else st] if x)
                            or (l or {}).get("LocalizedDescription") or "")
            jobs.append(_job(j.get("Id"), j.get("Title"), "; ".join(x for x in locs if x),
                             f"{base}/OpportunityDetail?opportunityId={j.get('Id')}", (j.get("PostedDate") or "")[:10]))
        skip += 50
        if not d["opportunities"] or skip >= total or skip >= 1000:
            if skip >= 1000 and skip < total:
                complete, note = False, "超过 1000 个，只读了前 1000 个"
            break
    return AdapterResult(_dedupe(jobs), complete, note)


# ---------- 网站地图（sitemap.xml 里列出全部职位链接的网站，如 careers.kpmg.ca） ----------
# ats_id 填 sitemap 地址；新职位会打开职位页读取标题和地点（每次最多 SITEMAP_DETAIL_MAX 个）
SITEMAP_DETAIL_MAX = 80
_SM_JOB = re.compile(r"/jobs?/|/job-|/posting|/opportunit|/position", re.I)


def sitemap(s, url, known=None):
    known = known or set()
    locs, todo, seen_maps = [], [url], 0
    while todo and seen_maps < 6:
        u = todo.pop(0)
        seen_maps += 1
        r = s.get(u, timeout=TIMEOUT)
        r.raise_for_status()
        found = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        if "<sitemapindex" in r.text:
            todo.extend(found)
        else:
            locs.extend(found)
    jobs_by_id, fetched, skipped = {}, 0, 0
    for loc in locs:
        loc = loc.replace("&amp;", "&")
        if not _SM_JOB.search(loc):
            continue
        num = re.search(r"/(\d{3,})(?:[/?#-]|$)", loc)
        jid = num.group(1) if num else re.sub(r"[?#].*$", "", loc)
        if jid in jobs_by_id and "lang=en" not in loc:
            continue
        slug_title = ""
        tail = re.sub(r"[?#].*$", "", loc).rstrip("/").rsplit("/", 1)[-1]
        if re.search(r"[A-Za-z]{3,}", tail) and not re.fullmatch(r"\d+", tail):
            slug_title = re.sub(r"[-_]+", " ", tail).strip()
        job = _job(jid, slug_title, "", loc)
        if jid not in known and not slug_title:
            if fetched < SITEMAP_DETAIL_MAX:
                fetched += 1
                try:
                    d = s.get(loc, timeout=TIMEOUT)
                    ld = _jsonld_jobs(d.text, loc)
                    if ld:
                        job.update(title=ld[0]["title"], location=ld[0]["location"], posted=ld[0]["posted"])
                    else:
                        t = re.search(r"<title>(.*?)</title>", d.text, re.S)
                        job["title"] = _clean(t.group(1)).split("|")[0].strip() if t else ""
                    time.sleep(0.3)
                except Exception:  # noqa: BLE001
                    skipped += 1
                    continue
            else:
                skipped += 1
                continue
        jobs_by_id[jid] = job
    complete = skipped == 0
    note = "" if complete else f"还有 {skipped} 个新职位页没读，下次继续"
    return AdapterResult(list(jobs_by_id.values()), complete, note)


sitemap.wants_known = True
successfactors.wants_known = True


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "workday": workday,
    "page": page,
    "successfactors": successfactors,
    "phenom": phenom,
    "oracle": oracle,
    "avature": avature,
    "radancy": radancy,
    "amazon": amazon,
    "jazzhr": jazzhr,
    "bamboohr": bamboohr,
    "ultipro": ultipro,
    "sitemap": sitemap,
}
