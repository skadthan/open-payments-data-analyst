# Plan: Model Selector UI + Multi-Provider APIs + Enhanced Prompt with Feedback

## Context

The chatbot currently uses only local Ollama (qwen2.5-coder:14b) hardcoded in config. The user wants:
1. **Model selection from the Chainlit home screen** — pick provider/model before chatting
2. **Cloud API support** — DeepSeek, Anthropic, OpenAI, Google alongside Ollama
3. **Enhanced system prompt** using the full data dictionary metadata + **user feedback loop** (thumbs up/down + text) that adjusts the AI's behavior within the session

## Feature 1: Model Selector on Home Screen

### Approach: Chainlit Chat Settings panel

Use `cl.ChatSettings` with `cl.input_widget.Select` and `cl.input_widget.TextInput` widgets, sent during `@cl.on_chat_start`. A gear icon appears in the UI letting users change model/provider mid-session.

**Settings panel will include:**
- **Provider** dropdown: `Ollama (Local)`, `OpenAI`, `Anthropic`, `Google AI`, `DeepSeek`
- **Model** dropdown: dynamically populated based on provider (e.g., Ollama shows pulled models, OpenAI shows gpt-4o/gpt-4o-mini, etc.)
- **API Key** text input: shown for cloud providers, hidden for Ollama
- **Temperature** slider: 0.0–1.0

**On settings change** (`@cl.on_settings_update`): re-initialize the LLM clients in the agent with the new provider/model/key. No need to restart the DuckDB connection.

### Files to modify

| File | Change |
|------|--------|
| `app.py` | Add `cl.ChatSettings` in `on_chat_start`, add `@cl.on_settings_update` handler |
| `agent.py` | Add `swap_llm()` method to SQLAgent that replaces `self.llm_sql` and `self.llm_summary` with a new provider |
| `config.yaml` | Add default model presets per provider |

---

## Feature 2: Multi-Provider API Integration

### Approach: LangChain provider packages

LangChain has drop-in `ChatXxx` classes for each provider, all sharing the same `.invoke()` / `.astream()` interface the agent already uses. This means `_generate_sql()` and `stream_summary()` work unchanged — we just swap the LLM object.

**Provider → Package → Class mapping:**

| Provider | pip package | Class | Model examples |
|----------|-------------|-------|----------------|
| Ollama | `langchain-ollama` (installed) | `ChatOllama` | qwen2.5-coder:14b |
| OpenAI | `langchain-openai` | `ChatOpenAI` | gpt-4o, gpt-4o-mini |
| Anthropic | `langchain-anthropic` | `ChatAnthropic` | claude-sonnet-4-20250514 |
| Google | `langchain-google-genai` | `ChatGoogleGenerativeAI` | gemini-2.5-flash |
| DeepSeek | `langchain-openai` | `ChatOpenAI` (with base_url override) | deepseek-chat, deepseek-coder |

**Key insight:** DeepSeek uses OpenAI-compatible API, so we reuse `ChatOpenAI` with `base_url="https://api.deepseek.com/v1"`.

### Implementation: `_create_llm()` factory function in `agent.py`

```python
def _create_llm(provider, model, temperature, api_key=None, base_url=None):
    if provider == "ollama":
        return ChatOllama(model=model, base_url=base_url or "http://localhost:11434", temperature=temperature)
    elif provider == "openai":
        return ChatOpenAI(model=model, api_key=api_key, temperature=temperature)
    elif provider == "anthropic":
        return ChatAnthropic(model=model, api_key=api_key, temperature=temperature)
    elif provider == "google":
        return ChatGoogleGenerativeAI(model=model, google_api_key=api_key, temperature=temperature)
    elif provider == "deepseek":
        return ChatOpenAI(model=model, api_key=api_key, base_url="https://api.deepseek.com/v1", temperature=temperature)
```

### Files to modify

| File | Change |
|------|--------|
| `agent.py` | Add `_create_llm()` factory, add `swap_llm()` method to SQLAgent |
| `requirements.txt` | Add `langchain-openai`, `langchain-anthropic`, `langchain-google-genai` |
| `config.yaml` | Add provider presets with default models |

---

## Feature 3: Enhanced System Prompt + User Feedback Loop

### 3A: Enrich system prompt with data dictionary descriptions

The data dictionary JSONs contain rich field descriptions, examples, and constraints that the current system prompt doesn't use. Currently, the compact schema only shows `column_name [TYPE]`. We'll enhance `SchemaManager` to also load descriptions from the data dictionary JSONs for the key columns and include them as inline comments.

**Before:**
```
general_payments:
  - Total_Amount_of_Payment_USDollars [DOUBLE]
```

**After:**
```
general_payments:
  - Total_Amount_of_Payment_USDollars [DOUBLE] — U.S. dollar amount of payment or other transfer of value to the recipient
  - Nature_of_Payment_or_Transfer_of_Value [VARCHAR] — e.g. "Consulting Fee", "Food and Beverage", "Travel and Lodging"
```

This gives the LLM much better context for choosing the right columns and understanding what each one means.

**Also add to system prompt:**
- A brief "dataset overview" paragraph explaining what CMS Open Payments is (the LLM needs this context to understand what questions make sense)
- Key relationships between tables (general = direct payments, research = research funding, ownership = investment interests)
- Common Nature_of_Payment values (Food and Beverage, Consulting Fee, etc.) so the LLM knows what to filter by

### 3B: Thumbs up/down + text feedback on each answer

After each streamed answer, add two action buttons: thumbs up and thumbs down. On thumbs down, prompt user for text explanation. The correction gets appended to the system prompt as a session-scoped "user correction" for subsequent queries.

**Implementation:**
- Add `session_corrections: list[str]` to session state
- On thumbs down + text: append the correction to the list
- In `_build_messages()`: if corrections exist, append them as a final system message block:
  ```
  User corrections for this session (follow these strictly):
  - "ownership table uses Total_Amount_Invested_USDollars, not Total_Amount_of_Payment_USDollars"
  - "use ILIKE not = for company names"
  ```
- On thumbs up: no action needed (just acknowledgment)

### Files to modify

| File | Change |
|------|--------|
| `agent.py` | Enhance `SchemaManager.compact_schema()` to include descriptions from data dictionaries; add `set_corrections()` method; update `_build_messages()` to inject corrections |
| `app.py` | Add thumbs up/down action buttons after each answer; add `@cl.action_callback("thumbs_down")` handler with text input; store corrections in session |
| `config.yaml` | Add `data.dictionaries_dir` reference (already exists) |

---

## Implementation Order

1. **Feature 2 first** (multi-provider factory) — foundation for Feature 1
2. **Feature 1** (UI settings panel) — connects UI to the factory
3. **Feature 3A** (enhanced prompt with data dictionary) — standalone improvement
4. **Feature 3B** (feedback loop) — UI + agent changes

## New dependencies to install

```
pip install langchain-openai langchain-anthropic langchain-google-genai
```

## Verification

1. Start with `python run.py`
2. On home screen, open settings (gear icon), select OpenAI provider, enter API key, pick gpt-4o
3. Ask: "Total US dollar value by each payment category in 2024?" — should succeed
4. Switch to Ollama in settings, confirm it works without API key
5. Test thumbs down → enter correction → verify next query uses the correction
6. Test each provider (DeepSeek, Anthropic, Google) with valid API keys
