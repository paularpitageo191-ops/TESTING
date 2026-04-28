Let me read the current agent code to give you an accurate answer.Good — I have everything I need. Let me now add the debug output saving into the agent functions and produce the explanation doc:Now I have everything. Let me add the debug output saver and write the explanation doc:Now wire `save_agent_debug` into `run_gherkin_agent`:---

## What each agent does

### Step 0 — Knowledge Builder *(not an LLM agent — pure Python)*
Runs before anything else. Reads **every source** — Jira JSON files from disk, both Qdrant collections, the inbox, and the PRD — and assembles one unified `kb` dict with labelled sections: `epic`, `story`, `subtasks`, `acceptance_criteria`, `validation_rules`, `test_data`, `comments`, `attachment_data`, `dom_element_texts`. Every subsequent agent reads only from this single KB, never from raw files directly.

### Agent 1 — AC Analyst *(3 focused LLM calls)*
Reads the KB in **three focused chunks** to stay within Ollama's context window:
- **Chunk 1** — story + explicit acceptance criteria → extracts each AC item
- **Chunk 2** — subtasks → each subtask with testable behaviour → at least 1 AC
- **Chunk 3** — validation rules + test data + attachment rows → 1 positive + 1 negative AC per rule

Output: a structured JSON list of ACs, each with `id`, `title`, `test_type`, `source`, `steps[]`, `expected_result`, `test_data[]`.

### Agent 2 — DOM Mapper *(1 LLM call per AC + deterministic keyword fallback)*
For each AC, finds the right page URL and real DOM elements to interact with. First queries **Qdrant ui_memory** semantically for relevant element texts, then calls the LLM with a small focused prompt (one AC at a time). If the LLM returns a bad/missing URL, a **pure Python keyword matcher** takes over — scores every page and element by word overlap with the AC title and description, no LLM needed.

Output: one mapping per AC with `page_url`, `elements[]` (label, selector, action, value), `assert_element`, `assert_text`.

### Agent 3 — Scenario Writer *(1 LLM call per AC)*
One small focused LLM call per AC. Writes the Gherkin steps using the AC's tester steps, test data, and DOM mapping. After each LLM response, `_clean_scenario()` runs deterministically to fix: tag format (`Tags: @x` → `@x`), enforce `Given I am on "URL"` as first step, strip any leaked `Feature:` headers.

Output: raw Gherkin scenarios (no Feature wrapper yet).

### Agent 4 — QA Reviewer *(1 LLM call + deterministic wrapping)*
`_deterministic_feature_wrap()` first builds the `Feature:` header in pure Python — so the structure is always correct regardless of what the LLM does. Then one LLM call fixes step wording and adds `@todo` placeholders for missing coverage categories. Post-processing strips any duplicate `Feature:` headers the LLM adds.

Output: final `.feature` file.

---

## Debug output file

After every run you'll now find:

```
docs/agent_debug_{PROJECT}.md
```

It contains every agent's output in one place:

| Section | What's in it |
|---|---|
| Step 0 | KB summary table, epic/story/subtasks, all AC blocks, validation rules, test data, attachment rows, comments |
| Agent 1 | Summary table of all extracted ACs + full detail per AC (description, steps, expected result) |
| Agent 2 | Mapping table per AC (page URL, element count, match method) + full element detail |
| Agent 3 | Raw Gherkin before QA review (so you can see what the reviewer changed) |
| Agent 4 | Final Gherkin file |
| Step 5 | Coverage % tables by type/source/priority + missing coverage list |