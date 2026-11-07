"""Claire 求职扫描器：读取 data/employers.csv 中所有雇主的招聘系统，找出新职位。

用法：
    python -m scanner.main scan                       # 扫描全部雇主
    python -m scanner.main scan --only onex,eq-bank   # 只扫指定雇主（调试用）
    python -m scanner.main reclassify                 # 规则改动后，重新给已有职位分类

输出（都先写临时文件再整体替换，中途出错不会留下写坏的文件）：
    data/seen.json         所有见过的职位，用于去重和判断下架
    data/latest.json       本次运行的新职位、待核实职位、重新出现/下架职位、读取失败的雇主
    data/status.csv        每个雇主本次读取结果（含是否读全）
    reports/latest.md      中文简报（同时按日期存档）

关于“下架”：只有本次**完整读取成功**（scan_state=complete）的雇主，才会把没再出现的职位
标记为下架。读取失败、只读到一部分、普通网页、返回零个职位的雇主，一律不判定下架。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import io
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone

import requests

from .adapters import ADAPTERS, AdapterDataError
from .filters import classify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(ROOT, "reports")
UA = "Mozilla/5.0 (compatible; ClaireJobScanner/1.0; personal job search)"

# 扫描状态（status.csv 的 scan_state 列；旧的 status 列 ok/fail/skip 保持不变）
STATE_LABELS = {
    "complete": "完整成功",
    "partial": "部分成功",
    "unverified": "需人工核验（普通网页，无法确认是否读全）",
    "zero_jobs": "返回零职位",
    "failed": "扫描失败",
    "invalid": "数据异常",
    "not_configured": "未配置招聘入口",
}
STATUS_FIELDS = ["id", "name", "status", "detail", "relevant_now", "checked",
                 "scan_state", "jobs_found", "complete", "note"]
UPDATABLE = ("title", "location", "url", "posted")
HISTORY_MAX = 5
REVIEW_REPORT_MAX = 60


def now_utc():
    return datetime.now(timezone.utc)


def today():
    return now_utc().strftime("%Y-%m-%d")


def new_scan_id():
    return now_utc().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


# ---------- 文件读写 ----------
def load_employers():
    with open(os.path.join(DATA, "employers.csv"), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save_json(name, obj):
    _atomic_write(os.path.join(DATA, name), json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True))


def validate_seen(seen):
    if not isinstance(seen, dict):
        raise ValueError("seen.json 不是字典")
    for k, r in seen.items():
        if not isinstance(r, dict) or "employer_id" not in r or "first_seen" not in r:
            raise ValueError(f"seen.json 记录结构异常：{k}")


def validate_latest(L):
    for key in ("date", "employers_total", "employers_ok", "new_relevant", "removed_relevant", "failed"):
        if key not in L:
            raise ValueError(f"latest.json 缺少字段 {key}")


# ---------- 扫描单个雇主 ----------
def target(emp):
    """决定用哪个读取模块：有 ATS 用 ATS，否则用官方招聘页。"""
    if emp.get("ats") in ADAPTERS and emp.get("ats_id"):
        return emp["ats"], emp["ats_id"]
    if emp.get("careers_url"):
        return "page", emp["careers_url"]
    return None, None


def scan_one(emp):
    """返回 (emp, status, detail, jobs, scan_state, complete, note)。"""
    kind, arg = target(emp)
    if not kind:
        return emp, "skip", "没有招聘系统或官方招聘页", [], "not_configured", False, ""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    last, state = None, "failed"
    for attempt in range(2):
        try:
            jobs = ADAPTERS[kind](s, arg)
            complete = bool(getattr(jobs, "complete", True))
            note = getattr(jobs, "note", "")
            if kind == "page":
                st = "zero_jobs" if not jobs else "unverified"
                complete = False
            elif not jobs:
                # ATS 返回零个职位也可能是接口临时异常，不据此判定所有职位下架
                st, complete = "zero_jobs", False
            else:
                st = "complete" if complete else "partial"
            return emp, "ok", f"{kind}: {len(jobs)} 个职位", list(jobs), st, complete, note
        except AdapterDataError as e:
            last, state = f"{kind}: 数据异常: {str(e)[:150]}", "invalid"
            break
        except Exception as e:  # noqa: BLE001
            last, state = f"{kind}: {type(e).__name__}: {str(e)[:150]}", "failed"
            time.sleep(2)
    return emp, "fail", last, [], state, False, ""


# ---------- 职位记录更新 ----------
def _upgrade(rec):
    """旧版 seen.json 记录补齐新字段（不改动已有值）。"""
    if "candidate_status" not in rec:
        st, why = classify(rec)
        rec["candidate_status"], rec["filter_reason"] = st, why
    rec.setdefault("active", True)
    return rec


def update_existing(rec, job, emp, d, scan_id):
    """职位再次出现：更新最新信息（空值不覆盖旧值），记录变化，必要时恢复为在招。返回是否“重新出现”。"""
    changes = {}
    for f in UPDATABLE:
        new = job.get(f)
        if new and new != rec.get(f):
            changes[f] = [rec.get(f, ""), new]
            rec[f] = new
    if emp.get("name") and emp["name"] != rec.get("employer"):
        rec["employer"] = emp["name"]
    if emp.get("tier"):
        rec["tier"] = emp["tier"]
    st, why = classify(rec)
    if st != rec.get("candidate_status"):
        changes["candidate_status"] = [rec.get("candidate_status", ""), st]
    rec["candidate_status"], rec["filter_reason"], rec["relevant"] = st, why, st == "relevant"
    if changes:
        rec["last_modified_seen"] = d
        hist = rec.get("change_history") or []
        hist.append({"date": d, "run": scan_id, "changes": changes})
        rec["change_history"] = hist[-HISTORY_MAX:]
    reopened = rec.get("active") is False
    if reopened:
        rec["reopened_seen"] = d
        rec.pop("closed_seen", None)
    rec["active"] = True
    rec["last_seen"] = d
    rec["last_seen_run"] = scan_id
    return reopened


# ---------- 主流程 ----------
def scan(only=None, workers=8):
    emps = load_employers()
    if only:
        emps = [e for e in emps if e["id"] in only]
    seen = load_json("seen.json", {})
    validate_seen(seen)
    first_run = not os.path.exists(os.path.join(DATA, "seen.json"))
    for rec in seen.values():
        _upgrade(rec)

    d, scan_id = today(), new_scan_id()
    new, review, reopened, status_rows = [], [], [], []
    ok_ids, complete_ids = set(), set()

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for emp, st, msg, jobs, state, complete, note in ex.map(scan_one, emps):
            n_rel = 0
            if st == "ok":
                ok_ids.add(emp["id"])
                if complete:
                    complete_ids.add(emp["id"])
                for j in jobs:
                    key = f"{emp['id']}::{j['job_id']}"
                    if key in seen:
                        rec = seen[key]
                        if update_existing(rec, j, emp, d, scan_id) and rec["candidate_status"] == "relevant":
                            reopened.append(rec)
                        n_rel += rec["candidate_status"] == "relevant"
                        continue
                    cst, why = classify(j)
                    rec = {**j, "employer_id": emp["id"], "employer": emp["name"], "tier": emp.get("tier", ""),
                           "source": target(emp)[0], "relevant": cst == "relevant",
                           "candidate_status": cst, "filter_reason": why,
                           "first_seen": d, "last_seen": d, "last_seen_run": scan_id,
                           "first_seen_run": scan_id, "active": True}
                    seen[key] = rec
                    n_rel += cst == "relevant"
                    if cst == "relevant":
                        new.append(rec)
                    elif cst == "review":
                        review.append(rec)
            status_rows.append({"id": emp["id"], "name": emp["name"], "status": st, "detail": msg,
                                "relevant_now": n_rel, "checked": d, "scan_state": state,
                                "jobs_found": len(jobs), "complete": "yes" if complete else "no",
                                "note": note})

    # 下架：只针对本次“完整读取成功”的雇主；以本次 scan_id 判断，同一天多次运行也正确
    removed = []
    for key, rec in seen.items():
        if (rec.get("employer_id") in complete_ids and rec.get("active")
                and rec.get("last_seen_run") != scan_id):
            rec["active"] = False
            rec["closed_seen"] = d
            rec["closed_run"] = scan_id
            if rec.get("candidate_status", "relevant" if rec.get("relevant") else "") == "relevant":
                removed.append(rec)

    states = {k: 0 for k in STATE_LABELS}
    for r in status_rows:
        states[r["scan_state"]] = states.get(r["scan_state"], 0) + 1
    fails = [r for r in status_rows if r["status"] == "fail"]
    latest = {"date": d, "scan_id": scan_id, "first_run": first_run, "employers_total": len(emps),
              "employers_ok": len(ok_ids), "employers_failed": len(fails),
              "employers_skipped": sum(r["status"] == "skip" for r in status_rows),
              "employers_complete": len(complete_ids), "scan_states": states,
              "new_relevant": sorted(new, key=lambda r: (r["tier"], r["employer"])),
              "new_review": sorted(review, key=lambda r: (r["tier"], r["employer"])),
              "reopened_relevant": reopened,
              "removed_relevant": removed, "failed": fails,
              "needs_attention": [r for r in status_rows if r["scan_state"] in ("partial", "invalid")]}

    # 先在内存里把所有输出准备好、校验通过，再统一写入
    validate_seen(seen)
    validate_latest(latest)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=STATUS_FIELDS)
    w.writeheader()
    w.writerows(sorted(status_rows, key=lambda r: (r["status"], r["id"])))
    report = render_report(latest)

    save_json("seen.json", seen)
    save_json("latest.json", latest)
    _atomic_write(os.path.join(DATA, "status.csv"), buf.getvalue())
    for name in ("latest.md", f"{d}.md"):
        _atomic_write(os.path.join(REPORTS, name), report)
    print(f"完成（{scan_id}）：{len(ok_ids)}/{len(emps)} 家读取成功，其中完整 {len(complete_ids)} 家；"
          f"新相关职位 {len(new)} 个，待核实 {len(review)} 个，重新出现 {len(reopened)} 个，"
          f"下架 {len(removed)} 个，失败 {len(fails)} 家")
    print("扫描状态：" + "，".join(f"{STATE_LABELS[k]} {v}" for k, v in states.items() if v))
    return latest


def render_report(L):
    st = L.get("scan_states", {})
    lines = [f"# 扫描简报 {L['date']}", "",
             f"雇主共 {L['employers_total']} 家：读取成功 {L['employers_ok']}，失败 {L['employers_failed']}，"
             f"无招聘页跳过 {L['employers_skipped']}。", "",
             f"读取质量：完整成功 {st.get('complete', 0)}，部分成功 {st.get('partial', 0)}，"
             f"返回零职位 {st.get('zero_jobs', 0)}，需人工核验 {st.get('unverified', 0)}，"
             f"数据异常 {st.get('invalid', 0)}。（只有“完整成功”的雇主才会判定职位下架）", ""]
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
    review = L.get("new_review", [])
    lines += ["", f"## 可能相关、需要核实（{len(review)}）", ""]
    if review:
        lines += ["| 层级 | 雇主 | 职位 | 地点 | 待核实原因 | 链接 |", "|---|---|---|---|---|---|"]
        for r in review[:REVIEW_REPORT_MAX]:
            lines.append(f"| {r['tier']} | {r['employer']} | {r['title']} | {r['location'] or '—'} | "
                         f"{r.get('filter_reason', '')} | [打开]({r['url']}) |")
        if len(review) > REVIEW_REPORT_MAX:
            lines.append(f"\n（另有 {len(review) - REVIEW_REPORT_MAX} 个未列出，见 data/latest.json）")
    else:
        lines.append("无")
    reopened = L.get("reopened_relevant", [])
    if reopened:
        lines += ["", f"## 重新出现的相关职位（{len(reopened)}）", ""]
        for r in reopened:
            lines.append(f"- {r['employer']} · {r['title']}（[打开]({r['url']})）")
    lines += ["", f"## 已下架的相关职位（{len(L['removed_relevant'])}）", ""]
    for r in L["removed_relevant"]:
        lines.append(f"- {r['employer']} · {r['title']}（首次出现 {r['first_seen']}）")
    attention = L.get("needs_attention", [])
    if attention:
        lines += ["", f"## 只读到部分或数据异常的雇主（{len(attention)}）", ""]
        for r in attention:
            lines.append(f"- {r['name']}：{STATE_LABELS.get(r['scan_state'], r['scan_state'])}"
                         f"{'，' + r['note'] if r.get('note') else ''}")
    lines += ["", f"## 读取失败的雇主（{len(L['failed'])}）", ""]
    for r in L["failed"]:
        lines.append(f"- {r['name']}：{r['detail']}")
    return "\n".join(lines) + "\n"


def write_report(L):
    """旧接口：保留以兼容。"""
    text = render_report(L)
    for name in ("latest.md", f"{L['date']}.md"):
        _atomic_write(os.path.join(REPORTS, name), text)


def reclassify():
    """用当前规则重新分类所有在招职位，列出分类发生变化的（规则改进后用）。"""
    seen = load_json("seen.json", {})
    validate_seen(seen)
    changed = []
    for rec in seen.values():
        old = rec.get("candidate_status")
        st, why = classify(rec)
        if st != old:
            changed.append((rec, old, st))
        rec["candidate_status"], rec["filter_reason"], rec["relevant"] = st, why, st == "relevant"
    save_json("seen.json", seen)
    up = [(r, o, n) for r, o, n in changed if r.get("active") and n in ("relevant", "review")]
    print(f"分类变化 {len(changed)} 个；其中在招且升级为相关/待核实的 {len(up)} 个：")
    for r, o, n in up[:200]:
        print(f"  [{o or '—'} → {n}] {r.get('employer')} · {r.get('title')} · {r.get('url')}")
    return changed


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan")
    sc.add_argument("--only", help="逗号分隔的雇主 id")
    sc.add_argument("--workers", type=int, default=8)
    sub.add_parser("reclassify")
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(set(a.only.split(",")) if a.only else None, a.workers)
    elif a.cmd == "reclassify":
        reclassify()


if __name__ == "__main__":
    main()
