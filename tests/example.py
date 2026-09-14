import asyncio
import os

from fidelity import fidelity


async def main() -> None:
    username = os.environ.get("FIDELITY_USERNAME") or input("Username: ")
    password = os.environ.get("FIDELITY_PASSWORD") or input("Password: ")
    totp_secret = os.environ.get("FIDELITY_TOTP_SECRET") or ""

    browser = fidelity.FidelityAutomation(headless=False)
    await browser.get_driver()

    try:
        # Login
        step_1, step_2 = await browser.login(
            username=username,
            password=password,
            totp_secret=totp_secret,
            save_device=True,
        )

        if step_1 and step_2:
            print("Logged in")
        elif step_1 and not step_2:
            print("2FA code needed")
            code = input("Enter the code:\n")
            if await browser.login_2FA(code):
                print("Logged in")
            else:
                print("Browser not logged in")
                return
        else:
            print("Browser not logged in")
            return

        # Get accounts and holdings info
        account_info = await browser.get_account_info()
        accounts = list(account_info.keys()) if account_info else []

        # Print withdrawal balance from each account
        acc_dict = await browser.get_list_of_accounts(set_flag=True, get_withdrawal_bal=True)
        for account in acc_dict:
            print(f"{acc_dict[account]['nickname']}:  {account}: {acc_dict[account]['withdrawal_balance']}")

        if not accounts and acc_dict:
            accounts = list(acc_dict.keys())

        # Test the transaction
        if accounts:
            success, errormsg = await browser.transaction(
                stock="INTC",
                quantity=1,
                action="buy",
                account=accounts[0],
                dry=True,
            )
            if success:
                print("Successfully tested transaction")
            else:
                print(errormsg)
        else:
            print("No accounts found for transaction test")

    finally:
        await browser.close_browser()


if __name__ == "__main__":
    asyncio.run(main())