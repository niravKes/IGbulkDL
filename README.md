# igdl

![igdl screenshot](https://i.imgur.com/zeTc3GH.png)

A desktop tool for batch-downloading Instagram posts - videos, reels, image posts, and carousels - from a plain list of URLs. Comes with a CLI, a tkinter GUI, and an HTML log viewer.

---

## Files

| File | Purpose |
|---|---|
| `ig_download.py` | Core download engine. Works standalone as a CLI. |
| `ig_gui.py` | Desktop GUI (tkinter). No extra dependencies. |
| `ig_dashboard.html` | Standalone HTML log viewer. Open directly in a browser. |
| `example.txt` | Example URL file format. |

---

## Requirements

- Python 3.8+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [gallery-dl](https://github.com/mikf/gallery-dl)

```
pip install -r requirements.txt
```

### Authentication (recommended)

Instagram rate-limits anonymous access heavily. Export your browser cookies and pass them to the tool - this significantly improves reliability.

Most browsers are supported via yt-dlp's `--cookies-from-browser` flag, or you can export a `cookies.txt` (Netscape format) using a browser extension such as [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).

---

## Usage

### GUI

```
python ig_gui.py
```

Fill in the URL file, collection name, and optionally a cookies file, then click **Start**.

### CLI

```
python ig_download.py <url-file> <collection-name> [options]
```

```
positional arguments:
  url_file              Path to a .txt file with one Instagram URL per line
  collection            Name for this collection (used as the log file name)

options:
  --cookies FILE        Path to a cookies.txt file
  --log FILE            Override the log file path (default: <collection>.json)
  --filename-template T Filename template for saved files (default: {shortcode})
  --retry-failed        Re-attempt URLs previously logged as failed
  --dry-run             Extract metadata only, do not download
```

**Example:**

```
python ig_download.py my-saves.txt design --cookies cookies.txt
```

---

## Features

- **Videos and reels** - downloaded via yt-dlp (best quality, merged to mp4)
- **Image posts** - downloaded via gallery-dl
- **Carousels** - yt-dlp handles mixed video/image carousels; falls back to gallery-dl for image-only carousels
- **Skip duplicates** - already-downloaded posts are skipped automatically (by log and by disk check)
- **Rate-limit protection** - waits and retries only after several consecutive rate-limited responses; isolated unavailable posts are logged and skipped without pausing
- **Retry failed** - re-run with `--retry-failed` to retry anything that previously failed
- **Custom filename templates** - control how files are named using variables like `{shortcode}`, `{author}`, `{date}`, `{upload_date}`, `{index}`
- **Live progress** - real-time output in both the terminal and the GUI
- **JSON log** - every download is logged with status, author, media type, file path, and error detail

---

## Filename templates

The `--filename-template` option (or the template field in the GUI) controls the output filename. Available variables:

| Variable | Description |
|---|---|
| `{shortcode}` | Instagram post shortcode (default) |
| `{author}` | Uploader username |
| `{title}` | Post title as reported by yt-dlp |
| `{upload_date}` | Original upload date (`YYYYMMDD`) |
| `{date}` | Today's date (`YYYY-MM-DD`) |
| `{index}` | Sequential position in the current run |
| `{index:04d}` | Zero-padded index (width 4) |

Examples:

```
{shortcode}                          →  DYFgyOEuIRN.mp4
{author}_{shortcode}                 →  natgeo_DYFgyOEuIRN.mp4
{date}_{index:04d}_{shortcode}       →  2026-05-18_0001_DYFgyOEuIRN.mp4
```

---

## Log viewer (dashboard)

Open `ig_dashboard.html` directly in a browser. Click **Load JSON** (or drag and drop) to load one or more log files. The dashboard shows download stats, a filterable/sortable table, and a video preview modal.

The log files are plain JSON (`<collection>.json`) written by `ig_download.py` to the same directory as the script. Multiple log files can be loaded and merged at once.

---

## Extensions

### IG Link Collector (Tampermonkey userscript)

A companion browser extension that scrolls through your Instagram saved collection and exports all post URLs as a plain text file - ready to feed directly into igdl.

**Install:** [IG Link Collector](https://github.com/doncezart/IGbulkCollector)

Requires [Tampermonkey](https://www.tampermonkey.net/). Once installed, navigate to your saved posts on Instagram. A small control panel appears in the corner. Hit **Start** and let it scroll; when done, click **Export** to download the URL list.

---

## Notes

- Downloads go to a `downloads/` folder next to the script by default.
- Instagram restricts access for non-authenticated requests. Using cookies is strongly recommended for any meaningful batch size.
- This tool is for personal archival use. Respect Instagram's terms of service and the rights of content creators.
