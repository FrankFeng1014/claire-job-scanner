"""初筛规则：按地点和职位名，把职位分成三类，并给出原因。

    relevant    可能相关：进入简报主表，交给 Claude 定时任务逐个核实
    review      需要核实：有相关性但有疑点（实习、兼职、地点未注明、头衔含糊等），简报单列
    irrelevant  明确不相关：资深岗、技术岗、门店岗、仅限 Co-op 学生、外省等

这里只做粗筛，不等于申请资格；资格（A/B/C/D）和匹配分由定时任务判断。
被判为不相关的职位仍然保存在 data/seen.json 里（含原因），规则改进后可以用
`python -m scanner.main reclassify` 重新分类，不会被永久丢弃。
修改关键词只需改下面几个列表。
"""
import re

# ---------- 地点 ----------
LOCATIONS = [
    "toronto", "north york", "scarborough", "etobicoke", "mississauga", "brampton", "markham",
    "vaughan", "concord", "woodbridge", "richmond hill", "oakville", "burlington", "milton",
    "pickering", "ajax", "whitby", "oshawa", "newmarket", "aurora", "thornhill", "gta",
    "ottawa", "kanata", "nepean", "gatineau",
]
REMOTE_CANADA = re.compile(r"remote.*(canada|ontario)|(canada|ontario).*remote|canada\s*\(remote\)", re.I)
# 普通网页没有地点字段时，从链接里识别外省/非目标城市
NON_TARGET_URL = re.compile(
    r"[/-](vancouver|calgary|edmonton|montreal|winnipeg|halifax|regina|saskatoon|victoria|"
    r"kelowna|nisku|arthur|kitchener|waterloo|guelph|hamilton|london|laval|quebec|surrey|"
    r"burnaby|red-deer|lethbridge|moncton|fredericton|st-johns|windsor|sudbury|barrie)[/-]", re.I)
_loc = re.compile(r"\b(" + "|".join(re.escape(c) for c in LOCATIONS) + r")\b")
# 明确在加拿大的标志（有它就不按美国处理）
CANADA_MARK = re.compile(r"\bcanada\b|\(can\)|\bontario\b|,\s*on\b|\bon,|\bcan\b", re.I)
# 美国/其他国家：有些美国地名和 GTA 重名（Concord、Scarborough、Burlington、Richmond Hill 等）
US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|"
             "hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|"
             "michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|"
             "new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|"
             "rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
             "west virginia|wisconsin|wyoming")
US_CODES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
            "NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
FOREIGN_LOC = re.compile(r"\(usa\)|\busa\b|united states|\b(" + US_STATES + r")\b|"
                         r"\b(mexico|india|brazil|philippines|united kingdom|england|ireland|france|"
                         r"germany|poland|spain|singapore|australia|china|japan|argentina|colombia|"
                         r"costa rica|kenya|nairobi|rio de janeiro)\b", re.I)
US_CODE_AT_END = re.compile(r",\s*(" + US_CODES + r")\s*$")
# Workday 等系统的职位链接里通常带地点（如 /job/United-States-of-America-Eagan/...）
FOREIGN_URL = re.compile(r"/job/[^/]*(united-states|usa-|mexico|india|brazil|philippines|"
                         r"united-kingdom|germany|poland|singapore|australia|argentina|colombia)", re.I)

# ---------- 职位名 ----------
# 明确不相关（资深 / 技术 / 门店与网点销售 / 需执照 / 体力岗）
HARD_EXCLUDE = [
    (r"\bsenior\b|\bsr\.?\b|\bdirector\b|\bhead of\b|\bvp\b|vice president|\bprincipal\b|\bchief\b|"
     r"\bmanager\b|\bteam lead\b|\blead\b|\bleader\b|\bpartner\b|associate director|associate vice|"
     r"associate partner",
     "资深或管理岗"),
    (r"\bavp\b|\bsvp\b|\bevp\b", "资深或管理岗"),
    (r"engineer|developer|devops|architect|scientist|programmer|technician|\bsoc\b|cyber|"
     r"identity|access management|cloud|data center|network|infrastructure|technical|tech support|"
     r"\bsap\b|oracle|salesforce|servicenow|\bjava\b|python|\.net\b|back[- ]?end|front[- ]?end|"
     r"full[- ]?stack|software|application packaging|application development|application consultant|"
     r"\bitam\b|kubernetes|\bqa\b|satellite|information security|ccaas|"
     r"server administrator|systems? administrator|database|help ?desk|desktop support", "技术/IT岗"),
    (r"\bstore\b|sales associate|retail associate|cashier|barista|\bcook\b|warehouse|driver|"
     r"security guard|electrician|mechanic|millwright|machinist|seasonal retail|team associate|"
     r"fulfil?lment|overnight|distribution cent|team leader|stocking|asset protection|"
     r"food & consumables|\bdeli\b|bakery|welder|merchandise associate|unloader|\bmeat\b|grocery|"
     r"dairy|frozen|\bproduce\b|fashion associate|consumables|over-the-counter|\bcafe\b|custodian|"
     r"baker|packager|rotisserie|member specialist|electronics associate|freight|stock associate|"
     r"\bcashier|front end|community specialist|educator \(store\)", "门店/仓储/体力岗"),
    (r"lawyer|counsel|paralegal|\blegal\b", "法律专业岗"),
    (r"\bjobs?\b|categor|\bsearch\b|^see |\bcareers\b|career planning|opportunit|talent community|"
     r"join our|learn more", "不是具体职位（导航链接）"),
    (r"\blille\b|\bparis\b|\blondon\b|dubai|abu dhabi|\boman\b|muscat|singapore|hong kong|"
     r"new york|f/h|\(e\)", "境外岗位"),
    (r"personal banker|financial services? representative|customer experience associate|"
     r"mortgage specialist|branch|teller|call cent|contact cent", "银行网点/客服销售岗"),
    (r"licen[sc]ed|financial advisor|wealth advisor|financial planner", "需执照/持牌顾问岗"),
    (r"nurse|clinical|pharmac|physician|therapist", "医疗专业岗"),
    (r"executive assistant", "高管助理（通常要多年经验）"),
    (r"\bvolunteer\b|\bunpaid\b|b[ée]n[ée]vole", "志愿者/无薪岗位"),
    (r"payroll|benefits", "薪酬福利操作岗"),
    (r"\bco-?op\b|summer student|^students?\s*[,:&–-]|student work experience|student (intern|position|job)",
     "仅限在校 Co-op/学生"),
]
# 相关职能（命中其一即视为可能相关）
RELEVANT = [
    # 应届项目
    r"new grad|graduate program|graduate development|development program|leadership development|"
    r"rotational|analyst program|management trainee|trainee|early career|entry[- ]level|campus",
    # 财务会计审计
    r"analyst|accountant|accounting|audit|finance|financial|fp&a|treasury|payable|receivable|"
    r"reconciliation|reporting|controller|bookkeep|tax\b|valuation|credit|risk|compliance",
    # 咨询战略运营
    r"consultant|consulting|advisory|strategy|strategic|business transformation|transformation|"
    r"operations|business analyst|commercial|pricing|procurement|purchasing|buyer|planning|"
    r"forecast|replenishment|category|merchandis|supply chain|project coordinator|program",
    # 保险
    r"underwrit|insurance|claims|actuar",
    # 研究
    r"research|econom|insights|market intelligence|data analyst",
    # 艺术与非营利
    r"gallery|museum|curatorial|collections|registrar|arts? admin|arts? management|exhibition|"
    r"fundrais|philanthrop|advancement|development coordinator|stewardship|donor|grant",
    # 通用入门头衔
    r"\bassociate\b|coordinator|administrator|assistant|officer|specialist|junior|\bjr\b",
]
# 有相关性但需要核实的信号
REVIEW_SIGNALS = [
    (r"\bintern(ship)?\b|summer analyst", "实习：需确认是否接受 2027 年 5 月毕业生"),
    (r"part[- ]?time|\bpt\b|casual|freelance|occasional", "兼职/临时：可在读期间做，需确认时间"),
    (r"contract|fixed[- ]term|temporary|\btemp\b|secondment|maternity", "合同/临时岗：需确认入职时间"),
    (r"^associate$|^specialist$|^officer$", "头衔过于笼统，需看职位描述"),
    (r"actuar", "精算岗通常要求考试进度"),
]

_hard = [(re.compile(p, re.I), why) for p, why in HARD_EXCLUDE]
_rel = re.compile("|".join(RELEVANT), re.I)
_review = [(re.compile(p, re.I), why) for p, why in REVIEW_SIGNALS]
_it = re.compile(r"\bIT\b")
_entry = re.compile(r"junior|\bjr\b|associate|analyst|entry|graduate|new grad|trainee|débutant|"
                    r"\bi\b|level 1", re.I)


def is_foreign(loc: str) -> bool:
    """地点明确在美国或其他国家（且没有加拿大标志）。"""
    if not loc or CANADA_MARK.search(loc):
        return False
    return bool(FOREIGN_LOC.search(loc) or US_CODE_AT_END.search(loc.strip()))


_WD_SEG = re.compile(r"/job/([^/]+)/")


def _url_location(url: str) -> str:
    """Workday 职位链接 /job/<地点>/<职位> 中的地点段，例如 Canada-Toronto-Ontario、Hong-Kong-Hong-Kong。"""
    m = _WD_SEG.search(url or "") if "myworkdayjobs.com" in (url or "") else None
    return m.group(1).replace("-", " ").strip() if m else ""


def _url_loc_ok(seg: str) -> bool:
    l = seg.lower()
    return bool(_loc.search(l)) or "canada" in l or "ontario" in l or "remote" in l or "multiple" in l


def location_ok(loc: str) -> bool:
    if not loc:
        return True  # 没有地点信息时先不排除，由 classify 标记为需核实
    if is_foreign(loc):
        return False
    # 按整词匹配，避免 "Hamilton" 被 "milton" 误匹配
    return bool(_loc.search(loc.lower())) or bool(REMOTE_CANADA.search(loc))


def classify(job: dict):
    """返回 (candidate_status, filter_reason)。candidate_status ∈ relevant / review / irrelevant。"""
    title = (job.get("title") or "").strip()
    loc = job.get("location") or ""
    url = job.get("url") or ""
    t = title.lower()

    if not loc and (NON_TARGET_URL.search(url) or FOREIGN_URL.search(url)):
        return "irrelevant", "地点不在目标地区（从链接识别）"
    # 只写了 "Canada"（全国性雇主常见），城市未知：不排除，但需要核实
    country_only = bool(re.fullmatch(r"\s*(canada|ca|can|multiple locations|various locations)\s*", loc, re.I))
    if country_only:
        loc = ""
    url_loc = _url_location(url) if not loc else ""
    if url_loc and not _url_loc_ok(url_loc):
        return "irrelevant", f"地点不在目标地区（链接显示：{url_loc[:40]}）"
    if loc and not location_ok(loc):
        return "irrelevant", f"地点不在目标地区：{loc[:40]}"

    review_reasons = [why for rx, why in _review if rx.search(t)]
    if _it.search(title):                 # 大写 IT 才算技术岗，避免误伤普通单词
        return "irrelevant", "技术/IT岗"
    for rx, why in _hard:
        if rx.search(t):
            # “仅限学生”的实习另作处理：实习本身进 review，Co-op/学生岗直接排除
            return "irrelevant", why
    if not _rel.search(t):
        return "irrelevant", "职位名没有相关职能关键词"
    if re.search(r"consultant|advisor", t) and not _entry.search(t):
        review_reasons.append("顾问/Advisor 头衔在银行和大公司常是中级岗，需确认是否入门")
    if country_only:
        review_reasons.append("地点只写了 Canada，需确认城市")
    if review_reasons:
        return "review", "；".join(review_reasons)
    if not loc:
        return "relevant", "职能相关；地点未注明"
    return "relevant", "职能与地点相关"


def title_ok(title: str) -> bool:
    """旧接口：职位名层面是否“可能相关”（不看地点）。"""
    st, _ = classify({"title": title, "location": "Toronto"})
    return st == "relevant"


def relevant(job: dict) -> bool:
    """旧接口：保留给旧报告字段 relevant 使用，等价于 candidate_status == relevant。"""
    return classify(job)[0] == "relevant"
