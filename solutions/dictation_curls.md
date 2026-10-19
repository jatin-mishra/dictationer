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

## Instructions → review → accept

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

## Real-LLM eval (costs money)

```bash
RUN_LLM_TESTS=1 uv run pytest -m llm -s tests/test_instructions_real_llm.py
```
