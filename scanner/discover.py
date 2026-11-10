"""自动识别雇主用的招聘系统（ATS），并给没有招聘页的雇主自动找招聘页。

用法：
    python -m scanner.discover            # 结果写到 data/discovered.csv，不改 employers.csv
    python -m scanner.discover --apply    # 把“高可信”结果写回 employers.csv
    python -m scanner.discover --all      # 连已经能完整读取的雇主也重新识别一遍（一般不需要）

哪些雇主会被处理：上次扫描没有“完整/部分成功”的雇主（读不到、零职位、普通网页、没有招聘页）。

识别方法（按顺序）：
1. 打开官方招聘页，在网页源码里找招聘系统的特征（Workday、SuccessFactors、Phenom、Oracle、
   Avature、Radancy、Greenhouse、Lever、Ashby、SmartRecruiters、Workable、JazzHR、BambooHR、UKG 等）。
2. 招聘页本身没有特征时，顺着页面上“搜索职位 / 查看空缺”之类的链接再找一层。
3. 没有招聘页的雇主：按公司名猜官网（xxx.com / xxx.ca），确认网页标题里有公司名后，
   找到官网上的 Careers 链接作为招聘页，再做第 1、2 步。
4. 都不行：按公司名猜 Greenhouse / Lever / Ashby 账号（只记录为“待确认”，不会自动写回）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import os
import re
from urllib.parse import urljoin, urlparse

import requests

from .main import DATA, load_employers

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# (系统, 正则, 如何从匹配结果得到 ats_id)
PATTERNS = [
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board(?:/js)?\?for=)?([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?!api|assets)([\w-]+)", re.I)),
    ("oracle", re.compile(r"([\w-]+\.fa\.[\w-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/(CX_?\d*)", re.I)),
    ("jazzhr", re.compile(r"([\w-]+)\.applytojob\.com", re.I)),
    ("bamboohr", re.compile(r"(?<![\w-])([\w-]+)\.bamboohr\.com/(?:careers|jobs)", re.I)),
    ("ultipro", re.compile(r"(recruiting\d*\.ultipro\.(?:com|ca))/([\w]+)/JobBoard/([0-9a-f-]{36})", re.I)),
]
IGNORE_IDS = {"embed", "v1", "js", "css", "static", "assets", "wday", "careers", "jobs", "www", "api",
              "bhrpendo", "images3", "images", "app"}
SEARCH_LINK = re.compile(r"search|view (all )?(jobs|openings|opportunit|positions)|current (opportunit|openings|"
                         r"positions|vacancies)|job (opportunit|openings|postings|board)|open (positions|roles)|"
                         r"career opportunit|explore (jobs|roles|opportunit)|see (all )?(jobs|openings)|"
                         r"find (a |your )?(job|role)|apply (now|today)|join (our team|us)|work (with|for) us|"
                         r"careers?$|jobs?$|emplois|carrières", re.I)


def detect(html, final_url=""):
    """在网页源码里找招聘系统。返回 (系统, ats_id) 或 (None, None)。"""
    for kind, pat in PATTERNS:
        ids = []
        for m in pat.finditer(html):
            g = m.groups()
            if kind == "workday":
                aid = "/".join(g)
            elif kind in ("oracle", "ultipro"):
                aid = "/".join(g)
            else:
                aid = g[0]
            if aid.split("/")[0].lower() in IGNORE_IDS or aid in ids:
                continue
            if kind == "workday" and g[2].lower() in ("wday", "en-us", "job"):
                continue
            ids.append(aid)
        if ids:
            # Workday 一家公司可能有几个招聘板（社招、校招），都读
            return kind, ";".join(ids[:3]) if kind == "workday" else ids[0]
    host_root = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}" if final_url else ""
    if re.search(r"rmkcdn|jobs2web|j2w\.", html) and host_root:
        return "successfactors", host_root
    if re.search(r"phApp|cdn\.phenompeople", html) and re.search(r'"refNum"\s*:', html) and final_url:
        m = re.search(r'"baseUrl"\s*:\s*"(https?://[^"]+?)/?"', html)
        if m:
            base = m.group(1).replace("\\/", "/")
            # 只接受招聘页本身就是 Phenom 网站的情况；页面里嵌的 Phenom 聊天助手（如 virtualhiringassistant）不算
            if urlparse(base).netloc != urlparse(final_url).netloc:
                return None, None
            return "phenom", base
        p = re.match(r"(https?://[^/]+(?:/[\w-]+)?/[a-z]{2}/[a-z]{2})(?:/|$)", final_url)
        if p:
            return "phenom", p.group(1)
    m = re.search(r'href="([^"]*/careers/SearchJobs)[/"?]', html, re.I)
    if m and final_url:
        return "avature", re.sub(r"/searchjobs$", "", urljoin(final_url, m.group(1)), flags=re.I)
    if re.search(r"radancy|tmpwebeng", html, re.I) and "/search-jobs" in html and host_root:
        return "radancy", host_root
    return None, None


def _session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9"})
    return s


def from_page(s, url, follow=True):
    """打开招聘页识别；找不到就顺着“搜索职位”之类的链接再看一层。"""
    r = s.get(url, timeout=25)
    r.raise_for_status()
    final = getattr(r, "url", None) or url
    k, a = detect(r.text, final)
    if k or not follow:
        return k, a, final
    tried = 0
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', r.text, re.S | re.I):
        t = re.sub(r"<[^>]+>|\s+", " ", text).strip()
        if not t or len(t) > 60 or not SEARCH_LINK.search(t):
            continue
        u2 = urljoin(final, href)
        if not u2.startswith("http") or u2.rstrip("/") == final.rstrip("/"):
            continue
        tried += 1
        try:
            k, a, f = from_page(s, u2, follow=False)
            if k:
                return k, a, f
        except Exception:  # noqa: BLE001
            pass
        if tried >= 4:
            break
    return None, None, final


def _words(name):
    base = re.sub(r"\(.*?\)", " ", name).lower().replace("co-op", "coop")
    base = re.sub(r"\b(inc|ltd|limited|corp|corporation|canada|group|the|of|and|company)\b\.?", " ", base)
    return re.findall(r"[a-z0-9]+", base)


# 不能当作“官网招聘页”的链接：社交网站、招聘网站
BAD_CAREER_HOSTS = re.compile(r"linkedin|indeed|glassdoor|facebook|twitter|x\.com|instagram|youtube|"
                              r"ziprecruiter|workopolis|monster|eluta|talent\.com", re.I)
ATS_HOSTS = re.compile(r"myworkdayjobs|greenhouse|lever\.co|ashbyhq|smartrecruiters|workable|icims|taleo|"
                       r"successfactors|oraclecloud|applytojob|bamboohr|ultipro|dayforcehcm|paycom|adp\.com|"
                       r"jobvite|recruitee|breezy|pinpointhq|teamtailor|njoyn|avature|phenompeople", re.I)


def _base_domain(host):
    parts = host.lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def guess_site(s, name):
    """按公司名猜官网，确认域名和网页标题都对得上公司名，再找官网上的招聘页链接。"""
    w = _words(name)
    if not w:
        return None
    joined = "".join(w)
    if len(joined) < 4:
        return None          # 名字太短（如 Ada），猜域名容易猜错
    cands = [f"https://www.{joined}.com", f"https://www.{joined}.ca", f"https://{joined}.com", f"https://{joined}.ca"]
    if len(w) > 1:
        cands += [f"https://www.{'-'.join(w)}.com", f"https://www.{'-'.join(w)}.ca"]
    for c in dict.fromkeys(cands):
        try:
            r = s.get(c, timeout=12)
        except Exception:  # noqa: BLE001
            continue
        if not r.ok:
            continue
        host = urlparse(getattr(r, "url", c) or c).netloc.lower()
        host_n = re.sub(r"[^a-z0-9]", "", host)
        if joined[:10] not in host_n:
            continue         # 跳转到了别的公司网站
        title = (re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I) or [None, ""])[1].lower()
        title_n = re.sub(r"[^a-z0-9]", "", title)
        if joined[:12] not in title_n and not all(x in title_n for x in w[:2]):
            continue
        for href, text in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', r.text, re.S | re.I):
            t = re.sub(r"<[^>]+>|\s+", " ", text).strip().lower()
            if not (re.fullmatch(r"(careers?|jobs|join (our team|us)|work (with|for) us|careers? at .{1,40}|"
                                 r"job opportunities|employment( opportunities)?|carrières|emplois)", t) or
                    re.search(r"/(careers?|jobs|employment|join-us|work-with-us)/?$", href, re.I)):
                continue
            u = urljoin(getattr(r, "url", c) or c, href)
            h2 = urlparse(u).netloc
            if not u.startswith("http") or BAD_CAREER_HOSTS.search(h2):
                continue
            if _base_domain(h2) != _base_domain(host) and not ATS_HOSTS.search(h2):
                continue
            return u
    return None


def slugs(name):
    w = _words(name)
    c = ["".join(w), "-".join(w)] if w else []
    return list(dict.fromkeys(x for x in c if len(x) >= 4))


def similar(a, b):
    na = re.sub(r"[^a-z0-9]", "", a.lower())
    nb = re.sub(r"[^a-z0-9]", "", b.lower())
    return bool(na) and bool(nb) and (na in nb or nb in na or na[:6] == nb[:6])


def guess(s, name):
    for sl in slugs(name):
        try:
            r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{sl}", timeout=15)
            if r.ok and similar(r.json().get("name", ""), name):
                return "greenhouse", sl
        except Exception:  # noqa: BLE001
            pass
        try:
            r = s.get(f"https://api.lever.co/v0/postings/{sl}?mode=json&limit=1", timeout=15)
            if r.ok and isinstance(r.json(), list) and r.json():
                return "lever", sl
        except Exception:  # noqa: BLE001
            pass
        try:
            r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{sl}", timeout=15)
            if r.ok and r.json().get("jobs"):
                return "ashby", sl
        except Exception:  # noqa: BLE001
            pass
    return None, None


def work(emp):
    s = _session()
    row = {**emp, "new_careers_url": "", "confidence": "", "how": ""}
    url = (emp.get("careers_url") or "").split(";")[0].strip()
    found_site = False
    if not url:
        try:
            url = guess_site(s, emp["name"]) or ""
            found_site = bool(url)
        except Exception:  # noqa: BLE001
            url = ""
    if url:
        try:
            k, a, final = from_page(s, url)
            if k:
                same = (k == emp.get("ats") and a == emp.get("ats_id"))
                return {**row, "ats": k, "ats_id": a, "new_careers_url": url if found_site else "",
                        "confidence": "" if same else "high",
                        "how": ("已是当前设置" if same else
                                ("按公司名找到官网招聘页，" if found_site else "") + "网页源码中找到招聘系统")}
            if found_site:
                return {**row, "new_careers_url": url, "confidence": "high",
                        "how": "按公司名找到官网招聘页（未识别招聘系统，按普通网页读取）"}
        except Exception as e:  # noqa: BLE001
            row["how"] = f"招聘页打不开：{type(e).__name__}"
            if found_site:
                return {**row, "new_careers_url": url, "confidence": "high", "how": "按公司名找到官网招聘页（暂时打不开）"}
    if emp.get("ats"):
        return {**row, "how": row["how"] or "未识别到新的招聘系统"}
    k, a = guess(s, emp["name"])
    if k:
        return {**row, "ats": k, "ats_id": a, "confidence": "review", "how": "按公司名猜测，需人工确认"}
    return {**row, "how": row["how"] or "未识别"}


def _search_links(html, base):
    out = []
    for href, text in re.findall(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        t = re.sub(r"<[^>]+>|\s+", " ", text).strip()
        if t and len(t) <= 60 and SEARCH_LINK.search(t):
            u = urljoin(base, href)
            if u.startswith("http") and u.rstrip("/") != base.rstrip("/") and not BAD_CAREER_HOSTS.search(u):
                out.append(u)
    return list(dict.fromkeys(out))[:3]


def browser_pass(rows):
    """普通读取没认出招聘系统的，用浏览器打开（等脚本加载完、包括 iframe）再认一次；
    还认不出就顺着页面上“搜索职位”之类的链接再打开一层。"""
    from . import browser
    todo = {r["id"]: (r.get("new_careers_url") or (r.get("careers_url") or "").split(";")[0]).strip()
            for r in rows if (r.get("confidence") != "high" or not r.get("ats")) and r.get("how") != "已是当前设置"}
    todo = {k: v for k, v in todo.items() if v.startswith("http")}
    if not todo or not browser.available():
        return rows
    rendered = browser.render_all(list(todo.values()))
    found, second = {}, {}
    for eid, u in todo.items():
        r = rendered.get(u)
        if not r or r.get("error"):
            continue
        k, a = detect(browser.combined_html(r), r.get("final") or u)
        if k:
            found[eid] = (k, a, "浏览器打开招聘页后找到招聘系统")
        else:
            links = _search_links(r.get("html", ""), r.get("final") or u)
            if links:
                second[eid] = links
    if second:
        rendered2 = browser.render_all([u for v in second.values() for u in v])
        for eid, links in second.items():
            for u in links:
                r = rendered2.get(u)
                if r and not r.get("error"):
                    k, a = detect(browser.combined_html(r), r.get("final") or u)
                    if k:
                        found[eid] = (k, a, "浏览器顺着招聘页链接找到招聘系统")
                        break
    print(f"浏览器识别：打开 {len(todo)} 家，新认出招聘系统 {len(found)} 家")
    out = []
    for r in rows:
        if r["id"] in found:
            k, a, how = found[r["id"]]
            r = {**r, "ats": k, "ats_id": a, "confidence": "high",
                 "how": (r.get("how", "") + "；" if r.get("new_careers_url") else "") + how}
        out.append(r)
    return out


def needs_discovery(emps, all_=False):
    """上次没读全的雇主。刚改过设置、还没按新设置扫描过的雇主先不动（等扫描结果出来再说）。"""
    path = os.path.join(DATA, "status.csv")
    rows = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            rows = {r["id"]: r for r in csv.DictReader(f)}
    if all_:
        return list(emps)
    out = []
    for e in emps:
        r = rows.get(e["id"])
        if r and r.get("scan_state") in ("complete", "partial"):
            continue
        if r and e.get("ats") and not (r.get("detail") or "").startswith(e["ats"] + ":"):
            continue
        out.append(e)
    return out


def write_employers(emps):
    from .main import save_employers
    save_employers(emps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把高可信结果写回 employers.csv")
    ap.add_argument("--all", action="store_true", help="所有雇主都重新识别")
    ap.add_argument("--only", help="逗号分隔的雇主 id")
    a = ap.parse_args()
    emps = load_employers()
    todo = needs_discovery(emps, a.all)
    if a.only:
        todo = [e for e in emps if e["id"] in set(a.only.split(","))]
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(work, todo))
    res = browser_pass(res)
    cols = ["id", "name", "tier", "careers_url", "new_careers_url", "ats", "ats_id", "confidence", "how"]
    with open(os.path.join(DATA, "discovered.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(res)
    high = {r["id"]: r for r in res if r["confidence"] == "high"}
    print(f"待识别 {len(res)} 家：高可信 {len(high)}，待确认 {sum(r['confidence'] == 'review' for r in res)}")
    if a.apply and high:
        for e in emps:
            h = high.get(e["id"])
            if not h:
                continue
            if h.get("new_careers_url") and not e.get("careers_url"):
                e["careers_url"] = h["new_careers_url"]
            if h.get("ats") and h.get("ats_id"):
                e["ats"], e["ats_id"] = h["ats"], h["ats_id"]
        write_employers(emps)
        print(f"已写回 employers.csv：{len(high)} 家")


if __name__ == "__main__":
    main()
