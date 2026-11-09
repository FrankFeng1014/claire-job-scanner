"""离线测试：用模拟的接口返回数据检查解析、筛选和去重逻辑（不联网）。"""
import json

import pytest

from scanner import adapters, filters, main


class Resp:
    def __init__(self, data=None, text=""):
        self._d, self.text, self.ok = data, text, True

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes, self.headers = routes, {}

    def _find(self, url):
        for k, v in self.routes.items():
            if k in url:
                return v(url) if callable(v) else v
        raise AssertionError("unexpected url " + url)

    def get(self, url, **kw):
        return self._find(url)

    def post(self, url, json=None, **kw):
        for k, v in self.routes.items():
            if k in url:
                return v(json) if callable(v) else v
        raise AssertionError("unexpected url " + url)


def test_greenhouse():
    s = FakeSession({"boards-api.greenhouse.io": Resp({"jobs": [
        {"id": 1, "title": "Financial Analyst", "location": {"name": "Toronto, ON"},
         "absolute_url": "https://x/1", "updated_at": "2026-11-01T10:00:00Z"}]})})
    j = adapters.greenhouse(s, "acme")[0]
    assert j["title"] == "Financial Analyst" and j["location"] == "Toronto, ON" and j["posted"] == "2026-11-01"


def test_lever():
    s = FakeSession({"api.lever.co": Resp([{"id": "a", "text": "Operations Coordinator",
                                            "categories": {"location": "Toronto"}, "hostedUrl": "u",
                                            "createdAt": 1762000000000}])})
    j = adapters.lever(s, "acme")[0]
    assert j["job_id"] == "a" and j["posted"].startswith("2025")


def test_ashby_smartrecruiters_workable():
    s = FakeSession({
        "api.ashbyhq.com": Resp({"jobs": [{"id": "z", "title": "Analyst", "location": "Toronto",
                                           "jobUrl": "u", "publishedAt": "2026-10-30T00:00:00Z"}]}),
        "api.smartrecruiters.com": Resp({"totalFound": 1, "content": [
            {"id": "9", "name": "Junior Accountant", "location": {"city": "Mississauga", "country": "ca"},
             "releasedDate": "2026-10-29T00:00:00Z"}]}),
        "apply.workable.com": Resp({"jobs": [{"shortcode": "AB1", "title": "Program Coordinator",
                                              "city": "Ottawa", "country": "Canada", "url": "u",
                                              "published_on": "2026-10-28"}]}),
    })
    assert adapters.ashby(s, "o")[0]["posted"] == "2026-10-30"
    assert adapters.smartrecruiters(s, "c")[0]["location"].startswith("Mississauga")
    assert adapters.workable(s, "w")[0]["job_id"] == "AB1"


def test_workday_small_board_paginates():
    posts = [{"title": f"Analyst {i}", "externalPath": f"/job/T/{i}", "locationsText": "Toronto",
              "postedOn": "Posted Today", "bulletFields": [f"R{i}"]} for i in range(30)]

    def handler(body):
        o = body["offset"]
        return Resp({"total": 30, "jobPostings": posts[o:o + 20]})
    s = FakeSession({"myworkdayjobs.com": handler})
    jobs = adapters.workday(s, "tmx/wd3/TMX_Careers")
    assert len(jobs) == 30 and jobs[0]["url"].startswith("https://tmx.wd3.myworkdayjobs.com/en-US/TMX_Careers/job/")


def test_page_extracts_joblike_links():
    html = """<a href="/careers">Careers</a><a href="/jobs/officer-procurement">Officer, Procurement</a>
              <a href="/about">About us</a><a href="https://x/pdf">Development Coordinator (PDF)</a>"""
    s = FakeSession({"museum.org": Resp(text=html)})
    titles = {j["title"] for j in adapters.page(s, "https://museum.org/jobs")}
    assert titles == {"Officer, Procurement", "Development Coordinator (PDF)"}


@pytest.mark.parametrize("title,loc,ok", [
    ("Financial Analyst", "Toronto, ON", True),
    ("Senior Financial Analyst", "Toronto", False),
    ("Software Engineer", "Toronto", False),
    ("Program Coordinator", "Vancouver, BC", False),
    ("Junior Accountant", "Remote - Canada", True),
    ("Summer Student - Finance", "Mississauga", False),
    ("2027 New Grad - Finance Rotational Program", "Ottawa", True),
])
def test_filters(title, loc, ok):
    assert filters.relevant({"title": title, "location": loc}) is ok


def test_scan_dedupe_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA", str(tmp_path))
    monkeypatch.setattr(main, "REPORTS", str(tmp_path / "r"))
    (tmp_path / "employers.csv").write_text(
        "id,name,tier,sector,ats,ats_id,careers_url\nacme,Acme,P2,x,greenhouse,acme,\n", encoding="utf-8")
    batches = [
        [{"job_id": "1", "title": "Financial Analyst", "location": "Toronto", "url": "u1", "posted": ""},
         {"job_id": "2", "title": "Senior Manager", "location": "Toronto", "url": "u2", "posted": ""}],
        [{"job_id": "1", "title": "Financial Analyst", "location": "Toronto", "url": "u1", "posted": ""},
         {"job_id": "3", "title": "Operations Coordinator", "location": "Mississauga", "url": "u3", "posted": ""}],
        [{"job_id": "3", "title": "Operations Coordinator", "location": "Mississauga", "url": "u3", "posted": ""}],
    ]
    days = iter(["2026-11-01", "2026-11-02", "2026-11-03"])
    monkeypatch.setitem(main.ADAPTERS, "greenhouse", lambda s, a: batches.pop(0))
    for _ in range(3):
        d = next(days)
        monkeypatch.setattr(main, "today", lambda d=d: d)
        main.scan()
        latest = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        if d == "2026-11-01":
            assert latest["first_run"] and [r["job_id"] for r in latest["new_relevant"]] == ["1"]
        if d == "2026-11-02":
            assert [r["job_id"] for r in latest["new_relevant"]] == ["3"] and not latest["removed_relevant"]
        if d == "2026-11-03":
            assert not latest["new_relevant"] and [r["job_id"] for r in latest["removed_relevant"]] == ["1"]
    assert (tmp_path / "r" / "latest.md").exists()


def test_discover_finds_ats_in_page():
    from scanner import discover
    html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmeco"></iframe>'
    s = FakeSession({"acme.com": Resp(text=html)})
    assert discover.from_page(s, "https://acme.com/careers")[:2] == ("greenhouse", "acmeco")
    html2 = '<a href="https://acme.wd3.myworkdayjobs.com/en-US/Acme_Careers">Jobs</a>'
    s2 = FakeSession({"acme.com": Resp(text=html2)})
    assert discover.from_page(s2, "https://acme.com/careers")[:2] == ("workday", "acme/wd3/Acme_Careers")


def test_workday_total_only_on_first_page():
    posts = [{"title": f"Analyst {i}", "externalPath": f"/job/T/{i}", "locationsText": "Toronto",
              "postedOn": "", "bulletFields": [f"R{i}"]} for i in range(55)]

    def handler(body):
        o = body["offset"]
        return Resp({"total": 55 if o == 0 else 0, "jobPostings": posts[o:o + 20]})
    s = FakeSession({"myworkdayjobs.com": handler})
    assert len(adapters.workday(s, "acme/wd3/Site")) == 55


def test_page_ignores_nav_and_news():
    html = """<a href="/a">Students & Graduates overview</a><a href="/b">Market operations indicators</a>
              <a href="/c">Join our Talent Community</a><a href="/d">Finance Coordinator</a>"""
    s = FakeSession({"x.org": Resp(text=html)})
    assert [j["title"] for j in adapters.page(s, "https://x.org/jobs")] == ["Finance Coordinator"]


def test_excludes_tech_and_licensed():
    for t in ["Data Center Specialist", "Client Associate (IR licensed)", "Executive Assistant to SVP"]:
        assert not filters.title_ok(t)


def test_location_word_boundary():
    assert not filters.location_ok("Hamilton, ON")
    assert filters.location_ok("Milton, ON") and filters.location_ok("Toronto, Ontario")


def test_jsessionid_stripped_and_out_of_province_dropped():
    html = '<a href="/jobsearch/jobposting/123;jsessionid=ABC.x">Junior Financial Analyst</a>'
    s = FakeSession({"jobbank": Resp(text=html)})
    j = adapters.page(s, "https://www.jobbank.gc.ca/search")[0]
    assert "jsessionid" not in j["url"]
    assert not filters.relevant({"title": "Accountant", "location": "",
                                 "url": "https://www.roberthalf.com/ca/en/job/calgary-ab/accountant/1"})
    assert filters.relevant({"title": "Accountant", "location": "",
                             "url": "https://www.roberthalf.com/ca/en/job/toronto-on/accountant/1"})
    assert not filters.title_ok("Identity Access Management Analyst")
