"""第二轮筛选：打开职位页，读出经验年限、截止日期、薪资和入职要求。

第一轮（filters.classify）只看职位名和地点；"Analyst" 这类头衔可能要应届生，也可能要 3 年经验。
这里对候选职位打开详情页（能用招聘系统接口的直接用接口），从职位描述里提取：
    years_min   要求的最少工作年限（找不到为 None）
    deadline    截止日期 YYYY-MM-DD（找不到为空）
    salary      薪资文字（找不到为空）
    flags       需要注意的条件：仅限在校生 / 要求立即入职 / 要求执照或证书
然后：
    要求 3 年以上经验 → 排除（不再出现在“可申请”里，简报里单独列出被排除的）
    要求 2 年        → 改为“需要核实”
    明确写 0–1 年 / 应届 → 匹配分加分
结果存进 seen.json 的记录里（details 字段），之后不会重复打开同一个职位页。
"""
from __future__ import annotations

import concurrent.futures as cf
import html as _html
import json
import re
from datetime import date, datetime

import requests

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
MAX_PER_RUN = 150          # 每次最多打开多少个职位页
TIMEOUT = 25
EXCLUDE_YEARS = 3          # 要求 >= 3 年经验：排除
REVIEW_YEARS = 2           # 要求 2 年：需要核实

_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10}
_YEARS = re.compile(
    r"(?:minimum of |at least |min\.? |minimum )?"
    r"(\d{1,2}|one|two|three|four|five|six|seven|eight|ten)\s*(?:\+|plus)?\s*"
    r"(?:(?:-|–|to|or)\s*(?:\d{1,2})\s*\+?\s*)?(?:\(\d+\)\s*)?"
    r"years?(?:['’]s?)?\s+(?:of\s+)?(?:[\w/&,-]+\s+){0,6}?(?:experience|work experience)", re.I)
_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_DEADLINE = re.compile(
    r"(?:apply by|deadline(?: to apply)?|closing date|close date|closes(?: on)?|applications? (?:will be accepted until|"
    r"close|closing|due|must be received by)|posting (?:end|close|closing) date|expiry date|end date|"
    r"date limite|accepting applications until)\s*(?:is|:|-|–|on)?\s*"
    r"((?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?(?:" + _MONTHS + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MONTHS + r")\.?,?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.I)
_SALARY = re.compile(
    r"(?:CAD|C\$|\$)\s?\d{2,3}(?:[,\s]?\d{3})*(?:\.\d{2})?\s*(?:k\b)?"
    r"(?:\s*(?:-|–|to|and)\s*(?:CAD|C\$|\$)?\s?\d{2,3}(?:[,\s]?\d{3})*(?:\.\d{2})?\s*(?:k\b)?)?"
    r"(?:\s*(?:per|/|an?)\s*(?:year|annum|hour|hr|annually))?", re.I)
_SALARY_CTX = re.compile(r"salary|compensation|pay range|pay rate|base pay|hourly|annual|per year|per hour|"
                         r"/hour|/year|range", re.I)
FLAGS = [
    (re.compile(r"currently enrolled|must be (?:a )?(?:current|full[- ]time) student|returning to (?:school|studies)|"
                r"return to (?:full[- ]time )?(?:school|studies)|enrolled in a (?:post-secondary|co-?op)|"
                r"co-?op (?:program|placement) requirement", re.I), "仅限在校生/需返校"),
    (re.compile(r"start(?:ing)? immediately|immediate start|available immediately|asap start|start date:? asap",
                re.I), "要求立即入职"),
    (re.compile(r"\b(?:CPA|CFA|CIA|CPP|LLQP|FRM)\b[^.]{0,40}\b(?:required|designation required|must)|"
                r"(?:must|required to) (?:hold|have) (?:a|an|the)? ?(?:CPA|CFA|LLQP|licen[cs]e)", re.I),
     "要求执照/证书"),
]
_NEW_GRAD_TXT = re.compile(r"new grad|recent grad|recent graduate|graduating in 2027|class of 2027|0\s*(?:-|–|to)\s*[12]"
                           r"\s*years|no (?:prior )?experience (?:is )?required|entry[- ]level", re.I)


# ---------- 取职位描述文字 ----------
def _text(html):
    t = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", t, flags=re.I)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"[ \t\r\f\v]+", " ", t)


def _ld(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            d = json.loads(block.strip())
        except Exception:  # noqa: BLE001
            continue
        for x in (d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []):
            if isinstance(x, dict) and x.get("@type") == "JobPosting":
                return x
    return None


def _workday_api(url):
    m = re.match(r"https://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)(/job/.+)$", url)
    if not m:
        return None
    t, wd, site, path = m.groups()
    return f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}{path}"


def _oracle_api(url):
    m = re.match(r"https://([\w.-]+oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}/sites/([\w]+)/job/(\d+)", url)
    if not m:
        return None
    host, site, jid = m.groups()
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{jid}%22,siteNumber={site}")


def fetch(s, url):
    """返回 (描述文字, JSON-LD 或其他结构化信息 dict)。"""
    api = _workday_api(url)
    if api:
        r = s.get(api, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        info = (r.json() or {}).get("jobPostingInfo") or {}
        return _text(info.get("jobDescription") or ""), {"startDate": info.get("startDate"),
                                                          "validThrough": info.get("endDate") or ""}
    api = _oracle_api(url)
    if api:
        r = s.get(api, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        items = (r.json() or {}).get("items") or [{}]
        it = items[0]
        txt = " ".join(_text(it.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalQualificationsStr",
                                                         "ExternalResponsibilitiesStr"))
        return txt, {"validThrough": (it.get("ExternalPostedEndDate") or "")[:10]}
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    ld = _ld(r.text) or {}
    txt = _text(ld.get("description") or "") if ld.get("description") else ""
    page = _text(r.text)
    if len(txt) < 400:
        txt = page
    if len(txt) < 1200:
        # 脚本加载的页面：职位描述常藏在网页脚本里的数据中
        scripts = " ".join(re.findall(r"<script[^>]*>(.*?)</script>", r.text, re.S | re.I))
        scripts = (scripts.replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&")
                   .replace('\\"', '"').replace("\\n", " ").replace("\\/", "/"))
        txt += " " + _text(scripts[:200000])
    return txt, ld


# ---------- 提取 ----------
def _parse_date(s):
    s = re.sub(r"(\d)(st|nd|rd|th)", r"\1", s.strip().replace(",", " ").replace(".", " "))
    s = re.sub(r"^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    for fmt in ("%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s.replace("Sept", "Sep"), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def extract(text, extra=None):
    extra = extra or {}
    out = {"years_min": None, "deadline": "", "salary": "", "flags": [], "new_grad_text": False}
    years = []
    for m in _YEARS.finditer(text or ""):
        v = m.group(1).lower()
        n = int(v) if v.isdigit() else _NUM.get(v)
        before = re.split(r"[.;\n]", text[max(0, m.start() - 60):m.start()])[-1]
        after = re.split(r"[.;\n]", text[m.end():m.end() + 60])[0]
        ctx = (before + m.group(0) + after).lower()
        # 超过 10 年的数字多半是在介绍公司（"over 15 years of experience in the industry"），不当要求
        if n is None or n > 10 or re.search(r"asset|preferred|nice to have|an advantage|ideally|bonus", ctx):
            continue
        years.append(n)
    if years:
        out["years_min"] = min(years)
    vt = (extra.get("validThrough") or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", vt):
        out["deadline"] = vt
    else:
        m = _DEADLINE.search(text or "")
        if m:
            out["deadline"] = _parse_date(m.group(1))
    bs = extra.get("baseSalary")
    if isinstance(bs, dict):
        v = bs.get("value") or {}
        if isinstance(v, dict) and (v.get("minValue") or v.get("value")):
            lo, hi = v.get("minValue") or v.get("value"), v.get("maxValue")
            out["salary"] = f"${lo}" + (f"–${hi}" if hi else "") + (f" /{v.get('unitText', '').lower()}"
                                                                       if v.get("unitText") else "")
    if not out["salary"]:
        for m in _SALARY.finditer(text or ""):
            ctx = text[max(0, m.start() - 80):m.end() + 30]
            if _SALARY_CTX.search(ctx) and re.search(r"\d{2,3}[,\s]?\d{3}|\d+\s*k\b|/\s*h|hour", m.group(0), re.I):
                out["salary"] = re.sub(r"\s+", " ", m.group(0)).strip()[:40]
                break
    for rx, label in FLAGS:
        if rx.search(text or ""):
            out["flags"].append(label)
    out["new_grad_text"] = bool(_NEW_GRAD_TXT.search(text or ""))
    return out


_PROGRAM_TITLE = re.compile(r"graduate|new grad|\b20(?:26|27|28)\b|intern|co-?op|summer|campus|rotational|"
                            r"development program|leadership program|trainee|amplify|student", re.I)


def summary(d):
    """简报里显示的一句话。"""
    if not d or not d.get("ok"):
        return ""
    parts = []
    if d.get("years_min") is not None:
        parts.append(f"经验≥{d['years_min']}年")
    elif d.get("new_grad_text"):
        parts.append("写明应届可申")
    if d.get("deadline"):
        parts.append(f"截止 {d['deadline']}")
    if d.get("salary"):
        parts.append(d["salary"])
    parts += d.get("flags") or []
    return "；".join(parts)


def apply(rec, d):
    """根据职位页信息调整一条记录。返回 'excluded' / 'review' / None。"""
    rec["details"] = d
    if not d.get("ok"):
        return None
    y = d.get("years_min")
    # 应届/实习/项目类职位：描述里常出现“3 年轮岗”“过去 3 年内毕业”等，提取容易误判，只提示、不排除
    program = bool(_PROGRAM_TITLE.search(rec.get("title") or ""))
    if program:
        notes = ([f"职位页提到 {y} 年经验，请确认是否针对应届生"] if y is not None and y >= REVIEW_YEARS else []) + \
                [f"职位页：{f}" for f in d.get("flags") or []]
        if notes and rec.get("candidate_status") == "relevant":
            rec["candidate_status"], rec["relevant"] = "review", False
            rec["filter_reason"] = "；".join(notes)
            return "review"
        return None
    if y is not None and y >= EXCLUDE_YEARS and not d.get("new_grad_text"):
        rec["candidate_status"] = "irrelevant"
        rec["relevant"] = False
        rec["filter_reason"] = f"职位页要求 {y} 年以上经验"
        rec["excluded_by_details"] = True
        return "excluded"
    if "仅限在校生/需返校" in d.get("flags", []) and not re.search(r"intern|co-?op|summer|student",
                                                                rec.get("title") or "", re.I):
        rec["candidate_status"] = "irrelevant"
        rec["relevant"] = False
        rec["filter_reason"] = "职位页写明仅限在校生"
        rec["excluded_by_details"] = True
        return "excluded"
    notes = []
    if y is not None and y >= REVIEW_YEARS:
        notes.append(f"职位页要求 {y} 年经验")
    for f in d.get("flags") or []:
        notes.append(f"职位页：{f}")
    if notes and rec.get("candidate_status") == "relevant":
        rec["candidate_status"] = "review"
        rec["relevant"] = False
        rec["filter_reason"] = "；".join(notes)
        return "review"
    if notes:
        rec["filter_reason"] = "；".join([rec.get("filter_reason") or ""] + notes).strip("；")
    return None


def fit_adjust(d):
    """职位页信息对匹配分的调整。"""
    if not d or not d.get("ok"):
        return 0
    y = d.get("years_min")
    if d.get("new_grad_text") or (y is not None and y <= 1):
        return 10
    if y == 2:
        return -15
    return 0


def check_many(recs, today=None, workers=8):
    """打开一批职位页。recs: 记录列表（会被原地修改）。返回 {'excluded': [...], 'review': [...], 'checked': n}。"""
    from . import browser
    today = today or date.today().isoformat()
    s = requests.Session()
    s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-CA,en;q=0.9"})
    results, short = {}, []

    def one(rec):
        try:
            txt, extra = fetch(s, rec["url"])
            return rec, txt, extra, None
        except Exception as e:  # noqa: BLE001
            return rec, "", {}, f"{type(e).__name__}"

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for rec, txt, extra, err in ex.map(one, recs):
            if err or len(txt.strip()) < 300:
                short.append(rec)
            results[id(rec)] = (txt, extra, err)
    if short and browser.available():
        rendered = browser.render_all([r["url"] for r in short])
        for rec in short:
            r = rendered.get(rec["url"])
            if r and not r.get("error"):
                html = browser.combined_html(r)
                ld = _ld(html) or {}
                results[id(rec)] = (_text(ld.get("description") or "") or _text(html), ld, None)
    out = {"excluded": [], "review": [], "checked": 0}
    for rec in recs:
        txt, extra, err = results.get(id(rec), ("", {}, "未读取"))
        if err or len(txt.strip()) < 300:
            rec["details"] = {"ok": False, "checked": today, "error": err or "职位页内容太少"}
            continue
        d = extract(txt, extra)
        d.update(ok=True, checked=today)
        out["checked"] += 1
        res = apply(rec, d)
        if res:
            out[res].append(rec)
    return out
