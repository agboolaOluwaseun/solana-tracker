"""
Fetch and parse CG Moonshots from March 1st to now, without pricing.
Just to see how many calls exist in that time range.
"""
from datetime import datetime, timezone
from ingestion.telethon_fetcher import fetch_window_sync
from ingestion.address_parser import parse_message
from db import get_connection

# CG Moonshots channel ID (from the database)
CHANNEL_ID = 23
CHANNEL_USERNAME = "cgmoonshots"

# Time range: March 1, 2026 to now
start_date = datetime(2026, 3, 1, 0, 0, 0)  # naive UTC
end_date = datetime.utcnow()  # naive UTC

print(f"Fetching {CHANNEL_USERNAME} from {start_date} to {end_date}")
print("=" * 60)

# Fetch messages
messages, channel_title = fetch_window_sync(CHANNEL_USERNAME, start_date, end_date)
print(f"\nTotal messages fetched: {len(messages)}")

# Parse for calls
calls = []
for msg in messages:
    parsed = parse_message(msg)
    if parsed:
        calls.append(parsed)

print(f"Total calls found: {len(calls)}")
print("\n" + "=" * 60)
print("Sample calls (first 10):")
print("=" * 60)

for i, call in enumerate(calls[:10], 1):
    print(f"\n{i}. {call.token_symbol}")
    print(f"   Address: {call.token_address[:20]}...")
    print(f"   Time: {call.timestamp}")
    print(f"   Message: {call.raw_text[:100]}...")

print("\n" + "=" * 60)
print(f"Summary: {len(calls)} calls found from March 1st to now")
print("=" * 60)
