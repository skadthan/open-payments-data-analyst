"""Scripted smoke test for Phase 2. Exercises a representative set of
questions through SQLAgent and writes per-query results to stdout for
capture into phase-2-plan.md."""
from __future__ import annotations

import sys
import time

from agent import SQLAgent


QUERIES: list[tuple[str, bool]] = [
    # (question, should_use_prior_history_as_followup)
    ("How many general payment records are there in 2024?", False),
    ("Top 10 companies by total payment amount across all years.", False),
    ("Now show me the same ranking but only for 2023.", True),
    ("Which 5 medical specialties received the largest total general payments in 2024?", False),
    ("What are the top 5 therapeutic areas by total research funding in 2024?", False),
    ("How many physicians have ownership interests across all years?", False),
    ("What is the weather in Baltimore?", False),
    ("In the state of TX, what is the highest paid nature of payment category in General Payment Category in 2024 for the covered recipient type is 'Covered Recipient Physician', also give me the total number of records for that nature of payment category?", False),
]


def main() -> int:
    agent = SQLAgent("config.yaml")
    history: list[tuple[str, str]] = []
    overall_start = time.perf_counter()

    for i, (q, is_followup) in enumerate(QUERIES, 1):
        print("=" * 72)
        print(f"[{i}/{len(QUERIES)}] Q: {q}")
        print(f"    (using history={len(history)} exchanges)")
        sys.stdout.flush()

        t0 = time.perf_counter()
        result = agent.run_query(q, history if is_followup else [])
        elapsed = time.perf_counter() - t0

        print(f"    attempts: {result['attempts']}   elapsed: {elapsed:.1f}s")
        if result["error"]:
            print(f"    ERROR: {result['error']}")
            print(f"    last SQL: {result['sql']!r}")
        else:
            print(f"    SQL:")
            for line in (result["sql"] or "").splitlines():
                print(f"      {line}")
            df = result["data"]
            if df is not None and not df.empty:
                print(f"    rows: {len(df)}    cols: {list(df.columns)}")
                print(f"    head:\n{df.head(10).to_string(index=False)}")
            else:
                print(f"    (empty result)")
            print(f"    answer: {result['answer']}")

            # Only append to history if it succeeded — we want the follow-up
            # query to have the ranking context from the prior answer.
            history.append((q, result["answer"] or ""))
            history = history[-4:]

        print()
        sys.stdout.flush()

    agent.close()
    print(f"Total wall time: {time.perf_counter() - overall_start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
