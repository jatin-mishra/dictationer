# curl examples

Server: `make serve` (port 8000). Every call needs the `author-id` header.

```bash
B=http://localhost:8000/v1
H='author-id: jatin_01'
```

## Books, chapters, dictation (existing)

```bash
curl -X POST $B/books -H "$H" -H 'content-type: application/json' -d '{"book_name":"My Book"}'

curl -X POST "$B/books/My%20Book/chapters" -H "$H" -H 'content-type: application/json' \
  -d '{"chapter_number":3,"chapter_name":"Rivers"}'

curl -X POST $B/dictations -H "$H" -H 'Idempotency-Key: clip-0001-abcd' \
  -F audio=@clip.m4a -F language=hinglish -F book_name="My Book" -F chapter=3 -F page_number=12

# GET now also returns content_sha256 (additive)
curl "$B/books/my-book/chapters/3?page=12" -H "$H"
```

## Manuscript and laps (`type` form field)

`type` = `chapter` (default) | `manuscript` | `laps`. New books get empty `manuscript.md` and
`laps.md`; older books get them on first write. Book must exist (404 `book_not_found`).

```bash
# manuscript: chapter / page_number not needed (ignored if sent); appended to manuscript.md
curl -X POST $B/dictations -H "$H" \
  -F audio=@clip.m4a -F language=hinglish -F book_name="My Book" -F type=manuscript

# laps: `chapter` carries the lap number; text goes under "# lap 2" in laps.md
curl -X POST $B/dictations -H "$H" \
  -F audio=@clip.m4a -F language=hinglish -F book_name="My Book" -F type=laps -F chapter=2
```

Response gains `type` and `lap`; `chapter` / `page` are null unless `type=chapter`.
Missing required field for the type → 422 `missing_field` (no STT call).

```bash
# read back: laps -> path number is the lap number; manuscript -> path number ignored
curl "$B/books/my-book/chapters/2?type=laps" -H "$H"        # 404 lap_not_found if never dictated
curl "$B/books/my-book/chapters/1?type=manuscript" -H "$H"  # content '' if nothing written yet
```

GET response gains `type`, `lap`, `available_laps`; `available_pages` is `[]` unless `type=chapter`.

## Instructions → review → accept

`type` = `chapter` (default) | `manuscript` | `laps` on both `/instructions` (form field) and
`/accept` (query). Chapter / laps need local Ollama running with `gemma4:e2b` (finds where the
edit goes; if nothing is spoken it checks pages from the end, max `LOCATOR_MAX_PAGES`=10, ~7 s
each). Manuscript skips Ollama. For laps the response's `chapter` is the lap number.

```bash
curl -m 180 -X POST $B/instructions -H "$H" \
  -F audio=@instruction.m4a -F language=en -F book_name="My Book" -F type=laps | tee resp.json
jq '.instructions[0]' resp.json > block.json
curl -X POST "$B/books/my-book/chapters/2/accept?type=laps" -H "$H" \
  -H 'content-type: application/json' -d @block.json
# manuscript: GET ?type=manuscript gives content + content_sha256; numbers in path/body ignored
curl -X POST "$B/books/my-book/chapters/1/accept?type=manuscript" -H "$H" \
  -H 'content-type: application/json' -d @block.json
```
New errors: 502 `locator_unavailable` (Ollama down / model missing), 502 `locator_bad_output`,
404 `lap_not_found`.

Needs `ANTHROPIC_AGENT_API_KEY` and `ANTHROPIC_AGENT_MODEL` exported before `make serve`.

```bash
# 1. Speak an instruction. Proposes edits, writes nothing. Two LLM calls: allow ≥ 60 s.
curl -m 90 -X POST $B/instructions -H "$H" \
  -F audio=@instruction.m4a -F language=hinglish -F book_name="My Book" | tee resp.json
```

Response (`instruction_response`):
```json
{
  "book": "my-book",
  "transcript": "chapter 3 page 12 mein flows hata do",
  "instructions": [
    { "chapter": 3,
      "instructions": [
        { "page": 12, "content_sha256": "9f2c…",
          "instructions": [ { "action": "remove", "details": { "from_offset": 10, "to_offset": 16 } } ] } ] }
  ],
  "unresolved": []
}
```

```bash
# 2. Accept one chapter block exactly as returned. No LLM; all-or-nothing.
jq '.instructions[0]' resp.json > chapter3.json
curl -X POST $B/books/my-book/chapters/3/accept -H "$H" \
  -H 'content-type: application/json' -d @chapter3.json
# → 200 {"book","chapter","pages":[{"page","content","content_sha256"}]}
```

Hand-written block (offsets are code points into the page as GET returns it; `[from, to)`;
`size` / `new_size` must equal the length of `text`):
```bash
curl -X POST $B/books/my-book/chapters/3/accept -H "$H" -H 'content-type: application/json' -d '{
  "chapter": 3,
  "instructions": [{
    "page": 12,
    "content_sha256": "<content_sha256 from GET>",
    "instructions": [
      {"action": "remove", "details": {"from_offset": 10, "to_offset": 16}},
      {"action": "add",    "details": {"at_offset": 0, "size": 6, "text": "Hello "}},
      {"action": "update", "details": {"from_offset": 20, "to_offset": 25, "new_size": 7, "text": "ज़िंदगी", "old_text": "<optional: content[20:25], verified>"}}
    ]
  }]
}'
```

| Result | Meaning |
|---|---|
| 409 `page_changed` | Page edited since `/instructions` (e.g. new dictation, or this block was already accepted). Call `/instructions` again. |
| 422 `invalid_edit` | Offsets out of range, overlapping, size ≠ len(text), split a Devanagari letter, or duplicate page. Message names page + edit. |
| 422 `chapter_mismatch` | Body `chapter` ≠ path chapter. |
| 422 `book_too_large` | No chapter spoken and the book exceeds the search budget: say the chapter. |
| 502 `llm_*` | `llm_not_configured`, `llm_upstream_error`, `llm_refused`, `llm_bad_output`. |

`unresolved[].reason`: `no_change`, `quote_not_found`, `page_not_found`, `splits_letter`,
`conflicts_with_other_edit`, `missing_text`, `missing_position`, `not_found` (the LLM could not
place it), `no_instruction_found`, `book_empty`.

## Live dictation (WebSocket, MVP)

`WS /v1/dictations/live`, independent of `POST /v1/dictations`. Author: header `author-id`, or
`author_id` in the start message (browsers cannot set WebSocket headers); if both are sent they must match.

1. Client sends JSON `{"type":"start","language":"en","book_name":"My Book","dictation_type":"chapter","chapter":1,"page_number":1,"author_id":"jatin_01","partials":true}`
   (same per-type rules as the form fields above; `author_id` and `partials` optional). Server replies
   `{"type":"ready","engine":"mlx-whisper"}` after checking the chapter/book exists.
2. Client sends binary frames: raw PCM16 little-endian, 16 kHz, mono (e.g. 100 ms = 3200 bytes).
3. After each pause (600 ms of silence; run-on speech is cut at 20 s) the server transcribes that
   utterance, **appends it to the book immediately**, and sends `{"type":"final","text":"...","warnings":[]}`.
   With `"partials": true` (default false), while a sentence is still being spoken the server also sends
   `{"type":"partial","text":"..."}` about every second: the sentence so far. Each partial replaces the
   previous one; the `final` replaces the last partial. Partials are never stored, and never arrive after
   their sentence's final. For hi / hinglish every partial is a paid Sarvam call.
4. Client sends `{"type":"stop"}`: the buffered tail is transcribed and written, then `{"type":"done","utterances":N}`.
   On a disconnect without `stop`, the tail is still written.

Errors: `{"type":"error","code","message"}` then close. Codes: `invalid_author_id`, `bad_message`,
`bad_audio_frame`, `missing_field`, `chapter_not_found`, `book_not_found`, plus transcription errors.
Text from earlier utterances stays written.

```bash
uv run python scripts/live_mic.py --book "My Book" --chapter 1 --page 1    # speak; Ctrl+C to stop
uv run python scripts/live_mic.py --book "My Book" --chapter 1 --page 1 --partials   # words as you speak
uv run python scripts/live_mic.py --book "My Book" --type laps --chapter 2
```

`POST /v1/dictations` long audio is now cut at pauses too (`SEGMENT_STRATEGY=pause`, default): near
each 10 s mark the quietest 30 ms in the last 3 s is found; if silent, the cut is there with no overlap,
otherwise the old 10 s / 2 s overlap window is used. `SEGMENT_STRATEGY=fixed` restores the old split.

## Real-LLM eval (costs money)

```bash
RUN_LLM_TESTS=1 uv run pytest -m llm -s tests/test_instructions_real_llm.py
```
