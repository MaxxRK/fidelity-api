import asyncio
import csv
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
            browser_args.extend(["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--window-size=1920,1080"])
        elif self.headless:
            browser_args.extend(["--headless=new", "--window-size=1920,1080",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
            "--disable-site-isolation-trials",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
            "--disable-features=TranslateUI,VizDisplayCompositor",
            "--no-first-run",
            "--disable-default-apps",
            "--disable-extensions",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1920,1080"])
        else:
            browser_args.extend([
                "--start-maximized",
                "--disable-session-crashed-bubble",
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
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
            print("✓ Zendriver browser launched successfully")

    async def close_browser(self) -> None:
        """Close the browser and clean up resources."""
        if self.browser:
            await self.browser.stop()
            if self.debug:
                print("✓ Browser closed")

    async def debug_screenshot(self, name: str) -> None:
        """Take a screenshot of the current page and save it to the current directory.

        Only works if debug mode is enabled.

        Args:
            name: The name of the screenshot file.

        """
        if self.debug:
            screenshot_path = f"./fidelity_debug_{name}{self.title or ''}.png"
            # Take screenshot and save
            await self.page.screenshot(path=screenshot_path)
            print(f"✓ Screenshot saved to {screenshot_path}")

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
        """Use the transfers page's dropdown to obtain the list of accounts.

        Separate the account number and nickname and place them into `self.account_dict`.

        Args:
            set_flag: If True, `self.account_dict` will be updated (default: True).
            get_withdrawal_bal: If True, the function will provide the available balance per account (default: False).

        Returns:
            A dictionary of the account information using account numbers as keys.

        """
        try:
            # Navigate to transfers page
            await self.navigate("https://digital.fidelity.com/ftgw/digital/transfer/?quicktransfer=cash-shares")

            # Wait for the "From" dropdown to be available
            await asyncio.sleep(1)

            # pvd-select is a web component whose inner <select> lives in shadow DOM;
            # read options directly from the pvd-options JSON attribute instead
            pvd_select = await self.page.query_selector("pvd-select[pvd-id='From-acct-select']")
            if not pvd_select:
                print("Could not find 'From' dropdown")
                return self.account_dict

            options_json = await pvd_select.attr("pvd-options")
            if not options_json:
                print("Could not read options from 'From' dropdown")
                return self.account_dict

            # Flatten option groups into [{value, text}, ...]
            raw_options = json.loads(options_json)
            flat_options = []
            for item in raw_options:
                if "options" in item:
                    flat_options.extend(item["options"])
                elif item.get("value"):
                    flat_options.append(item)

            local_dict = {}

            for opt in flat_options:
                option_text = opt.get("text", "")
                option_value = opt.get("value", "")

                # Try to find accounts using regex
                account_number = re.search(r"(?<=\()(Z|\d)\d{6,}(?=\))", option_text)
                nickname = re.search(r"^.+?(?=\()", option_text)
                with_bal = None

                # Get withdrawal balance if requested
                if get_withdrawal_bal and account_number and nickname:
                    # Set value on inner <select> via shadow root and fire change event
                    await pvd_select.evaluate(f"""
                        (el) => {{
                            const s = el.shadowRoot
                                ? el.shadowRoot.querySelector('select')
                                : el.querySelector('select');
                            if (s) {{
                                s.value = '{option_value}';
                                s.dispatchEvent(new Event('change', {{bubbles: true}}));
                            }}
                        }}
                    """)

                    # Wait for balance to update
                    await asyncio.sleep(0.5)

                    # Find the balance element
                    balance_elem = await self.page.query_selector("tr.pvd-table__row:nth-child(2) > td:nth-child(2)")
                    if balance_elem:
                        bal_text = await balance_elem.text
                        with_bal = float(bal_text.replace("$", "").replace(",", ""))

                # Add to account dict
                if set_flag and account_number and nickname:
                    acc_num = account_number.group(0)
                    nick = nickname.group(0).strip()

                    if not self.set_account_dict(
                        account_num=acc_num,
                        nickname=nick,
                        withdrawal_balance=with_bal,
                    ):
                        local_dict[acc_num] = {
                            "nickname": nick,
                            "withdrawal_balance": with_bal,
                        }

            if self.debug:
                print(f"✓ Found {len(self.account_dict)} accounts")

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

            # Enter username
            username_field = await self.page.select("#dom-username-input")
            password_field = await self.page.select("#dom-pswd-input")

            if not username_field or not password_field:
                raise Exception("Could not find username or password fields.")

            for letter in r"" + username:
                await username_field.send_keys(letter)
                await self.page.sleep(secrets.SystemRandom().uniform(0.05, 0.50))

            for letter in password:
                await password_field.send_keys(letter)
                await self.page.sleep(secrets.SystemRandom().uniform(0.05, 0.50))

            # Click login button
            login_btn = await self.page.select("#dom-login-button")
            if login_btn:
                await login_btn.mouse_click()
            else:
                print("Could not find login button")
                return (False, False)

            # Wait for loading and navigation
            await self.page.wait_for_ready_state("complete")
            await self.page.wait()
            await self.page.sleep(4)

            # Check if we made it to summary
            current_url = self.page.url
            if "summary" in current_url:
                if self.debug:
                    print("✓ Login successful - at summary")
                return (True, True)

            # Check if we're at 2FA page
            if "signin" in current_url:
                await self.page.wait_for_ready_state("complete")

                # Handle TOTP if provided
                if totp_secret and totp_secret != "NA":
                    totp = pyotp.TOTP(totp_secret)
                    totp_code = totp.now()

                    # Look for authenticator code input
                    totp_field = await self.page.query_selector("input[placeholder='XXXXXX']")
                    if totp_field:
                        for number in totp_code:
                            await totp_field.send_keys(number)
                            await self.page.sleep(secrets.SystemRandom().uniform(0.05, 0.50))

                    # Check "don't ask again" if save_device is True
                    if save_device:
                        try:
                            dont_ask_label = await self.page.find_element_by_text("Don't ask me again", best_match=True)
                            if dont_ask_label:
                                await dont_ask_label.click()
                        except Exception as e:
                            print(f"Error handling 'Don't ask me again' and 'Remember this device': {e}")
                        try:
                            remember_checkbox = await self.page.find_element_by_text("Remember this device", best_match=True)
                            if remember_checkbox:
                                pos = await remember_checkbox.get_position()
                                if pos:
                                    # Click near left edge where the checkbox input sits, not the label center
                                    await self.page.mouse_click(pos.left + 5, pos.center[1])
                        except Exception as e:
                            print(f"Error handling 'Remember this device': {e}")

                    # Submit 2FA code
                    try:
                        continue_btn = await self.page.find_element_by_text("Continue", best_match=True)
                        if continue_btn:
                            await continue_btn.mouse_click()
                            await asyncio.sleep(3)
                            final_url = self.page.url
                            print(final_url)
                            input()
                            if "summary" in final_url:
                                print("here")
                                if self.debug:
                                    print("✓ TOTP login successful")
                                return (True, True)
                    except Exception as e:
                        print(f"Error submitting TOTP code: {e}")
                        traceback.print_exc()

                # If we need SMS code
                text_btn = await self.page.find_element_by_text("Text me the code", best_match=True)
                if text_btn:
                    await text_btn.mouse_click()
                    if self.debug:
                        print("✓ SMS code sent - waiting for login_2FA()")
                    return (True, False)

            print(f"Unexpected state at URL: {current_url}")
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
            code_field = await self.page.select("input[placeholder='XXXXXX']")
            if code_field:
                await code_field.send_keys(code)
            else:
                print("Could not find code input field")
                return False

            # Check "don't ask again" if requested
            if save_device:
                dont_ask = await self.page.find_element_by_text("Don't ask me again", best_match=True)
                if dont_ask:
                    await dont_ask.click()

            # Submit code
            submit_btn = await self._find_button("Continue")
            if submit_btn:
                await submit_btn.click()

            await self.page.wait_for_ready_state("complete")

            # Check if we made it to summary
            final_url = self.page.url
            if "summary" in final_url:
                if self.debug:
                    print("✓ 2FA login successful")
                return True

            print(f"Still at login page. URL: {final_url}")
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
                await account_dropdown.click()
                await asyncio.sleep(0.5)

                # Find and click account option
                # XPath can combine attribute and text conditions
                xpath_expr = f"//button[@role='option' and contains(text(), '{account.upper()}')]"
                account_option = await self.page.xpath(xpath_expr)
                if account_option:
                    account_option = account_option[0]  # xpath returns a list
                    await account_option.click()
                    await asyncio.sleep(1)

            # Enter symbol
            symbol_field = await self.page.select("input[aria-label='Symbol']")
            if symbol_field:
                await symbol_field.send_keys(stock)
                await symbol_field.send_keys(["Enter"])
                await asyncio.sleep(1)

            # Select action (Buy/Sell)
            action_dropdown = await self.page.select(".eq-ticket-action-label")
            if action_dropdown:
                await action_dropdown.click()
                action_option = await self.page.select(f"option[value='{action}']")
                if action_option:
                    await action_option.click()

            # Enter quantity
            qty_field = await self.page.select("input[aria-label='Quantity']")
            if qty_field:
                await qty_field.send_keys(str(quantity))

            # Set order type
            if limit_price:
                order_type = await self._find_button("Limit")
                if order_type:
                    await order_type.click()

                    price_field = await self.page.select("input[aria-label='Limit Price']")
                    if price_field:
                        await price_field.send_keys(str(limit_price))

            # Review order (or place if dry)
            if dry:
                # Just test the order
                preview_btn = await self._find_button("Preview Order")
                if preview_btn:
                    await preview_btn.click()
                    await asyncio.sleep(1)
                    if self.debug:
                        print(f"✓ Test order preview: {action} {quantity} {stock}")
                    return (True, None)
            else:
                # Place real order
                submit_btn = await self._find_button("Submit Order")
                if submit_btn:
                    await submit_btn.click()
                    await asyncio.sleep(2)
                    if self.debug:
                        print(f"✓ Order submitted: {action} {quantity} {stock}")
                    return (True, None)

            return (False, "Could not find submit button")

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
                await from_select.evaluate(f"el => el.value = '{source_account}'")
                await from_select.evaluate("el => el.dispatchEvent(new Event('change'))")
                await asyncio.sleep(1)

            # Select destination account
            to_select = await self.page.select("select[aria-label='To']")
            if to_select:
                await to_select.evaluate(f"el => el.value = '{destination_account}'")
                await to_select.evaluate("el => el.dispatchEvent(new Event('change'))")
                await asyncio.sleep(1)

            # Enter amount
            amount_field = await self.page.select("input[aria-label*='Amount']")
            if amount_field:
                await amount_field.send_keys(str(transfer_amount))

            # Click transfer button
            transfer_btn = await self._find_button("Transfer")
            if transfer_btn:
                await transfer_btn.click()
                await asyncio.sleep(2)
                if self.debug:
                    print(f"✓ Transferred ${transfer_amount} from {source_account} to {destination_account}")
                return True

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
                await pennystock_checkbox.click()
                await asyncio.sleep(1)

                # Confirm
                confirm_btn = await self._find_button("Enable")
                if confirm_btn:
                    await confirm_btn.click()
                    await asyncio.sleep(2)
                    if self.debug:
                        print(f"✓ Penny stock trading enabled for {account}")
                    return True

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
            # Check for new UI first
            new_ui = True
            try:
                # Try new UI
                actions_btn = await self._find_button("Available Actions")
                if actions_btn:
                    await actions_btn.click()
                    download_btn = await self._find_button("Download")
                    if download_btn:
                        # Start download and get the file
                        async with self.page.expect_download() as download_info:
                            await download_btn.click()
                        download = await download_info
                    else:
                        new_ui = False
                else:
                    new_ui = False
            except Exception:
                new_ui = False

            if not new_ui:
                try:
                    # Use the old UI
                    download_btn = await self.page.select('*[aria-label="Download Positions"]', timeout=8000)
                    if download_btn:
                        async with self.page.expect_download() as download_info:
                            await download_btn.click()
                        download = await download_info
                    else:
                        print("Could not get positions csv")
                        return None
                except Exception:
                    print("Could not get positions csv")
                    return None

            # Get absolute path to file
            cur = Path.cwd()
            positions_csv = cur / download.suggested_filename
            # Save the download
            await download.save_as(positions_csv)

            # Process the CSV file
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

            # Clean up - delete the CSV file
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
                    await open_btn.click()
                    await asyncio.sleep(3)

                # Wait for congratulations message
                await asyncio.sleep(2)

                # Try to get account number from heading
                congrats = await self.page.find_element_by_text("Congratulations", best_match=True)
                if congrats:
                    if self.debug:
                        print("✓ Roth account opened successfully")
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
                        await next_btn.click()
                        await asyncio.sleep(1)

                # Open account
                open_btn = await self._find_button("Open account")
                if open_btn:
                    await open_btn.click()
                    await asyncio.sleep(5)

                # Get new account list and compare
                new_accounts = await self.get_list_of_accounts(set_flag=False)
                for new_acc in new_accounts:
                    if new_acc not in old_accounts:
                        self.new_account_number = new_acc
                        if self.debug:
                            print(f"✓ Brokerage account opened: {new_acc}")
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
                await date_btn.click()
                await asyncio.sleep(1)

                # Select year
                year_option = await self.page.find_element_by_text(f"{target_year}", best_match=True)
                if year_option:
                    await year_option.click()
                    await asyncio.sleep(2)

            # Look for statements matching the month
            statement_rows = await self.page.select_all("tr")
            saved_files = []

            for row in statement_rows:
                try:
                    row_text = await row.text
                    if month_name in row_text and str(target_year) in row_text:  # ruff: ignore[collapsible-if]
                        # Found a matching statement
                        # This would need to be enhanced to handle actual downloads
                        # For now just indicating the functionality
                        if self.debug:
                            print(f"✓ Found statement for {month_name}/{target_year}")
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
                await account_elem.click()
                await asyncio.sleep(1)

                # Find nickname field
                nickname_field = await self.page.select("input[aria-label*='nickname'], input[aria-label*='Nickname']")
                if nickname_field:
                    await nickname_field.send_keys(nickname)
                    await asyncio.sleep(0.5)

                    # Save
                    save_btn = await self._find_button("Save")
                    if save_btn:
                        await save_btn.click()
                        await asyncio.sleep(2)

                        if self.debug:
                            print(f"✓ Account {account_number} renamed to '{nickname}'")

                        # Update local dict
                        if account_number in self.account_dict:
                            self.account_dict[account_number]["nickname"] = nickname

                        return True

            return False

        except Exception as e:
            print(f"Error renaming account: {e}")
            traceback.print_exc()
            return False

    async def wait_for_loading_sign(self, timeout_ms: int = 30000) -> None:
        """Wait for loading spinners/indicators to disappear.

        Checks for common Fidelity loading indicators and waits for them to disappear.

        Args:
            timeout_ms: Timeout in milliseconds (not strictly enforced, best effort).

        """
        loading_selectors = [
            ".loading-spinner-mask-after",
            ".pvd-spinner__mask-inner",
            "pvd-loading-spinner",
            ".pvd3-spinner",
        ]

        loop = asyncio.get_running_loop()
        start_time = loop.time()
        timeout_seconds = timeout_ms / 1000

        for selector in loading_selectors:
            while loop.time() - start_time < timeout_seconds:
                # Check if element exists
                element = await self.page.query_selector(selector)
                if not element:
                    break
                # Check if element is hidden
                visibility = await element.evaluate("el => window.getComputedStyle(el).visibility")
                if visibility == "hidden":
                    break
                await asyncio.sleep(0.5)


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
