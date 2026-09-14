import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path if running directly from tests directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fidelity import fidelity

async def main() -> None:
    username = os.environ.get("FIDELITY_USERNAME") or input("Username: ")
    password = os.environ.get("FIDELITY_PASSWORD") or input("Password: ")
    totp_secret = os.environ.get("FIDELITY_TOTP_SECRET") or input("TOTP Secret (optional): ")

    browser = fidelity.FidelityAutomation(headless=False, debug=True)
    await browser.get_driver()

    try:
        # Step 1: Login
        step_1, step_2 = await browser.login(
            username=username,
            password=password,
            totp_secret=totp_secret,
            save_device=True,
        )

        if step_1 and step_2:
            print("logged in!")
        elif step_1 and not step_2:
            print("2FA code needed (SMS)")
            code = input("Enter the SMS code: ")
            if await browser.login_2FA(code):
                print("logged in!")
            else:
                print("2FA login failed")
                return
        else:
            print("Login failed")
            return

        # Step 2: Fetch Account List and Withdrawal Balances
        acc_dict = await browser.get_list_of_accounts(set_flag=True, get_withdrawal_bal=True)

        # Step 3: Fetch Detailed Account Info (positions and owned stocks)
        account_info = await browser.get_account_info()
        accounts_to_process = account_info if account_info else acc_dict

        if not accounts_to_process:
            print("No accounts found.")
            return

        # Step 4: Print Account Info (Stocks Owned and Balances)
        print("\n" + "=" * 60)
        print("                 ACCOUNT INFO & BALANCES")
        print("=" * 60)
        for account_num, info in accounts_to_process.items():
            nickname = info.get("nickname", "Account")
            withdrawal_bal = info.get("withdrawal_balance") or acc_dict.get(account_num, {}).get("withdrawal_balance", "N/A")
            total_bal = info.get("balance", "N/A")

            print(f"\nAccount: {nickname} ({account_num})")
            print(f"  Withdrawal Balance : ${withdrawal_bal}")
            if total_bal != "N/A":
                bal_str = f"${total_bal:,.2f}" if isinstance(total_bal, (int, float)) else f"${total_bal}"
                print(f"  Total Balance      : {bal_str}")

            stocks = info.get("stocks", [])
            if stocks:
                print(f"  Owned Stocks ({len(stocks)} position(s)):")
                for s in stocks:
                    ticker = s.get("ticker", "N/A")
                    qty = s.get("quantity", 0)
                    last_price = s.get("last_price", 0.0)
                    val = s.get("value", 0.0)
                    print(f"    • {ticker:<8} | Shares: {qty:<8} | Price: ${last_price:<8.2f} | Value: ${val:,.2f}")
            else:
                print("  Owned Stocks       : None or 0 positions found")

        # Aggregate summary of holdings across all accounts
        try:
            holdings = await browser.summary_holdings()
            if holdings:
                print("\n--- Summary of All Holdings ---")
                for ticker, data in holdings.items():
                    qty = data.get("quantity", 0)
                    price = data.get("last_price", 0.0)
                    val = data.get("value", 0.0)
                    print(f"  • {ticker:<8} : {qty} shares @ ${price:.2f} = ${val:,.2f}")
        except Exception as err:
            print(f"Could not retrieve holdings summary: {err}")

        # Step 5: Interactive Buy / Sell Order on All Discovered Accounts
        print("\n" + "=" * 60)
        print("           TRADE EXECUTION (ALL DISCOVERED ACCOUNTS)")
        print("=" * 60)

        # Action input: buy or sell
        while True:
            action_input = input("\nSelect action (buy / sell / skip) [default: buy]: ").strip().lower()
            if not action_input:
                action = "buy"
                break
            if action_input in ["buy", "sell", "skip", "q", "quit", "exit"]:
                action = action_input
                break
            print("Invalid choice. Please enter 'buy', 'sell', or 'skip'.")

        if action in ["skip", "q", "quit", "exit"]:
            print("Skipping trade execution.")
            return

        # Stock ticker input
        while True:
            stock = input("Enter stock ticker symbol to trade (e.g. SPAXX, AAPL, INTC): ").strip().upper()
            if stock:
                break
            print("Ticker symbol cannot be empty.")

        # Quantity input
        while True:
            qty_input = input(f"Enter quantity of {stock} to {action} per account [default: 1]: ").strip()
            if not qty_input:
                quantity = 1.0
                break
            try:
                quantity = float(qty_input)
                if quantity <= 0:
                    print("Quantity must be greater than 0.")
                    continue
                break
            except ValueError:
                print("Invalid number. Please enter a valid quantity.")

        # Dry-run vs Live order prompt
        dry_input = input("Execute as dry-run preview only? [Y/n] (Y = preview only, n = LIVE ORDER): ").strip().lower()
        dry_run = dry_input != "n"

        print(f"\nOrder Summary:")
        print(f"  Action   : {action.upper()}")
        print(f"  Stock    : {stock}")
        print(f"  Quantity : {quantity} share(s) per account")
        print(f"  Mode     : {'DRY RUN (Preview Only)' if dry_run else 'LIVE ORDER'}")
        print(f"  Accounts : {len(accounts_to_process)} account(s)")

        confirm = input(f"Proceed with order on all {len(accounts_to_process)} account(s)? [Y/n]: ").strip().lower()
        if confirm == "n":
            print("Order execution cancelled.")
            return

        # Execute across every discovered account
        print("\n--- Executing Orders Across All Accounts ---")
        for account_num, info in accounts_to_process.items():
            nickname = info.get("nickname", "Account")
            print(f"\nProcessing {action.upper()} {quantity} share(s) of {stock} for {nickname} ({account_num})...")
            try:
                success, error = await browser.transaction(
                    stock=stock,
                    quantity=quantity,
                    action=action,
                    account=account_num,
                    dry=dry_run,
                )
                if success:
                    status_type = "Dry-run preview" if dry_run else "Order"
                    print(f"  [SUCCESS] {status_type} succeeded for {account_num}")
                else:
                    print(f"  [FAILED] {account_num}: {error}")
            except Exception as trade_err:
                print(f"  [ERROR] Exception executing transaction on {account_num}: {trade_err}")

    finally:
        await browser.close_browser()


if __name__ == "__main__":
    asyncio.run(main())
