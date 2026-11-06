"""初筛规则：地点 + 职位名关键词。

这里只做粗筛，宁可多留；资格（A/B/C/D）和匹配分由 Claude 定时任务判断。
修改关键词只需改下面几个列表。
"""
import re

# 目标地点（Toronto/GTA/Ottawa + 加拿大远程）
LOCATIONS = [
    "toronto", "north york", "scarborough", "etobicoke", "mississauga", "brampton", "markham",
    "vaughan", "concord", "woodbridge", "richmond hill", "oakville", "burlington", "milton",
    "pickering", "ajax", "whitby", "oshawa", "newmarket", "aurora", "thornhill", "gta",
    "ottawa", "kanata", "nepean", "gatineau",
]
REMOTE_CANADA = re.compile(r"remote.*(canada|ontario)|(canada|ontario).*remote|canada\s*\(remote\)", re.I)

# 职位名必须包含其中之一（入门/相关职能）
INCLUDE = [
    r"analyst", r"associate", r"coordinator", r"accountant", r"accounting", r"assistant",
    r"administrator", r"officer", r"new grad", r"graduate", r"rotational", r"development program",
    r"leadership program", r"junior", r"jr\.?", r"entry", r"trainee", r"specialist",
    r"finance", r"financial", r"fp&a", r"audit", r"treasury", r"payable", r"receivable",
    r"reconciliation", r"reporting", r"pricing", r"procurement", r"purchasing", r"buyer",
    r"operations", r"research", r"program", r"fundrais", r"philanthrop", r"advancement",
    r"stewardship", r"donor", r"curatorial", r"collections", r"gallery", r"registrar",
]
# 职位名包含其中之一则排除（资深 / 技术 / 明显不符）
EXCLUDE = [
    r"\bsenior\b", r"\bsr\.?\b", r"\bmanager\b", r"\bdirector\b", r"\bhead of\b", r"\bvp\b",
    r"vice president", r"\bprincipal\b", r"\blead\b", r"\bchief\b", r"\bpartner\b",
    r"engineer", r"developer", r"devops", r"architect", r"scientist", r"technician",
    r"nurse", r"driver", r"warehouse", r"\bcook\b", r"security guard", r"electrician",
    r"mechanic", r"millwright", r"machinist", r"\bstore\b", r"sales associate",
    r"cashier", r"barista", r"\bco-?op\b", r"\bintern(ship)?\b", r"summer student",
]
_inc = re.compile("|".join(INCLUDE), re.I)
_exc = re.compile("|".join(EXCLUDE), re.I)


def location_ok(loc: str) -> bool:
    if not loc:
        return True  # 普通网页没有地点信息，先保留
    l = loc.lower()
    return any(c in l for c in LOCATIONS) or bool(REMOTE_CANADA.search(loc))


def title_ok(title: str) -> bool:
    return bool(_inc.search(title)) and not _exc.search(title)


def relevant(job: dict) -> bool:
    return location_ok(job.get("location", "")) and title_ok(job.get("title", ""))
