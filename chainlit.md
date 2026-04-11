# CMS Open Payments Data Analyst

A natural-language interface to the **CMS Open Payments** program — the
national disclosure dataset of financial relationships between
pharmaceutical and medical device manufacturers and U.S. physicians and
teaching hospitals, covering **calendar years 2021 through 2024**
(about 55 million records across general payments, research payments,
and ownership interests).

Ask questions in plain English. The agent translates them to DuckDB SQL,
runs them locally, and explains the results. Everything runs on your
machine — no cloud, no API keys, no data leaves the host.

## Example questions

- *Top 10 companies by total payment amount across all years*
- *Which medical specialties received the most general payments in 2024?*
- *Top 5 therapeutic areas by research funding in 2024*
- *Monthly payment trends for 2024*
- *How many physicians have ownership interests across all years?*

## How it works

Each turn, the agent generates a DuckDB SQL query from your question
(visible as a collapsible "Generating SQL" step), executes it against the
local database, and summarizes the result in plain English. If the first
SQL has an error, it self-corrects and retries up to 3 times.

## Stack

- **DuckDB** for analytical SQL over Parquet files
- **Ollama** + **Qwen2.5-Coder 14B** for local Text-to-SQL
- **Chainlit** for the chat UI
- **Plotly** for interactive charts

See the project README and `phase-*.md` plans on disk for the full design.
