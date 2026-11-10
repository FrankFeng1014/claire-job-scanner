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
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone

import requests

from .adapters import ADAPTERS, AdapterDataError
from .filters import classify, fit_score

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
REPORTS = os.path.join(ROOT, "reports")
UA = "Mozilla/5.0 (compatible; ClaireJobScanner/1.0; personal job search)"
# 有些公司网站会拒绝“非浏览器”访问，读网页时用普通浏览器标识
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

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
BASELINE_REPORT_MAX = 80
RECENT_DAYS = 7
NEW_EMPLOYERS_PER_RUN = 30
EMPLOYER_FIELDS = ["id", "name", "tier", "sector", "ats", "ats_id", "careers_url", "source", "added"]


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


def save_employers(emps):
    """整体写回 employers.csv（先写临时文件）。保留所有已有列，新列加在最后。"""
    fields = list(EMPLOYER_FIELDS)
    for e in emps:
        for k in e:
            if k not in fields:
                fields.append(k)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for e in emps:
        w.writerow({k: e.get(k, "") or "" for k in fields})
    _atomic_write(os.path.join(DATA, "employers.csv"), buf.getvalue())


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
    if emp.get("ats") in ADAPTERS and (emp.get("ats_id") or emp["ats"] == "amazon"):
        return emp["ats"], emp.get("ats_id") or ""
    if emp.get("careers_url"):
        return "page", emp["careers_url"]
    return None, None


def _run_adapter(kind, s, arg, known):
    fn = ADAPTERS[kind]
    if getattr(fn, "wants_known", False):
        return fn(s, arg, known=known)
    return fn(s, arg)


def scan_one(emp, known=None):
    """返回 (emp, status, detail, jobs, scan_state, complete, note)。

    ats_id / careers_url 里可以用分号写多个（例如一家公司的社招和校招两个招聘板），会全部读取后合并；
    只有每一个都读全，才算这家雇主“完整读取”。
    """
    kind, arg = target(emp)
    if not kind:
        return emp, "skip", "没有招聘系统或官方招聘页", [], "not_configured", False, ""
    s = requests.Session()
    s.headers["Accept-Language"] = "en-CA,en;q=0.9"
    s.headers["User-Agent"] = UA if kind not in ("page", "successfactors", "phenom", "avature", "radancy",
                                                 "sitemap") else BROWSER_UA
    parts = [x.strip() for x in arg.split(";") if x.strip()] or [arg]
    all_jobs, complete_all, notes, errors, any_ok = [], True, [], [], False
    for part in parts:
        err = None
        for attempt in range(2):
            try:
                jobs = _run_adapter(kind, s, part, known or set())
                complete_all &= bool(getattr(jobs, "complete", True))
                if getattr(jobs, "note", ""):
                    notes.append(jobs.note)
                all_jobs.extend(jobs)
                any_ok, err = True, None
                break
            except AdapterDataError as e:
                err = ("invalid", f"{kind}: 数据异常: {str(e)[:150]}")
                break
            except Exception as e:  # noqa: BLE001
                err = ("failed", f"{kind}: {type(e).__name__}: {str(e)[:150]}")
                time.sleep(2)
        if err:
            errors.append(err)
    if not any_ok:
        state, last = errors[-1]
        return emp, "fail", last, [], state, False, ""
    if errors:
        complete_all = False
        notes.append("部分入口读取失败：" + "；".join(e[1] for e in errors))
    jobs, note = all_jobs, "；".join(notes)
    if kind == "page":
        st = "zero_jobs" if not jobs else "unverified"
        complete = False
    elif not jobs:
        # ATS 返回零个职位也可能是接口临时异常，不据此判定所有职位下架
        st, complete = "zero_jobs", False
    else:
        complete = complete_all
        st = "complete" if complete else "partial"
    return emp, "ok", f"{kind}: {len(jobs)} 个职位", list(jobs), st, complete, note


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
    if st != "irrelevant" and (rec.get("details") or {}).get("ok"):
        # 之前已经打开过职位页：沿用职位页的结论（例如要求 3 年以上经验 → 排除）
        from .details import apply as details_apply
        tmp = {**rec, "candidate_status": st, "filter_reason": why}
        details_apply(tmp, rec["details"])
        st, why = tmp["candidate_status"], tmp["filter_reason"]
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
    # 不再每次都写 last_seen_run：一天扫描多次时，几万条记录每次都改会让仓库迅速变大。
    # “本次是否见到”改由扫描过程中的 seen_now 集合判断。
    rec.pop("last_seen_run", None)
    return reopened


def migrate_workday_keys(seen):
    """把旧版 Workday 记录的编号（bulletFields）换成链接路径（externalPath），与新版读取一致。

    旧编号在部分公司不唯一，不同职位会“撞号”。迁移后同一职位的新旧记录能对上，
    不会因为换编号而被当成新职位或误判下架。迁移不删除任何记录：
    若新编号已被占用，旧记录改为“已停用（编号迁移）”，保留原数据。
    """
    moved = retired = 0
    for key in list(seen):
        rec = seen[key]
        if rec.get("source") != "workday" or "/job/" not in (rec.get("url") or ""):
            continue
        path = rec["url"][rec["url"].index("/job/"):]
        new_key = f"{rec['employer_id']}::{path}"
        if new_key == key:
            continue
        if new_key in seen:
            if rec.get("active"):
                rec["active"] = False
                rec["closed_reason"] = "编号迁移：与另一条记录重复"
            retired += 1
            continue
        rec["legacy_job_id"] = rec.get("job_id")
        rec["job_id"] = path
        seen[new_key] = seen.pop(key)
        moved += 1
    if moved or retired:
        print(f"Workday 编号迁移：{moved} 条换成新编号，{retired} 条重复记录停用（未删除）")
    return moved, retired


# ---------- 雇主读取历史（判断“新接入”的雇主） ----------
def load_employer_state(seen):
    """data/employer_state.json：每家雇主上次成功读取时用的读取方式。

    某家雇主第一次被成功读取（或换了读取方式，例如从普通网页换成 Workday）时，读到的职位大多是
    早就挂出来的，不算“新发布”，单独列为“新接入雇主的现有职位”，不发新岗位通知。
    第一次使用这个文件时，从已有记录推断，避免把老雇主误当成新接入。
    """
    st = load_json("employer_state.json", None)
    if isinstance(st, dict):
        return st
    st = {}
    path = os.path.join(DATA, "status.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("status") == "ok":
                    st[r["id"]] = {"kind": (r.get("detail") or "").split(":")[0].strip() or None,
                                   "arg": None, "first_ok": r.get("checked", "")}
    for rec in seen.values():
        eid = rec.get("employer_id")
        if eid and eid not in st:
            st[eid] = {"kind": rec.get("source"), "arg": None, "first_ok": rec.get("first_seen", "")}
    return st


def is_new_source(state, key, kind, arg):
    old = state.get(key)
    if not old:
        return True
    if old.get("kind") and old["kind"] != kind:
        return True
    return old.get("arg") is not None and old["arg"] != arg


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _new_record(j, emp_id, employer, tier, source, cst, why, d, scan_id, baseline):
    rec = {k: v for k, v in j.items() if k not in ("company", "company_site", "apply_url")}
    rec.update({"employer_id": emp_id, "employer": employer, "tier": tier, "source": source,
                "relevant": cst == "relevant", "candidate_status": cst, "filter_reason": why,
                "first_seen": d, "last_seen": d, "first_seen_run": scan_id,
                "active": True})
    if baseline:
        rec["baseline"] = True
    return rec


# ---------- 名单以外的渠道 ----------
def scan_channels(seen, emps, state, d, scan_id, names=None):
    """读取 Job Bank、Work In Culture 等渠道。返回 (新相关, 新待核实, 新接入, 新雇主, 渠道状态)。

    - 职位所属雇主在名单里：记在该雇主名下（和它自己招聘系统里已有的同名职位去重）。
    - 不在名单里、且职位与 Claire 相关：把这家雇主自动加入名单（data/employers.csv），并报告。
    - 每个渠道第一次运行时，读到的职位只作为“现有职位”记录，不发新岗位通知。
    """
    from . import channels as C
    from .discover import detect

    index = [(C.norm_name(e["name"]), e) for e in emps]
    taken = {e["id"] for e in emps}
    active_titles = {}
    for rec in seen.values():
        if rec.get("active"):
            active_titles.setdefault(rec.get("employer_id"), set()).add(_norm_title(rec.get("title")))
    new, review, baseline, new_emps, status = [], [], [], [], []
    for ch, (label, fn) in C.CHANNELS.items():
        if names and ch not in names:
            continue
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "en-CA,en;q=0.9"})
        try:
            jobs = fn(s)
        except Exception as e:  # noqa: BLE001
            status.append({"channel": label, "status": "fail", "detail": f"{type(e).__name__}: {str(e)[:150]}"})
            continue
        if jobs is None:
            status.append({"channel": label, "status": "skip", "detail": "未设置接口密钥，跳过"})
            continue
        first_time = is_new_source(state, f"channel:{ch}", "channel", ch)
        n_rel = n_added = 0
        for j in jobs:
            cst, why = classify(j)
            if cst == "irrelevant":
                continue
            company = (j.get("company") or "").strip()
            emp = C.match_employer(company, index) if company else None
            if emp is None and company and cst == "relevant" and not C.SKIP_COMPANY.search(company) \
                    and C.CORE_ROLE.search(j.get("title") or "") and not C.NOT_CORE.search(j.get("title") or "") \
                    and len(new_emps) < NEW_EMPLOYERS_PER_RUN:
                k, a = (detect(j.get("apply_url") or "", j.get("apply_url") or "")
                        if j.get("apply_url") else (None, None))
                emp = {"id": C.employer_id(company, taken), "name": company, "tier": "P2",
                       "sector": f"自动发现（{label}）", "ats": k or "", "ats_id": a or "",
                       "careers_url": j.get("company_site") or "", "source": f"auto:{ch}", "added": d}
                taken.add(emp["id"])
                emps.append(emp)
                index.append((C.norm_name(company), emp))
                new_emps.append({**emp, "found_job": j.get("title"), "found_url": j.get("url")})
                n_added += 1
            emp_id = emp["id"] if emp else f"channel-{ch}"
            key = f"{emp_id}::{ch}:{j['job_id']}"
            if key in seen:
                update_existing(seen[key], j, emp or {"name": seen[key].get("employer")}, d, scan_id)
                continue
            if emp and _norm_title(j["title"]) in active_titles.get(emp["id"], set()):
                continue        # 该雇主自己的招聘系统里已经有同名职位
            rec = _new_record(j, emp_id, emp["name"] if emp else (company or label),
                              emp.get("tier", "P2") if emp else "—", f"channel:{ch}", cst,
                              f"{why}（来源：{label}）", d, scan_id, first_time)
            seen[key] = rec
            active_titles.setdefault(emp_id, set()).add(_norm_title(j["title"]))
            n_rel += cst == "relevant"
            if first_time:
                baseline.append(rec)
            elif cst == "relevant":
                new.append(rec)
            else:
                review.append(rec)
        state[f"channel:{ch}"] = {"kind": "channel", "arg": ch, "first_ok": state.get(f"channel:{ch}", {})
                                  .get("first_ok") or d}
        status.append({"channel": label, "status": "ok",
                       "detail": f"{len(jobs)} 个职位，新记录相关 {n_rel} 个，新加入名单的雇主 {n_added} 家"
                                 + ("（第一次运行，只建立基线）" if first_time else "")})
    return new, review, baseline, new_emps, status


# ---------- 最近 7 天汇总（给每天只看一次的定时任务用） ----------
def update_recent(L, now_iso):
    rec = load_json("recent.json", {"items": []})
    items = rec.get("items") if isinstance(rec, dict) else []
    cutoff = (now_utc().timestamp() - RECENT_DAYS * 86400)
    keep = []
    for it in items or []:
        try:
            t = datetime.fromisoformat(it["found_at"]).timestamp()
        except Exception:  # noqa: BLE001
            continue
        if t >= cutoff:
            keep.append(it)

    def slim(r, kind):
        return {"kind": kind, "found_at": now_iso, "scan_id": L["scan_id"], "employer": r.get("employer"),
                "employer_id": r.get("employer_id"), "tier": r.get("tier"), "title": r.get("title"),
                "location": r.get("location"), "url": r.get("url"), "posted": r.get("posted"),
                "status": r.get("candidate_status"), "reason": r.get("filter_reason"), "source": r.get("source"),
                "fit_score": r.get("fit_score"), "fit_reasons": r.get("fit_reasons"),
                "details": r.get("details_note") or "", "deadline": (r.get("details") or {}).get("deadline", "")}
    for r in L["new_relevant"]:
        keep.append(slim(r, "new_relevant"))
    for r in L["new_review"]:
        keep.append(slim(r, "new_review"))
    for r in L.get("baseline", []):
        keep.append(slim(r, "baseline"))
    for e in L.get("new_employers", []):
        keep.append({"kind": "new_employer", "found_at": now_iso, "scan_id": L["scan_id"], "employer": e["name"],
                     "employer_id": e["id"], "source": e.get("source"), "title": e.get("found_job"),
                     "url": e.get("found_url")})
    save_json("recent.json", {"about": f"最近 {RECENT_DAYS} 天每次扫描发现的新职位、新接入职位和新雇主（按发现时间）",
                              "updated": now_iso, "items": keep})


def _browser_suggestions(emps, rendered):
    """浏览器打开普通网页后，如果在里面认出了招聘系统，立即改用该系统读取（下次扫描生效）。"""
    if not rendered:
        return []
    from .discover import detect
    out = []
    for e in emps:
        kind, arg = target(e)
        if kind != "page":
            continue
        for u in arg.split(";"):
            r = rendered.get(u.strip())
            if not r or r.get("error"):
                continue
            from .browser import combined_html
            k, a = detect(combined_html(r), r.get("final") or u)
            if k and k in ADAPTERS:
                out.append({"id": e["id"], "name": e["name"], "ats": k, "ats_id": a})
                break
    return out


# ---------- 主流程 ----------
def scan(only=None, workers=8, channels=True):
    emps_all = load_employers()
    emps = [e for e in emps_all if e["id"] in only] if only else emps_all
    seen = load_json("seen.json", {})
    validate_seen(seen)
    first_run = not os.path.exists(os.path.join(DATA, "seen.json"))
    migrate_workday_keys(seen)
    for rec in seen.values():
        _upgrade(rec)
    state = load_employer_state(seen)

    d, scan_id = today(), new_scan_id()
    now_iso = now_utc().isoformat(timespec="seconds")
    new, review, reopened, status_rows, baseline = [], [], [], [], []
    ok_ids, complete_ids, seen_now, fresh_ids = set(), set(), set(), set()
    known = {}
    for rec in seen.values():
        known.setdefault(rec.get("employer_id"), set()).add(str(rec.get("job_id")))

    # 普通网页类雇主：先用浏览器统一打开（等脚本加载完），读取时直接用浏览器看到的内容
    page_urls = [u.strip() for e in emps if target(e)[0] == "page" for u in target(e)[1].split(";") if u.strip()]
    from . import browser
    t0 = time.time()
    ADAPTERS_RENDERED = browser.render_all(page_urls)
    from . import adapters as _ad
    _ad.RENDERED.clear()
    _ad.RENDERED.update(ADAPTERS_RENDERED)
    if ADAPTERS_RENDERED:
        ok_n = sum(1 for v in ADAPTERS_RENDERED.values() if not v.get("error"))
        print(f"浏览器打开 {len(page_urls)} 个招聘页：成功 {ok_n} 个，用时 {time.time() - t0:.0f} 秒")
    suggested = _browser_suggestions(emps, ADAPTERS_RENDERED)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = ex.map(lambda e: scan_one(e, known.get(e["id"])), emps)
        for emp, st, msg, jobs, state_, complete, note in results:
            n_rel = 0
            if st == "ok":
                ok_ids.add(emp["id"])
                if complete:
                    complete_ids.add(emp["id"])
                kind, arg = target(emp)
                prev = state.get(emp["id"]) or {}
                fresh = (not first_run) and (is_new_source(state, emp["id"], kind, arg)
                                             or bool(prev.get("pending_baseline")))
                if fresh:
                    fresh_ids.add(emp["id"])
                state[emp["id"]] = {"kind": kind, "arg": arg, "first_ok": prev.get("first_ok") or d}
                # 网站地图需要分几次才能读完所有职位页：读完之前，读到的都还算“现有职位”
                if fresh and not complete and kind == "sitemap":
                    state[emp["id"]]["pending_baseline"] = True
                done = set()
                for j in jobs:
                    key = f"{emp['id']}::{j['job_id']}"
                    if key in done:          # 同一次读取里重复出现，只处理第一次
                        continue
                    done.add(key)
                    seen_now.add(key)
                    if key in seen:
                        rec = seen[key]
                        if update_existing(rec, j, emp, d, scan_id) and rec["candidate_status"] == "relevant":
                            reopened.append(rec)
                        n_rel += rec["candidate_status"] == "relevant"
                        continue
                    if not j.get("title"):
                        continue
                    cst, why = classify(j)
                    rec = _new_record(j, emp["id"], emp["name"], emp.get("tier", ""), kind, cst, why, d,
                                      scan_id, fresh)
                    seen[key] = rec
                    n_rel += cst == "relevant"
                    if cst == "irrelevant":
                        continue
                    if fresh:
                        baseline.append(rec)
                    elif cst == "relevant":
                        new.append(rec)
                    else:
                        review.append(rec)
            status_rows.append({"id": emp["id"], "name": emp["name"], "status": st, "detail": msg,
                                "relevant_now": n_rel, "checked": d, "scan_state": state_,
                                "jobs_found": len(jobs), "complete": "yes" if complete else "no",
                                "note": note})

    # 下架：只针对本次“完整读取成功”的雇主；以本次 scan_id 判断，同一天多次运行也正确。
    # 来自外部渠道（Job Bank 等）的记录不在雇主自己的招聘系统里，不参与下架判断。
    removed = []
    for key, rec in seen.items():
        if (rec.get("employer_id") in complete_ids and rec.get("active")
                and key not in seen_now
                and not str(rec.get("source", "")).startswith("channel:")):
            rec["active"] = False
            rec["closed_seen"] = d
            rec["closed_run"] = scan_id
            if rec.get("employer_id") in fresh_ids:
                # 这家雇主刚换了读取方式（例如从普通网页换成招聘系统），旧记录的编号对不上，
                # 不能据此说职位下架；安静停用，不报告
                rec["closed_reason"] = "读取方式更换，旧记录停用"
                continue
            if rec.get("candidate_status", "relevant" if rec.get("relevant") else "") == "relevant":
                removed.append(rec)

    new_emps, ch_status = [], []
    if channels and not only:
        c_new, c_rev, c_base, new_emps, ch_status = scan_channels(seen, emps_all, state, d, scan_id)
        new += c_new
        review += c_rev
        baseline += c_base

    # 第二轮筛选：打开职位页读经验年限、截止日期、薪资（本次候选优先，再补以前没读过的在招职位）
    excluded, details_checked = [], 0
    if details_enabled() and not only:
        from . import details as D
        this_run = [r for r in new + review + baseline if not r.get("details")]
        ids = {id(r) for r in this_run}
        older = [r for r in seen.values() if r.get("active") and id(r) not in ids and not r.get("details")
                 and r.get("candidate_status") in ("relevant", "review") and classify(r)[0] != "irrelevant"]
        rank = lambda r: -fit_score(r)[0]   # noqa: E731
        batch = (sorted(this_run, key=rank) + sorted(older, key=rank))[:D.MAX_PER_RUN]
        if batch:
            t0 = time.time()
            res = D.check_many(batch, today=d)
            excluded, details_checked = res["excluded"], res["checked"]
            print(f"打开职位页 {len(batch)} 个：读到 {res['checked']} 个，排除 {len(res['excluded'])} 个，"
                  f"改为需核实 {len(res['review'])} 个，用时 {time.time() - t0:.0f} 秒")
        moved = [r for r in new if r.get("candidate_status") == "review"]
        new = [r for r in new if r.get("candidate_status") == "relevant"]
        review = [r for r in review if r.get("candidate_status") == "review"] + moved
        baseline = [r for r in baseline if r.get("candidate_status") in ("relevant", "review")]

    # 新发布的和新接入的一视同仁：都按与 Claire 的匹配度打分、排序（雇主层级只作很小的加分）
    from .details import fit_adjust, summary as details_summary
    for r in new + review + baseline:
        sc, why = fit_score(r)
        r["fit_score"] = max(0, min(100, sc + fit_adjust(r.get("details"))))
        r["fit_reasons"] = why
        r["details_note"] = details_summary(r.get("details"))
    deadlines = _upcoming_deadlines(seen, d)
    by_fit = lambda r: (-r.get("fit_score", 0), r.get("employer") or "")   # noqa: E731
    cand_rel = sorted([dict(r, found_as="新发布") for r in new] +
                      [dict(r, found_as="新接入") for r in baseline if r.get("candidate_status") == "relevant"],
                      key=by_fit)
    cand_rev = sorted([dict(r, found_as="新发布") for r in review] +
                      [dict(r, found_as="新接入") for r in baseline if r.get("candidate_status") != "relevant"],
                      key=by_fit)

    states = {k: 0 for k in STATE_LABELS}
    for r in status_rows:
        states[r["scan_state"]] = states.get(r["scan_state"], 0) + 1
    fails = [r for r in status_rows if r["status"] == "fail"]
    tiers = {e["id"]: e.get("tier", "") for e in emps_all}
    unreadable = sorted(({"id": r["id"], "name": r["name"], "tier": tiers.get(r["id"], ""),
                          "scan_state": r["scan_state"]} for r in status_rows
                         if r["scan_state"] in ("failed", "not_configured", "invalid")),
                        key=lambda r: (r["tier"], r["name"]))
    key_fn = lambda r: (r.get("tier") or "", r.get("employer") or "")   # noqa: E731
    latest = {"date": d, "scan_id": scan_id, "scanned_at": now_iso, "first_run": first_run,
              "employers_total": len(emps), "employers_ok": len(ok_ids), "employers_failed": len(fails),
              "employers_skipped": sum(r["status"] == "skip" for r in status_rows),
              "employers_complete": len(complete_ids), "scan_states": states,
              "new_relevant": sorted(new, key=key_fn),
              "new_review": sorted(review, key=key_fn),
              "baseline": sorted(baseline, key=lambda r: (r.get("candidate_status") != "relevant",) + key_fn(r)),
              "candidates": cand_rel, "candidates_review": cand_rev,
              "deadlines_7d": deadlines, "excluded_by_details": excluded, "details_checked": details_checked,
              "newly_broken": _newly_broken(status_rows, emps_all),
              "new_employers": new_emps, "channels": ch_status, "ats_detected_by_browser": suggested,
              "reopened_relevant": reopened,
              "removed_relevant": removed, "failed": fails, "unreadable": unreadable,
              "needs_attention": [r for r in status_rows if r["scan_state"] in ("partial", "invalid")]}

    # 先在内存里把所有输出准备好、校验通过，再统一写入
    validate_seen(seen)
    validate_latest(latest)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=STATUS_FIELDS)
    w.writeheader()
    w.writerows(sorted(status_rows, key=lambda r: (r["status"], r["id"])))
    report = render_report(latest)

    if suggested and not only:
        by_id = {x["id"]: x for x in suggested}
        for e in emps_all:
            if e["id"] in by_id and not e.get("ats"):
                e["ats"], e["ats_id"] = by_id[e["id"]]["ats"], by_id[e["id"]]["ats_id"]
        print("浏览器识别出招聘系统（下次扫描起改用）：" +
              "，".join(f"{x['name']}={x['ats']}" for x in suggested))
    if new_emps or (suggested and not only):
        save_employers(emps_all)
    save_json("seen.json", seen)
    save_json("latest.json", latest)
    save_json("employer_state.json", state)
    update_recent(latest, now_iso)
    _atomic_write(os.path.join(DATA, "status.csv"), buf.getvalue())
    for name in ("latest.md", f"{d}.md"):
        _atomic_write(os.path.join(REPORTS, name), report)
    print(f"完成（{scan_id}）：{len(ok_ids)}/{len(emps)} 家读取成功，其中完整 {len(complete_ids)} 家；"
          f"新相关职位 {len(new)} 个，待核实 {len(review)} 个，新接入雇主的现有职位 {len(baseline)} 个，"
          f"重新出现 {len(reopened)} 个，下架 {len(removed)} 个，失败 {len(fails)} 家，"
          f"新加入名单的雇主 {len(new_emps)} 家")
    print("扫描状态：" + "，".join(f"{STATE_LABELS[k]} {v}" for k, v in states.items() if v))
    for c in ch_status:
        print(f"渠道 {c['channel']}：{c['status']} {c['detail']}")
    return latest


def details_enabled():
    return os.environ.get("SCANNER_DETAILS", "1") != "0"


def _upcoming_deadlines(seen, today_s, days=7):
    """所有在招、可能相关的职位里，截止日期在今天到 7 天后之间的。"""
    from .details import summary as details_summary
    t = datetime.fromisoformat(today_s).date()
    out = []
    for r in seen.values():
        dl = (r.get("details") or {}).get("deadline") or ""
        if not dl or not r.get("active") or r.get("candidate_status") not in ("relevant", "review"):
            continue
        if classify(r)[0] == "irrelevant":
            continue
        try:
            left = (datetime.fromisoformat(dl).date() - t).days
        except ValueError:
            continue
        if 0 <= left <= days:
            out.append({**{k: r.get(k) for k in ("employer", "tier", "title", "location", "url", "candidate_status")},
                        "deadline": dl, "days_left": left, "details_note": details_summary(r.get("details"))})
    return sorted(out, key=lambda x: (x["deadline"], x["employer"] or ""))


def _newly_broken(status_rows, emps):
    """上次还能读到、这次突然读不到的 P0/P1 雇主（网站改版或接口变化，需要处理）。"""
    path = os.path.join(DATA, "status.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        old = {r["id"]: r for r in csv.DictReader(f)}
    tiers = {e["id"]: e.get("tier", "") for e in emps}
    out = []
    for r in status_rows:
        o = old.get(r["id"])
        if (o and o.get("scan_state") in ("complete", "partial") and tiers.get(r["id"]) in ("P0", "P1")
                and r["scan_state"] in ("failed", "invalid", "zero_jobs")):
            out.append({"id": r["id"], "name": r["name"], "tier": tiers.get(r["id"]), "was": o.get("scan_state"),
                        "now": r["scan_state"], "detail": r["detail"][:150]})
    return out


def _short_locations(loc, keep=3):
    """地点太多时只显示前几个（多伦多/渥太华等目标城市优先）。"""
    from .filters import _FIT_TARGET
    parts = [p.strip() for p in re.split(r";\s*", loc or "") if p.strip()]
    uniq = []
    for p in parts:
        key = re.sub(r",\s*(ON|Ontario)\b.*$", "", p, flags=re.I).strip().lower()
        if key not in [u[0] for u in uniq]:
            uniq.append((key, re.sub(r",\s*ON,\s*ON\b", ", ON", p)))
    items = [u[1] for u in uniq]
    items.sort(key=lambda x: 0 if _FIT_TARGET.search(x) else 1)
    if not items:
        return "—"
    return "、".join(items[:keep]) + (f" 等 {len(items)} 地" if len(items) > keep else "")


def _group_same_title(rows):
    """同一雇主、同一职位名在多个城市各发一条时，简报里合并成一行（保持原排序）。"""
    groups, order = {}, []
    for r in rows:
        k = (r.get("employer"), _norm_title(r.get("title")))
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(r)
    return [groups[k] for k in order]


def _group_locations(g):
    joined = "; ".join((r.get("location") or "").replace("|", "/") for r in g)
    return _short_locations(joined)


def _group_links(g):
    if len(g) == 1:
        return f"[打开]({g[0]['url']})"
    return " ".join(f"[{i + 1}]({r['url']})" for i, r in enumerate(g[:5])) + (f" 等 {len(g)} 个" if len(g) > 5 else "")


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
    dls = L.get("deadlines_7d", [])
    if dls:
        lines += [f"## ⏰ 7 天内截止（{len(dls)}）", "",
                  "所有在招、可能相关的职位里，职位页写明的截止日期在 7 天内的（不只是本次新出现的）。", "",
                  "| 截止 | 剩余 | 雇主 | 职位 | 地点 | 职位页信息 | 链接 |", "|---|---|---|---|---|---|---|"]
        for r in dls:
            lines.append(f"| {r['deadline']} | {r['days_left']} 天 | {r['employer']} | {r['title']} | "
                         f"{_short_locations(r.get('location'))} | {r.get('details_note') or '—'} | [打开]({r['url']}) |")
        lines.append("")
    broken = L.get("newly_broken", [])
    if broken:
        lines += [f"## ⚠️ 重点雇主突然读不到（{len(broken)}）", "",
                  "上次还能读取、这次读取失败的 P0/P1 雇主，可能是招聘网站改版，需要修复。", ""]
        for b in broken:
            lines.append(f"- {b['tier']} {b['name']}：{b['detail']}")
        lines.append("")
    cands = L.get("candidates", L["new_relevant"])
    n_new = sum(1 for r in cands if r.get("found_as", "新发布") == "新发布")
    lines += [f"## 本次可申请的职位（{len(cands)}）", "",
              f"其中新发布 {n_new} 个、新接入 {len(cands) - n_new} 个（雇主或渠道第一次被读到，职位已挂出一段时间，"
              "但 Claire 之前没见过）。两类一视同仁，**按与 Claire 的匹配度排序**：地点、是否入门级、"
              "职能是否对口（财务/审计/分析、艺术/文化）。雇主层级只在分数接近时起一点作用。"
              "这是按职位名和地点的初筛，申请前请打开链接确认经验要求。", ""]
    if cands:
        lines += ["| 匹配 | 雇主 | 职位 | 地点 | 来源 | 匹配原因 | 职位页信息 | 链接 |",
                  "|---|---|---|---|---|---|---|---|"]
        for g in _group_same_title(cands):
            r = g[0]
            lines.append(f"| {r.get('fit_score', '')} | {r['employer']} | {r['title']} | {_group_locations(g)} | "
                         f"{r.get('found_as', '新发布')} | {r.get('fit_reasons', '')} | "
                         f"{r.get('details_note') or '—'} | {_group_links(g)} |")
    else:
        lines.append("无")
    review = L.get("candidates_review", L.get("new_review", []))
    lines += ["", f"## 可能相关、需要核实（{len(review)}）", "",
              "实习、兼职、合同、地点只写了 Canada 等需要先确认的职位，同样按匹配度排序。", ""]
    if review:
        lines += ["| 匹配 | 雇主 | 职位 | 地点 | 来源 | 待核实原因 | 职位页信息 | 链接 |",
                  "|---|---|---|---|---|---|---|---|"]
        for g in _group_same_title(review):
            r = g[0]
            lines.append(f"| {r.get('fit_score', '')} | {r['employer']} | {r['title']} | {_group_locations(g)} | "
                         f"{r.get('found_as', '新发布')} | {r.get('filter_reason', '')} | "
                         f"{r.get('details_note') or '—'} | {_group_links(g)} |")
    else:
        lines.append("无")
    exc = L.get("excluded_by_details", [])
    if exc:
        lines += ["", f"## 打开职位页后排除（{len(exc)}）", "",
                  "职位名看起来合适，但职位页写明要求 3 年以上经验或仅限在校生。列在这里备查，不用申请。", ""]
        for r in exc[:60]:
            lines.append(f"- {r['employer']} · {r['title']}：{r.get('filter_reason', '')}（[打开]({r['url']})）")
        if len(exc) > 60:
            lines.append(f"- 另有 {len(exc) - 60} 个，见 data/latest.json 的 excluded_by_details")
    new_emps = L.get("new_employers", [])
    if new_emps:
        lines += ["", f"## 新加入名单的雇主（{len(new_emps)}）", "",
                  "这些雇主不在原来的名单里，是从外部招聘渠道发现的（发布了与 Claire 相关的职位），已自动加入 "
                  "`data/employers.csv`，以后每次都会扫描。", "",
                  "| 雇主 | 来源 | 发现的职位 | 招聘系统 |", "|---|---|---|---|"]
        for e in new_emps:
            lines.append(f"| {e['name']} | {e.get('source', '').replace('auto:', '')} | "
                         f"[{e.get('found_job') or '职位'}]({e.get('found_url') or ''}) | "
                         f"{e.get('ats') or ('官网' if e.get('careers_url') else '待识别')} |")
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
    chs = L.get("channels", [])
    if chs:
        lines += ["", "## 外部招聘渠道", ""]
        for c in chs:
            lines.append(f"- {c['channel']}：{ {'ok': '成功', 'fail': '失败', 'skip': '跳过'}.get(c['status'], c['status'])}，"
                         f"{c['detail']}")
    unread = [r for r in L.get("unreadable", []) if r["scan_state"] == "not_configured"]
    if unread:
        top = [r["name"] for r in unread if r["tier"] in ("P0", "P1")]
        lines += ["", f"## 还没有招聘入口的雇主（{len(unread)}）", "",
                  "每周一会自动按公司名寻找它们的官网招聘页。"
                  + (f"其中重点雇主：{'、'.join(top)}。" if top else "")]
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
    sc.add_argument("--no-channels", action="store_true", help="不读取 Job Bank 等外部渠道")
    sub.add_parser("reclassify")
    a = ap.parse_args()
    if a.cmd == "scan":
        scan(set(a.only.split(",")) if a.only else None, a.workers, channels=not a.no_channels)
    elif a.cmd == "reclassify":
        reclassify()


if __name__ == "__main__":
    main()
