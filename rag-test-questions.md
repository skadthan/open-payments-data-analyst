# RAG Test Question Set — Open Payments Data Analyst

## Context

10–15 questions to smoke-test the RAG pipeline built in `rag.py` (PDF/TXT → chunk → Ollama `nomic-embed-text` → ChromaDB → retrieval → LLM answer with citations). The goal is to verify:

1. **Routing** — `classify_question` correctly returns `rag` (or `hybrid`) instead of `sql` for policy/methodology questions.
2. **Retrieval quality** — relevant chunks come back from the right category (FAQ, law_policy, user_guides, data_dictionary, website).
3. **Answer grounding** — the LLM cites the source file + page from `build_rag_prompt` and does not hallucinate beyond the excerpts.
4. **Coverage** — questions span every indexed category under `ProgramData/`:
   - `FAQ/`
   - `law_policy/`
   - `publication_data_dictionary_methodology/`
   - `user_guides/`
   - `cms_website_content.txt` (category `website`)

Questions are phrased the way a real user would ask, and are mapped to the *expected* category so you can quickly spot retrieval misses.

## Recommended test questions (13)

### Program / policy (law_policy, website)

1. **What is the CMS Open Payments program and who runs it?**
   *Expected route:* `rag` · *Expected source category:* `website` / `law_policy`

2. **What is the Physician Payments Sunshine Act and how is it related to the Affordable Care Act?**
   *Expected route:* `rag` · *Expected source category:* `law_policy`

3. **Who is considered a "covered recipient" under Open Payments?**
   *Expected route:* `rag` · *Expected source category:* `law_policy` / `faq`

4. **Which organizations are required to report payments (applicable manufacturers and GPOs)?**
   *Expected route:* `rag` · *Expected source category:* `law_policy`

### FAQ-style (faq)

5. **What is the minimum dollar threshold (de minimis) for reporting a payment, and does it change each year?**
   *Expected route:* `rag` · *Expected source category:* `faq` / `law_policy`

6. **How can a physician or teaching hospital dispute a payment record they believe is incorrect?**
   *Expected route:* `rag` · *Expected source category:* `faq` / `user_guide`

7. **When are Open Payments data published each year and what is the review-and-dispute window?**
   *Expected route:* `rag` · *Expected source category:* `faq` / `user_guide`

### Methodology / data dictionary (data_dictionary)

8. **What is the difference between General Payments, Research Payments, and Ownership/Investment Interests?**
   *Expected route:* `rag` · *Expected source category:* `data_dictionary` / `faq`

9. **What does the `Nature_of_Payment_or_Transfer_of_Value` field represent and what categories can it take?**
   *Expected route:* `rag` · *Expected source category:* `data_dictionary`

10. **How does CMS handle removed or deleted records between publications?**
    *Expected route:* `rag` · *Expected source category:* `data_dictionary`

### User guides (user_guide)

11. **How do I register and submit data in the Open Payments system as a reporting entity?**
    *Expected route:* `rag` · *Expected source category:* `user_guide`

12. **What identity-verification steps must a physician complete before reviewing their records?**
    *Expected route:* `rag` · *Expected source category:* `user_guide`

### Hybrid (policy + data) — exercises hybrid routing

13. **For 2023 general payments, what counts as a "consulting fee" per CMS definitions, and which companies paid the most in that category?**
    *Expected route:* `hybrid` · *Expected behavior:* RAG chunks (definition of consulting fee from data dictionary) injected into SQL system prompt, then SQL runs against `all_general_payments`.

## How to run the tests

### A. CLI spot-check (retrieval only — no LLM answer)

```bash
python rag.py --status
python rag.py --query "What is the Physician Payments Sunshine Act?"
```

Confirm: top-k results include a `law_policy` or `website` chunk with `score > ~0.5`.

### B. End-to-end via the chat UI

```bash
python run.py
```

For each question above:
1. Paste it into the Chainlit composer.
2. Open the **Generating SQL** / **Searching CMS documentation** step — verify the router chose the expected route (`rag` / `hybrid`).
3. Verify the streamed answer ends with citations like `[cms_website_content.txt, page 1]`.
4. For hybrid (Q13), confirm both the retrieved chunks *and* a successful SQL query + table render.

### C. Router-only check

In a Python REPL:

```python
import asyncio, yaml
from agent import SQLAgent
from rag import classify_question
cfg = yaml.safe_load(open("config.yaml"))
agent = SQLAgent("config.yaml")
for q in [...questions...]:
    print(q, "->", asyncio.run(classify_question(q, agent.llm_sql, rag_available=True)))
```

Expected: questions 1–12 return `rag`; question 13 returns `hybrid`.

### D. Diagnose failures

If any question routes wrong or retrieval is empty, run `python diagnose_rag.py` to surface missing embeddings model, empty Chroma collection, or model mismatch.

## Critical files (read-only reference)

- `rag.py` — `DocumentRAG.query`, `classify_question`, `ROUTE_PROMPT`, `build_rag_prompt`
- `app.py` — `_answer_question` (router → RAG/SQL/hybrid dispatch), `_answer_rag_question`
- `config.yaml` — `rag.top_k`, `rag.embedding_model`
- `ProgramData/` — ensures each category has at least one indexed file
- `diagnose_rag.py` — environment/ingestion sanity check
