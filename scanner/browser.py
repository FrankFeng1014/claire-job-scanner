"""用“无界面浏览器”（Playwright + Chromium）打开招聘页。

很多招聘页的职位是网页打开后由脚本加载的，直接下载网页源码只能看到空壳。这里像真人一样
打开网页、等脚本加载完，再把最终的网页内容（包括内嵌的 iframe）交给程序分析。

- 只在 GitHub Actions 上用（工作流里会安装 Chromium）；没装 Playwright 时自动跳过，程序照常运行。
- 设置环境变量 SCANNER_BROWSER=0 可以关闭。
"""
from __future__ import annotations

import asyncio
import os

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
CONCURRENCY = 6
GOTO_TIMEOUT = 30000        # 毫秒
IDLE_TIMEOUT = 12000
TOTAL_BUDGET = 900          # 秒：整批最多花 15 分钟，超时的页面放弃


def available() -> bool:
    if os.environ.get("SCANNER_BROWSER", "1") == "0":
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def combined_html(r: dict) -> str:
    """主网页 + 各个 iframe 的内容 + 网页加载过程中请求过的网址（招聘系统常藏在这里）。"""
    parts = [r.get("html", "")]
    for url, html in r.get("frames", []):
        parts.append(f"\n<!-- frame {url} -->\n<a href=\"{url}\"></a>\n{html}")
    if r.get("requests"):
        parts.append("\n<!-- requests -->\n" + "\n".join(f'<link href="{u}">' for u in r["requests"]))
    return "\n".join(parts)


async def _render_all(urls, concurrency):
    from playwright.async_api import async_playwright
    out = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(user_agent=BROWSER_UA, locale="en-CA",
                                        viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(concurrency)

        async def one(u):
            async with sem:
                page = await ctx.new_page()
                reqs = []
                page.on("request", lambda req: reqs.append(req.url) if req.resource_type in
                        ("document", "xhr", "fetch", "script") and len(reqs) < 400 else None)
                try:
                    resp = await page.goto(u, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT)
                    except Exception:  # noqa: BLE001
                        pass
                    try:      # 滚动一下，触发“滚到底才加载”的列表
                        await page.mouse.wheel(0, 5000)
                        await page.wait_for_timeout(1500)
                    except Exception:  # noqa: BLE001
                        pass
                    frames = []
                    for f in page.frames[1:]:
                        try:
                            frames.append((f.url, await f.content()))
                        except Exception:  # noqa: BLE001
                            frames.append((f.url, ""))
                    out[u] = {"final": page.url, "status": resp.status if resp else 0,
                              "html": await page.content(), "frames": frames, "requests": reqs}
                except Exception as e:  # noqa: BLE001
                    out[u] = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
                finally:
                    try:
                        await page.close()
                    except Exception:  # noqa: BLE001
                        pass

        try:
            await asyncio.wait_for(asyncio.gather(*(one(u) for u in urls)), timeout=TOTAL_BUDGET)
        except asyncio.TimeoutError:
            pass
        await browser.close()
    return out


def render_all(urls, concurrency=CONCURRENCY) -> dict:
    """返回 {网址: {"final", "status", "html", "frames", "requests"} 或 {"error"}}。不可用时返回 {}。"""
    urls = list(dict.fromkeys(u for u in urls if u and u.startswith("http")))
    if not urls or not available():
        return {}
    try:
        return asyncio.run(_render_all(urls, concurrency))
    except Exception as e:  # noqa: BLE001  浏览器本身启动失败：整批跳过，退回普通读取
        print(f"浏览器不可用，跳过：{type(e).__name__}: {str(e)[:200]}")
        return {}
