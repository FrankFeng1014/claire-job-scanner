mkdir -p probe-out
python - <<'PY' > probe-out/bdo.txt 2>&1
import requests, time
from scanner import adapters
s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0"
t = time.time()
try:
    r = adapters.workday(s, "bdo/wd3/BDO")
    print("OK", len(r), "complete=", r.complete, "note=", r.note, "secs=", round(time.time()-t))
    for j in list(r)[:8]: print(" ", j.get("title"), "|", j.get("location"))
    print([j.get("title") for j in r if "BizApps" in (j.get("title") or "")])
except Exception as e:
    print("FAIL", type(e).__name__, str(e)[:400])
PY
