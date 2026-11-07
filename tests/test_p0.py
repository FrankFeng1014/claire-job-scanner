"""P0 可靠性修复的回归测试。全部离线：模拟接口返回，数据写在临时目录。"""
import csv
import json
import os

import pytest

from scanner import adapters, filters, main
from tests.test_scanner import FakeSession, Resp


# ---------- 公共工具 ----------
@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(adapters, "WORKDAY_DELAY", 0)
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """临时数据目录 + 可控的雇主名单和接口返回。"""
    monkeypatch.setattr(main, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(main, "REPORTS", str(tmp_path / "reports"))
    os.makedirs(tmp_path / "data")
    state = {"boards": {}, "day": "2026-11-07"}

    def write_emps(rows):
        with open(tmp_path / "data" / "employers.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "name", "tier", "sector", "ats", "ats_id", "careers_url"])
            w.writeheader()
            for r in rows:
                w.writerow({"tier": "P2", "sector": "", "careers_url": "", **r})

    def fake_adapter(s, board):
        v = state["boards"][board]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setitem(main.ADAPTERS, "greenhouse", fake_adapter)
    monkeypatch.setattr(main, "today", lambda: state["day"])
    state["write_emps"] = write_emps
    state["path"] = tmp_path
    return state


def job(i, title="Financial Analyst", loc="Toronto, ON"):
    return {"job_id": str(i), "title": title, "location": loc, "url": f"https://x/{i}", "posted": ""}


def seen(env):
    return json.loads((env["path"] / "data" / "seen.json").read_text(encoding="utf-8"))


# ---------- P0-1 同一天重复扫描 ----------
def test_same_day_second_run_detects_removal(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1), job(2)])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    L = main.scan()                       # 同一天第二次
    assert [r["job_id"] for r in L["removed_relevant"]] == ["2"]
    assert seen(env)["a::2"]["active"] is False
    assert seen(env)["a::1"]["active"] is True


def test_failed_scan_keeps_history(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    main.scan()
    env["boards"]["a"] = RuntimeError("boom")
    L = main.scan()
    assert L["removed_relevant"] == [] and seen(env)["a::1"]["active"] is True


def test_only_mode_leaves_other_employers_untouched(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"},
                       {"id": "b", "name": "B", "ats": "greenhouse", "ats_id": "b"}])
    env["boards"].update(a=adapters.AdapterResult([job(1)]), b=adapters.AdapterResult([job(2)]))
    main.scan()
    env["boards"]["b"] = adapters.AdapterResult([])
    env["day"] = "2026-11-08"
    main.scan(only={"a"})
    s = seen(env)
    assert s["b::2"]["active"] is True and s["b::2"]["last_seen"] == "2026-11-07"
    assert s["a::1"]["last_seen"] == "2026-11-08"


def test_legacy_seen_without_new_fields(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    legacy = {"a::1": {**job(1), "employer_id": "a", "employer": "A", "tier": "P2", "source": "greenhouse",
                       "relevant": True, "first_seen": "2026-11-01", "last_seen": "2026-11-01", "active": True},
              "a::9": {**job(9), "employer_id": "a", "employer": "A", "tier": "P2", "source": "greenhouse",
                       "relevant": True, "first_seen": "2026-11-01", "last_seen": "2026-11-01", "active": True}}
    (env["path"] / "data" / "seen.json").write_text(json.dumps(legacy), encoding="utf-8")
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    L = main.scan()
    s = seen(env)
    assert s["a::1"]["first_seen"] == "2026-11-01" and s["a::1"]["last_seen_run"] == L["scan_id"]
    assert s["a::9"]["active"] is False and [r["job_id"] for r in L["removed_relevant"]] == ["9"]


# ---------- P0-2 Workday ----------
def _wd(n, total=None, fail_at=None, dup=False, facets=None, scoped=None):
    posts = [{"title": f"Analyst {i}", "externalPath": f"/job/T/{i}", "locationsText": "Toronto",
              "postedOn": "", "bulletFields": [f"R{i}"]} for i in range(n)]
    calls = []

    def handler(body):
        calls.append(body)
        o = body["offset"]
        if fail_at is not None and o >= fail_at:
            raise RuntimeError("api down")
        if body.get("appliedFacets"):
            src = scoped
        else:
            src = posts
        page = src[o:o + 20]
        if dup and o == 20:
            page = src[0:20]              # 第二页返回重复职位
        d = {"jobPostings": page, "total": (total if total is not None else len(src)) if o == 0 else 0}
        if o == 0 and facets is not None and not body.get("appliedFacets"):
            d["facets"] = facets
        return Resp(d)
    return FakeSession({"myworkdayjobs.com": handler}), calls


def test_workday_under_limit_complete():
    s, _ = _wd(45)
    r = adapters.workday(s, "t/wd3/S")
    assert len(r) == 45 and r.complete


def test_workday_large_board_uses_location_facet_and_is_partial(monkeypatch):
    monkeypatch.setattr(adapters, "WORKDAY_FULL_MAX", 100)
    facets = [{"facetParameter": "locations", "values": [
        {"id": "L1", "descriptor": "Toronto, Ontario"}, {"id": "L2", "descriptor": "Dallas, TX"}]}]
    scoped = [{"title": f"Analyst T{i}", "externalPath": f"/job/T/t{i}", "locationsText": "Toronto",
               "postedOn": "", "bulletFields": [f"T{i}"]} for i in range(30)]
    s, calls = _wd(500, facets=facets, scoped=scoped)
    r = adapters.workday(s, "t/wd3/S")
    assert len(r) >= 30 and not r.complete and "地点筛选" in r.note
    assert any(c.get("appliedFacets") == {"locations": ["L1"]} for c in calls)


def test_workday_large_board_without_facets_marked_partial(monkeypatch):
    monkeypatch.setattr(adapters, "WORKDAY_FULL_MAX", 100)
    s, _ = _wd(500)
    r = adapters.workday(s, "t/wd3/S")
    assert not r.complete and "关键词" in r.note


def test_workday_midway_failure_is_partial():
    s, _ = _wd(80, fail_at=40)
    r = adapters.workday(s, "t/wd3/S")
    assert len(r) == 40 and not r.complete and "读取失败" in r.note


def test_workday_duplicates_deduped():
    s, _ = _wd(60, dup=True)
    r = adapters.workday(s, "t/wd3/S")
    assert len({j["job_id"] for j in r}) == len(r)
    assert not r.complete          # 少了 20 个，不能当作完整


def test_workday_total_mismatch_empty_page():
    s, _ = _wd(30, total=100)      # 说有 100 个，实际只有 30
    r = adapters.workday(s, "t/wd3/S")
    assert len(r) == 30 and not r.complete and "为空" in r.note


def test_workday_bad_structure_raises():
    s = FakeSession({"myworkdayjobs.com": lambda body: Resp({"oops": 1})})
    with pytest.raises(adapters.AdapterDataError):
        adapters.workday(s, "t/wd3/S")


def test_partial_scan_never_confirms_removal(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1), job(2)])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([job(1)], complete=False, note="只读了部分")
    L = main.scan()
    assert L["removed_relevant"] == [] and seen(env)["a::2"]["active"] is True
    st = list(csv.DictReader(open(env["path"] / "data" / "status.csv", encoding="utf-8")))
    assert st[0]["scan_state"] == "partial" and st[0]["complete"] == "no"


def test_zero_jobs_does_not_remove(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([])
    L = main.scan()
    assert L["removed_relevant"] == [] and L["scan_states"]["zero_jobs"] == 1


def test_page_source_never_removes(env, monkeypatch):
    env["write_emps"]([{"id": "m", "name": "Museum", "careers_url": "https://museum.org/jobs"}])
    pages = iter(['<a href="/j/1">Finance Coordinator</a><a href="/j/2">Gallery Assistant</a>',
                  '<a href="/j/1">Finance Coordinator</a>'])
    monkeypatch.setattr(main.requests, "Session",
                        lambda: FakeSession({"museum.org": lambda url: Resp(text=next(pages))}))
    main.scan()
    L = main.scan()
    assert L["removed_relevant"] == [] and L["scan_states"]["unverified"] == 1


def test_smartrecruiters_cap_marked_partial(monkeypatch):
    monkeypatch.setattr(adapters, "SMARTRECRUITERS_MAX", 200)

    def h(url):
        return Resp({"totalFound": 1000, "content": [{"id": str(i), "name": "Analyst",
                                                      "location": {"city": "Toronto"}} for i in range(100)]})
    s = FakeSession({"api.smartrecruiters.com": h})
    r = adapters.smartrecruiters(s, "c")
    assert not r.complete
    assert "只读了前 200" in r.note


# ---------- P0-3 过滤 ----------
@pytest.mark.parametrize("title,expected", [
    ("Business Transformation Associate", "relevant"),
    ("Junior Consultant", "relevant"),
    ("Financial Analyst", "relevant"),
    ("Graduate Development Program", "relevant"),
    ("Underwriting Analyst", "relevant"),
    ("Research Assistant", "relevant"),
    ("Gallery Operations Coordinator", "relevant"),
    ("Arts Administration Assistant", "relevant"),
    ("Senior Software Engineer", "irrelevant"),
    ("Director of Finance", "irrelevant"),
    ("Finance Co-op Student (Winter 2027)", "irrelevant"),
    ("Summer Analyst Internship 2027", "review"),
    ("Part-time Gallery Assistant", "review"),
    ("Consultant", "review"),
    ("Store Associate", "irrelevant"),
    ("Personal Banker", "irrelevant"),
    ("Associate Director, Strategy", "irrelevant"),
    ("Welder", "irrelevant"),
    ("Consultant, Java Back End Development", "irrelevant"),
    ("Lawyer, Tax Controversy", "irrelevant"),
    ("Search Jobs by Category", "irrelevant"),
    ("(CAN) Food & Consumables Team Associate (Part-Time)", "irrelevant"),
    ("AVP, GTO Strategy & Transformation", "irrelevant"),
    ("Consultant(e) Débutant(e) en Transaction Services - Lille - Septembre 2027 - F/H", "irrelevant"),
    ("IT Operations Analyst II", "irrelevant"),
    ("Commercial Mortgage Underwriter", "relevant"),
    ("Associate, Enterprise Strategy and Corporate Development", "relevant"),
    ("Merchandise Control Assistant", "relevant"),
    ("Consultant, Total Rewards", "review"),
    ("Campus Careers", "irrelevant"),
    ("Student Services Assistant", "relevant"),
    ("Student, Canadian Tax Accountant (Summer 2027, 12 Months)", "irrelevant"),
    ("Leader, Enterprise Compliance", "irrelevant"),
])
def test_classify_titles(title, expected):
    st, why = filters.classify({"title": title, "location": "Toronto, ON"})
    assert st == expected, (title, why)
    assert why


def test_classify_location_rules():
    assert filters.classify({"title": "Financial Analyst", "location": "Calgary, AB"})[0] == "irrelevant"
    assert filters.classify({"title": "Financial Analyst", "location": ""})[0] == "relevant"
    assert filters.classify({"title": "Financial Analyst", "location": "",
                             "url": "https://x.com/job/vancouver-bc/1"})[0] == "irrelevant"


def test_irrelevant_jobs_are_kept_with_reason(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1, "Senior Software Engineer"), job(2, "Summer Analyst Intern")])
    L = main.scan()
    s = seen(env)
    assert s["a::1"]["candidate_status"] == "irrelevant" and s["a::1"]["filter_reason"]
    assert s["a::1"]["relevant"] is False
    assert [r["job_id"] for r in L["new_review"]] == ["2"]
    assert "需要核实" in (env["path"] / "reports" / "latest.md").read_text(encoding="utf-8")


def test_reclassify_upgrades_after_rule_change(env, monkeypatch):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1, "Glassblower")])
    main.scan()
    monkeypatch.setattr(filters, "_rel", __import__("re").compile("glassblower", 2))
    monkeypatch.setattr(main, "classify", filters.classify)
    changed = main.reclassify()
    assert changed and seen(env)["a::1"]["candidate_status"] == "relevant"


# ---------- P0-4 已有职位信息更新 ----------
def test_existing_job_fields_update_and_history(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    main.scan()
    env["day"] = "2026-11-08"
    j = job(1, "Financial Analyst I", "Toronto, ON")
    j["url"] = ""                         # 临时空值不能覆盖旧链接
    env["boards"]["a"] = adapters.AdapterResult([j])
    main.scan()
    r = seen(env)["a::1"]
    assert r["title"] == "Financial Analyst I" and r["url"] == "https://x/1"
    assert r["first_seen"] == "2026-11-07" and r["last_modified_seen"] == "2026-11-08"
    assert r["change_history"][-1]["changes"]["title"] == ["Financial Analyst", "Financial Analyst I"]


def test_history_is_capped(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    for i in range(main.HISTORY_MAX + 4):
        env["boards"]["a"] = adapters.AdapterResult([job(1, f"Financial Analyst v{i}")])
        main.scan()
    assert len(seen(env)["a::1"]["change_history"]) == main.HISTORY_MAX


def test_reappearing_job_reactivated(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1), job(2)])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([job(1), job(2)])
    L = main.scan()
    assert seen(env)["a::2"]["active"] is True and "closed_seen" not in seen(env)["a::2"]
    assert [r["job_id"] for r in L["reopened_relevant"]] == ["2"]


# ---------- P0-5 数据保护 ----------
def test_empty_employer_list_does_not_crash(env):
    env["write_emps"]([])
    L = main.scan()
    assert L["employers_total"] == 0
    assert (env["path"] / "data" / "status.csv").read_text(encoding="utf-8").startswith("id,")


def test_unexpected_error_keeps_previous_outputs(env, monkeypatch):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    main.scan()
    before = {n: (env["path"] / "data" / n).read_text(encoding="utf-8") for n in ("seen.json", "latest.json")}
    monkeypatch.setattr(main, "render_report", lambda L: (_ for _ in ()).throw(RuntimeError("crash")))
    env["boards"]["a"] = adapters.AdapterResult([job(1), job(2)])
    with pytest.raises(RuntimeError):
        main.scan()
    for n, txt in before.items():
        assert (env["path"] / "data" / n).read_text(encoding="utf-8") == txt
    assert not [p for p in os.listdir(env["path"] / "data") if p.startswith(".tmp-")]


def test_corrupt_seen_is_rejected_not_overwritten(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    (env["path"] / "data" / "seen.json").write_text('["not a dict"]', encoding="utf-8")
    env["boards"]["a"] = adapters.AdapterResult([job(1)])
    with pytest.raises(ValueError):
        main.scan()
    assert (env["path"] / "data" / "seen.json").read_text(encoding="utf-8") == '["not a dict"]'


# ---------- P0-6 状态透明度与输出兼容 ----------
def test_status_and_latest_backward_compatible(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"},
                       {"id": "b", "name": "B", "ats": "greenhouse", "ats_id": "b"},
                       {"id": "c", "name": "C"}])
    env["boards"].update(a=adapters.AdapterResult([job(1)]), b=adapters.AdapterDataError("bad"))
    L = main.scan()
    for k in ("date", "first_run", "employers_total", "employers_ok", "employers_failed",
              "employers_skipped", "new_relevant", "removed_relevant", "failed"):
        assert k in L
    rows = {r["id"]: r for r in csv.DictReader(open(env["path"] / "data" / "status.csv", encoding="utf-8"))}
    assert rows["a"]["status"] == "ok" and rows["a"]["scan_state"] == "complete"
    assert rows["b"]["status"] == "fail" and rows["b"]["scan_state"] == "invalid"
    assert rows["c"]["status"] == "skip" and rows["c"]["scan_state"] == "not_configured"
    md = (env["path"] / "reports" / "latest.md").read_text(encoding="utf-8")
    assert "读取质量：完整成功 1" in md and "## 新出现的相关职位（1）" in md


# ---------- P0 后续：撞号、境外地点、门店岗 ----------
def test_workday_job_id_is_unique_path():
    posts = [{"title": t, "externalPath": f"/job/Toronto/{i}", "locationsText": "Toronto",
              "postedOn": "", "bulletFields": ["Posted Today"]}          # bulletFields 不唯一
             for i, t in enumerate(["Financial Analyst", "Software Engineer"])]
    s = FakeSession({"myworkdayjobs.com": lambda body: Resp({"jobPostings": posts if body["offset"] == 0 else [],
                                                              "total": 2 if body["offset"] == 0 else 0})})
    r = adapters.workday(s, "t/wd3/S")
    assert {j["job_id"] for j in r} == {"/job/Toronto/0", "/job/Toronto/1"}


def test_colliding_ids_do_not_overwrite_new_job(env):
    env["write_emps"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([job(1, "Financial Analyst"), job(1, "Software Engineer")])
    L = main.scan()
    assert [r["title"] for r in L["new_relevant"]] == ["Financial Analyst"]


def test_workday_legacy_keys_migrated_without_false_removal(env, monkeypatch):
    env["write_emps"]([{"id": "w", "name": "W", "ats": "workday", "ats_id": "t/wd3/S"}])
    base = "https://t.wd3.myworkdayjobs.com/en-US/S"
    legacy = {"w::R1": {"job_id": "R1", "title": "Financial Analyst", "location": "Toronto",
                        "url": base + "/job/Toronto/FA_R1", "posted": "", "employer_id": "w", "employer": "W",
                        "tier": "P1", "source": "workday", "relevant": True, "first_seen": "2026-11-01",
                        "last_seen": "2026-11-01", "active": True}}
    (env["path"] / "data" / "seen.json").write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setitem(main.ADAPTERS, "workday", lambda s, a: adapters.AdapterResult(
        [{"job_id": "/job/Toronto/FA_R1", "title": "Financial Analyst", "location": "Toronto",
          "url": base + "/job/Toronto/FA_R1", "posted": ""}]))
    L = main.scan()
    s = seen(env)
    assert "w::/job/Toronto/FA_R1" in s and "w::R1" not in s
    assert s["w::/job/Toronto/FA_R1"]["first_seen"] == "2026-11-01"
    assert L["new_relevant"] == [] and L["removed_relevant"] == []


@pytest.mark.parametrize("loc,ok", [
    ("Concord, CA", False), ("Concord, NH", False), ("(USA) ME SCARBOROUGH 08186 SAM'S CLUB", False),
    ("South Burlington, Vermont", False), ("Burlington, ON", True), ("(CAN) ON SCARBOROUGH 01117 WM SUPERCENTER", True),
    ("Concord, ON", True), ("Toronto, ON, ca", True), ("Canada, Full Time Remote", True), ("FULTON MARKET", False),
])
def test_us_namesakes_excluded(loc, ok):
    assert filters.location_ok(loc) is ok


def test_foreign_workday_url_excluded():
    j = {"title": "Analyst - West Coast", "location": "",
         "url": "https://thomsonreuters.wd5.myworkdayjobs.com/en-US/X/job/United-States-of-America-McLean/Analyst_1"}
    assert filters.classify(j)[0] == "irrelevant"


@pytest.mark.parametrize("title", ["(CAN) General Merchandise Associate", "(Can) Stock Unloader Associate",
                                   "(CAN) Meat Associate", "Fashion Associate", "Produce Associate"])
def test_walmart_store_roles_excluded(title):
    assert filters.classify({"title": title, "location": "Toronto, ON"})[0] == "irrelevant"


def test_associate_producer_not_mistaken_for_produce():
    assert filters.classify({"title": "Associate Producer", "location": "Toronto, ON"})[0] == "relevant"


@pytest.mark.parametrize("seg,ok", [("Hong-Kong-Hong-Kong", False), ("China-Beijing-Beijing", False),
                                    ("Japan-Tokyo", False), ("Korea-Seoul", False),
                                    ("Canada-Toronto-Ontario", True), ("300---Toronto", True),
                                    ("Toronto-Ontario", True), ("Canada-Remote", True)])
def test_workday_url_location_segment(seg, ok):
    j = {"title": "Business Analyst", "location": "",
         "url": f"https://x.wd5.myworkdayjobs.com/en-US/S/job/{seg}/Business-Analyst_1"}
    assert (filters.classify(j)[0] == "relevant") is ok
