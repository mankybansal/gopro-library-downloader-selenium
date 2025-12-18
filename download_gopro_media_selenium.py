#!/usr/bin/env python3
"""
Selenium-assisted GoPro cloud downloader: open a browser, let the user log in,
extract session cookies, then page through the API and download all media.

Usage:
  python download_gopro_media_selenium.py --out ./gopro_media --per-page 100 --concurrency 5

Requirements:
* selenium installed: `python -m pip install selenium`.
* Chrome/Chromium + matching chromedriver on PATH, or pass --driver-path.
* Network access to GoPro; you will complete login manually in the opened browser.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import InvalidElementStateException, TimeoutException

API_URL = "https://api.gopro.com/media/search"
LOGIN_URL = "https://gopro.com/login"
MEDIA_LIBRARY_URL = "https://gopro.com/media-library"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download all GoPro cloud media via Selenium login.")
    parser.add_argument(
        "--out",
        default="gopro_media",
        help="Directory to save downloads (default: %(default)s).",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=100,
        help="Items per page to request (max API permits is typically 100).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional safety cap on pages to fetch.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Parallel downloads (default: %(default)s).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be downloaded without saving files.",
    )
    parser.add_argument(
        "--driver-path",
        default=None,
        help="Path to chromedriver if not on PATH.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless (only if your login flow permits).",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("GOPRO_EMAIL"),
        help="GoPro account email (env GOPRO_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("GOPRO_PASSWORD"),
        help="GoPro account password (env GOPRO_PASSWORD).",
    )
    parser.add_argument(
        "--login-wait",
        type=int,
        default=120,
        help="Seconds to wait for automated login to complete.",
    )
    parser.add_argument(
        "--ui-download",
        action="store_true",
        help="Download via UI context menu instead of API.",
    )
    parser.add_argument(
        "--media-url",
        default=os.environ.get("GOPRO_MEDIA_URL", MEDIA_LIBRARY_URL),
        help="Media library URL to open for UI downloads.",
    )
    parser.add_argument(
        "--media-count",
        type=int,
        default=202,
        help="Number of media tiles to attempt right-click download for (default: %(default)s).",
    )
    parser.add_argument(
        "--ui-wait",
        type=int,
        default=15,
        help="Seconds to wait for UI elements like media tiles and context menu items.",
    )
    parser.add_argument(
        "--post-click-wait",
        type=float,
        default=1.5,
        help="Seconds to pause after clicking Original quality to allow download start.",
    )
    parser.add_argument(
        "--auto-exit-after-ui",
        action="store_true",
        help="Automatically close the browser after UI download flow (default waits for user to close).",
    )
    parser.add_argument(
        "--ui-batch-size",
        type=int,
        default=25,
        help="Number of tiles to process before pausing (default: %(default)s).",
    )
    parser.add_argument(
        "--ui-start-index",
        type=int,
        default=1,
        help="1-based index of the first tile to process (default: %(default)s).",
    )
    parser.add_argument(
        "--ui-batch-wait",
        type=int,
        default=300,
        help="Seconds to wait between batches (default: %(default)s).",
    )
    return parser.parse_args()


def build_driver(args: argparse.Namespace) -> webdriver.Chrome:
    chrome_options = Options()
    if args.headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-dev-shm-usage")
    download_dir = Path(getattr(args, "out", "gopro_media")).resolve()
    prefs = {
        "download.default_directory": str(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    if args.driver_path:
        driver = webdriver.Chrome(args.driver_path, options=chrome_options)
    else:
        driver = webdriver.Chrome(options=chrome_options)
    return driver


def _fill_if_present(driver: webdriver.Chrome, selectors: List[Tuple[By, str]], value: str) -> bool:
    for by, selector in selectors:
        elems = driver.find_elements(by, selector)
        if elems:
            elem = elems[0]
            try:
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((by, selector)))
                elem.clear()
                elem.send_keys(value)
            except InvalidElementStateException:
                # Fall back to JS set in case of masked/readonly inputs.
                driver.execute_script(
                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('input', {bubbles: true}));",
                    elem,
                    value,
                )
            return True
    return False


def _click_first(driver: webdriver.Chrome, selectors: List[Tuple[By, str]]) -> bool:
    for by, selector in selectors:
        elems = driver.find_elements(by, selector)
        if elems:
            elems[0].click()
            return True
    return False


def _click_first_button_with_text(driver: webdriver.Chrome, substrings: List[str]) -> bool:
    buttons = driver.find_elements(By.TAG_NAME, "button")
    for b in buttons:
        txt = (b.text or "").lower()
        if any(s in txt for s in substrings):
            b.click()
            return True
    return False


def _submit_first_form(driver: webdriver.Chrome) -> bool:
    forms = driver.find_elements(By.CSS_SELECTOR, "form")
    if not forms:
        return False
    driver.execute_script("arguments[0].submit();", forms[0])
    return True


def _extract_token_from_cookies(cookie_header: str) -> Optional[str]:
    for part in cookie_header.split(";"):
        if "=" in part:
            name, value = part.strip().split("=", 1)
            if name == "gp_access_token":
                return value
    return None


def _wait_and_click(driver: webdriver.Chrome, selectors: List[Tuple[By, str]], timeout: int) -> bool:
    """
    Try selectors in order; wait until clickable; click the first match.
    """
    for by, sel in selectors:
        try:
            elem = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, sel))
            )
            elem.click()
            return True
        except TimeoutException:
            continue
    return False


def download_media_via_ui(driver: webdriver.Chrome, args: argparse.Namespace) -> None:
    """
    Right-click each media tile and click the "Original quality" item to trigger download.
    """
    driver.get(args.media_url)
    print("Manually adjust the page (sort/filter, etc.) then press Enter here to start downloads...")
    try:
        input()
    except KeyboardInterrupt:
        pass

    try:
        all_root = WebDriverWait(driver, args.ui_wait).until(
            EC.presence_of_element_located((By.ID, "all"))
        )
        container = all_root.find_element(By.XPATH, "./div")
    except Exception:
        print('Could not find media container beneath element with id="all".')
        return

    tiles_all = container.find_elements(By.XPATH, "./*")
    print(f"Found {len(tiles_all)} tiles inside the media container.")
    if not tiles_all:
        return

    start_idx = max(1, args.ui_start_index)
    tiles = tiles_all[start_idx - 1 :]
    print(f"Processing tiles starting from index {start_idx}. Count this run: {len(tiles)}")

    menu_item_locators = [
        (By.CSS_SELECTOR, ".Options_subMenuItem__aMIPC"),
        (
            By.XPATH,
            "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'original') or contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'download')]",
        ),
    ]

    batch_size = max(1, args.ui_batch_size)
    for idx, elem in enumerate(tiles, start=start_idx):
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", elem)
        try:
            ActionChains(driver).move_to_element(elem).context_click(elem).perform()
        except Exception:  # noqa: BLE001
            try:
                ActionChains(driver).context_click(elem).perform()
            except Exception:
                # Fallback to JS-dispatched context menu.
                driver.execute_script(
                    "arguments[0].dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true}));",
                    elem,
                )

        clicked = False
        for loc in menu_item_locators:
            try:
                menu_item = WebDriverWait(driver, args.ui_wait).until(
                    EC.element_to_be_clickable(loc)
                )
                try:
                    menu_item.click()
                except Exception:  # noqa: BLE001
                    driver.execute_script("arguments[0].click();", menu_item)
                clicked = True
                break
            except TimeoutException:
                continue

        if clicked:
            print(f"[{idx}/{len(tiles_all)}] Clicked Original quality")
            time.sleep(args.post_click_wait)
        else:
            print(f"[{idx}/{len(tiles_all)}] Original quality/quantity not found; continuing.")

        if (idx - start_idx + 1) % batch_size == 0 and idx < len(tiles_all):
            print(f"Completed batch of {batch_size}. Waiting {args.ui_batch_wait}s or press Enter to continue now...")
            try:
                start_wait = time.time()
                while True:
                    remaining = args.ui_batch_wait - (time.time() - start_wait)
                    if remaining <= 0:
                        break
                    print(f"Press Enter to continue immediately (auto-continue in {int(remaining)}s)...", end="\r", flush=True)
                    try:
                        # Poll every second; break early if input is available.
                        time.sleep(1)
                        import select

                        if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:  # type: ignore[attr-defined]
                            sys.stdin.readline()
                            break
                    except KeyboardInterrupt:
                        break
                print("\nContinuing to next batch.")
            except KeyboardInterrupt:
                print("Batch wait interrupted; continuing immediately.")


def automated_login(driver: webdriver.Chrome, args: argparse.Namespace) -> None:
    if not args.email or not args.password:
        driver.quit()
        sys.exit("Missing credentials: set --email/--password or GOPRO_EMAIL/GOPRO_PASSWORD.")

    print(f"Opening {LOGIN_URL} and attempting automated login...")
    driver.get(LOGIN_URL)

    email_selectors: List[Tuple[By, str]] = [
        (By.XPATH, "/html/body/div[1]/div[2]/div[2]/div[1]/div[2]/div/div[1]/div[1]/input"),
        (By.CSS_SELECTOR, "input[type='email']"),
        (By.NAME, "email"),
        (By.ID, "email"),
        (By.CSS_SELECTOR, "input[name='username']"),
    ]
    password_selectors: List[Tuple[By, str]] = [
        (By.XPATH, "/html/body/div[1]/div[2]/div[2]/div[1]/div[3]/div/div[1]/div[1]/input"),
        (By.CSS_SELECTOR, "input[type='password']"),
        (By.NAME, "password"),
        (By.ID, "password"),
    ]
    email_flow_buttons: List[Tuple[By, str]] = [
        (By.XPATH, "//button[contains(., 'Email') or contains(., 'email')]"),
        (By.XPATH, "//div[contains(., 'Email') and @role='button']"),
        (By.CSS_SELECTOR, "button[data-testid*='email'], button[data-test-id*='email']"),
    ]
    submit_selectors: List[Tuple[By, str]] = [
        (By.CSS_SELECTOR, "button[type='submit']"),
        (By.CSS_SELECTOR, "button[data-testid='login-submit'], button[data-test-id='login-submit']"),
        (By.XPATH, "//button[contains(., 'Log in') or contains(., 'Sign in') or contains(., 'Log In') or contains(., 'Sign In')]"),
        (By.XPATH, "//button[contains(., 'LOGIN')]"),
        (By.CSS_SELECTOR, "input[type='submit']"),
        (By.XPATH, "/html/body/div[1]/div[2]/div[2]/div[1]/button"),
        (By.CSS_SELECTOR, "button.btn-primary, button.primary"),
    ]
    cookie_acceptors: List[Tuple[By, str]] = []

    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "form, input, button"))
        )
    except Exception:  # noqa: BLE001
        driver.quit()
        sys.exit("Login page did not load.")

    if cookie_acceptors:
        _click_first(driver, cookie_acceptors)

    _click_first(driver, email_flow_buttons)

    filled_email = _fill_if_present(driver, email_selectors, args.email)
    filled_password = _fill_if_present(driver, password_selectors, args.password)
    if not (filled_email and filled_password):
        driver.quit()
        sys.exit("Could not find email/password fields; login automation failed.")

    clicked = _click_first(driver, submit_selectors)
    if not clicked:
        # Fallback: click the first submit-type control we can find.
        submit_buttons = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        if submit_buttons:
            try:
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")))
                submit_buttons[0].click()
                clicked = True
            except Exception:  # noqa: BLE001
                pass
    if not clicked:
        clicked = _click_first_button_with_text(driver, ["login", "sign in", "log in"])
    if not clicked:
        if not _submit_first_form(driver):
            print("Debug: current URL", driver.current_url)
            print("Debug: found buttons", len(driver.find_elements(By.TAG_NAME, "button")))
            print("Debug: found inputs", len(driver.find_elements(By.TAG_NAME, "input")))
            driver.quit()
            sys.exit("Could not find login submit button; login automation failed.")

    try:
        WebDriverWait(driver, args.login_wait).until(
            EC.any_of(
                EC.url_changes(LOGIN_URL),
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-test-id='user-menu'], .user-menu")),
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid='user-menu']")),
            )
        )
    except Exception:  # noqa: BLE001
        driver.quit()
        sys.exit("Automated login did not complete before timeout.")

    time.sleep(3)


def obtain_cookies_via_selenium(args: argparse.Namespace) -> Tuple[str, Optional[str]]:
    driver = build_driver(args)
    driver.set_page_load_timeout(60)
    automated_login(driver, args)

    cookies = driver.get_cookies()
    driver.quit()

    if not cookies:
        sys.exit("No cookies found after login; cannot proceed.")

    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    token = next((c["value"] for c in cookies if c.get("name") == "gp_access_token"), None)
    print(f"Captured {len(cookies)} cookies.")
    if token:
        print("Found gp_access_token cookie; will send as Bearer token.")
    return cookie_header, token


def fetch_page(
    session: requests.Session,
    cookie_header: str,
    page: int,
    per_page: int,
) -> List[Dict]:
    headers = {
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://gopro.com/",
        "Cookie": cookie_header,
    }
    token = _extract_token_from_cookies(cookie_header)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {
        "page": page,
        "per_page": per_page,
        "order_by": "captured_at",
    }
    resp = session.get(API_URL, params=params, headers=headers, timeout=30)

    if resp.status_code == 429:
        wait_for = int(resp.headers.get("Retry-After", "5"))
        print(f"Hit rate limit, sleeping {wait_for}s…", flush=True)
        time.sleep(wait_for)
        resp = session.get(API_URL, params=params, headers=headers, timeout=30)

    resp.raise_for_status()
    data = resp.json() or {}

    for key in ("media", "data", "results", "items"):
        if key in data and isinstance(data[key], list):
            return data[key]

    if "response" in data and isinstance(data["response"], dict):
        for key in ("media", "data", "results", "items"):
            if key in data["response"] and isinstance(data["response"][key], list):
                return data["response"][key]

    return []


def pick_filename(url: str, media: Dict) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    if name:
        return name
    return f"{media.get('id', int(time.time()))}"


def extract_downloads(media: Dict) -> List[Tuple[str, str]]:
    downloads: List[Tuple[str, str]] = []

    def add_candidate(u: Optional[str]) -> None:
        if u:
            downloads.append((u, pick_filename(u, media)))

    for key in ("download_url", "downloadUrl", "url"):
        add_candidate(media.get(key))

    files: Iterable[Dict] = media.get("files") or media.get("media_files") or []
    for file_entry in files:
        url = (
            file_entry.get("url")
            or file_entry.get("download_url")
            or file_entry.get("downloadUrl")
        )
        add_candidate(url)

    versions = media.get("versions") or media.get("derived_media") or {}
    if isinstance(versions, dict):
        for variant in versions.values():
            if isinstance(variant, dict):
                add_candidate(
                    variant.get("url") or variant.get("download_url") or variant.get("downloadUrl")
                )
            elif isinstance(variant, list):
                for entry in variant:
                    if isinstance(entry, dict):
                        add_candidate(
                            entry.get("url")
                            or entry.get("download_url")
                            or entry.get("downloadUrl")
                        )

    seen = set()
    unique: List[Tuple[str, str]] = []
    for url, fname in downloads:
        if url not in seen:
            unique.append((url, fname))
            seen.add(url)
    return unique


def download_one(session: requests.Session, url: str, dest: Path, cookie_header: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"Skipping (exists): {dest}")
        return

    headers = {"Cookie": cookie_header}
    token = _extract_token_from_cookies(cookie_header)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with session.get(url, stream=True, timeout=120, headers=headers) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
    print(f"Downloaded: {dest}")


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.ui_download:
        driver = build_driver(args)
        driver.set_page_load_timeout(60)
        automated_login(driver, args)
        download_media_via_ui(driver, args)
        if args.auto_exit_after_ui:
            driver.quit()
            print("UI-driven downloads triggered. Browser closed (auto-exit enabled).")
        else:
            print("UI-driven downloads triggered. Browser will remain open; close it manually when downloads finish.")
            try:
                input("Press Enter here after you close the browser to exit... ")
            except KeyboardInterrupt:
                pass
            try:
                driver.quit()
            except Exception:
                pass
        return

    cookie_header, _token = obtain_cookies_via_selenium(args)
    session = requests.Session()

    page = 1
    download_jobs: List[Tuple[str, Path]] = []

    while True:
        if args.max_pages and page > args.max_pages:
            print("Reached max-pages limit, stopping.")
            break

        try:
            media_page = fetch_page(session, cookie_header, page, args.per_page)
        except requests.HTTPError as exc:  # noqa: BLE001
            print(f"API fetch failed ({exc}); try rerunning with --ui-download to fetch via browser UI.")
            return
        if not media_page:
            break

        print(f"Fetched page {page} with {len(media_page)} items.")
        for media in media_page:
            downloads = extract_downloads(media)
            if not downloads:
                print(f"No download URL found for media id={media.get('id')}, skipping.")
                continue

            url, fname = downloads[0]
            dest = out_dir / fname
            download_jobs.append((url, dest))

        page += 1

    if not download_jobs:
        print("No downloadable media found.")
        return

    print(f"Queued {len(download_jobs)} downloads. Saving to {out_dir.resolve()}")

    if args.dry_run:
        for url, dest in download_jobs:
            print(f"[dry-run] {dest} <= {url}")
        return

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(download_one, session, url, dest, cookie_header): (url, dest)
            for url, dest in download_jobs
        }
        for fut in as_completed(futures):
            url, dest = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"Failed: {dest} ({url}): {exc}")

    print("Done.")


if __name__ == "__main__":
    main()
