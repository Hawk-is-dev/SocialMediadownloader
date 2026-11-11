# Social Video Downloader Bot (up to 2 GB)

python-telegram-bot (UI) + yt-dlp (disk-chunked download) + Telethon userbot (2 GB upload).

## 1. Get the credentials

| Variable | Where from |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org -> API development tools |
| `BOT_TOKEN` | @BotFather -> /newbot |
| `SESSION_STRING` | step 2 below |
| `STORAGE_CHANNEL_ID` | step 3 below (recommended) |
| `COOKIES_TXT` | optional: contents of a Netscape cookies.txt (helps with Instagram / YouTube bot checks) |
| `INSTAGRAM_COOKIES_TXT` | optional: cookies.txt text used only for Instagram links (falls back to `COOKIES_TXT`) |
| `MAX_CONCURRENT` | optional, default `1` (parallel downloads) |

## 2. Generate SESSION_STRING (on your own PC)

```bash
pip install telethon
python generate_session.py
```

Enter API_ID, API_HASH, then your phone number, login code and 2FA password.
Copy the printed string into Railway. **Treat it like a password.** Use a secondary
Telegram account for the userbot, not your main one.

## 3. Storage channel (why it's needed)

A userbot can't message people it has never talked to, so it uploads to a private
channel and the bot copies the file to the user (instant, no re-upload).

1. With the userbot account, create a **private channel**.
2. Add your bot to it as **admin**.
3. Run `generate_session.py` again (or read its channel list) and copy the channel ID
   (looks like `-1001234567890`) into `STORAGE_CHANNEL_ID`.

## 4. Deploy on Railway

1. Push this folder to a GitHub repo.
2. Railway -> New Project -> Deploy from GitHub repo (the `Dockerfile` is detected automatically).
3. Add the variables above under **Variables**. Railway sets `PORT` itself.
4. Deploy. Logs should show `Userbot logged in as ...`.

## Notes

- yt-dlp writes straight to disk, so RAM stays low. Merging video+audio needs roughly
  2x the file size on disk for a moment; check your Railway plan's ephemeral disk limit.
- Files are deleted with `os.remove` in a `finally` block the moment the upload ends or fails.
- Railway IPs are often flagged by YouTube/Instagram; if you see "sign in" or "login required"
  errors, set `COOKIES_TXT`.
- Instagram/Pinterest posts: all images and videos of a post (carousels too) are sent, images as albums of up to 10.
  A post with a single video still shows the quality buttons. `MAX_GALLERY_ITEMS` (default 20) caps items per post.
- Instagram almost always needs login cookies for photos/carousels: set `INSTAGRAM_COOKIES_TXT`.
- Edit `QUALITY_OPTIONS` in `main.py` to change the buttons.
- Only download content you have the right to download.
