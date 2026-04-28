#!/usr/bin/env python3
"""
One-time session capture for authenticated apps.
Launches a headed browser, lets you log in manually,
then saves the session state for use by dom_capture --session.

Usage:
  python3 capture_session.py --url https://app.example.com/login \
    --output docs/session.json
"""
import argparse
from playwright.sync_api import sync_playwright

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url",    required=True, help="Login page URL")
    parser.add_argument("--output", default="docs/session.json",
                        help="Where to save the session state")
    args = parser.parse_args()

    print(f"Opening browser at {args.url}")
    print("Log in manually, then press ENTER in this terminal to save session...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        page    = context.new_page()
        page.goto(args.url)

        input("\n>>> Logged in? Press ENTER to save session and close browser...")

        context.storage_state(path=args.output)
        print(f"✓ Session saved to {args.output}")
        browser.close()

if __name__ == "__main__":
    main()