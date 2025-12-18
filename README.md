# GoPro Cloud Media Downloader (Selenium-assisted)

This script automates GoPro cloud downloads by logging in via Selenium, then right-clicking each media tile and choosing the download option.

## Setup
- Python 3.9+ with a virtualenv recommended.
- Install deps: `python -m pip install -r requirements.txt` (or `pip install selenium requests`).
- Chrome/Chromium and matching chromedriver on PATH.
- Network access to GoPro.

## Credentials
Provide your GoPro credentials via env vars or flags:
- `GOPRO_EMAIL` and `GOPRO_PASSWORD`
- Or `--email you@example.com --password 'yourpass'`

## Key CLI flags (UI mode)
- `--ui-download` : enable the UI-based flow (right-click tiles, click download).
- `--media-url <url>` : page that shows your media grid.
- `--ui-start-index <n>` : 1-based tile index to start from (resume after partial runs).
- `--ui-batch-size <n>` : number of tiles per batch (default 25).
- `--ui-batch-wait <seconds>` : pause between batches (default 300); press Enter during the pause to continue immediately.
- `--post-click-wait <seconds>` : pause after each click to let the download start.
- `--auto-exit-after-ui` : close the browser when done; default is to leave it open.

Other useful flags:
- `--headless` : run Chrome headless (if the site allows).
- `--out <dir>` : download directory (default `gopro_media`).

## How the UI flow works
1) Opens the media URL.
2) Waits for you to press Enter in the terminal (so you can handle sort/filter/scroll manually). Before continuing, dismiss all banners/notifications, open **Sort → By file size** so all tiles live in one container, and scroll to the very bottom to load every page of tiles.
3) Finds the media container at `#all > div` and takes its direct children as tiles.
4) Iterates tiles (respecting `--ui-start-index`), context-clicks each, and clicks:
   - `.Options_subMenuItem__aMIPC`, or
   - any clickable element containing “original” or “download” (case-insensitive).
5) Processes in batches; waits between batches with an option to press Enter to continue early.
6) Keeps the browser open by default so downloads can finish.

## Tips
- If your GoPro account signs in via Gmail/Google and you need a password, use “Forgot password” in the login flow and set a standalone password from the reset link, then rerun with that password.
- You can resume from a previous run with `--ui-start-index` (e.g., start at 76 if the first 75 succeeded).
- If headless is blocked, omit `--headless`.
- Manually scroll to the very bottom so pagination finishes and all files load; the CLI will show how many elements it found.
- Adjust `--ui-batch-size`, `--ui-batch-wait`, and `--post-click-wait` to reduce crashes or throttling.
- If the menu labels change, update the selectors in `download_media_via_ui` accordingly.
