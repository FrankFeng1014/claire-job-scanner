"""新增招聘系统、外部渠道、新接入雇主基线的离线测试。

接口返回的样例都是按 2026-11-09 在 GitHub Actions 上实际抓到的格式精简而来的。
"""
import csv
import json
import os

import pytest

from scanner import adapters, channels, discover, main


class Resp:
    def __init__(self, data=None, text="", status=200, url=""):
        self._d, self.text, self.status_code, self.url = data, text, status, url
        self.ok = status < 400
        if data is not None and not text:
            self.text = json.dumps(data)

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """routes: {网址片段: 返回值或函数(url, params, json)}，按插入顺序匹配第一个。"""

    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def _go(self, url, params=None, json=None):
        self.calls.append((url, params, json))
        for k, v in self.routes.items():
            if k in url:
                return v(url, params or {}, json) if callable(v) else v
        raise AssertionError("unexpected url " + url)

    def get(self, url, params=None, **kw):
        return self._go(url, params)

    def post(self, url, json=None, params=None, **kw):
        return self._go(url, params, json)

    def request(self, method, url, **kw):
        return self._go(url, kw.get("params"), kw.get("json"))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(adapters.time, "sleep", lambda *_: None)
    monkeypatch.setattr(channels.time, "sleep", lambda *_: None)


# ---------- SuccessFactors ----------
def _sf_row(i, title, loc):
    return (f'<tr class="data-row"><td class="colTitle"><span class="jobTitle hidden-phone">'
            f'<a href="/job/Toronto-{i}/14490{i:05d}/" class="jobTitle-link">{title}</a></span>'
            f'<span class="jobLocation">\n {loc}\n</span></td>'
            f'<td><span class="jobDate">Nov 9, 2026\n</span></td></tr>')


def test_successfactors_classic_paginates():
    def handler(url, params, body):
        start = params["startrow"]
        rows = "".join(_sf_row(i, f"Analyst {i}", "Toronto, ON, CA") for i in range(start, min(start + 25, 30)))
        return Resp(text=f'<span>Results <b>{start + 1} – {start + 25}</b> of <b>30</b></span>' + rows)
    r = adapters.successfactors(FakeSession({"/search/": handler}), "https://jobs.example.com")
    assert len(r) == 30 and r.complete
    assert r[0]["title"] == "Analyst 0" and r[0]["location"] == "Toronto, ON, CA"
    assert r[0]["url"].startswith("https://jobs.example.com/job/")


def test_successfactors_unify_api():
    body = {"totalJobs": 2, "jobSearchResult": [
        {"response": {"id": "1", "unifiedStandardTitle": "Audit Associate", "unifiedUrlTitle": "Audit-Associate",
                      "mfield1": ["Toronto, ON", "Ottawa, ON"], "unifiedStandardStart": "11/1/26"}},
        {"response": {"id": "2", "unifiedStandardTitle": "Tax Analyst", "urlTitle": "Tax-Analyst",
                      "mfield1": ["Montreal, QC"]}}]}
    s = FakeSession({"/search/": Resp(text="<html>react</html>"), "/services/recruiting/v1/jobs": Resp(body)})
    r = adapters.successfactors(s, "https://careers.example.ca")
    assert [j["title"] for j in r] == ["Audit Associate", "Tax Analyst"] and r.complete
    assert r[0]["location"] == "Toronto, ON; Ottawa, ON"
    assert r[0]["url"] == "https://careers.example.ca/job/Audit-Associate/1-en_US/"


def test_successfactors_falls_back_to_rss_feed():
    rss = ('<rss><channel><item><title>Financial Analyst (Toronto, ON, CA)</title>'
           '<link>https://jobs.x.com/job/Toronto-Financial-Analyst/1234567/</link></item></channel></rss>')
    s = FakeSession({"/search/": Resp(text="<html></html>"),
                     "/services/recruiting": Resp(status=404), "/sitemap.xml": Resp(text=rss)})
    r = adapters.successfactors(s, "https://jobs.x.com")
    assert r[0]["title"] == "Financial Analyst" and r[0]["location"] == "Toronto, ON, CA"
    assert r[0]["job_id"] == "1234567" and not r.complete


# ---------- Phenom ----------
def test_phenom_reads_widgets_api():
    page = '<script>phApp.ddo = {"refNum":"RBCAA0088","locale":"en_CA","country":"ca"}</script>'
    jobs = [{"jobId": f"R-{i}", "title": "Business Analyst", "location": "Toronto, Ontario, Canada",
             "postedDate": "2026-11-01T00:00:00.000+0000"} for i in range(60)]

    def widgets(url, params, body):
        assert body["refNum"] == "RBCAA0088" and body["lang"] == "en_ca"
        return Resp({"refineSearch": {"totalHits": 60, "data": {"jobs": jobs[body["from"]:body["from"] + 50]}}})
    r = adapters.phenom(FakeSession({"/search-results": Resp(text=page), "/widgets": widgets}),
                        "https://jobs.rbc.com/ca/en")
    assert len(r) == 60 and r.complete
    assert r[0]["url"] == "https://jobs.rbc.com/ca/en/job/R-0/Business-Analyst" and r[0]["posted"] == "2026-11-01"


# ---------- Oracle ----------
def test_oracle_and_country_only_location_goes_to_review():
    data = {"items": [{"TotalJobsCount": 1, "requisitionList": [
        {"Id": "117141", "Title": "Financial Analyst", "PostedDate": "2026-11-09", "PrimaryLocation": "Canada",
         "secondaryLocations": []}]}]}
    r = adapters.oracle(FakeSession({"oraclecloud.com": Resp(data)}), "ehtc.fa.ca2.oraclecloud.com/CX_1")
    assert r[0]["url"].endswith("/sites/CX_1/job/117141") and r.complete
    st, why = main.classify(r[0])
    assert st == "review" and "Canada" in why


# ---------- Avature / Radancy / Amazon / JazzHR / BambooHR ----------
def test_avature_table_rows():
    html = ('<div>1-2 of 2 result(s)</div><table><tr><th scope="row">'
            '<a class="link" href="https://x.ca/en_CA/careers/JobDetail/Analyst-Finance/36631" title="t">'
            '\n Analyst, Finance \n</a></th><td>\n Toronto, Ontario \n</td></tr>'
            '<tr><th><a href="https://x.ca/en_CA/careers/JobDetail/Clerk/36632">Clerk</a></th><td>Laval</td></tr></table>')
    r = adapters.avature(FakeSession({"SearchJobs": Resp(text=html)}), "https://x.ca/en_CA/careers/searchjobs")
    assert [(j["job_id"], j["title"], j["location"]) for j in r] == [
        ("36631", "Analyst, Finance", "Toronto, Ontario"), ("36632", "Clerk", "Laval")]
    assert r.complete


def test_radancy_results():
    res = ('<section data-total-pages="1"><ul><li><a href="/en/job/toronto/analyst/45831/1001" data-job-id="1001">'
           '<h2>Analyst</h2><span class="job-location">Toronto, Ontario</span>'
           '<span class="job-date-posted">10/30/2026</span></a></li></ul></section>')
    r = adapters.radancy(FakeSession({"search-jobs/results": Resp({"results": res})}), "https://careers.x.com/en")
    assert r[0]["url"] == "https://careers.x.com/en/job/toronto/analyst/45831/1001"
    assert r[0]["location"] == "Toronto, Ontario" and r.complete


def test_amazon_canada():
    data = {"hits": 1, "jobs": [{"id_icims": "10600206", "title": "Financial Analyst", "normalized_location":
                                 "Toronto, Ontario, CAN", "posted_date": "November  9, 2026",
                                 "job_path": "/en/jobs/10600206/financial-analyst"}]}
    r = adapters.amazon(FakeSession({"amazon.jobs": Resp(data)}), "CAN")
    assert r[0]["posted"] == "2026-11-09" and r[0]["url"].startswith("https://www.amazon.jobs/en/jobs/")


def test_jazzhr_and_bamboohr():
    html = ('<li class="list-group-item"><h4><a href="https://acme.applytojob.com/apply/4PJR9C57Ps/Content-Analyst">'
            '\n Content Analyst \n</a></h4><ul><li><i class="fa fa-map-marker"></i>Toronto, ON</li></ul></li>')
    r = adapters.jazzhr(FakeSession({"applytojob.com": Resp(text=html)}), "acme")
    assert r[0]["job_id"] == "4PJR9C57Ps" and r[0]["location"] == "Toronto, ON"
    b = {"meta": {}, "result": [{"id": "7", "jobOpeningName": "Gallery Coordinator",
                                 "location": {"city": "Toronto", "state": "Ontario"}, "isRemote": None}]}
    r = adapters.bamboohr(FakeSession({"bamboohr.com": Resp(b)}), "acme")
    assert r[0]["location"] == "Toronto, Ontario" and r[0]["url"] == "https://acme.bamboohr.com/careers/7"


def test_sitemap_only_fetches_unknown_job_pages():
    sm = ("<urlset><url><loc>https://c.ca/jobs/101?lang=en-us</loc></url><url><loc>https://c.ca/jobs/101?lang=fr-ca"
          "</loc></url><url><loc>https://c.ca/jobs/102?lang=en-us</loc></url><url><loc>https://c.ca/about</loc></url>"
          "</urlset>")
    ld = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Audit Associate","datePosted":'
          '"2026-11-02","jobLocation":{"address":{"addressLocality":"Toronto","addressRegion":"ON"}}}</script>')
    s = FakeSession({"sitemap.xml": Resp(text=sm), "/jobs/102": Resp(text=ld)})
    r = adapters.sitemap(s, "https://c.ca/sitemap.xml", known={"101"})
    by = {j["job_id"]: j for j in r}
    assert set(by) == {"101", "102"} and r.complete
    assert by["102"]["title"] == "Audit Associate" and by["102"]["location"] == "Toronto, ON"
    assert not any("/jobs/101" in c[0] for c in s.calls)       # 已知职位不再打开详情页


def test_page_adapter_reads_jsonld_and_unescapes():
    html = ('<script type="application/ld+json">[{"@type":"JobPosting","title":"Studio Assistant &amp; Coordinator",'
            '"url":"https://x/1","jobLocation":[{"address":{"addressLocality":"Toronto"}}]}]</script>')
    r = adapters.page(FakeSession({"x.org": Resp(text=html)}), "https://x.org/careers")
    assert r[0]["title"] == "Studio Assistant & Coordinator" and r[0]["location"] == "Toronto"


# ---------- 识别招聘系统 ----------
@pytest.mark.parametrize("html,url,expected", [
    ('<a href="https://ehtc.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/requisitions">',
     "https://m.com", ("oracle", "ehtc.fa.ca2.oraclecloud.com/CX_1")),
    ('<link href="/platform/css/j2w/x.css"><script src="https://rmkcdn.successfactors.com/a.js">',
     "https://jobs.bce.ca/bell/go/x/1/", ("successfactors", "https://jobs.bce.ca")),
    ('<script>var phApp = {}; phApp.ddo = {"refNum":"X1"}</script><script src="https://cdn.phenompeople.com/a.js">',
     "https://jobs.bmo.com/ca/en/recent-grad", ("phenom", "https://jobs.bmo.com/ca/en")),
    ('<a href="/en_CA/careers/SearchJobs">Search</a>', "https://emplois.bnc.ca/en_CA/careers",
     ("avature", "https://emplois.bnc.ca/en_CA/careers")),
    ('<a href="https://hoopp.wd10.myworkdayjobs.com/HOOPP">a</a><a href="https://hoopp.wd10.myworkdayjobs.com/en-US/HOOPPStudent">b</a>',
     "https://hoopp.com", ("workday", "hoopp/wd10/HOOPP;hoopp/wd10/HOOPPStudent")),
    ('<a href="https://ratehubinc.applytojob.com/apply">Jobs</a>', "https://r.ca", ("jazzhr", "ratehubinc")),
])
def test_detect_platforms(html, url, expected):
    assert discover.detect(html, url) == expected


def test_guess_site_rejects_other_company_domain():
    # 猜的域名跳转到了别家网站 → 不采用
    s = FakeSession({"www.rsm.com": Resp(text="<title>RSM</title>", url="https://sensitron.com/"),
                     "": Resp(status=404)})
    assert discover.guess_site(s, "RSM Canada") is None
    home = ('<title>Acme Widgets | Home</title><a href="https://www.linkedin.com/company/acme/jobs">Careers</a>'
            '<a href="/about/careers/">Careers</a>')
    s = FakeSession({"www.acmewidgets.com": Resp(text=home, url="https://www.acmewidgets.com/"), "": Resp(status=404)})
    assert discover.guess_site(s, "Acme Widgets Inc.") == "https://www.acmewidgets.com/about/careers/"


# ---------- 外部渠道解析 ----------
def test_workinculture_parse():
    items = [{"id": 1, "date": "2026-11-09T14:54:56", "link": "https://workinculture.ca/jobs-and-more/a/",
              "title": {"rendered": "Gallery Coordinator &#038; Registrar"},
              "meta": {"_company_name": "Bau-Xi Gallery", "_company_website": "https://bau-xi.com",
                       "_application": "https://bauxi.bamboohr.com/careers/3", "_remote_position": 0},
              "class_list": ["job_listing", "si_place-q16", "si_place-q1904", "si_place-q172", "job-type-full-time"]},
             {"id": 2, "date": "2026-11-09", "link": "l2", "title": {"rendered": "Arts Administrator"},
              "meta": {"_company_name": "X"}, "class_list": ["si_place-q16", "si_place-q1974", "job-type-part-time"]}]
    j = channels.workinculture(FakeSession({"workinculture.ca": Resp(items)}))
    assert j[0]["title"] == "Gallery Coordinator & Registrar" and j[0]["location"] == "Toronto, ON"
    assert j[0]["company"] == "Bau-Xi Gallery" and j[0]["apply_url"].startswith("https://bauxi.bamboohr")
    assert j[1]["location"] == "Outside Ontario" and j[1]["title"].endswith("(part time)")


def test_jobbank_parse():
    art = ('<article id="article-50693301" class="x"><a href="/jobsearch/jobposting/50693301;jsessionid=AB?source=x">'
           '<h3 class="title"><span class="noctitle"> financial analyst\n</span></h3><ul>'
           '<li class="date">November 09, 2026\n</li><li class="business">Dexterra</li>'
           '<li class="location"><span class="fas fa-map-marker-alt" aria-hidden="true"></span> '
           '<span class="wb-inv">Location</span>\n  Mississauga (ON)\n</li></ul></a></article>')
    j = channels.jobbank(FakeSession({"jobbank.gc.ca": Resp(text=art)}), queries=["analyst"], pages=1)
    assert j == [{"job_id": "50693301", "title": "financial analyst", "location": "Mississauga, ON",
                  "url": "https://www.jobbank.gc.ca/jobsearch/jobposting/50693301", "posted": "November 09, 2026",
                  "company": "Dexterra", "company_site": "", "apply_url": ""}]


@pytest.mark.parametrize("company,expect", [
    ("Kroll Consulting Canada Co", "kroll-canada"), ("KROLL CANADA", "kroll-canada"),
    ("Art Gallery of Ontario", "ago"), ("Bau-Xi Gallery Ltd.", "bau-xi"), ("Krollwood Farms", None),
])
def test_match_employer(company, expect):
    emps = [{"id": "kroll-canada", "name": "Kroll Canada"}, {"id": "ago", "name": "Art Gallery of Ontario"},
            {"id": "bau-xi", "name": "Bau-Xi Gallery"}]
    idx = [(channels.norm_name(e["name"]), e) for e in emps]
    m = channels.match_employer(company, idx)
    assert (m["id"] if m else None) == expect


# ---------- 扫描流程：新接入基线、渠道加雇主、换读取方式 ----------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(main, "REPORTS", str(tmp_path / "reports"))
    os.makedirs(tmp_path / "data")
    boards = {}

    def fake(s, arg):
        return boards[arg]
    monkeypatch.setitem(main.ADAPTERS, "greenhouse", fake)
    monkeypatch.setitem(main.ADAPTERS, "lever", fake)
    days = iter(["2026-11-0%d" % i for i in range(1, 10)])
    monkeypatch.setattr(main, "today", lambda: next(days))

    def write(rows):
        with open(tmp_path / "data" / "employers.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["id", "name", "tier", "sector", "ats", "ats_id", "careers_url"])
            w.writeheader()
            for r in rows:
                w.writerow({"tier": "P1", "sector": "", "careers_url": "", **r})
    return {"boards": boards, "write": write, "path": tmp_path}


def J(i, title="Financial Analyst", loc="Toronto, ON"):
    return {"job_id": str(i), "title": title, "location": loc, "url": f"https://x/{i}", "posted": ""}


def test_newly_connected_employer_goes_to_baseline_not_new(env):
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1)])
    main.scan()                                              # 第一次运行（全局）：照旧算新职位
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"},
                  {"id": "b", "name": "B", "ats": "greenhouse", "ats_id": "b"}])
    env["boards"]["b"] = adapters.AdapterResult([J(5), J(6)])
    L = main.scan()
    assert L["new_relevant"] == [] and {r["job_id"] for r in L["baseline"]} == {"5", "6"}
    env["boards"]["b"] = adapters.AdapterResult([J(5), J(6), J(7)])
    L = main.scan()
    assert [r["job_id"] for r in L["new_relevant"]] == ["7"] and L["baseline"] == []
    recent = json.loads((env["path"] / "data" / "recent.json").read_text())
    assert {(i["kind"], i["title"]) for i in recent["items"]} >= {("baseline", "Financial Analyst"),
                                                                    ("new_relevant", "Financial Analyst")}


def test_changing_source_retires_old_records_quietly(env):
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1)])
    main.scan()
    main.scan()
    env["write"]([{"id": "a", "name": "A", "ats": "lever", "ats_id": "a2"}])
    env["boards"]["a2"] = adapters.AdapterResult([J("x1")])
    L = main.scan()
    seen = json.loads((env["path"] / "data" / "seen.json").read_text())
    assert L["removed_relevant"] == [] and seen["a::1"]["active"] is False
    assert seen["a::1"]["closed_reason"].startswith("读取方式更换")
    assert [r["job_id"] for r in L["baseline"]] == ["x1"]


def test_multiple_boards_merged_and_partial_if_one_fails(env):
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a1;a2"}])
    env["boards"]["a1"] = adapters.AdapterResult([J(1)])
    env["boards"]["a2"] = adapters.AdapterResult([J(2)])
    L = main.scan()
    assert L["scan_states"]["complete"] == 1 and len(L["new_relevant"]) == 2
    env["boards"]["a2"] = RuntimeError("down")

    def fake(s, arg):
        v = env["boards"][arg]
        if isinstance(v, Exception):
            raise v
        return v
    main.ADAPTERS["greenhouse"] = fake
    L = main.scan()
    assert L["scan_states"]["partial"] == 1 and L["removed_relevant"] == []


def test_channel_adds_unknown_employer_and_matches_known(env, monkeypatch):
    env["write"]([{"id": "kroll-canada", "name": "Kroll Canada", "ats": "greenhouse", "ats_id": "k"}])
    env["boards"]["k"] = adapters.AdapterResult([J(1, "Valuation Analyst")])
    feed = {"jobs": []}

    def chan(s):
        return [dict(j) for j in feed["jobs"]]
    monkeypatch.setattr(channels, "CHANNELS", {"jobbank": ("Job Bank", chan)})

    def cj(i, title, company, loc="Toronto, ON"):
        return {**J(i, title, loc), "company": company, "company_site": "", "apply_url": ""}
    feed["jobs"] = [cj(10, "financial analyst", "Dawson Partners Inc.")]
    L = main.scan()                                     # 渠道第一次：只建立基线，但雇主照样加入
    assert [e["name"] for e in L["new_employers"]] == ["Dawson Partners Inc."]
    assert L["new_relevant"] == [r for r in L["new_relevant"] if r["employer_id"] == "kroll-canada"]
    emps = main.load_employers()
    added = [e for e in emps if e["name"] == "Dawson Partners Inc."][0]
    assert added["source"] == "auto:jobbank" and added["tier"] == "P2"
    feed["jobs"] = [cj(10, "financial analyst", "Dawson Partners Inc."),
                    cj(11, "Research Analyst", "Brand New Co"),
                    cj(12, "receptionist & office assistant", "Tiny Shop"),       # 不是核心岗位：不加雇主
                    cj(13, "Valuation Analyst", "KROLL CANADA"),                  # 与招聘系统里的同名职位去重
                    cj(14, "Junior Analyst", "Randstad Canada"),                   # 招聘中介：不加
                    cj(15, "Data Analyst", "Far Away Ltd", "Calgary, AB")]          # 地点不符：忽略
    L = main.scan()
    names = [e["name"] for e in L["new_employers"]]
    assert names == ["Brand New Co"]
    assert {r["title"] for r in L["new_relevant"]} >= {"Research Analyst"}
    assert all(r["title"] != "Valuation Analyst" or r["source"] != "channel:jobbank" for r in L["new_relevant"])
    # 渠道记录不参与“下架”判断
    feed["jobs"] = []
    L = main.scan()
    assert L["removed_relevant"] == []
    report = (env["path"] / "reports" / "latest.md").read_text()
    assert "外部招聘渠道" in report


def test_sitemap_stays_baseline_until_fully_read(env, monkeypatch):
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1)])
    main.scan()
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"},
                  {"id": "k", "name": "K", "ats": "sitemap", "ats_id": "https://k/sitemap.xml"}])
    batches = iter([adapters.AdapterResult([J(1)], False, "还有职位没读"),
                    adapters.AdapterResult([J(1), J(2)], True),
                    adapters.AdapterResult([J(1), J(2), J(3)], True)])
    monkeypatch.setitem(main.ADAPTERS, "sitemap", lambda s, arg, known=None: next(batches))
    main.ADAPTERS["sitemap"].wants_known = True
    L = main.scan()
    assert [r["job_id"] for r in L["baseline"]] == ["1"]
    L = main.scan()                       # 上次没读完：这次补读到的仍算现有职位
    assert [r["job_id"] for r in L["baseline"]] == ["2"] and L["new_relevant"] == []
    L = main.scan()                       # 已经读完过一次：之后出现的才是新职位
    assert [r["job_id"] for r in L["new_relevant"]] == ["3"] and L["baseline"] == []


def test_page_uses_browser_rendered_content_and_frames(monkeypatch):
    rendered = {"https://x.org/careers": {
        "final": "https://x.org/careers", "status": 200,
        "html": '<div><a href="/jobs/7">Junior Financial Analyst</a></div>',
        "frames": [("https://board.x.org/list", '<a href="/p/9">Gallery Programs Coordinator</a>')], "requests": []}}
    monkeypatch.setattr(adapters, "RENDERED", rendered)
    r = adapters.page(None, "https://x.org/careers")      # 不应再联网
    assert {(j["title"], j["url"]) for j in r} == {
        ("Junior Financial Analyst", "https://x.org/jobs/7"),
        ("Gallery Programs Coordinator", "https://board.x.org/p/9")}


def test_browser_detects_ats_hidden_in_rendered_page():
    from scanner import browser
    emps = [{"id": "a", "name": "A", "ats": "", "ats_id": "", "careers_url": "https://a.org/careers"}]
    rendered = {"https://a.org/careers": {"final": "https://a.org/careers", "status": 200, "html": "<div></div>",
                "frames": [("https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers", "")], "requests": []}}
    assert main._browser_suggestions(emps, rendered) == [
        {"id": "a", "name": "A", "ats": "workday", "ats_id": "acme/wd3/Acme_Careers"}]
    assert "myworkdayjobs" in browser.combined_html(rendered["https://a.org/careers"])


def test_unify_location_dicts_are_cleaned():
    body = {"totalJobs": 1, "jobSearchResult": [{"response": {
        "id": "9", "unifiedStandardTitle": "Staff Accountant", "urlTitle": "x",
        "jobLocationShort": ["Toronto, ON, ON, CAN, M5C 3G7<br/>"],
        "mfield1": [{"value": "Toronto, ON, ON, CAN, M5C 3G7<br/>", "key": "43.65,-79.37"}, "Ottawa, ON"]}}]}
    s = FakeSession({"/search/": Resp(text="<html></html>"), "/services/recruiting/v1/jobs": Resp(body)})
    r = adapters.successfactors(s, "https://c.ca")
    assert r[0]["location"] == "Toronto, ON, ON, CAN, M5C 3G7; Ottawa, ON"


@pytest.mark.parametrize("title,status", [
    ("Associate, Research Analytics Permanent Full-Time Mexico City APPLY", "irrelevant"),
    ("Plant Controller, Fremont, OH, US", "irrelevant"),
    ("Sales Assistant-Zhejiang", "irrelevant"),
    ("Junior Financial Analyst", "relevant"),
    ("Financial Analyst, Toronto", "relevant"),
    ("Analyst, US Equities", "relevant"),
])
def test_foreign_place_in_title_when_location_missing(title, status):
    assert main.classify({"title": title, "location": ""})[0] == status


def test_embedded_phenom_assistant_is_not_the_careers_site():
    html = ('<script>phApp.ddo = {"refNum":"MDLZ"}; "baseUrl":"https://virtualhiringassistant.mdlz.com/global/en/"'
            '</script><script src="https://cdn.phenompeople.com/x.js"></script>')
    assert discover.detect(html, "https://www.mdlz.com/careers/jobs/") == (None, None)


def test_new_and_baseline_ranked_together_by_fit_not_tier(env):
    env["write"]([{"id": "a", "name": "BigBank", "ats": "greenhouse", "ats_id": "a", "tier": "P0"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1, "Business Analyst")])
    main.scan()
    env["write"]([{"id": "a", "name": "BigBank", "ats": "greenhouse", "ats_id": "a", "tier": "P0"},
                  {"id": "b", "name": "SmallCo", "ats": "greenhouse", "ats_id": "b", "tier": "P2"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1, "Business Analyst"), J(2, "Personal Banking Associate")])
    env["boards"]["b"] = adapters.AdapterResult([J(5, "Junior Financial Analyst")])     # 新接入
    L = main.scan()
    order = [(r["employer"], r["found_as"]) for r in L["candidates"]]
    assert order == [("SmallCo", "新接入"), ("BigBank", "新发布")]
    assert L["candidates"][0]["fit_score"] > L["candidates"][1]["fit_score"]
    md = (env["path"] / "reports" / "latest.md").read_text()
    assert "## 本次可申请的职位（2）" in md and md.index("SmallCo") < md.index("BigBank")


# ---------- 第二轮筛选：职位页 ----------
from scanner import details  # noqa: E402


def test_details_extract_and_api_urls():
    d = details.extract("Requirements: 3+ years of experience in audit. Excel is an asset. "
                        "Salary range: $58,000 - $65,000 per year. Applications close November 21, 2026.")
    assert (d["years_min"], d["deadline"], d["salary"]) == (3, "2026-11-21", "$58,000 - $65,000 per year")
    assert details.extract("5 years of experience is considered an asset.")["years_min"] is None
    assert details.extract("Minimum of two (2) years of relevant experience")["years_min"] == 2
    assert details.extract("Open to new grads and recent graduates. 0-1 years of experience")["new_grad_text"]
    assert details.extract("Must be currently enrolled in a post-secondary program")["flags"] == ["仅限在校生/需返校"]
    assert details._workday_api("https://td.wd3.myworkdayjobs.com/en-US/TD_Bank_Careers/job/Toronto/Analyst_R1") == \
        "https://td.wd3.myworkdayjobs.com/wday/cxs/td/TD_Bank_Careers/job/Toronto/Analyst_R1"
    assert "Id=%22117141%22,siteNumber=CX_1" in details._oracle_api(
        "https://ehtc.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/117141")


def test_details_second_pass_in_scan(env, monkeypatch):
    monkeypatch.setenv("SCANNER_DETAILS", "1")
    pages = {
        "https://x/1": "Junior Financial Analyst. Requirements: 4 years of experience in FP&A. " * 5,
        "https://x/2": ("Business Analyst. Requirements: 2 years of experience in analysis. "
                        "Apply by 2026-11-0{}.".format(5)) * 5,
        "https://x/3": "Research Analyst. Recent graduates welcome. Apply by 2026-11-04. " * 10,
    }
    monkeypatch.setattr(details, "fetch", lambda s, url: (pages[url], {}))
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([J(0, "Data Entry Clerk")])
    main.scan()                                                      # 2026-11-01
    env["boards"]["a"] = adapters.AdapterResult([J(0, "Data Entry Clerk"), J(1, "Junior Financial Analyst"),
                                                 J(2, "Business Analyst"), J(3, "Research Analyst")])
    L = main.scan()                                                  # 2026-11-02
    assert [r["title"] for r in L["excluded_by_details"]] == ["Junior Financial Analyst"]
    assert [r["title"] for r in L["candidates"]] == ["Research Analyst"]
    assert L["candidates"][0]["details_note"].startswith("写明应届可申；截止 2026-11-04")
    assert [r["title"] for r in L["candidates_review"]] == ["Business Analyst"]
    assert {r["title"]: r["days_left"] for r in L["deadlines_7d"]} == {"Research Analyst": 2, "Business Analyst": 3}
    md = (env["path"] / "reports" / "latest.md").read_text()
    assert "## ⏰ 7 天内截止（2）" in md and "## 打开职位页后排除（1）" in md
    seen = json.loads((env["path"] / "data" / "seen.json").read_text())
    assert seen["a::1"]["details"]["years_min"] == 4 and seen["a::1"]["candidate_status"] == "irrelevant"


def test_newly_broken_priority_employer(env):
    env["write"]([{"id": "a", "name": "BigBank", "ats": "greenhouse", "ats_id": "a", "tier": "P0"}])
    env["boards"]["a"] = adapters.AdapterResult([J(1)])
    main.scan()

    def boom(s, arg):
        raise RuntimeError("site changed")
    main.ADAPTERS["greenhouse"] = boom
    L = main.scan()
    assert [b["name"] for b in L["newly_broken"]] == ["BigBank"]
    assert "重点雇主突然读不到" in (env["path"] / "reports" / "latest.md").read_text()


def test_details_exclusion_survives_next_run(env, monkeypatch):
    monkeypatch.setenv("SCANNER_DETAILS", "1")
    monkeypatch.setattr(details, "fetch", lambda s, url: ("Requirements: 4 years of experience in FP&A. " * 10, {}))
    env["write"]([{"id": "a", "name": "A", "ats": "greenhouse", "ats_id": "a"}])
    env["boards"]["a"] = adapters.AdapterResult([J(0, "Data Entry Clerk")])
    main.scan()
    env["boards"]["a"] = adapters.AdapterResult([J(0, "Data Entry Clerk"), J(1, "Junior Financial Analyst")])
    main.scan()
    L = main.scan()
    seen = json.loads((env["path"] / "data" / "seen.json").read_text())
    assert seen["a::1"]["candidate_status"] == "irrelevant" and L["candidates"] == [] and L["reopened_relevant"] == []


def test_page_job_without_location_goes_to_review():
    from scanner.filters import classify
    st, why = classify({"title": "Financial Analyst", "location": "", "source": "page"})
    assert st == "review" and "地点" in why
    st, _ = classify({"title": "Financial Analyst", "location": "", "source": "greenhouse"})
    assert st == "relevant"


def test_program_title_hides_years():
    from scanner.details import summary
    d = {"ok": True, "years_min": 3, "deadline": "2026-11-15", "salary": "", "flags": [], "new_grad_text": False}
    assert "经验" not in summary(d, "Graduate Leadership Development Program 2027")
    assert "经验≥3年" in summary(d, "Financial Analyst")
