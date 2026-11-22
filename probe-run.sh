mkdir -p probe-out
python - <<'PY' > probe-out/bmo2.txt 2>&1
import requests
from scanner import details
s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
for url in ["https://jobs.bmo.com/global/en/job/R260031276/Scorecard-Reporting-Analyst",
            "https://jobs.bmo.com/ca/en/job/R260031276/Scorecard-Reporting-Analyst",
            "https://careers.omers.com/ca/en/job/R0015090/Associate-People-Experience-Operations"]:
    try:
        txt, info = details.fetch(s, url); d = details.extract(txt, info)
        print(url.split("/job/")[1][:50], "| txt", len(txt), "|", d)
    except Exception as e:
        print("FAIL", url, type(e).__name__, str(e)[:200])
PY
