"""
High-capacity social video downloader bot.

 - python-telegram-bot (v20+)  -> talks to users (link, quality buttons, status)
 - yt-dlp                      -> downloads straight to disk in chunks (never into RAM)
 - Telethon (userbot session)  -> uploads from disk in chunks, up to 2 GB
 - Tiny HTTP server thread     -> Railway health check on $PORT
"""
import asyncio
import ctypes
import gc
import html
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

# --------------------------------------------------------------------------- #
# Config (all secrets come from environment variables)
# --------------------------------------------------------------------------- #
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]

# Optional but recommended: private channel where the userbot uploads, then the
# bot copies the file to the requesting user (a userbot usually can't message a
# user it has never talked to).  Format: -1001234567890
_storage = os.environ.get("STORAGE_CHANNEL_ID", "").strip()
STORAGE_CHANNEL_ID = int(_storage) if _storage else None

PORT = int(os.environ.get("PORT", 8080))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 1))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/tmp/downloads")
MAX_BYTES = 2000 * 1024 * 1024  # Telegram's 2 GB upload limit (regular accounts)
QUALITY_OPTIONS = [1080, 720, 480, 320]
SPLIT_PART_MB = int(os.environ.get("SPLIT_PART_MB", 450))  # target size of each part
MAX_GALLERY_ITEMS = int(os.environ.get("MAX_GALLERY_ITEMS", 20))  # per post
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
PENDING_TTL = 3600  # seconds a quality menu stays valid
SUPPORTED = ("youtube.com", "youtu.be", "instagram.com", "pinterest.", "pin.it")
URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("dlbot")

tele: TelegramClient | None = None  # created in post_init (needs a running loop)
SEM: asyncio.Semaphore | None = None
bot_tele: TelegramClient | None = None  # bot-token MTProto client (big files from users)
COOKIES_PATH: str | None = None  # from COOKIES_TXT (YouTube / general)
IG_COOKIES_PATH: str | None = None  # from INSTAGRAM_COOKIES_TXT


# --------------------------------------------------------------------------- #
# Health-check server (Railway)
# --------------------------------------------------------------------------- #
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    do_HEAD = do_GET

    def log_message(self, *args):  # keep logs clean
        pass


def start_health_server() -> None:
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health-check server listening on port %s", PORT)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def progress_bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def clean_error(exc: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()[:300]


class Throttle:
    """Lets an action through at most once per `interval` seconds."""

    def __init__(self, interval: float = 3.0):
        self.interval = interval
        self.last = 0.0

    def ready(self, force: bool = False) -> bool:
        now = time.monotonic()
        if force or now - self.last >= self.interval:
            self.last = now
            return True
        return False


async def safe_edit(message, text: str, markup=None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramError:  # "message not modified", flood limits, deleted msg...
        pass


def wipe_dir(job_dir: str) -> None:
    """Delete every file of a job with os.remove, then the folder itself."""
    try:
        for name in os.listdir(job_dir):
            p = os.path.join(job_dir, name)
            try:
                os.remove(p)
            except IsADirectoryError:
                shutil.rmtree(p, ignore_errors=True)
        os.rmdir(job_dir)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("Cleanup problem in %s: %s", job_dir, exc)


def free_memory() -> None:
    """Drop unreachable objects and hand freed heap pages back to the OS."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)  # glibc (Railway/Linux)
    except (OSError, AttributeError):
        pass  # not glibc / not Linux: gc.collect() alone is fine


# --------------------------------------------------------------------------- #
# yt-dlp (runs in worker threads)
# --------------------------------------------------------------------------- #
def cookies_for(url: str) -> str | None:
    """Instagram links use INSTAGRAM_COOKIES_TXT (falls back to COOKIES_TXT)."""
    if "instagram.com" in url.lower() and IG_COOKIES_PATH:
        return IG_COOKIES_PATH
    return COOKIES_PATH


def base_opts(url: str = "") -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "cachedir": False,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
    }
    cookies = cookies_for(url)
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({**base_opts(url), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):  # carousel / playlist-like -> first item
        info = next((e for e in info["entries"] if e), info)
    return info


def is_post_url(url: str) -> bool:
    """Single Instagram post/reel or Pinterest pin (not profiles or boards)."""
    u = url.lower()
    return bool(
        re.search(r"instagram\.com/(?:[^/]+/)?(p|reel|reels|tv)/", u)
        or re.search(r"pinterest\.[a-z.]+/pin/", u)
        or "pin.it/" in u
    )


def url_ext(u: str) -> str:
    return os.path.splitext(urlparse(u).path)[1].lower()


def list_gallery_urls(url: str) -> list[str]:
    """Direct media URLs (all images + videos of the post) via gallery-dl."""
    cmd = [sys.executable, "-m", "gallery_dl", "-g", "--range", f"1-{MAX_GALLERY_ITEMS}"]
    cookies = cookies_for(url)
    if cookies:
        cmd += ["--cookies", cookies]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    return [ln.strip() for ln in res.stdout.splitlines() if ln.strip().startswith("http")]


def stream_to_disk(url: str, dest: str) -> None:
    """Download in 1 MB chunks straight to disk (never the whole file in RAM)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, 1024 * 1024)


def fetch_pin_image(url: str) -> tuple[list[str], str]:
    """Image-only pins have no video formats. Read the page's og:image instead."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # follows pin.it redirects
        page = resp.read(1_500_000).decode("utf-8", "ignore")

    def meta(prop: str) -> str | None:
        for pat in (
            rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
            rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{prop}["\']',
        ):
            m = re.search(pat, page)
            if m:
                return html.unescape(m.group(1))
        return None

    image = meta("og:image")
    candidates = []
    if image:
        candidates.append(re.sub(r"/\d+x/", "/originals/", image))  # full quality
        candidates.append(image)  # safe fallback
    return candidates, meta("og:title") or ""


async def send_pin_image(msg, status, url: str) -> bool:
    """Send an image pin straight from Pinterest's CDN (no disk, no RAM)."""
    try:
        candidates, title = await asyncio.to_thread(fetch_pin_image, url)
        for image_url in candidates:
            try:
                await msg.reply_photo(photo=image_url, caption=title[:200])
            except TelegramError:
                try:
                    await msg.reply_document(document=image_url, caption=title[:200])
                except TelegramError:
                    continue
            try:
                await status.delete()
            except TelegramError:
                pass
            return True
    except Exception as exc:
        log.warning("Pin image fallback failed: %s", exc)
    return False


def quality_options(info: dict) -> list[int]:
    heights = {
        f["height"]
        for f in info.get("formats") or []
        if f.get("height") and f.get("vcodec") != "none"
    }
    if not heights:
        return [0]  # 0 = "best available" (Instagram/Pinterest often hide heights)
    max_h = max(heights)
    return sorted({q for q in QUALITY_OPTIONS if q <= max_h} | {max_h}, reverse=True)


def download_video(url: str, height: int, job_dir: str, hook) -> tuple[str, dict]:
    """yt-dlp always writes to disk; chunked HTTP ranges keep memory flat."""
    fmt = (
        "bv*+ba/b"
        if not height
        else f"bv*[height<={height}]+ba/b[height<={height}]/b"
    )
    opts = {
        **base_opts(url),
        "format": fmt,
        "format_sort": ["res", "vcodec:h264", "acodec:m4a"],  # Telegram-friendly mp4
        "merge_output_format": "mp4",
        "outtmpl": os.path.join(job_dir, "%(id)s.%(ext)s"),
        "http_chunk_size": 10 * 1024 * 1024,  # fetch + write 10 MB at a time
        "concurrent_fragment_downloads": 3,
        "max_filesize": MAX_BYTES,
        "noprogress": True,
        "progress_hooks": [hook],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    files = [
        os.path.join(job_dir, f)
        for f in os.listdir(job_dir)
        if not f.endswith((".part", ".ytdl", ".temp"))
    ]
    if not files:
        raise RuntimeError("Nothing was downloaded (file may exceed 2 GB or be unavailable).")
    return max(files, key=os.path.getsize), info


# --------------------------------------------------------------------------- #
# Telethon upload (chunked from disk, with live progress)
# --------------------------------------------------------------------------- #
async def upload_video(path: str, info: dict, status, target: int):
    throttle = Throttle(3.0)

    async def progress(current: int, total: int) -> None:
        pct = current * 100 / total if total else 0
        if throttle.ready(force=current >= total):
            await safe_edit(
                status,
                f"⬆️ Uploading: {pct:.0f}% [{progress_bar(pct)}]\n"
                f"{human(current)} / {human(total)}",
            )

    attributes = []
    if info.get("width") and info.get("height"):
        attributes.append(
            DocumentAttributeVideo(
                duration=int(info.get("duration") or 0),
                w=int(info["width"]),
                h=int(info["height"]),
                supports_streaming=True,
            )
        )

    # Passing a *path* makes Telethon read the file from disk part by part
    return await tele.send_file(
        target,
        path,
        caption=(info.get("title") or "")[:200],
        attributes=attributes or None,
        supports_streaming=True,
        force_document=False,
        progress_callback=progress,
    )


# --------------------------------------------------------------------------- #
# Job runner
# --------------------------------------------------------------------------- #
async def _process(bot, chat_id: int, status, url: str, height: int) -> None:
    loop = asyncio.get_running_loop()
    job_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    dl_throttle = Throttle(3.0)

    def hook(d: dict) -> None:  # called from the yt-dlp worker thread
        if d.get("status") == "downloading" and dl_throttle.ready():
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = d.get("downloaded_bytes", 0) * 100 / total if total else 0
            text = f"⬇️ Downloading: {pct:.0f}% [{progress_bar(pct)}]"
            asyncio.run_coroutine_threadsafe(safe_edit(status, text), loop)

    if SEM.locked():
        await safe_edit(status, "⏳ Queued — waiting for a free slot…")

    sent = None
    try:
        async with SEM:
            try:
                await safe_edit(status, "⬇️ Downloading: 0% [░░░░░░░░░░]")
                path, info = await asyncio.to_thread(
                    download_video, url, height, job_dir, hook
                )
                if os.path.getsize(path) > MAX_BYTES:
                    raise RuntimeError("File is larger than Telegram's 2 GB limit.")
                sent = await upload_video(path, info, status, STORAGE_CHANNEL_ID or chat_id)
            finally:
                wipe_dir(job_dir)  # runs the instant upload succeeds OR fails
                free_memory()
    except Exception as exc:
        log.exception("Job failed")
        hint = ""
        if isinstance(exc, ValueError) and "input entity" in str(exc):
            hint = "\nSet STORAGE_CHANNEL_ID (see README) so the userbot can deliver files."
        await safe_edit(status, f"❌ Failed: {clean_error(exc)}{hint}")
        return

    try:
        if STORAGE_CHANNEL_ID:
            await bot.copy_message(
                chat_id=chat_id, from_chat_id=STORAGE_CHANNEL_ID, message_id=sent.id
            )
            try:
                await tele.delete_messages(STORAGE_CHANNEL_ID, sent.id)
            except Exception as exc:
                log.warning("Couldn't delete storage message: %s", exc)
        try:
            await status.delete()
        except TelegramError:
            pass
    except Exception as exc:
        log.exception("Delivery failed")
        await safe_edit(status, f"❌ Uploaded, but delivery failed: {clean_error(exc)}")


async def process(bot, chat_id: int, status, url: str, height: int) -> None:
    try:
        await _process(bot, chat_id, status, url, height)
    finally:
        free_memory()  # after delivery (or failure): release leftover RAM


async def process_gallery(bot, chat_id: int, status, urls: list[str]) -> None:
    """Instagram/Pinterest posts: send every image and video, albums of up to 10."""
    target = STORAGE_CHANNEL_ID or chat_id

    batches, album = [], []
    for u in urls:
        if url_ext(u) in IMAGE_EXTS:
            album.append(u)
            if len(album) == 10:
                batches.append(album)
                album = []
        else:  # videos / gifs go one by one
            if album:
                batches.append(album)
                album = []
            batches.append([u])
    if album:
        batches.append(album)

    done = 0
    try:
        async with SEM:
            for batch in batches:
                job_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
                os.makedirs(job_dir, exist_ok=True)
                span = f"{done + 1}-{done + len(batch)} of {len(urls)}"
                try:
                    await safe_edit(status, f"⬇️ Downloading {span}…")
                    paths = []
                    for i, u in enumerate(batch):
                        p = os.path.join(job_dir, f"{i}{url_ext(u) or '.bin'}")
                        await asyncio.to_thread(stream_to_disk, u, p)
                        if os.path.getsize(p) > MAX_BYTES:
                            raise RuntimeError("File is larger than Telegram's 2 GB limit.")
                        paths.append(p)

                    await safe_edit(status, f"⬆️ Uploading {span}…")
                    if len(paths) > 1:
                        sent = await tele.send_file(target, paths, force_document=False)
                    elif url_ext(batch[0]) in IMAGE_EXTS:
                        sent = [await tele.send_file(target, paths[0], force_document=False)]
                    else:
                        sent = [await upload_video(paths[0], {}, status, target)]
                finally:
                    wipe_dir(job_dir)  # files gone the moment this batch is uploaded/failed
                    free_memory()

                if STORAGE_CHANNEL_ID:
                    ids = [m.id for m in sent]
                    await bot.copy_messages(
                        chat_id=chat_id, from_chat_id=STORAGE_CHANNEL_ID, message_ids=ids
                    )
                    try:
                        await tele.delete_messages(STORAGE_CHANNEL_ID, ids)
                    except Exception as exc:
                        log.warning("Couldn't delete storage messages: %s", exc)
                done += len(batch)

        try:
            await status.delete()
        except TelegramError:
            pass
    except Exception as exc:
        log.exception("Gallery job failed")
        hint = ""
        if isinstance(exc, ValueError) and "input entity" in str(exc):
            hint = "\nSet STORAGE_CHANNEL_ID (see README) so the userbot can deliver files."
        await safe_edit(
            status, f"❌ Failed after {done}/{len(urls)} items: {clean_error(exc)}{hint}"
        )


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a YouTube, Instagram or Pinterest link and I'll let you pick the quality.\n\n"
        f"Or send me a big video file and I'll split it into ~{SPLIT_PART_MB} MB parts."
    )


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    match = URL_RE.search(msg.text or "")
    if not match:
        return
    url = match.group(0)
    if not any(s in url.lower() for s in SUPPORTED):
        await msg.reply_text("❌ Only YouTube, Instagram and Pinterest links are supported.")
        return

    status = await msg.reply_text("🔎 Reading link…")

    # Instagram posts / Pinterest pins: grab ALL images + videos of the post.
    # A lone video still goes to the quality menu below.
    if is_post_url(url):
        try:
            gallery = await asyncio.to_thread(list_gallery_urls, url)
        except Exception as exc:
            log.warning("gallery-dl failed: %s", exc)
            gallery = []
        lone_video = len(gallery) == 1 and url_ext(gallery[0]) not in IMAGE_EXTS
        if gallery and not lone_video:
            await safe_edit(status, f"📥 Found {len(gallery)} item(s)…")
            context.application.create_task(
                process_gallery(context.bot, msg.chat_id, status, gallery)
            )
            return

    try:
        info = await asyncio.to_thread(fetch_info, url)
    except Exception as exc:
        if "No video formats" in str(exc) and "pin" in url.lower():
            if await send_pin_image(msg, status, url):
                return
        await safe_edit(status, f"❌ Couldn't read that link.\n{clean_error(exc)}")
        return

    pending = context.bot_data.setdefault("pending", {})
    now = time.time()
    for k in [k for k, v in pending.items() if now - v["ts"] > PENDING_TTL]:
        pending.pop(k, None)

    token = uuid.uuid4().hex[:10]
    pending[token] = {"url": url, "user_id": msg.from_user.id, "ts": now}

    buttons = [
        InlineKeyboardButton(
            f"{h}p" if h else "Best quality", callback_data=f"dl|{token}|{h}"
        )
        for h in quality_options(info)
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    dur = info.get("duration")
    dur_txt = time.strftime("%H:%M:%S", time.gmtime(dur)) if dur else "unknown"
    title = (info.get("title") or "Video")[:150]
    await safe_edit(
        status,
        f"🎬 {title}\n⏱ {dur_txt}\n\nChoose a quality:",
        InlineKeyboardMarkup(rows),
    )


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, token, height = q.data.split("|")
    req = context.bot_data.get("pending", {}).get(token)

    if not req:
        await q.answer("This menu expired — send the link again.", show_alert=True)
        return
    if q.from_user.id != req["user_id"]:
        await q.answer("This menu belongs to someone else.", show_alert=True)
        return

    context.bot_data["pending"].pop(token, None)  # one click = one download
    await q.answer()
    # Run as a task so other users' updates aren't blocked by this download
    context.application.create_task(
        process(context.bot, q.message.chat_id, q.message, req["url"], int(height))
    )


# --------------------------------------------------------------------------- #
# Split big videos sent TO the bot into ~SPLIT_PART_MB parts
# --------------------------------------------------------------------------- #
def is_video_upload(e) -> bool:
    if not e.is_private or not e.file:
        return False
    return bool(e.video) or (e.file.mime_type or "").startswith("video/")


def tele_progress(status, label: str):
    throttle = Throttle(3.0)

    async def cb(current: int, total: int) -> None:
        pct = current * 100 / total if total else 0
        if throttle.ready(force=current >= total):
            try:
                await status.edit(
                    f"{label}: {pct:.0f}% [{progress_bar(pct)}]\n"
                    f"{human(current)} / {human(total)}"
                )
            except Exception:
                pass

    return cb


async def run_cmd(*cmd: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="ignore"), err.decode(errors="ignore")


async def on_video_upload(event) -> None:
    size = event.file.size or 0
    part_bytes = SPLIT_PART_MB * 1024 * 1024
    if size > MAX_BYTES:
        await event.reply("❌ This file is over 2 GB, I can't process it.")
        return
    if size <= part_bytes * 1.1:
        await event.reply(f"✅ This video is only {human(size)}, no need to split it.")
        return

    parts = math.ceil(size / part_bytes)
    job_dir = os.path.join(DOWNLOAD_DIR, uuid.uuid4().hex)
    os.makedirs(job_dir, exist_ok=True)
    status = await event.reply(f"⏳ Got {human(size)}. Will split into {parts} parts…")
    try:
        async with SEM:
            try:
                ext = event.file.ext or ".mp4"
                src = await bot_tele.download_media(
                    event.message,
                    file=os.path.join(job_dir, "src" + ext),
                    progress_callback=tele_progress(status, "⬇️ Downloading"),
                )
                rc, out, err = await run_cmd(
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=nw=1:nk=1", src,
                )
                if rc != 0 or not out.strip():
                    raise RuntimeError("Couldn't read the video (ffprobe failed).")
                duration = float(out.strip().splitlines()[0])
                part_dur = duration / parts
                out_ext = ".mp4" if ext.lower() in (".mp4", ".mov", ".m4v") else ext

                for i in range(parts):
                    out_path = os.path.join(job_dir, f"part{i + 1}{out_ext}")
                    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{i * part_dur:.3f}", "-i", src]
                    if i < parts - 1:
                        cmd += ["-t", f"{part_dur:.3f}"]
                    cmd += ["-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                            "-avoid_negative_ts", "make_zero"]
                    if out_ext in (".mp4", ".mov"):
                        cmd += ["-movflags", "+faststart"]
                    cmd.append(out_path)
                    await safe_tele_edit(status, f"✂️ Cutting part {i + 1}/{parts}…")
                    rc, _, err = await run_cmd(*cmd)
                    if rc != 0:
                        raise RuntimeError(f"ffmpeg failed: {err[-200:]}")
                    try:
                        await bot_tele.send_file(
                            event.chat_id,
                            out_path,
                            caption=f"Part {i + 1}/{parts}",
                            supports_streaming=True,
                            force_document=False,
                            progress_callback=tele_progress(status, f"⬆️ Part {i + 1}/{parts}"),
                        )
                    finally:
                        try:
                            os.remove(out_path)  # part deleted right after it is sent
                        except FileNotFoundError:
                            pass
            finally:
                wipe_dir(job_dir)  # source + anything left, success or failure
                free_memory()
        try:
            await status.delete()
        except Exception:
            pass
        await event.reply(f"✅ Done: {parts} parts sent.")
    except Exception as exc:
        log.exception("Split job failed")
        await safe_tele_edit(status, f"❌ Failed: {clean_error(exc)}")


async def safe_tele_edit(status, text: str) -> None:
    try:
        await status.edit(text)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
async def post_init(app: Application) -> None:
    global tele, SEM, bot_tele
    SEM = asyncio.Semaphore(MAX_CONCURRENT)
    tele = TelegramClient(
        StringSession(SESSION_STRING), API_ID, API_HASH,
        connection_retries=5, flood_sleep_threshold=60,
    )
    await tele.connect()
    if not await tele.is_user_authorized():
        raise RuntimeError("SESSION_STRING is invalid or expired — generate a new one.")
    await tele.get_dialogs()  # fills the entity cache so STORAGE_CHANNEL_ID resolves
    me = await tele.get_me()
    log.info("Userbot logged in as %s", me.first_name)

    # Second MTProto client logged in as the BOT itself: can download/send 2 GB files
    bot_tele = TelegramClient(
        StringSession(), API_ID, API_HASH, connection_retries=5, flood_sleep_threshold=60
    )
    await bot_tele.start(bot_token=BOT_TOKEN)
    bot_tele.add_event_handler(
        on_video_upload, events.NewMessage(incoming=True, func=is_video_upload)
    )


async def post_shutdown(app: Application) -> None:
    if tele:
        await tele.disconnect()
    if bot_tele:
        await bot_tele.disconnect()


def setup_cookies() -> None:
    """Optional: paste Netscape cookies.txt text into COOKIES_TXT (YouTube etc.)
    and/or INSTAGRAM_COOKIES_TXT (Instagram only)."""
    global COOKIES_PATH, IG_COOKIES_PATH
    raw = os.environ.get("COOKIES_TXT")
    if raw:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "w") as f:
            f.write(raw)
    raw_ig = os.environ.get("INSTAGRAM_COOKIES_TXT")
    if raw_ig:
        IG_COOKIES_PATH = "/tmp/cookies_instagram.txt"
        with open(IG_COOKIES_PATH, "w") as f:
            f.write(raw_ig)


def main() -> None:
    start_health_server()
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)  # leftovers from a crashed run
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    setup_cookies()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    app.add_handler(CallbackQueryHandler(on_quality, pattern=r"^dl\|"))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
