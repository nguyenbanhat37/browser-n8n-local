"""
douyin_scraper.py — Playwright-based Douyin channel scraper.

Uses raw playwright.async_api (no browser-use Agent/LLM).
Scrapes all videos posted today (Asia/Shanghai timezone) from a Douyin channel URL.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Union
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# Import all browser state, configs, models and common helpers from douyin_common
from douyin_common import (
    get_browser_context,
    close_browser_context,
    schedule_browser_close_if_idle,
    CHROME_PATH,
    TZ_SHANGHAI,
    MAX_SCROLL_ITERATIONS,
    SCROLL_WAIT_SECONDS,
    PAGE_LOAD_TIMEOUT_MS,
    PAGE_STABILIZE_SECONDS,
    DOUYIN_BASE,
    STREAM_CDN_DOMAINS,
    VIDEO_CARD_SELECTOR,
    VIDEO_CARD_FALLBACK_SELECTORS,
    ScrapeChannelRequest,
    VideoItem,
    ScrapeChannelResponse,
    ChannelVideoListRequest,
    ChannelVideoListItem,
    ChannelVideoListResponse,
    VideoDetailRequest,
    VideoDetailResponse,
    _launch_browser,
    _navigate_to_channel,
    _extract_video_items,
    _is_today_shanghai,
    _is_past_today_shanghai,
    logger,
)

# Import login modal handling from login_modal_handler
from login_modal_handler import dismiss_login_modal

# Import Flow 1 & 2 APIs and helpers
from douyin_flows_1_2 import (
    get_channel_video_list,
    run_phase2_background,
    get_video_detail,
    _scroll_and_scrape,
    _hover_and_collect_play_urls,
    _get_video_details_cdp,
)

async def _get_video_details(page, video_url: str, timeout: float = 30.0) -> dict:
    """
    Navigate to video page, wait for it to fully load, extract metadata.
    
    Simple approach: wait for load state then extract from DOM + window.player.
    No modal handling needed — using same browser session as channel page.
    stream_video_url/audio captured via page.on("request") if available.
    """
    result = {
        "stream_video_url": None,
        "stream_audio_url": None,
        "title_detail": None,
        "episode": None,
        "posted_at": None,
        "video_download_url": None,
    }

    def on_request(request):
        url = request.url
        if not any(domain in url for domain in STREAM_CDN_DOMAINS):
            return
        if "media-video" in url and result["stream_video_url"] is None:
            result["stream_video_url"] = url
        if "media-audio" in url and result["stream_audio_url"] is None:
            result["stream_audio_url"] = url

    page.on("request", on_request)

    try:
        # Navigate and wait for page to be interactive
        await page.goto(video_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))

        # Wait for video player or publish time to appear — whichever comes first
        try:
            await page.wait_for_selector(
                '[data-e2e="detail-video-publish-time"], .xgplayer, h1',
                timeout=15_000
            )
        except PlaywrightTimeoutError:
            pass

        # Give player a moment to initialize and start loading
        await asyncio.sleep(2)

        # Extract title + episode from h1
        try:
            for sel in ["h1.B7xjsf10", "#douyin-right-container h1", "h1"]:
                h1 = await page.query_selector(sel)
                if not h1:
                    continue
                extracted = await page.evaluate("""(el) => {
                    const fullText = el.textContent.trim();
                    let episodeText = null;
                    const outerSpan = el.querySelector(':scope > span');
                    if (outerSpan) {
                        const childSpans = outerSpan.querySelectorAll(':scope > span');
                        if (childSpans.length >= 2) {
                            const parts = childSpans[0].textContent.trim().split(/\\s*[|｜]\\s*/);
                            const epNum = parts[0].match(/第\\d+[集话期章]/);
                            if (epNum) episodeText = epNum[0];
                        }
                    }
                    if (!episodeText) {
                        const m = fullText.match(/^(第\\d+[集话期章])\\s*[|｜]\\s*/);
                        if (m) episodeText = m[1];
                    }
                    return { full: fullText, episode: episodeText };
                }""", h1)
                if extracted and extracted.get("full"):
                    result["title_detail"] = extracted["full"]
                    result["episode"] = extracted.get("episode")
                    break
        except Exception as e:
            logger.debug("  title/episode error: %s", e)

        # Extract publish date
        try:
            pub_el = await page.query_selector('[data-e2e="detail-video-publish-time"]')
            if pub_el:
                raw = (await pub_el.inner_text()).strip()
                if "：" in raw:
                    raw = raw.split("：", 1)[1].strip()
                result["posted_at"] = raw or None
        except Exception as e:
            logger.debug("  publish time error: %s", e)

        # Extract download URL from window.player (most reliable)
        try:
            dl_url = await page.evaluate("""() => {
                try { return window.player?.videoList?.[0]?.playAddr?.[0]?.src || null; }
                catch(e) { return null; }
            }""")
            if dl_url:
                from urllib.parse import unquote
                result["video_download_url"] = unquote(dl_url)
                logger.info("  ✓ Download URL: %s...", result["video_download_url"][:60])
        except Exception as e:
            logger.debug("  video_download_url error: %s", e)

        # If missing data, wait a bit more for page to fully render
        if not result.get("title_detail") or not result.get("posted_at"):
            await asyncio.sleep(3)
            # Retry extraction
            if not result.get("posted_at"):
                try:
                    pub_el = await page.query_selector('[data-e2e="detail-video-publish-time"]')
                    if pub_el:
                        raw = (await pub_el.inner_text()).strip()
                        if "：" in raw:
                            raw = raw.split("：", 1)[1].strip()
                        result["posted_at"] = raw or None
                except Exception:
                    pass
            if not result.get("video_download_url"):
                try:
                    dl_url = await page.evaluate("""() => {
                        try { return window.player?.videoList?.[0]?.playAddr?.[0]?.src || null; }
                        catch(e) { return null; }
                    }""")
                    if dl_url:
                        from urllib.parse import unquote
                        result["video_download_url"] = unquote(dl_url)
                except Exception:
                    pass

    except Exception as e:
        logger.warning("  Error on %s: %s", video_url, e)
    finally:
        page.remove_listener("request", on_request)

    return result

async def scrape_douyin_channel(url: Union[str, List[str]], include_paid: bool = True) -> ScrapeChannelResponse:
    """
    Full channel scrape using persistent browser.
    Accepts single URL or list of channel URLs.
    If include_paid=False, videos with is_paid=True are excluded.

    Phase 1: For each channel URL, open page, scroll to collect video list.
    Phase 2: Visit each video page sequentially, extract metadata.
    """
    scraped_at = datetime.now(TZ_SHANGHAI).isoformat()

    # Normalize to list
    urls = [url] if isinstance(url, str) else list(url)
    # Deduplicate while preserving order
    seen = set()
    unique_urls = []
    for u in urls:
        # Normalize: strip query params for dedup check
        base = u.split("?")[0].rstrip("/")
        if base not in seen:
            seen.add(base)
            unique_urls.append(u)

    context = await get_browser_context()
    all_raw_items: List[dict] = []

    # --- Phase 1: collect video list from each channel ---
    for ch_idx, channel_url in enumerate(unique_urls):
        logger.info("Phase 1 [%d/%d]: %s", ch_idx + 1, len(unique_urls), channel_url)
        channel_page = await context.new_page()
        try:
            await _navigate_to_channel(channel_page, channel_url)
            raw_items = await _scroll_and_scrape(channel_page)
            # Tag each item with its channel URL
            for item in raw_items:
                item["channel_url"] = channel_url
            all_raw_items.extend(raw_items)
        except Exception as e:
            logger.error("Phase 1 error for %s: %s", channel_url, e)
        finally:
            await channel_page.close()

    # Filter: only items with URL, apply paid filter
    valid_items = [
        item for item in all_raw_items
        if item.get("url") and (include_paid or not item.get("is_paid", False))
    ]
    logger.info("Phase 1 complete: %d videos across %d channels (include_paid=%s)",
                len(valid_items), len(unique_urls), include_paid)

    # --- Phase 2: visit each video sequentially on 1 tab ---
    video_page = await context.new_page()
    try:
        for idx, item in enumerate(valid_items):
            logger.info("Phase 2 [%d/%d]: %s", idx + 1, len(valid_items),
                        item.get("title") or item["url"])
            details = await _get_video_details(video_page, item["url"], timeout=30.0)
            item.update(details)
    finally:
        try:
            await video_page.close()
        except Exception:
            pass
        await close_browser_context()

    videos = [VideoItem(**{
        "title": item.get("title"),
        "title_detail": item.get("title_detail"),
        "episode": item.get("episode"),
        "url": item.get("url"),
        "views": item.get("views"),
        "posted_at": item.get("posted_at"),
        "stream_video_url": item.get("stream_video_url"),
        "stream_audio_url": item.get("stream_audio_url"),
        "video_download_url": item.get("video_download_url"),
    }) for item in valid_items]

    channel_label = unique_urls[0] if len(unique_urls) == 1 else f"{len(unique_urls)} channels"
    return ScrapeChannelResponse(
        url=channel_label,
        scraped_at=scraped_at,
        total=len(videos),
        videos=videos,
        note=f"Scraped {len(unique_urls)} channel(s). include_paid={include_paid}.",
    )
