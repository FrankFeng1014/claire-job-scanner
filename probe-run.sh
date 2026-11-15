mkdir -p probe-out
python - <<'PY' > probe-out/bdo2.txt 2>&1
import asyncio
from playwright.async_api import async_playwright
UA=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await b.new_context(user_agent=UA, locale="en-CA")
        page = await ctx.new_page()
        log=[]
        async def on_resp(r):
            if "/wday/" in r.url or "myworkdayjobs" in r.url and r.request.resource_type in ("xhr","fetch","document"):
                body=""
                try:
                    if "/jobs" in r.url and r.request.method=="POST": body=(await r.text())[:300]
                except Exception as e: body=str(e)
                log.append((r.request.method, r.status, r.url[:160], body))
        page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        for u in ["https://bdo.wd3.myworkdayjobs.com/en-US/BDO", "https://bdo.wd3.myworkdayjobs.com/BDO",
                  "https://bdo.wd3.myworkdayjobs.com/en-US/BDO/job/Toronto---Bay-St/Business-Analyst---BizApps_JR7066"]:
            try:
                resp = await page.goto(u, wait_until="domcontentloaded", timeout=40000)
                try: await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception: pass
                print("GOTO", u, "->", page.url, "status", resp.status if resp else None, "title", await page.title())
                txt = await page.inner_text("body")
                print("  body:", txt[:400].replace("\n"," | "))
            except Exception as e:
                print("GOTO FAIL", u, type(e).__name__, str(e)[:200])
        print("\n--- workday network ---")
        for l in log[:40]: print(l)
        await b.close()
asyncio.run(main())
PY
curl -s -o /dev/null -w "curl GET board: %{http_code} %{redirect_url}\n" -A "Mozilla/5.0" https://bdo.wd3.myworkdayjobs.com/en-US/BDO >> probe-out/bdo2.txt
curl -s -w "\ncurl POST cxs: %{http_code}\n" -A "Mozilla/5.0" -H "Content-Type: application/json" -d '{"appliedFacets":{},"limit":1,"offset":0,"searchText":""}' https://bdo.wd3.myworkdayjobs.com/wday/cxs/bdo/BDO/jobs | tail -c 400 >> probe-out/bdo2.txt
