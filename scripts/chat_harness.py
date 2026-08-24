"""Talk to the bot's logic directly from the terminal — no WhatsApp
involved. Exercises the exact same handle_message() the real webhook
calls, so the whole pipeline (intent parsing, Swiggy search, ranking,
confirm-before-checkout) is testable while WhatsApp Cloud API is blocked.

Requires GROQ_API_KEY set and a working Swiggy login (first call will
open a browser for the PKCE flow if no cached token exists).

Usage: python scripts/chat_harness.py
"""

import asyncio

from feedme_bot.handlers import handle_message

FAKE_JID = "test-user@local"


async def main() -> None:
    print("FeedMe Bot local harness — type a message, Ctrl+C to quit.\n")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        reply = await handle_message(FAKE_JID, text)
        print(f"bot> {reply}\n")


if __name__ == "__main__":
    asyncio.run(main())
