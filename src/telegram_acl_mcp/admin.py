"""Offline provisioning utility. Never exposed over MCP."""
import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Chat, User

from .config import protected_text


async def provision(args):
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("Run provisioning as the VPS administrator (root) on Linux")
    api_hash = protected_text(args.api_hash_file)
    saved = protected_text(args.session_file) if args.session_file.exists() else ""
    client = TelegramClient(StringSession(saved), args.api_id, api_hash, receive_updates=False)
    try:
        await client.start(phone=lambda: input("Phone (+country code): "), password=lambda: getpass.getpass("Telegram 2FA: "))
        if not saved:
            # Exclusive creation, no accidental replacement or terminal disclosure.
            fd = os.open(args.session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(client.session.save())
        print("Admin-only dialog metadata. Copy chosen entries into ACL manually:")
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            kind = "channel" if isinstance(entity, Channel) else ("group" if isinstance(entity, Chat) else "user" if isinstance(entity, User) else None)
            if kind:
                print(json.dumps({"key": "replace_with_unique_key", "title": dialog.name, "kind": kind, "peer_id": entity.id, "access_hash": getattr(entity, "access_hash", 0)}, ensure_ascii=False))
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash-file", type=Path, required=True)
    parser.add_argument("--session-file", type=Path, required=True)
    asyncio.run(provision(parser.parse_args()))


if __name__ == "__main__":
    main()
