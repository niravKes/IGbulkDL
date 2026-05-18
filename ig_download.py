#!/usr/bin/env python3
"""
ig_download.py — Download Instagram posts from a URL list using yt-dlp + gallery-dl.

Usage:
    python ig_download.py urls.txt art
    python ig_download.py urls.txt art --cookies cookies.txt
    python ig_download.py urls.txt art --dry-run
    python ig_download.py urls.txt art --retry-failed

Rate-limit protection: consecutive rate-limit responses trigger exponential backoff
(30s -> 60s -> 120s -> 300s) and the same URL is retried automatically.

Log deduplication: each URL has at most one entry — retries overwrite the previous
result rather than creating a second entry.

Caption: actual post caption is now stored alongside the synthetic title.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed.\nRun: pip install yt-dlp")

try:
    import gallery_dl
    import gallery_dl.config
    import gallery_dl.job
    _GALLERY_DL_AVAILABLE = True
except ImportError:
    _GALLERY_DL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RATE_LIMIT_DELAYS = [30, 60, 120, 300]
_MAX_CONSECUTIVE_RATE_LIMITS = len(_RATE_LIMIT_DELAYS) + 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[mGKHABCDFJsu]')

def strip_ansi(s):
    return _ANSI_RE.sub('', s or '').strip()


def load_urls(path):
    with open(path, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    seen = set()
    unique = []
    for url in lines:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def load_log(path):
    """Load log JSON, deduplicating so each URL appears only once (latest wins)."""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("items"), list):
                    seen = {}
                    for item in data["items"]:
                        seen[item.get("url", id(item))] = item
                    data["items"] = list(seen.values())
                    return data
            except json.JSONDecodeError:
                pass
    return {"items": [], "summary": {}}


def save_log(log, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def upsert_log_entry(log, result):
    """Insert or update a log entry keyed by URL. Prevents duplicates on retry runs."""
    url = result.get("url")
    for i, item in enumerate(log["items"]):
        if item.get("url") == url:
            log["items"][i] = result
            return
    log["items"].append(result)


def shortcode_from_url(url):
    m = re.search(r"/(?:p|reel)/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else "unknown"


def already_done(log, url):
    return any(item["url"] == url and item.get("status") == "ok" for item in log["items"])


def already_attempted(log, url):
    return any(item["url"] == url for item in log["items"])


def classify_error(msg):
    msg_lower = msg.lower()
    if "rate-limit reached" in msg_lower or "rate limit reached" in msg_lower:
        return "rate_limited"
    if "private" in msg_lower or "login" in msg_lower or "not available" in msg_lower or "age" in msg_lower:
        return "private_or_restricted"
    if "does not exist" in msg_lower or "sorry" in msg_lower or "removed" in msg_lower or "deleted" in msg_lower:
        return "deleted_or_not_found"
    if "unsupported url" in msg_lower:
        return "unsupported_url"
    if "no video" in msg_lower or "image" in msg_lower or "photo" in msg_lower or "no formats" in msg_lower:
        return "image_only"
    if "network" in msg_lower or "connect" in msg_lower or "timed out" in msg_lower or "http error" in msg_lower:
        return "network_error"
    return "other_error"


# ---------------------------------------------------------------------------
# Filename template system
# ---------------------------------------------------------------------------

_TEMPLATE_TO_YTDLP = {
    "{shortcode}":   "%(id)s",
    "{author}":      "%(uploader_id)s",
    "{title}":       "%(title)s",
    "{upload_date}": "%(upload_date)s",
}

FILENAME_PRESETS = {
    "{shortcode}":                              "Shortcode only (default)",
    "{author}_{shortcode}":                     "Author + shortcode",
    "{date}_{shortcode}":                       "Download date + shortcode",
    "{index:04d}_{shortcode}":                  "4-digit index + shortcode",
    "{upload_date}_{shortcode}":                "Upload date + shortcode",
    "{author}_{upload_date}_{shortcode}":       "Author + upload date + shortcode",
    "{date}_{index:04d}_{author}_{shortcode}": "Date + index + author + shortcode",
}

FILENAME_VARIABLE_DOCS = """
Available template variables
────────────────────────────
{shortcode}      Instagram post shortcode, e.g. DYFgyOEuIRN
{author}         Uploader username (e.g. natgeo)
{title}          Post title as reported by yt-dlp
{upload_date}    Original upload date in YYYYMMDD format (e.g. 20240318)
{date}           Today's date in YYYY-MM-DD format (e.g. 2026-05-18)
{index}          Sequential number within the current run (0-based)
{index:04d}      Zero-padded index, width 4  →  0001, 0002, …
{index:03d}      Zero-padded index, width 3  →  001, 002, …

The file extension (.mp4, .jpg, …) is always appended automatically.
For carousel posts the per-slide index (_01, _02, …) is appended before the ext.
"""


def build_outtmpl(template: str, output_dir: str, file_index: int = 0) -> str:
    """Convert our {var} template to a yt-dlp output path string."""
    today = datetime.now().strftime("%Y-%m-%d")
    t = template
    # Pre-substitute non-yt-dlp variables
    t = re.sub(r"\{index:(\d+)d\}", lambda m: f"{file_index:0{int(m.group(1))}d}", t)
    t = t.replace("{index}", str(file_index))
    t = t.replace("{date}", today)
    # Map our var names to yt-dlp %(...)s placeholders
    for our_var, ytdlp_var in _TEMPLATE_TO_YTDLP.items():
        t = t.replace(our_var, ytdlp_var)
    return os.path.join(output_dir, t + ".%(ext)s")


def extract_author_from_title(title, fallback):
    if title and " by " in title:
        return title.split(" by ")[-1].strip()
    return fallback or "unknown"


def _wait_rate_limit(consecutive, stop_event=None, progress_cb=None):
    """Sleep with countdown. Returns True if completed, False if stop_event fired."""
    delay = _RATE_LIMIT_DELAYS[min(consecutive - 1, len(_RATE_LIMIT_DELAYS) - 1)]
    print(f"\n  WARNING Rate-limited ({consecutive}x in a row) - waiting {delay}s before retrying...", flush=True)
    if progress_cb:
        progress_cb({"type": "rate_limited", "wait_seconds": delay, "consecutive": consecutive})
    deadline = time.monotonic() + delay
    while True:
        if stop_event and stop_event.is_set():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"\r  Waiting: {int(remaining):3d}s remaining...  ", end="", flush=True)
        if progress_cb:
            progress_cb({"type": "rate_limit_tick", "remaining": int(remaining)})
        time.sleep(min(1.0, remaining))
    print(f"\r  Resuming...                   ", flush=True)
    return True


def update_manifest(log_path):
    log_dir = os.path.dirname(os.path.abspath(log_path)) or "."
    manifest_path = os.path.join(log_dir, "ig_logs_manifest.json")
    try:
        files = sorted(
            f for f in os.listdir(log_dir)
            if f.endswith(".json") and f != "ig_logs_manifest.json"
        )
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"files": files, "updated": datetime.now(timezone.utc).isoformat()}, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Quiet logger
# ---------------------------------------------------------------------------

class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------

def _ydl_opts(outtmpl=None, cookies_file=None, skip_download=False, noplaylist=True):
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": noplaylist,
    }
    if skip_download:
        opts["skip_download"] = True
    else:
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["embedmetadata"] = True
    if outtmpl:
        opts["outtmpl"] = outtmpl
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


def yt_extract_info(url, cookies_file):
    # noplaylist=False so carousels return their full entries list
    with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True, cookies_file=cookies_file, noplaylist=False)) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("yt-dlp returned no info")
    return info


def yt_download(url, outtmpl, cookies_file):
    with yt_dlp.YoutubeDL(_ydl_opts(outtmpl=outtmpl, cookies_file=cookies_file)) as ydl:
        retcode = ydl.download([url])
    if retcode != 0:
        raise RuntimeError(f"yt-dlp download failed with return code {retcode}")


def yt_download_carousel(url, output_dir, shortcode, cookies_file,
                         filename_template="{shortcode}", file_index=0):
    base = build_outtmpl(filename_template, output_dir, file_index)
    # For carousels, %(id)s is the per-item media ID, not the post shortcode.
    # Replace it with the actual shortcode so all slides share a consistent prefix.
    base = base.replace("%(id)s", shortcode)
    # Insert per-slide index before the extension
    outtmpl = base.replace(".%(ext)s", "_%(playlist_index)02d.%(ext)s")
    opts = {
        "logger": _QuietLogger(),
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "embedmetadata": True,
        "ignoreerrors": True,
        "outtmpl": outtmpl,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    try:
        return sorted(
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if shortcode in f
        )
    except OSError:
        return []


# ---------------------------------------------------------------------------
# gallery-dl wrapper
# ---------------------------------------------------------------------------

def gdl_download(url, output_dir, cookies_file):
    if not _GALLERY_DL_AVAILABLE:
        raise RuntimeError("gallery-dl is not installed. Run: pip install gallery-dl")
    gallery_dl.config.clear()
    gallery_dl.config.set((), "base-directory", output_dir)
    gallery_dl.config.set((), "directory", [])
    if cookies_file:
        gallery_dl.config.set(("extractor",), "cookies", cookies_file)
    try:
        before = set(os.listdir(output_dir))
    except OSError:
        before = set()
    job = gallery_dl.job.DownloadJob(url)
    job.run()
    try:
        after = set(os.listdir(output_dir))
        return sorted(os.path.join(output_dir, f) for f in (after - before))
    except OSError:
        return []


def find_saved_files(output_dir, shortcode):
    try:
        return [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if shortcode in f
        ]
    except OSError:
        return []


def file_exists_on_disk(output_dir, shortcode):
    try:
        return any(shortcode in f for f in os.listdir(output_dir))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Core per-URL handler
# ---------------------------------------------------------------------------

def download_url(url, output_dir, cookies_file, dry_run,
                 filename_template="{shortcode}", file_index=0):
    shortcode = shortcode_from_url(url)
    timestamp = datetime.now(timezone.utc).isoformat()

    username = None
    title = None
    author = None
    caption = ""
    media_type = "unknown"

    try:
        info = yt_extract_info(url, cookies_file)
        username = (info.get("uploader_id") or info.get("uploader") or "unknown").lstrip("@")
        title = info.get("title") or shortcode
        author = extract_author_from_title(title, username)
        caption = (info.get("description") or "").strip()

        entries = info.get("entries")
        formats = info.get("formats") or []

        if entries is not None:
            media_type = "carousel"
        elif not formats:
            media_type = "image_only"
        else:
            media_type = "video"

    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            if dry_run:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "dry_run", "timestamp": timestamp}
            try:
                saved = gdl_download(url, output_dir, cookies_file)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "ok",
                        "saved_path": saved[0] if saved else None, "timestamp": timestamp}
            except Exception as gdl_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "gallery_dl_error",
                        "error_detail": str(gdl_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "failed",
                "error_category": "extract_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    if dry_run:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": media_type, "status": "dry_run", "timestamp": timestamp}

    if media_type == "image_only":
        try:
            saved_files = gdl_download(url, output_dir, cookies_file)
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "image", "status": "ok",
                    "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "image", "status": "failed",
                    "error_category": "gallery_dl_error",
                    "error_detail": str(e), "timestamp": timestamp}

    if media_type == "carousel":
        try:
            saved = yt_download_carousel(url, output_dir, shortcode, cookies_file,
                                         filename_template, file_index)
            if saved:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "carousel", "status": "ok",
                        "carousel_count": len(saved),
                        "saved_path": saved[0], "timestamp": timestamp}
            # yt-dlp got nothing — likely an image-only carousel; try gallery-dl
            saved_gdl = gdl_download(url, output_dir, cookies_file)
            if saved_gdl:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "carousel", "status": "ok",
                        "carousel_count": len(saved_gdl),
                        "saved_path": saved_gdl[0], "timestamp": timestamp}
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "no_files_saved",
                    "error_detail": "Neither yt-dlp nor gallery-dl saved any files for this carousel",
                    "timestamp": timestamp}
        except Exception as e:
            return {"url": url, "shortcode": shortcode, "username": username,
                    "author": author, "title": title, "caption": caption,
                    "media_type": "carousel", "status": "failed",
                    "error_category": "other_error",
                    "error_detail": strip_ansi(str(e)), "timestamp": timestamp}

    outtmpl = build_outtmpl(filename_template, output_dir, file_index)
    try:
        yt_download(url, outtmpl, cookies_file)
        saved_files = find_saved_files(output_dir, shortcode)
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "ok",
                "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
    except yt_dlp.utils.DownloadError as e:
        err_str = strip_ansi(str(e))
        cat = classify_error(err_str)
        if cat == "image_only":
            try:
                saved_files = gdl_download(url, output_dir, cookies_file)
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "ok",
                        "saved_path": saved_files[0] if saved_files else None, "timestamp": timestamp}
            except Exception as gdl_err:
                return {"url": url, "shortcode": shortcode, "username": username,
                        "author": author, "title": title, "caption": caption,
                        "media_type": "image", "status": "failed",
                        "error_category": "gallery_dl_error",
                        "error_detail": str(gdl_err), "timestamp": timestamp}
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": cat, "error_detail": err_str, "timestamp": timestamp}
    except Exception as e:
        return {"url": url, "shortcode": shortcode, "username": username,
                "author": author, "title": title, "caption": caption,
                "media_type": "video", "status": "failed",
                "error_category": "other_error",
                "error_detail": strip_ansi(str(e)), "timestamp": timestamp}


# ---------------------------------------------------------------------------
# Programmatic entry point
# ---------------------------------------------------------------------------

def run_download(
    url_file,
    collection,
    log_file=None,
    cookies=None,
    retry_failed=False,
    dry_run=False,
    filename_template="{shortcode}",
    progress_cb=None,
    stop_event=None,
):
    """
    Download URLs from url_file into downloads/<collection>/.

    filename_template: how to name output files — see FILENAME_VARIABLE_DOCS.
    progress_cb: optional callable(event_dict) for real-time progress reporting.
    stop_event:  optional threading.Event; set to cancel cleanly mid-run.
    Returns the final summary dict.
    """
    if not _GALLERY_DL_AVAILABLE:
        print("Warning: gallery-dl not installed — image-only downloads will fail.\n"
              "Install with: pip install gallery-dl", file=sys.stderr)

    # Derive log file name from collection if not specified
    if log_file is None:
        log_file = f"{collection}.json"

    urls = load_urls(url_file)
    if not urls:
        raise ValueError(f"No URLs found in {url_file}")

    log = load_log(log_file)
    if "items" not in log:
        log["items"] = []

    total = len(urls)

    before = len(urls)
    if retry_failed:
        urls = [u for u in urls if not already_done(log, u)]
    else:
        urls = [u for u in urls if not already_attempted(log, u)]
    skipped_auto = before - len(urls)

    if skipped_auto:
        print(f"Skipping {skipped_auto} already-processed URL(s) - {len(urls)} remaining.")
        if progress_cb:
            progress_cb({"type": "skip_info", "skipped": skipped_auto, "remaining": len(urls)})

    output_dir = os.path.join("downloads", collection)
    os.makedirs(output_dir, exist_ok=True)

    counts = {}
    skipped_disk = 0
    consecutive_rl = 0
    i = 0

    if progress_cb:
        progress_cb({"type": "start", "total": total, "to_process": len(urls),
                     "log_file": log_file, "collection": collection})

    while i < len(urls):
        if stop_event and stop_event.is_set():
            print("\n  Download stopped by user.")
            if progress_cb:
                progress_cb({"type": "stopped"})
            break

        url = urls[i]
        shortcode = shortcode_from_url(url)

        if not dry_run and file_exists_on_disk(output_dir, shortcode):
            skipped_disk += 1
            print(f"[{i + 1 + skipped_auto}/{total}] {url}")
            print(f"  -> {shortcode} already on disk, skipping")
            if progress_cb:
                progress_cb({"type": "skipped_disk", "url": url, "shortcode": shortcode,
                             "index": i + skipped_auto, "total": total})
            i += 1
            continue

        print(f"[{i + 1 + skipped_auto}/{total}] {url}", flush=True)
        if progress_cb:
            progress_cb({"type": "downloading", "url": url, "shortcode": shortcode,
                         "index": i + skipped_auto, "total": total})

        result = download_url(url, output_dir, cookies, dry_run, filename_template,
                              i + skipped_auto)

        if result.get("error_category") == "rate_limited":
            consecutive_rl += 1
            if consecutive_rl >= 5:
                # 5+ consecutive hits — treat as a real rate limit, wait and retry
                if consecutive_rl > _MAX_CONSECUTIVE_RATE_LIMITS:
                    print(f"\n  {consecutive_rl} consecutive rate limits - saving progress and aborting.")
                    if progress_cb:
                        progress_cb({"type": "rate_limit_abort", "consecutive": consecutive_rl})
                    upsert_log_entry(log, result)
                    save_log(log, log_file)
                    update_manifest(log_file)
                    break
                ok = _wait_rate_limit(consecutive_rl, stop_event=stop_event, progress_cb=progress_cb)
                if not ok:
                    break
                continue  # retry same URL
            # Fewer than 5 consecutive hits — may be unavailable/deleted posts, log and continue
            print(f"  FAIL [rate_limited] ({consecutive_rl}x, not waiting yet) {result.get('error_detail', '')[:120]}")
        else:
            consecutive_rl = 0
        upsert_log_entry(log, result)

        status = result["status"]
        counts[status] = counts.get(status, 0) + 1

        if status == "ok":
            display = result.get("author") or result.get("username") or "?"
            print(f"  OK {display} - {result.get('saved_path', '')}")
        elif status == "failed":
            print(f"  FAIL [{result.get('error_category', 'error')}] {result.get('error_detail', '')[:120]}")
        elif status == "dry_run":
            display = result.get("author") or result.get("username") or "?"
            print(f"  DRY {display} / {result.get('media_type', '?')}")

        if progress_cb:
            progress_cb({"type": "progress", "index": i + skipped_auto, "total": total,
                         "url": url, "shortcode": shortcode, "status": status,
                         "author": result.get("author") or result.get("username") or "?",
                         "error_category": result.get("error_category"),
                         "error_detail": (result.get("error_detail") or "")[:300]})

        summary = {
            "total_in_file": total,
            "processed": skipped_auto + i + 1,
            "ok": counts.get("ok", 0),
            "failed": counts.get("failed", 0),
            "dry_run": counts.get("dry_run", 0),
            "skipped_auto": skipped_auto,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        log["summary"] = summary
        save_log(log, log_file)
        update_manifest(log_file)
        i += 1

    final_summary = log.get("summary") or {
        "total_in_file": total, "processed": skipped_auto + i,
        "ok": counts.get("ok", 0), "failed": counts.get("failed", 0),
        "dry_run": counts.get("dry_run", 0), "skipped_auto": skipped_auto,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    print("\n--- Done ---")
    print(f"  OK:      {counts.get('ok', 0)}")
    print(f"  Failed:  {counts.get('failed', 0)}")
    print(f"  Skipped: {skipped_auto} (already in log)")
    if skipped_disk:
        print(f"  Skipped: {skipped_disk} (file already on disk)")
    if dry_run:
        print(f"  Dry-run: {counts.get('dry_run', 0)}")
    print(f"  Output:  {output_dir}")
    print(f"  Log:     {log_file}")

    if progress_cb:
        progress_cb({"type": "done", "summary": final_summary})

    return final_summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download Instagram posts from a URL list using yt-dlp + gallery-dl."
    )
    parser.add_argument("url_file")
    parser.add_argument("collection")
    parser.add_argument("-l", "--log", default=None,
                        help="Log file path (default: <collection>.json)")
    parser.add_argument("--cookies", default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--filename-template", default="{shortcode}",
                        metavar="TEMPLATE",
                        help=(
                            "Output filename template. Default: {shortcode}. "
                            "Variables: {shortcode} {author} {title} {upload_date} "
                            "{date} {index} {index:04d}. "
                            "Example: --filename-template '{author}_{shortcode}'"
                        ))
    args = parser.parse_args()

    run_download(
        url_file=args.url_file,
        collection=args.collection,
        log_file=args.log,
        cookies=args.cookies,
        retry_failed=args.retry_failed,
        dry_run=args.dry_run,
        filename_template=args.filename_template,
    )


if __name__ == "__main__":
    main()
