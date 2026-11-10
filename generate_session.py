"""
Run this on YOUR OWN computer (never on Railway) to create SESSION_STRING.

    pip install telethon
    python generate_session.py
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

# Telegram will ask for your phone number, the login code and (if enabled) your 2FA password
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n=== SESSION_STRING (keep it secret, it is a full login to this account) ===\n")
    print(client.session.save())

    print("\n=== Your channels (copy the ID of your private storage channel) ===")
    for d in client.iter_dialogs():
        if d.is_channel:
            print(f"{d.id}\t{d.name}")
