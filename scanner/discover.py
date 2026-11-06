"""自动识别雇主用的招聘系统（ATS），减少人工整理。

用法：
    python -m scanner.discover            # 识别结果写到 data/discovered.csv，不改 employers.csv
    python -m scanner.discover --apply    # 把“高可信”结果写回 employers.csv

两种识别方式：
1. 高可信：打开雇主的官方招聘页，在网页源码里找 Greenhouse / Lever / Ashby / Workday /
   SmartRecruiters / Workable 的链接或嵌入代码。
2. 待人工确认：没有招聘页的雇主，按公司名猜测 Greenhouse / Lever / Ashby 的账号名并试探接口，
   且招聘板名称与公司名相近才记录。猜出来的结果不会自动写回。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import os
import re

import requests

from .main import DATA, UA, load_employers

PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/|embed/job_board\?for=)?([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)),
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([\w-]+)", re.I)),
]
IGNORE_IDS = {"embed", "v1", "js", "css", "static", "assets", "wday", "careers", "jobs"}


def from_page(s, url):
    r = s.get(url, timeout=25)
    r.raise_for_status()
    html = r.text
    for kind, pat in PATTERNS:
        for m in pat.finditer(html):
            g = m.groups()
            aid = "/".join(g) if kind == "workday" else g[0]
            if aid.split("/")[0].lower() not in IGNORE_IDS:
                return kind, aid
    return None, None


def slugs(name):
    base = re.sub(r"\(.*?\)", "", name).lower()
    base = re.sub(r"\b(inc|ltd|limited|corp|corporation|canada|group|the|of|co)\b\.?", " ", base)
    words = re.findall(r"[a-z0-9]+", base)
    if not words:
        return []
    # 只用完整公司名（不再用第一个单词），避免把 "National Ballet" 猜成 "national"
    c = ["".join(words), "-".join(words)]
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
        except Exception:
            pass
        try:
            r = s.get(f"https://api.lever.co/v0/postings/{sl}?mode=json&limit=1", timeout=15)
            if r.ok and isinstance(r.json(), list) and r.json():
                return "lever", sl
        except Exception:
            pass
        try:
            r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{sl}", timeout=15)
            if r.ok and r.json().get("jobs"):
                return "ashby", sl
        except Exception:
            pass
    return None, None


def work(emp):
    if emp.get("ats"):
        return None
    s = requests.Session()
    s.headers["User-Agent"] = UA
    if emp.get("careers_url"):
        try:
            k, a = from_page(s, emp["careers_url"])
            if k:
                return {**emp, "ats": k, "ats_id": a, "confidence": "high", "how": "招聘页源码中找到"}
        except Exception as e:  # noqa: BLE001
            return {**emp, "ats": "", "ats_id": "", "confidence": "", "how": f"招聘页打不开：{type(e).__name__}"}
    k, a = guess(s, emp["name"])
    if k:
        return {**emp, "ats": k, "ats_id": a, "confidence": "review", "how": "按公司名猜测，需人工确认"}
    return {**emp, "ats": "", "ats_id": "", "confidence": "", "how": "未识别"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="把高可信结果写回 employers.csv")
    a = ap.parse_args()
    emps = load_employers()
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        res = [r for r in ex.map(work, emps) if r]
    cols = ["id", "name", "tier", "careers_url", "ats", "ats_id", "confidence", "how"]
    with open(os.path.join(DATA, "discovered.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(res)
    high = {r["id"]: r for r in res if r["confidence"] == "high"}
    print(f"待识别 {len(res)} 家：高可信 {len(high)}，待确认 {sum(r['confidence']=='review' for r in res)}")
    if a.apply and high:
        for e in emps:
            if e["id"] in high:
                e["ats"], e["ats_id"] = high[e["id"]]["ats"], high[e["id"]]["ats_id"]
        with open(os.path.join(DATA, "employers.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(emps[0]))
            w.writeheader()
            w.writerows(emps)
        print(f"已写回 employers.csv：{len(high)} 家")


if __name__ == "__main__":
    main()
