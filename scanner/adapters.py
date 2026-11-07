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
            jid = (j.get("bulletFields") or [path])[0]
            jobs[path] = _job(jid, j.get("title"), j.get("locationsText"),
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
        # 去掉每次访问都会变的会话编号（如 Job Bank 的 ;jsessionid=...），否则同一职位会被当成新职位
        full = re.sub(r";jsessionid=[^?#]*", "", full, flags=re.I)
        out.append(_job(full, text, "", full))
    # 普通网页无法确认是否读全（可能有 JS 加载、分页、改版），一律标记为不完整
    return AdapterResult(_dedupe(out), False, "普通网页：无法确认是否读全，只用于发现新职位")


ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
    "workday": workday,
    "page": page,
}
