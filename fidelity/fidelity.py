import asyncio
import csv
import datetime
import json
import re
import secrets
import traceback
from enum import Enum
from pathlib import Path

import anyio
import pyotp
import zendriver as zd


class FidMonths(Enum):
    """Months that fidelity uses in the statement labeling."""

    Jan = 1
    Feb = 2
    March = 3
    April = 4
    May = 5
    June = 6
    July = 7
    Aug = 8
    Sep = 9
    Oct = 10
    Nov = 11
    Dec = 12


class FidelityAutomation:
    """A class to manage and control a zendriver webdriver with Fidelity.

    Uses zendriver (CDP-based) instead of Selenium for better anti-detection.

    Args:
        headless: If True the browser will be headless.
        debug: If the driver should print debug info.
        title: The title of this session. Used for profile path if present.
        source_account: Account to use as the "From" account for transfers.
        save_state: Determine whether to save cookies/profile data.
        profile_path: Path used to store browser session data.

    """

    def __init__(  # ruff: ignore[too-many-arguments]
        self,
        *,  # Enforce keyword arguments
        headless: bool = True,
        debug: bool = False,
        title: str | None = None,
        source_account: str | None = None,
        profile_path: str = ".",
        docker: bool = False,
    ) -> None:
        """Initialize FidelityAutomation class."""
        self.headless: bool = headless
        self.title: str | None = title
        self.debug = debug
        self.profile_path: str = profile_path
        self.docker: bool = docker
        # Browser and page will be set in launch()
        self.browser: zd.Browser = None
        self.page: zd.Tab = None
        # Some class variables
        self.account_dict: dict = {}
        self.source_account = source_account
        self.new_account_number = None

    async def get_driver(self) -> None:
        """Initialize a browser instance using zendriver.

        Example:
            automation = FidelityAutomation()
            await automation.launch()

        """
        # Determine profile path
        profile_dir = await anyio.Path(self.profile_path).resolve() / self.title if self.title else await anyio.Path(self.profile_path).resolve() / "ZenFid"

        self.profile_path = str(profile_dir)

        # Create profile directory if it doesn't exist
        if not await profile_dir.exists():
            await profile_dir.parent.mkdir(parents=True, exist_ok=True)

        # Build zendriver Config
        browser_args = []

        if self.docker:
            browser_args.extend([
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1920,1080",
            ])
        elif self.headless:
            browser_args.extend([
                "--headless=new",
                "--window-size=1920,1080",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "--disable-site-isolation-trials",
                "--disable-features=IsolateOrigins,site-per-process,TranslateUI,VizDisplayCompositor",
                "--disable-session-crashed-bubble",
                "--disable-infobars",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-extensions",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ])
        else:
            browser_args.extend([
                "--start-maximized",
                "--disable-session-crashed-bubble",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "--disable-infobars",
                "--disable-features=TranslateUI,VizDisplayCompositor",
                "--no-first-run",
                "--disable-default-apps",
                "--disable-extensions",
            ])

        # Start the browser
        self.browser = await zd.start(
            browser_args=browser_args,
            user_data_dir=str(self.profile_path) if self.title else None,
        )

        # Get the first/main tab
        self.page = await self.browser.get()

        if self.debug:
            print("[+] Zendriver browser launched successfully")

    async def close_browser(self) -> None:
        """Close the browser and clean up resources."""
        if self.browser:
            await self.browser.stop()
            if self.debug:
                print("[+] Browser closed")

    @staticmethod
    def _mask(value: str | None) -> str:
        """Mask sensitive identifiers (e.g. account numbers) for logging privacy."""
        if not value:
            return "***"
        val_str = str(value).strip()
        if len(val_str) <= 4:
            return "****"
        return f"***{val_str[-4:]}"

    @staticmethod
    def _clean_url(url: str | None) -> str:
        """Sanitize URLs to strip query parameters or hashes that may divulge personal info."""
        if not url:
            return ""
        return str(url).split("?")[0].split("#")[0]

    def debug_log(self, msg: str) -> None:
        """Print timestamped debug messages when debug mode is enabled."""
        if self.debug:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] [DEBUG] {msg}")

    async def debug_screenshot(self, name: str, *, log_page_state: bool = True) -> str | None:
        """Take a screenshot of the current page and inspect page errors/state.

        Only works if debug mode is enabled. Saves to .debug_artifacts/ (ignored by git).

        Args:
            name: The name of the screenshot file.
            log_page_state: Whether to check and log visible error banners/alerts.

        Returns:
            Path to saved screenshot or None.

        """
        if not self.debug or not self.page:
            return None

        debug_dir = Path(".debug_artifacts")
        debug_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(debug_dir / f"fidelity_debug_{name}{self.title or ''}.png")
        try:
            await self.page.save_screenshot(filename=screenshot_path, format="png")
            self.debug_log(f"Screenshot captured: {name}")
        except Exception as e:
            self.debug_log(f"Error taking screenshot ({name}): {e}")

        if log_page_state:
            try:
                js_get_state = """
                (() => {
                    const errors = [];
                    const errorSelectors = [
                        '.pvd-alert',
                        '.pvd3-alert',
                        '[role="alert"]',
                        '.error-message',
                        '.alert-error',
                        '.system-error',
                        '.banner-alert'
                    ];
                    for (const sel of errorSelectors) {
                        for (const el of document.querySelectorAll(sel)) {
                            const txt = (el.textContent || el.innerText || '').trim();
                            if (txt && el.offsetWidth > 0 && el.offsetHeight > 0) {
                                errors.push(txt.replace(/\\s+/g, ' ').slice(0, 150));
                            }
                        }
                    }
                    return {
                        url: window.location.href,
                        title: document.title,
                        readyState: document.readyState,
                        alerts: errors
                    };
                })()
                """
                state = await self.page.evaluate(js_get_state)
                if isinstance(state, dict):
                    url_cleaned = self._clean_url(state.get("url"))
                    self.debug_log(f"Page State @ {name}: URL='{url_cleaned}' | Title='{state.get('title')}' | Ready='{state.get('readyState')}'")
                    if state.get("alerts"):
                        for alert in state["alerts"]:
                            self.debug_log(f"Page Alert/Error: {alert}")
            except Exception as e:
                self.debug_log(f"Error checking page state: {e}")

        return screenshot_path

    async def debug_dump_dom(self, name: str, selector: str = "body") -> None:
        """Dump the HTML structure of elements to a debug file in .debug_artifacts/."""
        if not self.debug or not self.page:
            return
        try:
            js_dump = f"""
            (() => {{
                const el = document.querySelector('{selector}');
                if (!el) return 'Element {selector} not found';
                return el.outerHTML.slice(0, 50000);
            }})()
            """
            content = await self.page.evaluate(js_dump)
            if content:
                debug_dir = Path(".debug_artifacts")
                debug_dir.mkdir(parents=True, exist_ok=True)
                dump_path = debug_dir / f"fidelity_debug_{name}.html"
                dump_path.write_text(str(content), encoding="utf-8")
                self.debug_log(f"DOM snapshot dumped to {dump_path}")
        except Exception as e:
            self.debug_log(f"Error dumping DOM: {e}")

    async def _find_button(self, text: str, timeout: float = 5.0) -> zd.Element | None:
        """Find a button or clickable element containing the specified text."""
        # 1. Try button/link/role='button' via xpath
        try:
            xpath_expr = (
                f"//button[contains(normalize-space(.), '{text}')] | "
                f"//a[contains(normalize-space(.), '{text}')] | "
                f"//*[@role='button'][contains(normalize-space(.), '{text}')] | "
                f"//input[@type='submit' or @type='button'][@value='{text}']"
            )
            elems = await self.page.xpath(xpath_expr)
            if elems:
                return elems[0]
        except Exception:
            pass

        # 2. Try case-insensitive xpath
        try:
            xpath_expr = (
                f"//*[self::button or self::a or @role='button']"
                f"[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{text.lower()}')]"
            )
            elems = await self.page.xpath(xpath_expr)
            if elems:
                return elems[0]
        except Exception:
            pass

        # 3. Try find_elements_by_text filtering out script/style/head tags
        try:
            elems = await self.page.find_elements_by_text(text)
            if elems:
                for el in elems:
                    tag = getattr(el, 'tag_name', '') or getattr(el, 'local_name', '') or ''
                    if tag.lower() not in ('style', 'script', 'head', 'meta', 'link'):
                        return el
        except Exception:
            pass

        return None

    async def _mouse_click(self, elem: zd.Element | None) -> bool:
        """Click an element using native CDP mouse_click with realistic human movement for anti-detection."""
        if not elem:
            return False
        try:
            await elem.scroll_into_view()
            await asyncio.sleep(secrets.SystemRandom().uniform(0.08, 0.18))
        except Exception:
            pass
        try:
            pos = await elem.get_position()
            if pos and pos.width > 0 and pos.height > 0:
                # Add human jitter within the center 50% of the element
                jitter_x = (secrets.SystemRandom().random() - 0.5) * (pos.width * 0.4)
                jitter_y = (secrets.SystemRandom().random() - 0.5) * (pos.height * 0.4)
                target_x = pos.center[0] + jitter_x
                target_y = pos.center[1] + jitter_y
                await self.page.mouse_click(target_x, target_y)
                await asyncio.sleep(secrets.SystemRandom().uniform(0.12, 0.28))
                return True
        except Exception:
            pass
        try:
            await elem.mouse_click()
            await asyncio.sleep(secrets.SystemRandom().uniform(0.12, 0.28))
            return True
        except Exception:
            try:
                await elem.click()
                await asyncio.sleep(secrets.SystemRandom().uniform(0.12, 0.28))
                return True
            except Exception as e:
                if self.debug:
                    print(f"Error clicking element: {e}")
                return False

    async def _human_type(self, elem: zd.Element | None, text: str, *, clear_first: bool = False) -> bool:
        """Focus an element via mouse click and type text with realistic human cadence and micro-delays."""
        if not elem:
            return False
        try:
            await self._mouse_click(elem)
            await asyncio.sleep(secrets.SystemRandom().uniform(0.12, 0.25))
            if clear_first:
                try:
                    await elem.send_keys(["Control", "a"])
                    await asyncio.sleep(0.05)
                    await elem.send_keys(["Backspace"])
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
            for char in str(text):
                await elem.send_keys(char)
                delay = secrets.SystemRandom().uniform(0.04, 0.12)
                # Occasional slight human hesitation (8% probability)
                if secrets.SystemRandom().random() < 0.08:
                    delay += secrets.SystemRandom().uniform(0.1, 0.22)
                await asyncio.sleep(delay)
            await asyncio.sleep(secrets.SystemRandom().uniform(0.15, 0.3))
            return True
        except Exception as e:
            if self.debug:
                print(f"Error typing into element: {e}")
            return False

    async def navigate(self, url: str) -> None:
        """Navigate to a URL and wait for the page to load.

        Args:
            url: The URL to navigate to.

        """
        await self.page.get(url)
        # Wait for page load
        await asyncio.sleep(0.5)

    async def get_list_of_accounts(
        self,
        *,  # Everything after this must be a keyword argument
        set_flag: bool = True,
        get_withdrawal_bal: bool = False,
    ) -> dict:
        """Obtain the list of accounts from the transfers page dropdown or portfolio summary.

        Separate the account number and nickname and place them into `self.account_dict`.

        Args:
            set_flag: If True, `self.account_dict` will be updated (default: True).
            get_withdrawal_bal: If True, the function will provide the available balance per account (default: False).

        Returns:
            A dictionary of the account information using account numbers as keys.

        """
        try:
            local_dict = {}

            # Helper JS function to extract accounts from the Portfolio Summary page sidebar
            js_extract_summary_accounts = r"""
            (() => {
                const results = [];
                const seen = new Set();
                const blacklist = new Set([
                    'investment', 'all accounts', 'accounts', 'brokerage', 'ira',
                    'rollover ira', 'roth ira', 'total', 'fidelity', 'balance',
                    "today's gain/loss", 'more', 'planning', 'documents', 'balances',
                    'activity & orders', 'positions', 'summary'
                ]);

                // Strategy 1: Parse the rendered text layout stream (from document.body.innerText)
                try {
                    const bodyText = document.body.innerText || '';
                    const lines = bodyText.split('\n').map(l => l.trim()).filter(Boolean);
                    for (let i = 0; i < lines.length; i++) {
                        const match = lines[i].match(/\b([Zz\d]\d{7,9})\b/);
                        if (match) {
                            const accNum = match[1].toUpperCase();
                            if (!seen.has(accNum)) {
                                seen.add(accNum);
                                let nickname = "Account";
                                for (let j = i - 1; j >= Math.max(0, i - 4); j--) {
                                    const candidate = lines[j];
                                    if (!candidate.startsWith('$') && !candidate.startsWith('+') && !candidate.startsWith('-') &&
                                        !candidate.includes('%') && !candidate.includes(':') && candidate.length < 50 &&
                                        !blacklist.has(candidate.toLowerCase())) {
                                        nickname = candidate;
                                        break;
                                    }
                                }
                                let balance = 0.0;
                                for (let j = i + 1; j <= Math.min(lines.length - 1, i + 4); j++) {
                                    const balMatch = lines[j].match(/^\$([\d,]+\.?\d*)/);
                                    if (balMatch) {
                                        balance = parseFloat(balMatch[1].replace(/,/g, ''));
                                        break;
                                    }
                                }
                                results.push({ account_num: accNum, nickname: nickname, balance: balance });
                            }
                        }
                    }
                } catch (e) {}

                // Strategy 2: Check custom elements (e.g. pvd-card, pvd3-action-card, account links)
                try {
                    const cardSelectors = [
                        'pvd3-action-card',
                        'pvd-action-card',
                        '[data-testid*="account" i]',
                        '[class*="account-card" i]',
                        '[class*="account-item" i]',
                        'a[href*="summary"]',
                        'a[href*="portfolio"]'
                    ];
                    const cards = document.querySelectorAll(cardSelectors.join(', '));
                    for (const card of cards) {
                        const cardText = (card.innerText || card.textContent || '').trim();
                        const match = cardText.match(/\b([Zz\d]\d{7,9})\b/);
                        if (match) {
                            const accNum = match[1].toUpperCase();
                            if (!seen.has(accNum)) {
                                seen.add(accNum);
                                const cardLines = cardText.split('\n').map(l => l.trim()).filter(Boolean);
                                let nickname = "Account";
                                let balance = 0.0;
                                for (let i = 0; i < cardLines.length; i++) {
                                    if (cardLines[i].includes(accNum)) {
                                        if (i > 0 && !cardLines[i - 1].startsWith('$')) {
                                            nickname = cardLines[i - 1];
                                        }
                                        for (let j = i + 1; j < cardLines.length; j++) {
                                            const bal = cardLines[j].match(/^\$([\d,]+\.?\d*)/);
                                            if (bal) {
                                                balance = parseFloat(bal[1].replace(/,/g, ''));
                                                break;
                                            }
                                        }
                                        break;
                                    }
                                }
                                results.push({ account_num: accNum, nickname: nickname, balance: balance });
                            }
                        }
                    }
                } catch (e) {}

                return results;
            })()
            """

            # 1. Navigate to transfers page
            if self.debug:
                print("[+] Navigating to Transfers page to locate 'From' dropdown...")
            await self.navigate("https://digital.fidelity.com/ftgw/digital/transfer/?quicktransfer=cash-shares")
            await self.page.wait_for_ready_state("complete")
            await self.wait_for_loading_sign()
            await asyncio.sleep(1.5)

            if self.debug:
                self.debug_log(f"Current transfers page URL: {self._clean_url(self.page.url)}")
                await self.debug_screenshot("transfers_page_loaded")

            # JS script to find the "From" dropdown trigger coordinates and element info
            js_find_dropdown_trigger = """
            (() => {
                function findTrigger() {
                    const selectors = [
                        'pvd-select[pvd-id="From-acct-select"]',
                        'pvd3-select[pvd-id="From-acct-select"]',
                        'pvd-select[pvd-id*="From" i]',
                        'pvd3-select[pvd-id*="From" i]',
                        'pvd3-select[id*="From" i]',
                        'pvd-select[id*="From" i]',
                        'pvd3-select[pvd-label*="From" i]',
                        'pvd-select[pvd-label*="From" i]',
                        'select[aria-label*="From" i]',
                        'select[id*="from" i]',
                        'select[name*="from" i]',
                        '[data-testid*="from" i]',
                        'pvd3-select',
                        'pvd-select'
                    ];

                    for (const sel of selectors) {
                        const elems = document.querySelectorAll(sel);
                        for (const el of elems) {
                            const pvdId = (el.getAttribute('pvd-id') || el.id || el.getAttribute('pvd-label') || el.getAttribute('aria-label') || '').toLowerCase();
                            const parentText = (el.parentElement ? el.parentElement.textContent : '').toLowerCase();
                            const isFrom = pvdId.includes('from') || parentText.includes('from') || sel.includes('from') || elems.length === 1;

                            if (isFrom) {
                                // Scroll into view
                                el.scrollIntoView({ block: 'center', inline: 'center' });
                                // If shadowRoot has a clickable button or trigger
                                let target = el;
                                if (el.shadowRoot) {
                                    const innerBtn = el.shadowRoot.querySelector('button, .pvd-select__trigger, [role="combobox"], [role="button"], select, input');
                                    if (innerBtn) target = innerBtn;
                                }
                                const rect = target.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    return {
                                        found: true,
                                        selector: sel,
                                        pvdId: pvdId,
                                        x: rect.left + rect.width / 2,
                                        y: rect.top + rect.height / 2,
                                        width: rect.width,
                                        height: rect.height
                                    };
                                }
                            }
                        }
                    }
                    return { found: false };
                }
                return findTrigger();
            })()
            """

            # JS script to extract options from the DOM / shadow roots
            js_extract_dropdown_options = """
            (() => {
                function extractOpts() {
                    const optionsList = [];

                    // 1. Check all elements matching option-like tags
                    const optElems = document.querySelectorAll(
                        'option, pvd-option, pvd3-option, [role="option"], .pvd-select__option, .pvd3-select__option'
                    );
                    for (const o of optElems) {
                        const val = o.getAttribute('value') || o.value || '';
                        const txt = (o.textContent || o.innerText || '').trim();
                        if (txt || val) {
                            optionsList.push({ value: val, text: txt });
                        }
                    }

                    // 2. Search shadow roots of select elements
                    const selects = document.querySelectorAll('pvd-select, pvd3-select');
                    for (const s of selects) {
                        // Check JS property
                        if (Array.isArray(s.pvdOptions) && s.pvdOptions.length > 0) {
                            for (const item of s.pvdOptions) {
                                if (item.options && Array.isArray(item.options)) {
                                    optionsList.push(...item.options);
                                } else {
                                    optionsList.push(item);
                                }
                            }
                        }
                        if (s.options && s.options.length > 0) {
                            for (const o of Array.from(s.options)) {
                                optionsList.push({
                                    value: o.value || o.getAttribute('value') || '',
                                    text: (o.text || o.textContent || o.innerText || '').trim()
                                });
                            }
                        }
                        // Check shadowRoot children
                        if (s.shadowRoot) {
                            const sOpts = s.shadowRoot.querySelectorAll('option, pvd-option, pvd3-option, [role="option"], .pvd-select__option');
                            for (const o of sOpts) {
                                const val = o.getAttribute('value') || o.value || '';
                                const txt = (o.textContent || o.innerText || '').trim();
                                if (txt || val) {
                                    optionsList.push({ value: val, text: txt });
                                }
                            }
                        }
                    }

                    return optionsList;
                }
                return extractOpts();
            })()
            """

            found_options = []

            # Step 1: Poll for dropdown trigger, then click it via CDP mouse_click
            trigger_info = None
            for _ in range(10):  # wait up to 5 seconds
                trigger_info = await self.page.evaluate(js_find_dropdown_trigger)
                if isinstance(trigger_info, dict) and trigger_info.get("found"):
                    break
                await asyncio.sleep(0.5)

            if isinstance(trigger_info, dict) and trigger_info.get("found"):
                if self.debug:
                    print(f"[DEBUG] Found 'From' dropdown trigger ({trigger_info.get('selector')}) at coordinates ({trigger_info.get('x'):.1f}, {trigger_info.get('y'):.1f})")
                    print("[DEBUG] Clicking 'From' dropdown trigger via CDP mouse_click...")

                # Native CDP mouse click on dropdown trigger
                await self.page.mouse_click(trigger_info["x"], trigger_info["y"])
                await asyncio.sleep(secrets.SystemRandom().uniform(0.6, 1.0))

                if self.debug:
                    await self.debug_screenshot("transfers_dropdown_clicked")

                # Step 2: Extract options from opened dropdown
                for _ in range(8):  # wait up to 4 seconds for dropdown options
                    raw_opts = await self.page.evaluate(js_extract_dropdown_options)
                    if isinstance(raw_opts, list) and len(raw_opts) > 0:
                        found_options = raw_opts
                        if self.debug:
                            print(f"[DEBUG] Found {len(found_options)} options from opened dropdown")
                        break
                    await asyncio.sleep(0.5)
            else:
                if self.debug:
                    print("[DEBUG] Could not locate 'From' dropdown trigger on transfers page")

            # Flatten options if nested
            flat_options = []
            for item in found_options:
                if isinstance(item, dict):
                    if "options" in item and isinstance(item["options"], list):
                        flat_options.extend(item["options"])
                    elif item.get("value") or item.get("text"):
                        flat_options.append(item)

            for opt in flat_options:
                option_text = opt.get("text", "")
                option_value = opt.get("value", "")

                account_number = re.search(r"(?<=\()(Z|\d)\d{6,}(?=\))", option_text)
                nickname = re.search(r"^.+?(?=\()", option_text)
                with_bal = None

                if not account_number or not nickname:
                    alt_match = re.search(r"([A-Z\d]{8,10})", option_text)
                    if alt_match:
                        acc_num = alt_match.group(1)
                        nick = option_text.replace(acc_num, "").strip(" ()-\t\r\n")
                    else:
                        continue
                else:
                    acc_num = account_number.group(0)
                    nick = nickname.group(0).strip()

                if get_withdrawal_bal:
                    try:
                        js_select = f"""
                        (() => {{
                            const sel = '{option_value}';
                            const el = document.querySelector('pvd-select[pvd-id="From-acct-select"]') ||
                                       document.querySelector('pvd-select[pvd-id*="From"]') ||
                                       document.querySelector('pvd3-select[pvd-id*="From"]') ||
                                       document.querySelector('select[aria-label*="From" i]') ||
                                       document.querySelector('pvd-select') ||
                                       document.querySelector('pvd3-select');
                            if (el) {{
                                try {{ el.value = sel; }} catch(e) {{}}
                                try {{ el.setAttribute('value', sel); }} catch(e) {{}}
                                const s = el.shadowRoot ? el.shadowRoot.querySelector('select') : el.querySelector('select');
                                if (s) {{
                                    s.value = sel;
                                    s.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    s.dispatchEvent(new Event('input', {{bubbles: true}}));
                                }}
                                el.dispatchEvent(new CustomEvent('pvd-change', {{detail: {{value: sel}}, bubbles: true}}));
                                el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            }}
                        }})()
                        """
                        await self.page.evaluate(js_select)
                        await asyncio.sleep(0.7)

                        js_balance = """
                        (() => {
                            const cell = document.querySelector('tr.pvd-table__row:nth-child(2) > td:nth-child(2)');
                            if (cell && cell.textContent.includes('$')) return cell.textContent.trim();
                            const rows = document.querySelectorAll('tr.pvd-table__row, .pvd-table tr');
                            for (const r of rows) {
                                const cells = r.querySelectorAll('td');
                                if (cells.length >= 2 && cells[1].textContent.includes('$')) {
                                    return cells[1].textContent.trim();
                                }
                            }
                            return null;
                        })()
                        """
                        bal_str = await self.page.evaluate(js_balance)
                        if bal_str:
                            with_bal = float(bal_str.replace("$", "").replace(",", ""))
                    except Exception as e:
                        if self.debug:
                            self.debug_log(f"Error fetching withdrawal balance for {self._mask(acc_num)}: {e}")

                if set_flag:
                    if not self.set_account_dict(
                        account_num=acc_num,
                        nickname=nick,
                        withdrawal_balance=with_bal or 0.0,
                    ):
                        local_dict[acc_num] = {
                            "nickname": nick,
                            "withdrawal_balance": with_bal or 0.0,
                        }
                else:
                    local_dict[acc_num] = {
                        "nickname": nick,
                        "withdrawal_balance": with_bal or 0.0,
                    }

            # 2. Fallback to Portfolio Summary page sidebar if transfers page didn't yield accounts
            if not local_dict and not self.account_dict:
                if self.debug:
                    print("[+] Transfers page dropdown not populated. Querying Portfolio Summary sidebar...")
                await self.navigate("https://digital.fidelity.com/ftgw/digital/portfolio/summary")
                await self.page.wait_for_ready_state("complete")
                await self.wait_for_loading_sign()

                for attempt in range(20):  # poll up to 10 seconds for sidebar to populate
                    summary_accounts = await self.page.evaluate(js_extract_summary_accounts)
                    if isinstance(summary_accounts, list) and summary_accounts:
                        if self.debug:
                            self.debug_log(f"Sidebar extracted {len(summary_accounts)} accounts on attempt {attempt + 1}")
                        for acc in summary_accounts:
                            acc_num = acc.get("account_num")
                            nick = acc.get("nickname")
                            bal = acc.get("balance", 0.0)
                            if acc_num and nick:
                                if set_flag:
                                    self.set_account_dict(acc_num, nick, bal)
                                local_dict[acc_num] = {"nickname": nick, "withdrawal_balance": bal}
                        if local_dict:
                            break
                    await asyncio.sleep(0.5)

            if not local_dict and not self.account_dict:
                print("Could not find 'From' dropdown or accounts")
                if self.debug:
                    await self.debug_screenshot("no_accounts_found")
                    await self.debug_dump_dom("no_accounts_found")
                return self.account_dict

            if self.debug:
                print(f"[+] Found {len(self.account_dict or local_dict)} accounts")

            return local_dict or self.account_dict

        except Exception as e:
            print(f"Error getting account list: {e}")
            traceback.print_exc()
            return self.account_dict

    async def get_stocks_in_account(self, account_number: str) -> dict:
        """`self.getAccountInfo() must be called before this to work.

        Stocks that a specific account has.

        Args:
            account_number (str): The account number to get stocks for.

        Returns:
            dict: A dictionary with stock tickers as keys and quantities as values.

        """
        if account_number in self.account_dict:
            all_stock_dict = {}
            for single_stock_dict in self.account_dict[account_number]["stocks"]:
                stock = single_stock_dict.get("ticker", None)
                quantity = single_stock_dict.get("quantity", None)
                if stock is not None and quantity is not None:
                    all_stock_dict[stock] = quantity

            return all_stock_dict

        return None

    def set_account_dict(
        self,
        account_num: str,
        nickname: str,
        withdrawal_balance: float = 0.0,
    ) -> bool:
        """Add account to the account dictionary.

        Args:
            account_num: The account number.
            nickname: The account nickname.
            withdrawal_balance: The withdrawal balance (optional).

        Returns:
            False if account already exists, True if newly added.

        """
        if account_num in self.account_dict:
            return False

        self.account_dict[account_num] = {
            "nickname": nickname,
            "withdrawal_balance": withdrawal_balance,
        }
        return True

    async def login(
        self,
        username: str,
        password: str,
        totp_secret: str = "",
        *,
        save_device: bool = False) -> tuple[bool, bool]:
        """Login to Fidelity with username and password.

        Optionally handles TOTP 2FA if totp_secret is provided.

        Args:
            username: The username.
            password: The password.
            totp_secret: The TOTP secret for 2FA if enabled.
            save_device: Whether to save this device for future logins.

        Returns:
            Tuple of (fully_logged_in, two_fa_pending):
            (True, True) - fully logged in
            (True, False) - 2FA code needed via SMS
            (False, False) - login failed

        Raises:
            Exception: If login process encounters an error.

        """
        try:
            # Navigate to login page
            await self.navigate("https://digital.fidelity.com/prgw/digital/signin/retail")
            await asyncio.sleep(1)

            # Enter username and password with mouse focus and human typing
            username_field = await self.page.select("#dom-username-input")
            password_field = await self.page.select("#dom-pswd-input")

            if not username_field or not password_field:
                raise Exception("Could not find username or password fields.")

            await self._human_type(username_field, username)
            await asyncio.sleep(secrets.SystemRandom().uniform(0.2, 0.45))
            await self._human_type(password_field, password)
            await asyncio.sleep(secrets.SystemRandom().uniform(0.3, 0.6))

            # Click login button with mouse_click
            login_btn = await self.page.select("#dom-login-button")
            if login_btn:
                await self._mouse_click(login_btn)
            else:
                print("Could not find login button")
                return (False, False)

            # Wait for loading and navigation
            await self.page.wait_for_ready_state("complete")
            await self.page.wait()
            await self.page.sleep(3)

            # Poll for redirect to summary, TOTP input field, or 2FA options
            for _ in range(40):  # wait up to 20 seconds
                current_url = self.page.url or ""
                if "summary" in current_url:
                    if self.debug:
                        print("[+] Login successful - at summary")
                    return (True, True)

                # Check if TOTP code field is present
                if totp_secret and totp_secret != "NA":
                    totp_field = await self.page.query_selector(
                        "input[placeholder='XXXXXX'], #dom-totp-input, input[aria-label*='code' i], input[type='tel']"
                    )
                    if totp_field:
                        totp = pyotp.TOTP(totp_secret)
                        totp_code = totp.now()
                        await self._human_type(totp_field, totp_code)

                        # Check "don't ask again" if save_device is True
                        if save_device:
                            try:
                                dont_ask_label = await self._find_button("Don't ask me again")
                                if dont_ask_label:
                                    await self._mouse_click(dont_ask_label)
                            except Exception as e:
                                if self.debug:
                                    print(f"Error handling 'Don't ask me again': {e}")
                            try:
                                remember_checkbox = await self.page.find_element_by_text("Remember this device", best_match=True)
                                if remember_checkbox:
                                    pos = await remember_checkbox.get_position()
                                    if pos:
                                        await self.page.mouse_click(pos.left + 5, pos.center[1])
                            except Exception as e:
                                if self.debug:
                                    print(f"Error handling 'Remember this device': {e}")

                        # Submit 2FA code
                        try:
                            continue_btn = await self._find_button("Continue")
                            if continue_btn:
                                await self._mouse_click(continue_btn)
                                # Wait for redirect to portfolio summary
                                for _ in range(30):
                                    await asyncio.sleep(0.5)
                                    current_url = self.page.url or ""
                                    if "summary" in current_url:
                                        if self.debug:
                                            print("[+] TOTP login successful")
                                        return (True, True)
                        except Exception as e:
                            print(f"Error submitting TOTP code: {e}")
                            traceback.print_exc()

                # Check if SMS option is present
                text_btn = await self._find_button("Text me the code")
                if text_btn:
                    await self._mouse_click(text_btn)
                    if self.debug:
                        print("[+] SMS code sent - waiting for login_2FA()")
                    return (True, False)

                await asyncio.sleep(0.5)

            if self.debug:
                await self.debug_screenshot("login_unexpected_state")
            print(f"Unexpected state at URL: {self._clean_url(self.page.url)}")
            return (False, False)

        except Exception as e:
            print(f"Login error: {e}")
            traceback.print_exc()
            return (False, False)

    async def login_2FA(self, code: str, *, save_device: bool = True) -> bool:  # ruff: ignore[invalid-function-name]
        """Complete the 2FA portion of login using SMS code.

        Args:
            code: The 6-digit code from SMS.
            save_device: Whether to save this device for future logins (default: True).

        Returns:
            True if successful, False otherwise.

        """
        try:
            # Find the code input field
            code_field = await self.page.select("input[placeholder='XXXXXX'], #dom-totp-input, input[aria-label*='code' i]")
            if code_field:
                await self._human_type(code_field, code)
            else:
                print("Could not find code input field")
                if self.debug:
                    await self.debug_screenshot("2fa_code_field_missing")
                    await self.debug_dump_dom("2fa_code_field_missing")
                return False

            # Check "don't ask again" if requested
            if save_device:
                dont_ask = await self._find_button("Don't ask me again")
                if dont_ask:
                    await self._mouse_click(dont_ask)

            # Submit code
            submit_btn = await self._find_button("Continue")
            if submit_btn:
                await self._mouse_click(submit_btn)

            await self.page.wait_for_ready_state("complete")

            # Check if we made it to summary
            final_url = self.page.url
            if "summary" in final_url:
                if self.debug:
                    print("[+] 2FA login successful")
                return True

            if self.debug:
                await self.debug_screenshot("2fa_failed")
                await self.debug_dump_dom("2fa_failed")
            print(f"Still at login page. URL: {self._clean_url(final_url)}")
            return False

        except Exception as e:
            print(f"2FA error: {e}")
            traceback.print_exc()
            return False

    async def summary_holdings(self) -> dict:
        """Get a summary of all holdings across all accounts.

        Returns:
            Dictionary with ticker symbols as keys, containing quantity, last_price, and value.

        """
        unique_stocks = {}

        for account_number in self.account_dict:
            stocks = self.account_dict[account_number].get("stocks", [])
            for stock_dict in stocks:
                ticker = stock_dict.get("ticker")
                if ticker:
                    if ticker not in unique_stocks:
                        unique_stocks[ticker] = {
                            "quantity": float(stock_dict.get("quantity", 0)),
                            "last_price": float(stock_dict.get("last_price", 0)),
                            "value": float(stock_dict.get("value", 0)),
                        }
                    else:
                        unique_stocks[ticker]["quantity"] += float(stock_dict.get("quantity", 0))
                        unique_stocks[ticker]["value"] += float(stock_dict.get("value", 0))

        return unique_stocks

    async def transaction(  # ruff: ignore[too-many-arguments]
        self,
        stock: str,
        quantity: float,
        action: str,
        account: str,
        limit_price: float = 0.0,
        *,
        dry: bool = True,
    ) -> tuple[bool, str | None]:
        """Process a buy/sell order on Fidelity.

        Args:
            stock: Ticker symbol.
            quantity: Number of shares.
            action: 'buy' or 'sell'.
            account: Account number to trade in.
            limit_price: Limit price for limit orders.
            dry: True for test run, False for real order.

        Returns:
            Tuple of (success, error_message).

        """
        try:
            action = action.lower()
            if action not in {"buy", "sell"}:
                return (False, "Action must be 'buy' or 'sell'")

            # Navigate to trade page
            await self.navigate("https://digital.fidelity.com/ftgw/digital/trade-equity/index/orderEntry")
            await self.page.wait_for_ready_state("complete")

            # Select account
            account_dropdown = await self.page.query_selector("#dest-acct-dropdown")
            if account_dropdown:
                await self._mouse_click(account_dropdown)
                await asyncio.sleep(0.5)

                # Find and click account option
                xpath_expr = f"//button[@role='option' and contains(text(), '{account.upper()}')]"
                account_option = await self.page.xpath(xpath_expr)
                if account_option:
                    await self._mouse_click(account_option[0])
                    await asyncio.sleep(1)

            # Enter symbol
            symbol_field = await self.page.select("input[aria-label='Symbol']")
            if symbol_field:
                await self._human_type(symbol_field, stock)
                await symbol_field.send_keys(["Enter"])
                await asyncio.sleep(secrets.SystemRandom().uniform(0.8, 1.4))

            # Select action (Buy/Sell)
            action_dropdown = await self.page.select(".eq-ticket-action-label")
            if action_dropdown:
                await self._mouse_click(action_dropdown)
                action_option = await self.page.select(f"option[value='{action}']")
                if action_option:
                    await self._mouse_click(action_option)

            # Enter quantity
            qty_field = await self.page.select("input[aria-label='Quantity']")
            if qty_field:
                await self._human_type(qty_field, str(quantity))

            # Set order type
            if limit_price:
                order_type = await self._find_button("Limit")
                if order_type:
                    await self._mouse_click(order_type)

                    price_field = await self.page.select("input[aria-label='Limit Price']")
                    if price_field:
                        await self._human_type(price_field, str(limit_price))

            # Review order (or place if dry)
            if self.debug:
                await self.debug_screenshot("trade_form_filled")

            if dry:
                preview_btn = await self._find_button("Preview Order")
                if preview_btn:
                    await self._mouse_click(preview_btn)
                    await asyncio.sleep(1)
                    if self.debug:
                        print(f"[+] Test order preview: {action} {quantity} {stock}")
                    return (True, None)
            else:
                submit_btn = await self._find_button("Submit Order")
                if submit_btn:
                    await self._mouse_click(submit_btn)
                    await asyncio.sleep(2)
                    if self.debug:
                        print(f"[+] Order submitted: {action} {quantity} {stock}")
                        await self.debug_screenshot("trade_submitted")
                    return (True, None)

            if self.debug:
                await self.debug_screenshot("trade_button_missing")
                await self.debug_dump_dom("trade_button_missing")
            return (False, "Could not find submit/preview button")

        except Exception as e:
            error_msg = f"Transaction error: {e!s}"
            print(error_msg)
            traceback.print_exc()
            return (False, error_msg)

    async def transfer_acc_to_acc(
        self,
        source_account: str,
        destination_account: str,
        transfer_amount: float,
    ) -> bool:
        """Transfer funds between two accounts.

        Args:
            source_account: Source account number.
            destination_account: Destination account number.
            transfer_amount: Amount to transfer.

        Returns:
            True if successful.

        """
        try:
            # Navigate to transfer page
            await self.navigate("https://digital.fidelity.com/ftgw/digital/transfer/?quicktransfer=cash-shares")
            await asyncio.sleep(2)

            # Select source account
            from_select = await self.page.select("select[aria-label='From']")
            if from_select:
                await from_select.apply(f"(el) => {{ el.value = '{source_account}'; el.dispatchEvent(new Event('change')); }}")
                await asyncio.sleep(1)

            # Select destination account
            to_select = await self.page.select("select[aria-label='To']")
            if to_select:
                await to_select.apply(f"(el) => {{ el.value = '{destination_account}'; el.dispatchEvent(new Event('change')); }}")
                await asyncio.sleep(1)

            # Enter amount
            amount_field = await self.page.select("input[aria-label*='Amount']")
            if amount_field:
                await self._human_type(amount_field, str(transfer_amount))

            # Click transfer button
            transfer_btn = await self._find_button("Transfer")
            if transfer_btn:
                await self._mouse_click(transfer_btn)
                await asyncio.sleep(2)
                if self.debug:
                    print(f"[+] Transferred funds from {self._mask(source_account)} to {self._mask(destination_account)}")
                    await self.debug_screenshot("transfer_completed")
                return True

            if self.debug:
                await self.debug_screenshot("transfer_failed")
                await self.debug_dump_dom("transfer_failed")
            return False

        except Exception as e:
            print(f"Transfer error: {e}")
            traceback.print_exc()
            return False

    async def enable_pennystock_trading(self, account: str) -> bool:
        """Enable penny stock trading for an account.

        Args:
            account: Account number.

        Returns:
            True if successful.

        """
        try:
            # Navigate to account settings
            await self.navigate("https://digital.fidelity.com/ftgw/digital/settings/account")
            await asyncio.sleep(2)

            # Look for penny stock trading option
            pennystock_checkbox = await self.page.select("input[aria-label*='penny']")
            if pennystock_checkbox:
                await self._mouse_click(pennystock_checkbox)
                await asyncio.sleep(1)

                # Confirm
                confirm_btn = await self._find_button("Enable")
                if confirm_btn:
                    await self._mouse_click(confirm_btn)
                    await asyncio.sleep(2)
                    if self.debug:
                        print(f"[+] Penny stock trading enabled for {self._mask(account)}")
                        await self.debug_screenshot("pennystock_enabled")
                    return True

            if self.debug:
                await self.debug_screenshot("pennystock_failed")
                await self.debug_dump_dom("pennystock_failed")
            return False

        except Exception as e:
            print(f"Pennystock enable error: {e}")
            traceback.print_exc()
            return False

    async def add_stock_to_account_dict(self, account_num: str, stock: dict, *, overwrite: bool = False) -> bool:
        """Add a stock to the account dict under an account. You can use/import `create_stock_dict` for help.

        Returns
        -------
        True
            If successful
        False
            If account doesn't yet exist in account_dict

        """
        if not validate_stocks([stock]):
            return False
        if account_num in self.account_dict:
            if overwrite:
                self.account_dict[account_num]["stocks"] = [stock]
                self.account_dict[account_num]["balance"] = round(stock["value"], 2)
            else:
                self.account_dict[account_num]["stocks"].append(stock)
                self.account_dict[account_num]["balance"] += round(stock["value"], 2)
            return True
        return False

    async def get_account_info(self) -> dict | None:
        """Get detailed information about all accounts including stocks held.

        Must be called to populate account_dict with stock information.

        Returns:
            Updated account_dict or None if an error occurs.

        Raises:
            Exception: If required CSV fields are missing or other processing errors occur.

        """
        try:
            # Go to positions page
            await self.page.get("https://digital.fidelity.com/ftgw/digital/portfolio/positions")

            # Wait for loading to complete
            await self.wait_for_loading_sign()
            await asyncio.sleep(1)
            # Sometimes this can take a while to load. Set to 2.5 minutes
            await self.page.wait_for_ready_state("complete")

            # Download the positions as a csv
            cur = Path.cwd()
            await self.page.set_download_path(cur)

            # Record CSV files before click
            before_files = set(cur.glob("*.csv"))

            new_ui = True
            try:
                # Try new UI
                actions_btn = await self._find_button("Available Actions")
                if actions_btn:
                    await self._mouse_click(actions_btn)
                    await asyncio.sleep(0.5)
                    download_btn = await self._find_button("Download")
                    if download_btn:
                        await self._mouse_click(download_btn)
                    else:
                        new_ui = False
                else:
                    new_ui = False
            except Exception:
                new_ui = False

            if not new_ui:
                try:
                    # Use the old UI
                    download_btn = await self.page.select('*[aria-label="Download Positions"]', timeout=8)
                    if download_btn:
                        await self._mouse_click(download_btn)
                    else:
                        print("Could not get positions csv")
                        if self.debug:
                            await self.debug_screenshot("positions_failed")
                            await self.debug_dump_dom("positions_failed")
                        return None
                except Exception:
                    print("Could not get positions csv")
                    if self.debug:
                        await self.debug_screenshot("positions_failed")
                        await self.debug_dump_dom("positions_failed")
                    return None

            # Wait for downloaded CSV file
            positions_csv = None
            for _ in range(20):  # wait up to 10 seconds
                await asyncio.sleep(0.5)
                after_files = set(cur.glob("*.csv"))
                new_files = after_files - before_files
                if new_files:
                    positions_csv = next(iter(new_files))
                    break

            if not positions_csv or not positions_csv.exists():
                print("Could not get positions csv")
                if self.debug:
                    await self.debug_screenshot("positions_csv_missing")
                    await self.debug_dump_dom("positions_csv_missing")
                return None

            # Process the CSV file
            try:
                with Path.open(positions_csv, newline="", encoding="utf-8-sig") as csv_file:
                    reader = csv.DictReader(csv_file)

                    # Ensure all required fields are present
                    required_elements = [
                        "Account Number",
                        "Account Name",
                        "Symbol",
                        "Description",
                        "Quantity",
                        "Last Price",
                        "Last Price Change",
                        "Current Value",
                    ]

                    # Check if fieldnames exists (could be None for empty CSV)
                    if reader.fieldnames is None:
                        raise Exception("CSV file has no headers or is empty")

                    intersection_set = set(reader.fieldnames).intersection(set(required_elements))
                    if len(intersection_set) != len(required_elements):
                        raise Exception("Not enough elements in fidelity positions csv")

                    for row in reader:
                        # Skip empty rows
                        if row["Account Number"] is None:
                            continue
                        # Last couple of rows have disclaimers, filter those out
                        if "and" in row["Account Number"]:
                            break
                        # Skip accounts that start with 'Y' (Fidelity managed)
                        if row["Account Number"][0] == "Y":
                            continue

                        # Get the value and remove '$' from it
                        cur_val = str(row["Current Value"]).replace("$", "").replace("-", "")
                        # Get the last price
                        last_price = str(row["Last Price"]).replace("$", "").replace("-", "")
                        # Get the last price change
                        last_price_change = str(row["Last Price Change"]).replace("$", "")
                        # Get quantity
                        quantity = str(row["Quantity"]).replace("-", "")
                        # Get ticker
                        ticker = str(row["Symbol"])

                        # Catch any pending activity with special handling
                        if "Pending" in ticker:
                            cur_val = last_price_change
                        # If the value isn't present, move to next row
                        if len(cur_val) == 0:
                            continue
                        # If the last price isn't available, just use the current value
                        if len(last_price) == 0:
                            last_price = cur_val
                        # If the quantity is missing set it to 1 (For SPAXX or any other cash position)
                        if len(quantity) == 0:
                            quantity = 1

                        # Check for anything that isn't a number
                        try:
                            float(cur_val)
                        except ValueError:
                            cur_val = 0
                        try:
                            float(last_price)
                        except ValueError:
                            last_price = 0
                        try:
                            float(quantity)
                        except ValueError:
                            quantity = 0

                        # Create stock dictionary
                        stock_dict = {
                            "ticker": ticker,
                            "quantity": float(quantity),
                            "last_price": float(last_price),
                            "value": float(cur_val),
                        }

                        # Try setting in the account dict without overwrite
                        if not self.set_account_dict(
                            account_num=row["Account Number"],
                            nickname=row["Account Name"],
                            withdrawal_balance=0.0,
                        ):
                            # Account exists, just add the stock
                            self.add_stock_to_account_dict(row["Account Number"], stock_dict)
                        else:
                            # New account, add the stock to it
                            self.add_stock_to_account_dict(row["Account Number"], stock_dict, overwrite=True)
            finally:
                # Clean up - delete the CSV file immediately
                if positions_csv and positions_csv.exists():
                    positions_csv.unlink()

            return self.account_dict

        except Exception as e:
            print(f"Error in get_account_info: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return None

    async def open_account(self, account_type: str) -> bool:
        """Open a new Fidelity account (roth or brokerage).

        Note: Use login(save_device=False) when logging in for better compatibility with this function.

        Args:
            account_type: Either 'roth' or 'brokerage'.

        Returns:
            True if successful. For roth accounts, the number is stored in self.new_account_number.

        """
        try:
            if account_type.lower() == "roth":
                await self.navigate("https://digital.fidelity.com/ftgw/digital/aox/RothIRAccountOpening/PersonalInformation")
                await self.wait_for_loading_sign()

                # Click open account button
                open_btn = await self._find_button("Open account")
                if open_btn:
                    await self._mouse_click(open_btn)
                    await asyncio.sleep(3)

                # Wait for congratulations message
                await asyncio.sleep(2)

                # Try to get account number from heading
                congrats = await self.page.find_element_by_text("Congratulations", best_match=True)
                if congrats:
                    if self.debug:
                        print("[+] Roth account opened successfully")
                    return True

            elif account_type.lower() == "brokerage":
                # Get old account list
                old_accounts = await self.get_list_of_accounts(set_flag=False)

                await self.navigate("https://digital.fidelity.com/ftgw/digital/aox/BrokerageAccountOpening/JointSelectionPage")
                await self.wait_for_loading_sign()

                # Click through the setup
                for _ in range(3):
                    next_btn = await self._find_button("Next")
                    if next_btn:
                        await self._mouse_click(next_btn)
                        await asyncio.sleep(1)

                # Open account
                open_btn = await self._find_button("Open account")
                if open_btn:
                    await self._mouse_click(open_btn)
                    await asyncio.sleep(5)

                # Get new account list and compare
                new_accounts = await self.get_list_of_accounts(set_flag=False)
                for new_acc in new_accounts:
                    if new_acc not in old_accounts:
                        self.new_account_number = new_acc
                        if self.debug:
                            print(f"[+] Brokerage account opened: {self._mask(new_acc)}")
                        return True

            return False

        except Exception as e:
            print(f"Error opening account: {e}")
            traceback.print_exc()
            return False

    async def download_statements(self, date: str) -> list[str] | None:
        """Download account statements for a specific month.

        Args:
            date: Format: 'YYYY/MM' e.g. '2024/01'.

        Returns:
            List of file paths downloaded, or None if error.

        """
        try:
            # Parse date
            parts = date.split("/")
            if len(parts) != 2:
                print("Date format must be YYYY/MM")
                return None

            target_year = int(parts[0])
            target_month = int(parts[1])

            # Get month name
            month_name = FidMonths(target_month).name

            # Navigate to documents page
            await self.navigate("https://digital.fidelity.com/ftgw/digital/portfolio/documents/dochub")
            await asyncio.sleep(2)

            # Click date filter button
            date_btn = await self._find_button("Changing")
            if date_btn:
                await self._mouse_click(date_btn)
                await asyncio.sleep(1)

                # Select year
                year_option = await self.page.find_element_by_text(f"{target_year}", best_match=True)
                if year_option:
                    await self._mouse_click(year_option)
                    await asyncio.sleep(2)

            # Look for statements matching the month
            statement_rows = await self.page.select_all("tr")
            saved_files = []

            for row in statement_rows:
                try:
                    row_text = row.text_all or row.text
                    if month_name in row_text and str(target_year) in row_text:  # ruff: ignore[collapsible-if]
                        # Found a matching statement
                        # This would need to be enhanced to handle actual downloads
                        # For now just indicating the functionality
                        if self.debug:
                            print(f"[+] Found statement for {month_name}/{target_year}")
                except Exception as e:
                    print(f"Error processing statement row: {e}")
                    traceback.print_exc()
                    continue

            return saved_files or None

        except Exception as e:
            print(f"Error downloading statements: {e}")
            traceback.print_exc()
            return None

    async def nickname_account(self, account_number: str, nickname: str) -> bool:
        """Set or update the nickname for an account.

        Args:
            account_number: The account number to rename.
            nickname: The new nickname for the account.

        Returns:
            True if successful.

        """
        try:
            # Navigate to account settings
            await self.navigate("https://digital.fidelity.com/ftgw/digital/settings/account")
            await asyncio.sleep(2)

            # Find account in list
            account_elem = await self.page.select(f"div:has-text('{account_number}')")
            if account_elem:
                # Click to edit
                await self._mouse_click(account_elem)
                await asyncio.sleep(1)

                # Find nickname field
                nickname_field = await self.page.select("input[aria-label*='nickname'], input[aria-label*='Nickname']")
                if nickname_field:
                    await self._human_type(nickname_field, str(nickname), clear_first=True)
                    await asyncio.sleep(0.5)

                    # Save
                    save_btn = await self._find_button("Save")
                    if save_btn:
                        await self._mouse_click(save_btn)
                        await asyncio.sleep(2)

                        if self.debug:
                            print(f"[+] Account {self._mask(account_number)} renamed successfully")

                        # Update local dict
                        if account_number in self.account_dict:
                            self.account_dict[account_number]["nickname"] = nickname

                        return True

            return False

        except Exception as e:
            print(f"Error renaming account: {e}")
            traceback.print_exc()
            return False

    async def wait_for_loading_sign(self, timeout_ms: int = 15000) -> None:
        """Wait for loading spinners/indicators to disappear.

        Checks for common Fidelity loading indicators and waits for them to disappear.

        Args:
            timeout_ms: Timeout in milliseconds.

        """
        js_is_loading = """
        (() => {
            const loadingSelectors = [
                ".loading-spinner-mask-after",
                ".pvd-spinner__mask-inner",
                "pvd-loading-spinner",
                ".pvd3-spinner",
                ".pvd-loading-spinner__spinner"
            ];
            for (const sel of loadingSelectors) {
                const elems = document.querySelectorAll(sel);
                for (const el of elems) {
                    const style = window.getComputedStyle(el);
                    if (style.visibility !== 'hidden' && style.display !== 'none' && el.offsetWidth > 0) {
                        return true;
                    }
                }
            }
            return false;
        })()
        """
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        timeout_seconds = timeout_ms / 1000

        while loop.time() - start_time < timeout_seconds:
            try:
                is_loading = await self.page.evaluate(js_is_loading)
                if not is_loading:
                    break
            except Exception:
                break
            await asyncio.sleep(0.3)


def create_stock_dict(
    ticker: str,
    quantity: float,
    last_price: float,
    value: float,
    stock_list: list | None = None,
    ) -> dict:
    """Create a dictionary for a stock. Appends it to a list if provided.

    Args:
        ticker (str): The stock ticker symbol
        quantity (float): The quantity of shares
        last_price (float): The last price of the stock
        value (float): The total value of the stock holding
        stock_list (list, optional): If provided, the created stock dict is appended to this

    Returns:
        stock_dict (dict): The dictionary for the stock with given info

    """
    # Build the dict for the stock
    stock_dict = {
        "ticker": ticker,
        "quantity": quantity,
        "last_price": last_price,
        "value": value,
    }
    if stock_list is not None:
        stock_list.append(stock_dict)
    return stock_dict


def validate_stocks(stocks: list) -> bool:
    """Check a list of stocks (which are dictionaries) for valid fields.

    Args:
        stocks (list): List of stock dictionaries to validate

    Returns:
        bool: True if stocks are none or valid, False if fields are left empty or types are incorrect

    Raises:
        Exception: If fields are missing or types are incorrect

    """
    if stocks is not None:
        for stock in stocks:
            try:
                if (stock["ticker"] is None or
                    stock["quantity"] is None or
                    stock["last_price"] is None or
                    stock["value"] is None
                ):
                    raise Exception("Missing fields")
                if (type(stock["ticker"]) is not str or
                    type(stock["quantity"]) is not float or
                    type(stock["last_price"]) is not float or
                    type(stock["value"]) is not float
                ):
                    raise Exception("Incorrect types for entries")
            except Exception as e:
                print(f"Error in stocks list. {e}")
                print("Create list of dictionaries with the following fields populated to initialize with given list")
                print("ticker: str")
                print("quantity: float")
                print("last_price: float")
                print("value: float")
                return False
    return True
