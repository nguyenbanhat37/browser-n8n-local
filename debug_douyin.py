"""
Debug script: opens Douyin channel, waits 15s, then dumps page HTML and screenshots
to help identify login modal selectors.
"""
import asyncio
import os
from playwright.async_api import async_playwright

CHROME_PATH = os.environ.get("CHROME_PATH", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
URL = "https://www.douyin.com/user/MS4wLjABAAAAPXdNM2J_zAWoTD_4XuXFm99nmZOWuYgE60XTwTFjBg4hrvs8i-MibxyDJrfUVbE-"

async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            executable_path=CHROME_PATH,
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        print("Navigating...")
        await page.goto(URL, wait_until="commit", timeout=60_000)
        
        print("Waiting 15s for page to render (dismiss any modal manually if needed)...")
        await asyncio.sleep(15)
        
        # Screenshot
        await page.screenshot(path="debug_screenshot.png", full_page=False)
        print("Screenshot saved: debug_screenshot.png")

        # --- Find video card selectors ---
        print("\n=== LOOKING FOR VIDEO CARDS ===")
        
        # Try data-e2e attributes first
        data_e2e_els = await page.evaluate("""() => {
            const all = document.querySelectorAll('[data-e2e]');
            const unique = {};
            all.forEach(el => {
                const v = el.getAttribute('data-e2e');
                if (!unique[v]) unique[v] = 0;
                unique[v]++;
            });
            return Object.entries(unique).sort((a,b) => b[1]-a[1]).slice(0, 30);
        }""")
        print("Top data-e2e values found:")
        for k, v in data_e2e_els:
            print(f"  [{v}x] data-e2e=\"{k}\"")

        # Try to find the video grid container
        print("\n--- Looking for video grid / list containers ---")
        grid_selectors = [
            'ul[class*="video"]', 'ul[class*="list"]', 'ul[class*="grid"]',
            'div[class*="video-list"]', 'div[class*="videoList"]',
            'li[class*="video"]', 'li[class*="item"]',
        ]
        for sel in grid_selectors:
            els = await page.query_selector_all(sel)
            if els:
                print(f"  FOUND {len(els)}x: {sel}")
                for el in els[:2]:
                    cls = await el.get_attribute("class") or ""
                    print(f"    class='{cls[:80]}'")

        # Find <li> elements that look like video cards
        print("\n--- Inspecting <li> elements (likely video cards) ---")
        li_info = await page.evaluate("""() => {
            const lis = document.querySelectorAll('li');
            const results = [];
            for (const li of lis) {
                const cls = li.className || '';
                const e2e = li.getAttribute('data-e2e') || '';
                const hasLink = li.querySelector('a') !== null;
                const hasImg = li.querySelector('img') !== null;
                if (hasLink && hasImg && (cls || e2e)) {
                    results.push({cls: cls.substring(0,80), e2e, childCount: li.children.length});
                }
            }
            // Group by class
            const grouped = {};
            for (const r of results) {
                const key = r.cls + '|' + r.e2e;
                if (!grouped[key]) grouped[key] = {cls: r.cls, e2e: r.e2e, count: 0};
                grouped[key].count++;
            }
            return Object.values(grouped).sort((a,b) => b.count - a.count).slice(0, 10);
        }""")
        print("li elements with links+images (likely video cards):")
        for item in li_info:
            print(f"  [{item['count']}x] class='{item['cls']}' data-e2e='{item['e2e']}'")

        # Find <a> tags that link to /video/
        print("\n--- Links to /video/ (direct video URLs) ---")
        video_links = await page.evaluate("""() => {
            const links = document.querySelectorAll('a[href*="/video/"]');
            const seen = new Set();
            const results = [];
            for (const a of links) {
                const href = a.getAttribute('href');
                if (!seen.has(href)) {
                    seen.add(href);
                    const parent = a.parentElement;
                    const parentCls = parent ? (parent.className || '').substring(0,60) : '';
                    const parentTag = parent ? parent.tagName : '';
                    results.push({href: href.substring(0,80), parentTag, parentCls});
                }
            }
            return results.slice(0, 10);
        }""")
        print(f"Found {len(video_links)} unique /video/ links:")
        for lnk in video_links[:5]:
            print(f"  {lnk['href']}")
            print(f"    parent: <{lnk['parentTag']} class='{lnk['parentCls']}'>")

        # Dump login modal info
        print("\n=== LOGIN MODAL CHECK ===")
        login_el = await page.query_selector("#login-panel-new")
        print(f"#login-panel-new present: {login_el is not None}")
        login_el2 = await page.query_selector("#douyin_login_comp_flat_panel")
        print(f"#douyin_login_comp_flat_panel present: {login_el2 is not None}")
        
        print("\nBrowser staying open for 60s — inspect manually...")
        await asyncio.sleep(60)
        await browser.close()

asyncio.run(main())
