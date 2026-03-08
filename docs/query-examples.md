# Query Examples

Practical examples for using the Layer 2 QueryBuilder and SmartChunker.

## Setup

```python
from layer2_query.query_builder import from_env
from layer2_query.chunker import SmartChunker

qb = from_env()  # reads DB_* vars from .env
```

---

## QueryBuilder Examples

### Who appears in multiple servers?

```python
# Users active in 3+ servers (the "connectors")
connectors = qb.users_across_servers(min_servers=3)

for user in connectors:
    print(f"{user['current_username']} is in {user['server_count']} servers:")
    for server in user['servers']:
        print(f"  - {server}")
```

Output:

```text
alice is in 5 servers:
  - 42 School
  - C Programmers
  - Piscine Alumni
  - ...
```

---

### What has a specific user been talking about?

```python
# All messages from user_id=42 in the last 90 days
messages = qb.user_message_context(user_id=42, time_range_days=90)

print(f"Found {len(messages)} messages")
for msg in messages[:5]:
    print(f"[{msg['created_at']}] #{msg['channel_name']} on {msg['server_name']}")
    print(f"  {msg['content'][:100]}")
```

For full history (no time limit):

```python
messages = qb.user_message_context(user_id=42)
```

---

### Get all messages from a channel

```python
# Last 30 days of #general in server 123456789
messages = qb.channel_messages(
    server_id=123456789,
    channel_id=987654321,
    time_range_days=30
)

print(f"{len(messages)} messages fetched")
```

---

### Server health (member growth/churn)

```python
health = qb.server_health(server_id=123456789)

for row in health[:6]:  # Last 6 months
    print(f"{row['month']}: +{row['new_members']} joined, -{row['left_members']} left (net: {row['net_change']})")
```

Output:

```text
2024-06-01: +12 joined, -3 left (net: +9)
2024-05-01: +8 joined, -5 left (net: +3)
2024-04-01: +15 joined, -2 left (net: +13)
```

---

### Weekly summary data

```python
# Get data for the last 7 days
summary = qb.server_summary_data(server_id=123456789, days=7)

if summary:
    print(f"Server: {summary['server_name']}")
    print(f"Messages: {summary['total_messages']}")
    print(f"Active users: {summary['active_users']}")
    print(f"Active channels: {summary['active_channels']}")
    print("\nTop contributors:")
    for user in summary['top_users']:
        print(f"  {user['current_username']}: {user['message_count']} messages")
```

---

### Most active users recently

```python
top_users = qb.recent_active_users(server_id=123456789, days=30, limit=10)

for user in top_users:
    print(f"{user['current_username']}: {user['message_count']} messages across {user['channels_used']} channels")
```

---

### Search messages

```python
# Search for "malloc" across all servers
results = qb.search_messages("malloc")

# Limit to one server
results = qb.search_messages("malloc", server_id=123456789)

for r in results[:5]:
    print(f"[{r['server_name']} #{r['channel_name']}] {r['author']}: {r['content'][:100]}")
```

---

## SmartChunker Examples

### Check if a message set needs chunking

```python
messages = qb.channel_messages(server_id=123, channel_id=456, time_range_days=90)

stats = SmartChunker.stats(messages)
print(f"Messages: {stats['total_messages']}")
print(f"Estimated tokens: {stats['total_tokens']}")
print(f"Will need {stats['estimated_chunks']} LLM call(s)")
```

---

### Chunk and format for LLM

```python
# Most common pattern: chunk + format in one step
chunks = SmartChunker.chunk_and_format(messages)

print(f"Split into {len(chunks)} chunk(s)")
for i, chunk_text in enumerate(chunks):
    print(f"\n--- Chunk {i+1} ---")
    print(chunk_text[:200])   # preview
```

---

### Manual chunk → format → LLM pipeline

```python
from layer3_llm.processor import DiscordLLMProcessor

llm = DiscordLLMProcessor()

# 1. Fetch
messages = qb.channel_messages(server_id=123, channel_id=456, time_range_days=30)

# 2. Chunk
chunks = SmartChunker.chunk_messages(messages, max_tokens=8000)

# 3. Process each chunk
results = []
for chunk in chunks:
    text = SmartChunker.format_for_llm(chunk)
    result = llm.channel_summary_faq(text, channel_name="general")
    results.append(result)

# 4. Combine if multiple chunks
final = "\n\n---\n\n".join(results)
print(final)
```

---

### Format without timestamps (cleaner for some prompts)

```python
messages = qb.user_message_context(user_id=42, time_range_days=30)
text = SmartChunker.format_for_llm(messages, include_timestamps=False)

# Output:
# alice: I'm stuck on this segfault
# bob: Can you share the backtrace?
# alice: Sure, here it is...
```

---

## Complete Workflow: User Story

```python
# Find the most active user in a server
top_users = qb.recent_active_users(server_id=123456789, days=90, limit=1)
user = top_users[0]

print(f"Profiling: {user['current_username']}")

# Get their full message history
messages = qb.user_message_context(user_id=user['discord_id'], time_range_days=365)

# Chunk for LLM (only need first chunk for user story — most recent 8k tokens)
chunks = SmartChunker.chunk_and_format(messages, max_tokens=8000)
first_chunk = chunks[0] if chunks else ""

# Run LLM
from layer3_llm.processor import DiscordLLMProcessor
llm = DiscordLLMProcessor()
story = llm.user_story(first_chunk, user['current_username'])
print(story)
```

---

## Cleanup

```python
qb.close()
```
