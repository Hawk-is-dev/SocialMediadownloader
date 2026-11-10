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
import logging
import os
import re
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

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
from telethon import TelegramClient
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
PENDING_TTL = 3600  # seconds a quality menu stays valid
SUPPORTED = ("youtube.com", "youtu.be", "instagram.com", "pinterest.", "pin.it")
URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("dlbot")

tele: TelegramClient | None = None  # created in post_init (needs a running loop)
SEM: asyncio.Semaphore | None = None
COOKIES_PATH: str | None = None


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
def base_opts() -> dict:
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
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    return opts


def fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):  # carousel / playlist-like -> first item
        info = next((e for e in info["entries"] if e), info)
    return info


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
        **base_opts(),
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
async def process(bot, chat_id: int, status, url: str, height: int) -> None:
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


# --------------------------------------------------------------------------- #
# Telegram handlers
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a YouTube, Instagram or Pinterest link and I'll let you pick the quality."
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
    try:
        info = await asyncio.to_thread(fetch_info, url)
    except Exception as exc:
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
# Lifecycle
# --------------------------------------------------------------------------- #
async def post_init(app: Application) -> None:
    global tele, SEM
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


async def post_shutdown(app: Application) -> None:
    if tele:
        await tele.disconnect()


def setup_cookies() -> None:
    """Optional: paste a Netscape cookies.txt into the COOKIES_TXT variable."""
    global COOKIES_PATH
    raw = os.environ.get("COOKIES_TXT")
    if raw:
        COOKIES_PATH = "/tmp/cookies.txt"
        with open(COOKIES_PATH, "w") as f:
            f.write(raw)


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
