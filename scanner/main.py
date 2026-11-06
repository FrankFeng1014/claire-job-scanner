"""Claire 求职扫描器：读取 data/employers.csv 中所有雇主的招聘系统，找出新职位。

用法：
    python -m scanner.main scan       # 扫描全部雇主
    python -m scanner.main scan --only onex,eq-bank   # 只扫指定雇主（调试用）

输出：
    data/seen.json         所有见过的职位（含首次/最近出现日期），用于去重
    data/latest.json       本次运行的新职位、下架职位、读取失败的雇主
    data/status.csv        每个雇主本次读取结果
    reports/latest.md      中文简报（同时按日期存档）
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import time
from datetime import datetime, timezone

import requests

from .adapters import ADAPTERS
from .filters import relevant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(ROOT, "reports")
UA = "Mozilla/5.0 (compatible; ClaireJobScanner/1.0; personal job search)"


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_employers():
    with open(os.path.join(DATA, "employers.csv"), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def target(emp):
    """决定用哪个读取模块：有 ATS 用 ATS，否则用官方招聘页。"""
    if emp.get("ats") in ADAPTERS and emp.get("ats_id"):
        return emp["ats"], emp["ats_id"]
    if emp.get("careers_url"):
        return "page", emp["careers_url"]
    return None, None


def scan_one(emp):
    kind, arg = target(emp)
    if not kind:
        return emp, "skip", "没有招聘系统或官方招聘页", []
    s = requests.Session()
    s.headers["User-Agent"] = UA
    last = None
    for attempt in range(2):
        try:
            jobs = ADAPTERS[kind](s, arg)
            return emp, "ok", f"{kind}: {len(jobs)} 个职位", jobs
        except Exception as e:  # noqa: BLE001
            last = f"{kind}: {type(e).__name__}: {str(e)[:150]}"
            time.sleep(2)
    return emp, "fail", last, []


def scan(only=None, workers=8):
    emps = load_employers()
    if only:
        emps = [e for e in emps if e["id"] in only]
    seen = load_json("seen.json", {})
    d = today()
    new, status_rows, ok_ids = [], [], set()

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for emp, st, msg, jobs in ex.map(scan_one, emps):
            n_rel = 0
            if st == "ok":
                ok_ids.add(emp["id"])
                for j in jobs:
                    rel = relevant(j)
                    n_rel += rel
                    key = f"{emp['id']}::{j['job_id']}"
                    if key in seen:
                        seen[key]["last_seen"] = d
                        seen[key]["active"] = True
                        continue
                    rec = {**j, "employer_id": emp["id"], "employer": emp["name"], "tier": emp.get("tier", ""),
                           "source": target(emp)[0], "relevant": rel, "first_seen": d, "last_seen": d,
                           "active": True}
                    seen[key] = rec
                    if rel:
                        new.append(rec)
            status_rows.append({"id": emp["id"], "name": emp["name"], "status": st, "detail": msg,
                                "relevant_now": n_rel, "checked": d})

    # 下架：本次读取成功的雇主里，之前在、这次不在的相关职位
    removed = []
    for key, rec in seen.items():
        if rec.get("employer_id") in ok_ids and rec.get("active") and rec.get("last_seen") != d:
            rec["active"] = False
            rec["closed_seen"] = d
            if rec.get("relevant"):
                removed.append(rec)

    first_run = not os.path.exists(os.path.join(DATA, "seen.json"))
    save_json("seen.json", seen)
    fails = [r for r in status_rows if r["status"] == "fail"]
    latest = {"date": d, "first_run": first_run, "employers_total": len(emps),
              "employers_ok": len(ok_ids), "employers_failed": len(fails),
              "employers_skipped": sum(r["status"] == "skip" for r in status_rows),
              "new_relevant": sorted(new, key=lambda r: (r["tier"], r["employer"])),
              "removed_relevant": removed, "failed": fails}
    save_json("latest.json", latest)
    with open(os.path.join(DATA, "status.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(status_rows[0]))
        w.writeheader()
        w.writerows(sorted(status_rows, key=lambda r: (r["status"], r["id"])))
    write_report(latest)
    print(f"完成：{len(ok_ids)}/{len(emps)} 家读取成功，新相关职位 {len(new)} 个，下架 {len(removed)} 个，失败 {len(fails)} 家")


def write_report(L):
    os.makedirs(REPORTS, exist_ok=True)
    lines = [f"# 扫描简报 {L['date']}", "",
             f"雇主共 {L['employers_total']} 家：读取成功 {L['employers_ok']}，失败 {L['employers_failed']}，"
             f"无招聘页跳过 {L['employers_skipped']}。", ""]
    if L["first_run"]:
        lines += ["> 这是第一次运行：所有现有职位都会被记为“新职位”，属正常现象，从第二次运行起才是真正的新增。", ""]
    lines += [f"## 新出现的相关职位（{len(L['new_relevant'])}）", ""]
    if L["new_relevant"]:
        lines += ["| 层级 | 雇主 | 职位 | 地点 | 发布 | 链接 |", "|---|---|---|---|---|---|"]
        for r in L["new_relevant"]:
            lines.append(f"| {r['tier']} | {r['employer']} | {r['title']} | {r['location'] or '—'} | "
                         f"{r['posted'] or '—'} | [打开]({r['url']}) |")
    else:
        lines.append("无")
    lines += ["", f"## 已下架的相关职位（{len(L['removed_relevant'])}）", ""]
    for r in L["removed_relevant"]:
        lines.append(f"- {r['employer']} · {r['title']}（首次出现 {r['first_seen']}）")
    lines += ["", f"## 读取失败的雇主（{len(L['failed'])}）", ""]
    for r in L["failed"]:
        lines.append(f"- {r['name']}：{r['detail']}")
    text = "\n".join(lines) + "\n"
    for name in ("latest.md", f"{L['date']}.md"):
        with open(os.path.join(REPORTS, name), "w", encoding="utf-8") as f:
            f.write(text)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--only", help="逗号分隔的雇主 id")
    sc.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(set(a.only.split(",")) if a.only else None, a.workers)


if __name__ == "__main__":
    main()
