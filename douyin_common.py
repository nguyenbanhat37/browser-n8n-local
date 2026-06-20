import asyncio
import logging
import os
import zoneinfo
import time
from datetime import datetime
from typing import List, Optional, Union
from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel

logger = logging.getLogger("douyin-scraper")

# ---------------------------------------------------------------------------
# Persistent browser state — reused across API calls to maintain session/cookies
# ---------------------------------------------------------------------------

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None
_browser_lock = asyncio.Lock()
_last_activity_time: float = 0.0
_close_timer_task: Optional[asyncio.Task] = None

def mark_browser_activity():
    global _last_activity_time, _close_timer_task
    _last_activity_time = time.time()
    if _close_timer_task and not _close_timer_task.done():
        _close_timer_task.cancel()
        _close_timer_task = None

async def _close_browser_after_delay(delay: float):
    try:
        await asyncio.sleep(delay)
        async with _browser_lock:
            if time.time() - _last_activity_time >= delay:
                global _playwright, _browser, _context
                if _browser:
                    try:
                        await _browser.close()
                    except Exception:
                        pass
                if _playwright:
                    try:
                        await _playwright.stop()
                    except Exception:
                        pass
                _playwright = None
                _browser = None
                _context = None
                logger.info("Persistent browser context closed automatically due to 10s idle timeout")
    except asyncio.CancelledError:
        pass

def schedule_browser_close_if_idle(delay: float = 10.0):
    global _close_timer_task
    if _close_timer_task and not _close_timer_task.done():
        _close_timer_task.cancel()
    _close_timer_task = asyncio.create_task(_close_browser_after_delay(delay))

async def get_browser_context() -> BrowserContext:
    """
    Return a shared browser context, launching it if not already running.
    Reusing the same context means Douyin sees a consistent session across calls.
    """
    global _playwright, _browser, _context
    mark_browser_activity()
    async with _browser_lock:
        if _context is not None:
            try:
                await _context.pages
                return _context
            except Exception:
                _context = None
                _browser = None
                _playwright = None

        logger.info("Launching persistent browser context...")
        _playwright = await async_playwright().start()
        headful = os.environ.get("BROWSER_USE_HEADFUL", "false").lower() == "true"
        launch_kwargs: dict = {
            "headless": not headful,
            "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        }
        chrome_path = os.environ.get("CHROME_PATH") or CHROME_PATH
        if chrome_path and os.path.exists(chrome_path):
            launch_kwargs["executable_path"] = chrome_path
            logger.info("Using custom Chrome: %s", chrome_path)
        else:
            logger.info("Using Playwright bundled Chromium")
            
        try:
            _browser = await _playwright.chromium.launch(**launch_kwargs)
        except Exception as e:
            if not launch_kwargs.get("headless"):
                logger.warning("Failed to launch headful browser (%s). Retrying in headless mode...", e)
                launch_kwargs["headless"] = True
                _browser = await _playwright.chromium.launch(**launch_kwargs)
            else:
                raise
        _context = await _browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        logger.info("Persistent browser context ready")
        return _context

async def close_browser_context():
    """Close the persistent browser. Call on server shutdown if needed."""
    global _playwright, _browser, _context, _close_timer_task
    if _close_timer_task and not _close_timer_task.done():
        _close_timer_task.cancel()
        _close_timer_task = None
    async with _browser_lock:
        if _browser:
            try:
                await _browser.close()
            except Exception:
                pass
        if _playwright:
            try:
                await _playwright.stop()
            except Exception:
                pass
        _playwright = None
        _browser = None
        _context = None
        logger.info("Persistent browser context closed")

async def take_screenshot_bytes() -> Optional[bytes]:
    """Capture a screenshot of the active page in the persistent browser context."""
    global _context
    if _context:
        try:
            pages = _context.pages
            if pages:
                return await pages[-1].screenshot(type="png")
        except Exception as e:
            logger.warning("Failed to take screenshot: %s", e)
    return None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHROME_PATH: Optional[str] = os.environ.get("CHROME_PATH")
TZ_SHANGHAI = zoneinfo.ZoneInfo("Asia/Shanghai")

MAX_SCROLL_ITERATIONS = 50
SCROLL_WAIT_SECONDS = 1.0
PAGE_LOAD_TIMEOUT_MS = 60_000
PAGE_STABILIZE_SECONDS = 2

DOUYIN_BASE = "https://www.douyin.com"

# CDN domains that host Douyin video streams
STREAM_CDN_DOMAINS = [
    "zjcdn.com", "douyinvod.com", "bytecdn.cn",
    "pstatp.com", "douyinstatic.com", "amemv.com",
]

# Selector for video cards on a Douyin channel page
VIDEO_CARD_SELECTOR = 'li:has(a[href*="/video/"])'
VIDEO_CARD_FALLBACK_SELECTORS = [
    '[data-e2e="user-post-item"]',
    'ul li:has(a[href*="/video/"])',
]

# Import login modal methods lazily or dynamically to keep modularity clean
from login_modal_handler import dismiss_login_modal

async def _launch_browser(playwright):
    """Launch Chromium, respecting BROWSER_USE_HEADFUL (headless by default), using CHROME_PATH if set and exists."""
    headful = os.environ.get("BROWSER_USE_HEADFUL", "false").lower() == "true"
    launch_kwargs: dict = {
        "headless": not headful,
        "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
    }
    chrome_path = os.environ.get("CHROME_PATH") or CHROME_PATH
    if chrome_path and os.path.exists(chrome_path):
        launch_kwargs["executable_path"] = chrome_path
        logger.info("Using custom Chrome executable: %s", chrome_path)
    else:
        logger.info("Using Playwright bundled Chromium")
    try:
        return await playwright.chromium.launch(**launch_kwargs)
    except Exception as e:
        if not launch_kwargs.get("headless"):
            logger.warning("Failed to launch headful browser (%s). Retrying in headless mode...", e)
            launch_kwargs["headless"] = True
            return await playwright.chromium.launch(**launch_kwargs)
        else:
            raise

async def _navigate_to_channel(page, url: str) -> None:
    """Navigate to the Douyin channel URL."""
    try:
        await page.goto(url, wait_until="commit", timeout=PAGE_LOAD_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise TimeoutError("Page load timed out") from exc

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass

    await dismiss_login_modal(page)

    has_videos = False
    for attempt in range(5):
        try:
            cards = await page.query_selector_all(VIDEO_CARD_SELECTOR)
            if not cards:
                for sel in VIDEO_CARD_FALLBACK_SELECTORS:
                    cards = await page.query_selector_all(sel)
                    if cards:
                        break
            
            if cards and len(cards) > 0:
                logger.info("Video cards detected on attempt %d/20", attempt + 1)
                has_videos = True
                break
        except Exception as e:
            logger.warning("Query selector failed: %s", e)

        logger.info("No video cards found. Checking login modal and waiting 12s (attempt %d/5)...", attempt + 1)
        await dismiss_login_modal(page)
        await asyncio.sleep(12.0)

    if not has_videos:
        logger.warning("No video list found after 5 attempts. Stopping.")
        return

    try:
        await page.stop()
    except Exception:
        pass

    await asyncio.sleep(PAGE_STABILIZE_SECONDS)

async def _extract_video_items(page) -> List[dict]:
    """Extract video card data from the current page DOM."""
    cards = await page.query_selector_all(VIDEO_CARD_SELECTOR)
    if not cards:
        for sel in VIDEO_CARD_FALLBACK_SELECTORS:
            cards = await page.query_selector_all(sel)
            if cards:
                logger.info("Using fallback selector: %s (%d cards)", sel, len(cards))
                break

    results: List[dict] = []

    for card in cards:
        item: dict = {"title": None, "url": None, "views": None, "posted_at": None, "is_paid": False}

        try:
            link_el = await card.query_selector("a[href*='/video/']")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    item["url"] = href if href.startswith("http") else f"{DOUYIN_BASE}{href}"
        except Exception:
            pass

        try:
            img_el = await card.query_selector("img[alt]")
            if img_el:
                alt = (await img_el.get_attribute("alt") or "").strip()
                if "：" in alt:
                    alt = alt.split("：", 1)[1].strip()
                elif ":" in alt:
                    alt = alt.split(":", 1)[1].strip()
                item["title"] = alt or None
        except Exception:
            pass

        if not item["title"]:
            try:
                p_els = await card.query_selector_all("p")
                best = ""
                for p_el in p_els:
                    text = (await p_el.inner_text()).strip()
                    if len(text) > len(best):
                        best = text
                item["title"] = best or None
            except Exception:
                pass

        try:
            spans = await card.query_selector_all("span")
            for span in spans:
                text = (await span.inner_text()).strip()
                if text and len(text) <= 8 and any(c.isdigit() for c in text):
                    if "-" not in text:
                        item["views"] = text
                        break
        except Exception:
            pass

        try:
            tag_els = await card.query_selector_all(".user-video-stats-tag")
            for tag_el in tag_els:
                tag_text = (await tag_el.inner_text()).strip()
                if "付费" in tag_text:
                    item["is_paid"] = True
                    break
        except Exception:
            pass

        results.append(item)

    return results

def _is_today_shanghai(posted_at: Optional[str]) -> bool:
    if not posted_at:
        return False
    today = datetime.now(TZ_SHANGHAI).date()
    for fmt in ("%m-%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(posted_at.strip(), fmt)
            if fmt == "%m-%d":
                parsed = parsed.replace(year=today.year)
            if parsed.date() == today:
                return True
        except ValueError:
            continue
    return False

def _is_past_today_shanghai(posted_at: Optional[str]) -> bool:
    if not posted_at:
        return False
    today = datetime.now(TZ_SHANGHAI).date()
    for fmt in ("%m-%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(posted_at.strip(), fmt)
            if fmt == "%m-%d":
                parsed = parsed.replace(year=today.year)
            return parsed.date() < today
        except ValueError:
            continue
    return False

# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class ScrapeChannelRequest(BaseModel):
    url: str

class VideoItem(BaseModel):
    title: Optional[str] = None
    title_detail: Optional[str] = None
    episode: Optional[str] = None
    url: Optional[str] = None
    views: Optional[str] = None
    posted_at: Optional[str] = None
    stream_video_url: Optional[str] = None
    stream_audio_url: Optional[str] = None
    video_download_url: Optional[str] = None

class ScrapeChannelResponse(BaseModel):
    url: str
    scraped_at: str
    total: int
    videos: List[VideoItem]
    note: Optional[str] = None

class ChannelVideoListRequest(BaseModel):
    url: Union[str, List[str]]
    is_paid: Optional[bool] = False

class ChannelVideoListItem(BaseModel):
    title: Optional[str] = None
    url: str
    views: Optional[str] = None
    play_url: Optional[str] = None
    is_paid: bool = False
    stream_video_url: Optional[str] = None
    stream_audio_url: Optional[str] = None

class ChannelVideoListResponse(BaseModel):
    channel_url: Union[str, List[str]]
    scraped_at: str
    total: int
    videos: List[ChannelVideoListItem]
    stream_task_id: Optional[str] = None

class VideoDetailRequest(BaseModel):
    url: str

class VideoDetailResponse(BaseModel):
    url: str
    title_detail: Optional[str] = None
    episode: Optional[str] = None
    posted_at: Optional[str] = None
    stream_video_url: Optional[str] = None
    stream_audio_url: Optional[str] = None
    video_download_url: Optional[str] = None
