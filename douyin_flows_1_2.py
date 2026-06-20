import asyncio
import logging
import os
from datetime import datetime
from typing import List, Optional, Union
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# Import shared configs, constants, and classes from douyin_common
from douyin_common import (
    get_browser_context,
    close_browser_context,
    schedule_browser_close_if_idle,
    TZ_SHANGHAI,
    MAX_SCROLL_ITERATIONS,
    SCROLL_WAIT_SECONDS,
    PAGE_LOAD_TIMEOUT_MS,
    PAGE_STABILIZE_SECONDS,
    VIDEO_CARD_SELECTOR,
    VIDEO_CARD_FALLBACK_SELECTORS,
    STREAM_CDN_DOMAINS,
    logger,
    _navigate_to_channel,
    _extract_video_items,
    _is_past_today_shanghai,
    ChannelVideoListResponse,
    ChannelVideoListItem,
    VideoDetailResponse,
)

# Import login modal handling from login_modal_handler
from login_modal_handler import dismiss_login_modal, wait_for_no_modal

async def _hover_and_collect_play_urls(page, video_items: List[dict]) -> List[dict]:
    """
    Hover over each video card, capture CDN video URLs via CDP network monitoring.
    CDP captures ALL network events including iframes and workers.
    Writes hover.log for debugging.
    """
    vid_to_idx: dict = {}
    for idx, item in enumerate(video_items):
        url = item.get("url", "")
        vid = url.rstrip("/").split("/")[-1]
        if vid.isdigit():
            vid_to_idx[vid] = idx

    play_urls: dict = {}
    video_streams: dict = {}
    audio_streams: dict = {}
    log_lines = [f"=== hover.log — {datetime.now(TZ_SHANGHAI).isoformat()} ===\n",
                 f"Channel videos: {len(video_items)}\n\n"]

    # Use CDP client to monitor ALL network requests (including iframes/workers)
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Network.enable")

    captured_requests: list = []
    _current_hovering_vid = None

    def on_cdp_request(event):
        nonlocal _current_hovering_vid
        url = event.get("request", {}).get("url", "")
        captured_requests.append(url)
        is_cdn = any(domain in url for domain in STREAM_CDN_DOMAINS)
        if not is_cdn:
            return

        # Map directly to the currently hovered video card ID
        vid = _current_hovering_vid
        if vid:
            if "media-video" in url:
                if vid not in video_streams:
                    video_streams[vid] = url
                    logger.info("  ✓ CDP video stream captured for vid %s: %s...", vid, url[:80])
            elif "media-audio" in url:
                if vid not in audio_streams:
                    audio_streams[vid] = url
                    logger.info("  ✓ CDP audio stream captured for vid %s: %s...", vid, url[:80])
            else:
                if vid not in play_urls:
                    play_urls[vid] = url
                    logger.info("  ✓ CDP play URL captured for vid %s: %s...", vid, url[:80])

    cdp.on("Network.requestWillBeSent", on_cdp_request)

    try:
        # --- Phase 0: Wait for no modal before starting ---
        logger.info("Checking for login modal before hover phase (max 5 checks × 12s)...")
        await wait_for_no_modal(page, max_checks=5, interval=12.0)

        cards = await page.query_selector_all(VIDEO_CARD_SELECTOR)
        if not cards:
            for sel in VIDEO_CARD_FALLBACK_SELECTORS:
                cards = await page.query_selector_all(sel)
                if cards:
                    break

        logger.info("Hovering over %d video cards (CDP monitoring)...", len(cards))

        for idx, card in enumerate(cards):
            card_vid_url = video_items[idx]["url"] if idx < len(video_items) else "unknown"
            vid = card_vid_url.rstrip("/").split("/")[-1]
            log_lines.append(f"--- Card {idx+1}: {card_vid_url}\n")

            try:
                _current_hovering_vid = vid
                got_play_url = False
                for attempt in range(5):
                    await card.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    await card.hover()
                    
                    # Wait up to 3s for play URL or BOTH video and audio streams to appear
                    for _ in range(6):
                        if (vid in play_urls) or (vid in video_streams and vid in audio_streams):
                            got_play_url = True
                            break
                        await asyncio.sleep(0.5)
                        
                    if got_play_url:
                        logger.info("  ✓ play_url/stream captured for card %d/%d (attempt %d/5)", idx + 1, len(cards), attempt + 1)
                        break

                    # Check if modal is present and dismiss it
                    modal_dismissed = await dismiss_login_modal(page)
                    if modal_dismissed:
                        logger.warning("  Login modal detected and dismissed. Waiting 12s before retrying hover (attempt %d/5)...", attempt + 1)
                        await asyncio.sleep(12.0)
                    else:
                        logger.info("  No login modal detected. Retrying hover with 1s short delay (attempt %d/5)...", attempt + 1)
                        await asyncio.sleep(1.0)
                    
                    if (vid in play_urls) or (vid in video_streams and vid in audio_streams):
                        got_play_url = True
                        break

                if not got_play_url:
                    logger.warning("  Failed to capture play_url/stream for card %d/%d after 5 attempts.", idx + 1, len(cards))

            except Exception as e:
                log_lines.append(f"  ERROR: {e}\n")
                logger.debug("  Hover error card %d: %s", idx, e)
            finally:
                _current_hovering_vid = None

        await asyncio.sleep(1.0)

    finally:
        try:
            await cdp.send("Network.disable")
            await cdp.detach()
        except Exception:
            pass

    # Write log
    log_path = os.path.join(os.path.dirname(__file__), "hover.log")
    log_lines.append(f"\n=== SUMMARY ===\n")
    log_lines.append(f"Total requests captured: {len(captured_requests)}\n")
    log_lines.append(f"play_urls captured: {len(play_urls)}\n")
    log_lines.append(f"video_streams captured: {len(video_streams)}\n")
    log_lines.append(f"audio_streams captured: {len(audio_streams)}\n")
    for vid, url in play_urls.items():
        log_lines.append(f"  {vid} (play_url): {url}\n")
    for vid, url in video_streams.items():
        log_lines.append(f"  {vid} (video_stream): {url}\n")
    # Also dump ALL captured requests for debugging
    log_lines.append(f"\n=== ALL CAPTURED REQUESTS ({len(captured_requests)}) ===\n")
    for req_url in captured_requests:
        if any(k in req_url for k in ["zjcdn", "douyinvod", "__vid", "video", "media"]):
            log_lines.append(f"  {req_url[:200]}\n")
    try:
        with open(log_path, "w", encoding="utf-8") as f:
            f.writelines(log_lines)
        logger.info("hover.log written: %s", log_path)
    except Exception as e:
        logger.warning("Could not write hover.log: %s", e)

    # Map back to video items
    for vid in vid_to_idx.keys():
        idx = vid_to_idx[vid]
        if vid in play_urls:
            video_items[idx]["play_url"] = play_urls[vid]
        if vid in video_streams:
            video_items[idx]["stream_video_url"] = video_streams[vid]
        if vid in audio_streams:
            video_items[idx]["stream_audio_url"] = audio_streams[vid]

    found = sum(1 for item in video_items if item.get("play_url") or item.get("stream_video_url"))
    logger.info("Play URLs collected: %d/%d", found, len(video_items))
    return video_items

async def _scroll_and_scrape(page) -> List[dict]:
    """
    Scroll the page, collecting video items until older-than-today videos appear.

    Stop conditions (in priority order):
    1. Any video card with a date BEFORE today is detected → we have all today's
       videos, stop immediately and return current items.
    2. Two consecutive scrolls yield no new items → end of feed.
    3. MAX_SCROLL_ITERATIONS reached → safety cap.

    After each scroll, page.stop() is called to prevent Douyin from making
    additional background requests triggered by the scroll event.
    """
    prev_count = 0
    no_change_streak = 0

    for iteration in range(MAX_SCROLL_ITERATIONS):
        # Dismiss login modal if it appeared during scrolling
        await dismiss_login_modal(page)

        # Scroll one viewport down
        await page.evaluate("window.scrollBy(0, window.innerHeight)")
        await asyncio.sleep(SCROLL_WAIT_SECONDS)

        # Stop any new network requests triggered by scroll
        try:
            await page.stop()
        except Exception:
            pass

        # Give React a moment to render newly loaded cards
        await asyncio.sleep(0.3)

        current_items = await _extract_video_items(page)
        current_count = len(current_items)

        logger.info("Scroll %d/%d — %d video cards found", iteration + 1, MAX_SCROLL_ITERATIONS, current_count)

        # STOP CONDITION 1: found a video older than today → all today's videos are loaded
        if any(_is_past_today_shanghai(item.get("posted_at")) for item in current_items):
            logger.info("Found video from previous day — stopping scroll (today's videos fully loaded)")
            break

        # STOP CONDITION 2: no new items after 2 consecutive scrolls → end of feed
        if current_count == prev_count:
            no_change_streak += 1
            if no_change_streak >= 2:
                logger.info("No new items for 2 consecutive scrolls — stopping")
                break
        else:
            no_change_streak = 0

        prev_count = current_count

    return await _extract_video_items(page)

async def _get_video_details_cdp(page, video_url: str, timeout: float = 25.0) -> dict:
    """
    Navigate to video page, use CDP (DevTools Network tab) to capture
    media-video-avc1 and media-audio-und-mp4a stream URLs.
    Logic mirrors hover approach: CDP Network.requestWillBeSent catches all requests.
    """
    result = {
        "stream_video_url": None,
        "stream_audio_url": None,
        "title_detail": None,
        "episode": None,
        "posted_at": None,
        "video_download_url": None,
    }

    video_found = asyncio.Event()
    audio_found = asyncio.Event()

    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Network.enable")

    # Also enable on all child frames/targets via Target domain
    try:
        await cdp.send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })
    except Exception:
        pass

    all_cdp_requests: list = []

    def on_cdp_request(event):
        url = event.get("request", {}).get("url", "")
        if any(domain in url for domain in STREAM_CDN_DOMAINS):
            all_cdp_requests.append(url)
            logger.info("  [CDP] %s...", url[:120])
        if not any(domain in url for domain in STREAM_CDN_DOMAINS):
            return
        if "media-video" in url and result["stream_video_url"] is None:
            result["stream_video_url"] = url
            logger.info("  ✓ CDP video: %s...", url[:80])
            video_found.set()
        if "media-audio" in url and result["stream_audio_url"] is None:
            result["stream_audio_url"] = url
            logger.info("  ✓ CDP audio: %s...", url[:80])
            video_found.set()

    cdp.on("Network.requestWillBeSent", on_cdp_request)

    # Also listen on all attached sessions (iframes)
    async def on_session_attached(event):
        try:
            session_id = event.get("sessionId")
            if session_id:
                sub_cdp = await page.context.new_cdp_session(page)
                await sub_cdp.send("Network.enable")
                sub_cdp.on("Network.requestWillBeSent", on_cdp_request)
        except Exception:
            pass

    cdp.on("Target.attachedToTarget", on_session_attached)

    # Playwright page.on("request") catches Fetch/XHR in main frame directly
    def on_page_request(request):
        url = request.url
        if not any(domain in url for domain in STREAM_CDN_DOMAINS):
            return
        all_cdp_requests.append(f"[page] {url}")
        logger.info("  [page.request] %s...", url[:120])
        if "media-video" in url and result["stream_video_url"] is None:
            result["stream_video_url"] = url
            video_found.set()
        if "media-audio" in url and result["stream_audio_url"] is None:
            result["stream_audio_url"] = url
            audio_found.set()

    page.on("request", on_page_request)

    try:
        await page.goto(video_url, wait_until="commit", timeout=int(timeout * 1000))

        # Wait for player element
        try:
            await page.wait_for_selector(".xgplayer, xg-controls, video", timeout=12_000)
        except PlaywrightTimeoutError:
            pass

        # Ensure video is playing — try multiple approaches
        async def _trigger_play():
            for sel in ["xg-icon.xgplayer-time", ".xgplayer-play", "xg-left-grid xg-icon", "video", ".xgplayer"]:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        logger.debug("  Clicked play: %s", sel)
                        return
                except Exception:
                    continue
            try:
                await page.evaluate("document.querySelector('video')?.play()")
                logger.debug("  JS play() triggered")
            except Exception:
                pass
            try:
                await page.keyboard.press("Space")
            except Exception:
                pass

        await _trigger_play()

        # Poll until video is actually playing (time-current > "00:00") — max 20 checks × 10s
        async def _wait_for_video_playing(max_checks: int = 20, interval: float = 10.0) -> bool:
            for attempt in range(max_checks):
                # Check 1: time-current span shows video is running
                try:
                    time_el = await page.query_selector(".time-current")
                    if time_el:
                        time_text = (await time_el.inner_text()).strip()
                        if time_text and time_text != "00:00":
                            logger.info("  Video playing: time=%s (attempt %d)", time_text, attempt + 1)
                            return True
                except Exception:
                    pass

                # Check 2: xgplayer has class "xgplayer-playing"
                try:
                    playing_el = await page.query_selector(".xgplayer-playing")
                    if playing_el:
                        logger.info("  xgplayer-playing detected (attempt %d)", attempt + 1)
                        return True
                except Exception:
                    pass

                # Check 3: streams already captured by CDP
                if video_found.is_set() and audio_found.is_set():
                    logger.info("  Streams already captured")
                    return True

                logger.debug("  Video not playing yet (attempt %d/%d) — waiting %ds...",
                             attempt + 1, max_checks, int(interval))

                # Re-trigger play every few checks
                if attempt % 3 == 2:
                    await _trigger_play()

                await asyncio.sleep(interval)

            return False

        # Define DOM extraction helper
        async def _extract():
            nonlocal result
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
            except Exception:
                pass
            try:
                pub_el = await page.query_selector('[data-e2e="detail-video-publish-time"]')
                if pub_el:
                    raw = (await pub_el.inner_text()).strip()
                    if "：" in raw:
                        raw = raw.split("：", 1)[1].strip()
                    result["posted_at"] = raw or None
            except Exception:
                pass
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
            return bool(result.get("posted_at") and result.get("video_download_url"))

        # Step 1: Flow 3 style - try to extract date + video_download_url WITHOUT closing modal first
        logger.info("  [Hybrid] Attempting pre-extraction of date and download URL (Flow 3 style)...")
        for pre_attempt in range(5):
            if await _extract():
                logger.info("  ✓ Successfully pre-extracted date + download URL without closing modal on attempt %d/5", pre_attempt + 1)
                break
            await asyncio.sleep(1.0)

        # Step 2: Dismiss login modal exactly once
        logger.info("  [Hybrid] Dismissing login modal exactly once...")
        await dismiss_login_modal(page)
        await asyncio.sleep(2.0)

        # Step 3: Perform Flow 1 processing (CDP stream capture)
        logger.info("  [Hybrid] Starting Flow 1 CDP stream capture...")
        playing = await _wait_for_video_playing(max_checks=10, interval=1.0)
        if not playing:
            logger.warning("  Video may not be playing — CDP streams may be empty")

        # Wait for both streams with a short timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(video_found.wait(), audio_found.wait()),
                timeout=10.0
            )
            logger.info("  ✓ Both CDN streams captured via CDP")
        except asyncio.TimeoutError:
            logger.warning("  CDP stream timeout (optional) — video=%s audio=%s",
                           result["stream_video_url"] is not None,
                           result["stream_audio_url"] is not None)

        # Final extraction of all DOM info to catch anything that loaded late
        await _extract()

    except Exception as e:
        logger.warning("  CDP video detail error on %s: %s", video_url, e)
    finally:
        try:
            page.remove_listener("request", on_page_request)
        except Exception:
            pass
        # Write debug log
        log_path = os.path.join(os.path.dirname(__file__), "hover.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"=== video-detail CDP log — {datetime.now(TZ_SHANGHAI).isoformat()} ===\n")
                f.write(f"URL: {video_url}\n")
                f.write(f"stream_video_url: {result.get('stream_video_url')}\n")
                f.write(f"stream_audio_url: {result.get('stream_audio_url')}\n\n")
                f.write(f"=== ALL CDN REQUESTS CAPTURED ({len(all_cdp_requests)}) ===\n")
                for u in all_cdp_requests:
                    f.write(f"  {u}\n")
        except Exception:
            pass
        try:
            await cdp.send("Network.disable")
            await cdp.detach()
        except Exception:
            pass

    return result

async def get_channel_video_list(channel_url: str, task_id: str = None) -> ChannelVideoListResponse:
    """
    Phase 1: open channel page, scroll, return list immediately.
    If task_id provided, continues Phase 2 in background using the same browser context.
    Browser stays open after this call to allow Phase 2 stream capture.
    """
    scraped_at = datetime.now(TZ_SHANGHAI).isoformat()
    context = await get_browser_context()
    page = await context.new_page()
    try:
        await _navigate_to_channel(page, channel_url)
        raw_items = await _scroll_and_scrape(page)
        # Hover over each card to collect /aweme/v1/play/ URLs
        raw_items = await _hover_and_collect_play_urls(page, raw_items)
    finally:
        await page.close()
        schedule_browser_close_if_idle(10.0)

    valid = [item for item in raw_items if item.get("url")]
    logger.info("Channel list: %d videos found", len(valid))
    return ChannelVideoListResponse(
        channel_url=channel_url,
        scraped_at=scraped_at,
        total=len(valid),
        videos=[ChannelVideoListItem(
            title=item.get("title"),
            url=item["url"],
            views=item.get("views"),
            play_url=item.get("play_url"),
            is_paid=item.get("is_paid", False),
            stream_video_url=item.get("stream_video_url"),
            stream_audio_url=item.get("stream_audio_url"),
        ) for item in valid],
    )

async def run_phase2_background(task_id: str, task_store: dict):
    """
    Phase 2: Wait up to 10s for caller to confirm (GET stream-results/{task_id}).
    If confirmed → process each video sequentially using CDP for stream URLs.
    If not confirmed within 10s → close browser and abort.
    """
    deadline = asyncio.get_event_loop().time() + 10.0
    while asyncio.get_event_loop().time() < deadline:
        if task_store.get(task_id, {}).get("confirmed"):
            break
        await asyncio.sleep(0.5)
    else:
        logger.info("Phase 2 [%s]: No confirmation within 10s — closing browser", task_id)
        task_store[task_id]["status"] = "failed"
        task_store[task_id]["error"] = "No confirmation received within 10s"
        await close_browser_context()
        return

    videos = task_store[task_id]["videos"]
    logger.info("Phase 2 [%s]: Confirmed — processing %d videos", task_id, len(videos))
    context = await get_browser_context()
    page = await context.new_page()
    try:
        for idx, video_item in enumerate(videos):
            video_url = video_item["url"]
            logger.info("Phase 2 [%d/%d]: %s", idx + 1, len(videos), video_url)
            details = await _get_video_details_cdp(page, video_url, timeout=25.0)
            
            # Update the video item dict in-place
            video_item.update(details)
            
            task_store[task_id]["completed"] = idx + 1
            task_store[task_id]["videos"] = list(videos)
    except Exception as e:
        logger.exception("Phase 2 background error: %s", e)
        task_store[task_id]["error"] = str(e)
    finally:
        try:
            await page.close()
        except Exception:
            pass
        await close_browser_context()
        task_store[task_id]["status"] = "finished"
        logger.info("Phase 2 complete: %d/%d videos", len(videos), len(videos))

async def get_video_detail(video_url: str) -> VideoDetailResponse:
    """
    Single video detail: open 1 video page, capture stream URLs + metadata.
    Uses CDP (DevTools Network tab) to capture media-video-avc1 and media-audio-und-mp4a.
    Uses persistent browser context (~15-25s per video).
    """
    context = await get_browser_context()
    page = await context.new_page()
    try:
        details = await _get_video_details_cdp(page, video_url, timeout=25.0)
    finally:
        await page.close()
        schedule_browser_close_if_idle(10.0)

    return VideoDetailResponse(
        url=video_url,
        title_detail=details.get("title_detail"),
        episode=details.get("episode"),
        posted_at=details.get("posted_at"),
        stream_video_url=details.get("stream_video_url"),
        stream_audio_url=details.get("stream_audio_url"),
        video_download_url=details.get("video_download_url"),
    )
