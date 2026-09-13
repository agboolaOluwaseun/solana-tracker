"""
Check what CG Moonshots was posting between March 1 and May 5
"""
from datetime import datetime
from ingestion.telethon_fetcher import fetch_window_sync
from ingestion.address_parser import parse_message
import re

CHANNEL_USERNAME = "cgmoonshots"

# March 1 to May 5
start_date = datetime(2026, 3, 1, 0, 0, 0)
end_date = datetime(2026, 5, 5, 0, 0, 0)

print(f"Fetching {CHANNEL_USERNAME} from {start_date} to {end_date}")
print("=" * 60)

messages, _ = fetch_window_sync(CHANNEL_USERNAME, start_date, end_date)
print(f"Total messages in this period: {len(messages)}")

# Check how many contain Solana-like addresses (32-44 base58 chars)
solana_pattern = re.compile(r'[1-9A-HJ-NP-Za-km-z]{32,44}')

calls_found = 0
msgs_with_address = 0
msgs_without_address = 0

for msg in messages:
    text = msg.text or ""
    parsed = parse_message(msg)
    
    if parsed:
        calls_found += 1
        print(f"\n✓ CALL: {msg.timestamp} - {parsed.token_symbol or 'Unknown'}")
        print(f"  Address: {parsed.token_address[:20]}...")
    else:
        # Check if there's a solana-like address that wasn't parsed
        matches = solana_pattern.findall(text)
        if matches:
            msgs_with_address += 1
            if msgs_with_address <= 5:  # Show first 5
                print(f"\n✗ ADDRESS BUT NOT PARSED: {msg.timestamp}")
                print(f"  Found: {matches[0][:20]}...")
                print(f"  Text: {text[:150]}...")
        else:
            msgs_without_address += 1

print("\n" + "=" * 60)
print(f"Summary:")
print(f"  Total messages: {len(messages)}")
print(f"  Calls parsed: {calls_found}")
print(f"  Messages with Solana address (not parsed): {msgs_with_address}")
print(f"  Messages without any address: {msgs_without_address}")
print("=" * 60)
