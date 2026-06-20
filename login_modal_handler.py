import asyncio
import logging
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("douyin-scraper")

LOGIN_MODAL_SELECTOR = "#login-panel-new"

# Selectors for login modal dismissal — try these in order
LOGIN_MODAL_CLOSE_SELECTORS = [
    # New custom requested selectors
    "#douyin-login-new-id [class*='close']",
    "#douyin-login-new-id [class*='Close']",
    "#douyin-login-new-id .douyin_login_new_class",
    ".douyin_login_new_class",
    "#douyin-login-new-id",
    # Stable ID selectors (preferred)
    "#login-panel-new [class*='close']",
    "#login-panel-new [class*='Close']",
    "#douyin_login_comp_flat_panel [class*='close']",
    "#douyin_login_comp_flat_panel [class*='Close']",
    # The specific structure found: div.YoNA2Hyj inside div.uotczcdY
    # YoNA2Hyj and qKr0RhiL are likely the close button wrapper/icon
    "#douyin_login_comp_flat_panel .YoNA2Hyj",
    "div.YoNA2Hyj.qKr0RhiL",
    "div.YoNA2Hyj",
    "div.qKr0RhiL",
    # Generic fallbacks
    '[data-e2e="login-close"]',
    '[data-e2e="modal-close"]',
    "#login-panel-new + * button",  # close button may be outside panel
    "div[id*='login'] [class*='close']",
]

VIDEO_CARD_SELECTOR = 'li:has(a[href*="/video/"])'

async def dismiss_login_modal(page) -> bool:
    """
    Attempt to close the Douyin login modal if present.

    Returns True if modal was dismissed, False if not found or could not dismiss.
    After dismissing, waits for video cards to appear.
    """
    modal_selectors = [
        LOGIN_MODAL_SELECTOR,
        "#douyin-login-new-id",
        ".douyin_login_new_class",
        "#douyin_login_comp_flat_panel",
        "div[id*='login']"
    ]
    try:
        modal = None
        for sel in modal_selectors:
            modal = await page.query_selector(sel)
            if modal:
                break
        
        # Check if login modal text is present on the page if selector failed
        if not modal:
            has_login_text = await page.evaluate("""() => {
                const text = document.body.innerText;
                return text.includes('扫码登录') || text.includes('密码登录') || text.includes('手机号登录') || text.includes('验证码') || text.includes('社交账号登录');
            }""")
            if not has_login_text:
                return False
    except Exception:
        return False

    logger.info("Login modal detected — attempting to dismiss")

    async def _cleanup_masks():
        try:
            await page.evaluate("""() => {
                document.querySelectorAll('div').forEach(div => {
                    const c = (div.className || '').toLowerCase();
                    if (c.includes('mask') || c.includes('overlay') || c.includes('backdrop')) {
                        div.remove();
                    }
                });
            }""")
        except Exception:
            pass

    # Try each close button selector
    for selector in LOGIN_MODAL_CLOSE_SELECTORS:
        try:
            el = await page.query_selector(selector)
            if el:
                await el.click()
                logger.info("Clicked close button: %s", selector)
                await asyncio.sleep(1.0)
                
                modal_still_present = False
                for sel in modal_selectors:
                    if await page.query_selector(sel):
                        modal_still_present = True
                        break
                
                if not modal_still_present:
                    logger.info("Login modal dismissed successfully")
                    await _cleanup_masks()
                    try:
                        await page.wait_for_selector(VIDEO_CARD_SELECTOR, timeout=8_000)
                    except PlaywrightTimeoutError:
                        logger.warning("Video cards still not visible after dismissing modal")
                    return True
        except Exception as e:
            logger.debug("Selector %s failed: %s", selector, e)
            continue

    # Fallback 1: Click via SVG path pattern matching
    try:
        clicked = await page.evaluate("""() => {
            const paths = document.querySelectorAll('svg path');
            for (const path of paths) {
                const d = path.getAttribute('d') || '';
                // Match the specific close path pattern
                if (d.includes('M12.7929 22.2426') || d.includes('M12.7929') || d.includes('12.7929')) {
                    const parentDiv = path.closest('div');
                    if (parentDiv) {
                        parentDiv.click();
                        return true;
                    }
                }
            }
            return false;
        }""")
        if clicked:
            logger.info("Login modal dismissed via SVG path matching")
            await _cleanup_masks()
            await asyncio.sleep(1.0)
            return True
    except Exception as e:
        logger.debug("SVG path matching click failed: %s", e)

    # Fallback 2: press Escape
    try:
        await page.keyboard.press("Escape")
        await asyncio.sleep(1.0)
        
        modal_still_present = False
        for sel in modal_selectors:
            if await page.query_selector(sel):
                modal_still_present = True
                break
                
        if not modal_still_present:
            logger.info("Login modal dismissed via Escape key")
            await _cleanup_masks()
            return True
    except Exception:
        pass

    logger.warning("Could not dismiss login modal — scraping may return empty results")
    return False

async def wait_for_no_modal(page, max_checks: int = 5, interval: float = 12.0) -> bool:
    """
    Poll for login modal up to max_checks times (every interval seconds).
    Dismiss modal if found. Return True when no modal present (ready to proceed).
    Returns False if modal never went away after all checks.
    """
    modal_selectors = [
        LOGIN_MODAL_SELECTOR,
        "#douyin-login-new-id",
        ".douyin_login_new_class",
        "#douyin_login_comp_flat_panel",
        "div[id*='login']"
    ]
    for attempt in range(max_checks):
        modal = None
        for sel in modal_selectors:
            modal = await page.query_selector(sel)
            if modal:
                break
        if not modal:
            if attempt > 0:
                logger.info("  Modal gone after %d checks — proceeding", attempt)
            return True
        # Modal present — try to dismiss
        dismissed = await dismiss_login_modal(page)
        if dismissed:
            logger.info("  Modal dismissed on check %d/%d", attempt + 1, max_checks)
            await asyncio.sleep(1.0)
        else:
            logger.info("  Modal check %d/%d: modal present but failed to dismiss. Waiting %.1fs...",
                        attempt + 1, max_checks, interval)
            await asyncio.sleep(interval)
            
    # Final check
    modal = None
    for sel in modal_selectors:
        modal = await page.query_selector(sel)
        if modal:
            break
    return not modal
