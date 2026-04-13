"""
RAG (Retrieval-Augmented Generation) over CMS Open Payments documentation.

Parses PDF documents from the ProgramData/ folder, chunks them, embeds with
nomic-embed-text via Ollama, and stores in ChromaDB for semantic retrieval.

Usage:
    python rag.py --ingest          # Build/rebuild the vector store
    python rag.py --query "What is Open Payments?"   # Test a query
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy imports — these are heavy libraries; only import when actually needed
# so that the rest of the application doesn't pay the import cost if RAG is
# disabled or unavailable.
# ---------------------------------------------------------------------------


def _import_pymupdf():
    try:
        import pymupdf  # noqa: F811
        return pymupdf
    except ImportError:
        raise ImportError(
            "pymupdf is required for RAG. Install with: pip install pymupdf"
        )


def _import_chromadb():
    try:
        import chromadb  # noqa: F811
        return chromadb
    except ImportError:
        raise ImportError(
            "chromadb is required for RAG. Install with: pip install chromadb"
        )


# ---------------------------------------------------------------------------
# Text chunking — simple recursive splitter (avoids pulling in langchain
# just for splitting).
# ---------------------------------------------------------------------------

SEPARATORS = ["\n\n", "\n", ". ", " "]


def _recursive_split(
    text: str,
    chunk_size: int = 3200,
    chunk_overlap: int = 200,
    separators: list[str] | None = None,
) -> list[str]:
    """Split *text* into chunks of at most *chunk_size* characters with overlap."""
    if separators is None:
        separators = list(SEPARATORS)

    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    sep = separators[0] if separators else ""
    remaining_seps = separators[1:] if len(separators) > 1 else []
    parts = text.split(sep) if sep else list(text)

    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = (current + sep + part) if current else part
        if len(candidate) > chunk_size and current:
            # Current chunk is full — flush it.
            if len(current) > chunk_size and remaining_seps:
                # Still too big — recurse with a finer separator.
                chunks.extend(
                    _recursive_split(current, chunk_size, chunk_overlap, remaining_seps)
                )
            else:
                chunks.append(current)
            # Start new chunk with overlap from the end of the previous one.
            overlap_text = current[-chunk_overlap:] if chunk_overlap else ""
            current = overlap_text + sep + part if overlap_text else part
        else:
            current = candidate

    if current.strip():
        if len(current) > chunk_size and remaining_seps:
            chunks.extend(
                _recursive_split(current, chunk_size, chunk_overlap, remaining_seps)
            )
        else:
            chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------


def _extract_pdf_pages(
    pdf_path: Path,
) -> list[tuple[int, str]]:
    """Return list of (page_number, text) for each page in *pdf_path*."""
    pymupdf = _import_pymupdf()
    pages: list[tuple[int, str]] = []
    with pymupdf.open(str(pdf_path)) as doc:
        for page_num in range(len(doc)):
            text = doc[page_num].get_text()
            if text and text.strip():
                pages.append((page_num + 1, text))
    return pages


# ---------------------------------------------------------------------------
# Ollama embedding helper
# ---------------------------------------------------------------------------


def _sanitize_text(text: str) -> str:
    """Remove characters that can cause embedding API failures."""
    # Replace common problematic Unicode with ASCII equivalents.
    replacements = {
        "\u2013": "-", "\u2014": "-",  # en-dash, em-dash
        "\u2018": "'", "\u2019": "'",  # curly single quotes
        "\u201c": '"', "\u201d": '"',  # curly double quotes
        "\u2022": "-",  # bullet
        "\u00a0": " ",  # non-breaking space
        "\u2026": "...",  # ellipsis
        "\ufffd": "",  # replacement character
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Strip any remaining non-ASCII control characters (keep printable Unicode).
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or (ord(ch) >= 32))


def _embed_texts_single(text: str, model: str, base_url: str) -> list[float] | None:
    """Embed a single text, returning None on failure."""
    import urllib.request

    url = f"{base_url}/api/embed"
    sanitized = _sanitize_text(text)
    payload = json.dumps({"model": model, "input": [sanitized]}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        return data["embeddings"][0]
    except Exception:
        return None


def _embed_texts(
    texts: list[str], model: str, base_url: str
) -> tuple[list[list[float]], list[int] | None]:
    """Embed a batch of texts using the Ollama /api/embed endpoint.

    Returns (embeddings, failed_indices). *failed_indices* is None when the
    whole batch succeeded, or a list of indices that were skipped when the
    batch had to fall back to per-chunk embedding.
    """
    import urllib.request

    sanitized = [_sanitize_text(t) for t in texts]
    url = f"{base_url}/api/embed"
    payload = json.dumps({"model": model, "input": sanitized}).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read())
        return data["embeddings"], None
    except Exception:
        # Batch failed — fall back to one-at-a-time embedding.
        log.warning("Batch embedding failed, falling back to per-chunk embedding")
        results: list[list[float]] = []
        ok_indices: list[int] = []
        for i, t in enumerate(sanitized):
            emb = _embed_texts_single(t, model, base_url)
            if emb is None:
                log.warning("Skipping chunk %d that failed to embed: %s...", i, t[:80])
            else:
                results.append(emb)
                ok_indices.append(i)
        if not results:
            raise RuntimeError("All chunks in batch failed to embed")
        failed = [i for i in range(len(sanitized)) if i not in ok_indices]
        return results, failed


def _check_model_available(model: str, base_url: str) -> bool:
    """Return True if *model* is pulled in Ollama."""
    import urllib.request

    try:
        url = f"{base_url}/api/tags"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        names = [m["name"] for m in data.get("models", [])]
        # Ollama tags can be "nomic-embed-text:latest" — match prefix.
        return any(n == model or n.startswith(f"{model}:") for n in names)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# DocumentRAG — main class
# ---------------------------------------------------------------------------

# Category derived from subdirectory name.
_DIR_TO_CATEGORY = {
    "faq": "faq",
    "law_policy": "law_policy",
    "publication_data_dictionary_methodology": "data_dictionary",
    "user_guides": "user_guide",
}


class DocumentRAG:
    """Semantic search over CMS Open Payments PDF documentation."""

    def __init__(self, config: dict | None = None) -> None:
        rag_cfg = (config or {}).get("rag", {})
        self._pdf_dir = Path(rag_cfg.get("pdf_dir", "./ProgramData"))
        self._store_dir = Path(rag_cfg.get("vectorstore_dir", "./data/vectorstore"))
        self._embedding_model = rag_cfg.get("embedding_model", "nomic-embed-text")
        self._top_k = int(rag_cfg.get("top_k", 5))
        self._max_file_size_mb = float(rag_cfg.get("max_file_size_mb", 50))
        self._chunk_size = int(rag_cfg.get("chunk_size", 3200))
        self._chunk_overlap = int(rag_cfg.get("chunk_overlap", 200))
        self._base_url = (config or {}).get("model", {}).get(
            "base_url", "http://localhost:11434"
        )
        self._collection_name = "cms_open_payments_docs"

        self._client = None  # Lazy-init ChromaDB
        self._collection = None

    # -- ChromaDB lazy init --------------------------------------------------

    def _get_collection(self):
        if self._collection is not None:
            return self._collection
        chromadb = _import_chromadb()
        self._store_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self._store_dir))
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    # -- Public API ----------------------------------------------------------

    def is_available(self) -> bool:
        """True if the vector store exists and has indexed documents."""
        try:
            col = self._get_collection()
            return col.count() > 0
        except Exception:
            return False

    def ingest(self, force_rebuild: bool = False) -> int:
        """Parse PDFs, chunk, embed, and store. Returns number of chunks indexed."""
        if not self._pdf_dir.exists():
            log.warning("PDF directory %s does not exist", self._pdf_dir)
            return 0

        # Check embedding model availability.
        if not _check_model_available(self._embedding_model, self._base_url):
            log.error(
                "Embedding model '%s' not found in Ollama. "
                "Run: ollama pull %s",
                self._embedding_model,
                self._embedding_model,
            )
            return 0

        col = self._get_collection()

        if force_rebuild:
            # Drop and recreate.
            chromadb = _import_chromadb()
            self._client.delete_collection(self._collection_name)
            self._collection = self._client.get_or_create_collection(
                name=self._collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            col = self._collection

        # Build a manifest of PDFs and text files to process.
        pdf_files = sorted(self._pdf_dir.rglob("*.pdf"))
        txt_files = sorted(self._pdf_dir.rglob("*.txt"))
        all_files = pdf_files + txt_files
        if not all_files:
            log.warning("No PDF or text files found in %s", self._pdf_dir)
            return 0

        total_chunks = 0
        for pdf_path in all_files:
            size_mb = pdf_path.stat().st_size / (1024 * 1024)
            if size_mb > self._max_file_size_mb:
                log.info(
                    "Skipping %s (%.1f MB > %.1f MB limit)",
                    pdf_path.name,
                    size_mb,
                    self._max_file_size_mb,
                )
                continue

            # Determine category from parent directory or file name.
            parent_name = pdf_path.parent.name.lower()
            if "cms_website" in pdf_path.name.lower():
                category = "website"
            else:
                category = _DIR_TO_CATEGORY.get(parent_name, "other")

            # Check if already indexed (by file hash).
            file_hash = _file_hash(pdf_path)
            existing = col.get(where={"file_hash": file_hash}, limit=1)
            if existing and existing["ids"]:
                log.info("Already indexed: %s", pdf_path.name)
                continue

            log.info("Processing: %s (%.1f MB, category=%s)", pdf_path.name, size_mb, category)

            # Extract text — PDF page by page, text files as a single "page".
            try:
                if pdf_path.suffix.lower() == ".txt":
                    text = pdf_path.read_text(encoding="utf-8", errors="ignore")
                    pages = [(1, text)] if text.strip() else []
                else:
                    pages = _extract_pdf_pages(pdf_path)
            except Exception as e:
                log.error("Failed to parse %s: %s", pdf_path.name, e)
                continue

            if not pages:
                log.info("No text extracted from %s", pdf_path.name)
                continue

            # Chunk each page.
            all_chunks: list[dict] = []
            for page_num, page_text in pages:
                chunks = _recursive_split(
                    page_text,
                    chunk_size=self._chunk_size,
                    chunk_overlap=self._chunk_overlap,
                )
                for ci, chunk_text in enumerate(chunks):
                    chunk_id = f"{file_hash}_{page_num}_{ci}"
                    all_chunks.append(
                        {
                            "id": chunk_id,
                            "text": chunk_text,
                            "metadata": {
                                "source_file": pdf_path.name,
                                "page_number": page_num,
                                "chunk_index": ci,
                                "category": category,
                                "file_hash": file_hash,
                            },
                        }
                    )

            if not all_chunks:
                continue

            # Embed in batches and upsert.
            batch_size = 32
            for i in range(0, len(all_chunks), batch_size):
                batch = all_chunks[i : i + batch_size]
                texts = [c["text"] for c in batch]
                try:
                    embeddings, failed = _embed_texts(
                        texts, self._embedding_model, self._base_url
                    )
                except Exception as e:
                    log.error("Embedding failed at batch %d: %s", i, e)
                    continue

                if failed:
                    # Some chunks were skipped — filter batch to only succeeded.
                    ok_set = set(range(len(batch))) - set(failed)
                    batch = [batch[j] for j in sorted(ok_set)]
                    texts = [c["text"] for c in batch]

                col.add(
                    ids=[c["id"] for c in batch],
                    documents=texts,
                    embeddings=embeddings,
                    metadatas=[c["metadata"] for c in batch],
                )
                total_chunks += len(batch)

            log.info("  -> %d chunks from %s", len(all_chunks), pdf_path.name)

        log.info("Ingestion complete: %d total chunks indexed", total_chunks)
        return total_chunks

    # Categories ordered by priority for general questions.  Higher-priority
    # categories contain concise, authoritative answers (FAQ, website, data
    # dictionary) while lower-priority ones are verbose (user guides, law).
    _PRIORITY_CATEGORIES = ["faq", "website", "data_dictionary", "law_policy", "user_guide"]

    # Score bonus for high-priority categories so concise, authoritative
    # sources rank above verbose user-guide pages.
    _CATEGORY_BOOST = {
        "faq": 0.15,
        "website": 0.15,
        "data_dictionary": 0.10,
        "law_policy": 0.0,
        "user_guide": -0.05,
        "other": 0.0,
    }

    def query(
        self,
        question: str,
        top_k: int | None = None,
        category: str | None = None,
    ) -> list[dict]:
        """Retrieve the most relevant document chunks for *question*.

        Returns a list of dicts with keys: text, source_file, page_number, score.
        When *category* is None, results from all categories are merged with
        priority boosting so FAQ/website content ranks above verbose guides.
        """
        k = top_k or self._top_k
        col = self._get_collection()
        if col.count() == 0:
            return []

        # Embed the question.
        try:
            embeddings, _ = _embed_texts(
                [question], self._embedding_model, self._base_url
            )
            q_embedding = embeddings[0]
        except Exception as e:
            log.error("Failed to embed query: %s", e)
            return []

        if category:
            # Single-category query — no boosting needed.
            results = col.query(
                query_embeddings=[q_embedding],
                n_results=k,
                where={"category": category},
            )
            return self._format_results(results)

        # Multi-category query with priority boosting.  Query each priority
        # category separately (top-3 each), merge, re-rank with boosts.
        candidates: list[dict] = []
        for cat in self._PRIORITY_CATEGORIES:
            try:
                results = col.query(
                    query_embeddings=[q_embedding],
                    n_results=3,
                    where={"category": cat},
                )
                for item in self._format_results(results):
                    boost = self._CATEGORY_BOOST.get(item["category"], 0.0)
                    item["boosted_score"] = item["score"] + boost
                    candidates.append(item)
            except Exception:
                continue

        # Sort by boosted score and return top-k.
        candidates.sort(key=lambda x: x["boosted_score"], reverse=True)
        return candidates[:k]

    @staticmethod
    def _format_results(results: dict) -> list[dict]:
        """Convert ChromaDB query results to a list of dicts."""
        if not results or not results["documents"] or not results["documents"][0]:
            return []
        output = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            output.append(
                {
                    "text": doc,
                    "source_file": meta.get("source_file", ""),
                    "page_number": meta.get("page_number", 0),
                    "category": meta.get("category", ""),
                    "score": 1 - dist,  # cosine distance → similarity
                }
            )
        return output


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _file_hash(path: Path) -> str:
    """Return a short SHA-256 hex digest for deduplication."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# RAG prompt templates
# ---------------------------------------------------------------------------

RAG_ANSWER_PROMPT = """\
You are answering a question about CMS Open Payments policy, methodology,
or program rules based on official CMS documentation.

Relevant documentation excerpts:
{context}

Question: {question}

Instructions:
- Answer based ONLY on the documentation excerpts above.
- If the excerpts don't contain enough information, say so clearly.
- Cite which document and page your answer comes from.
- Be concise and direct.
"""


def build_rag_prompt(question: str, chunks: list[dict]) -> str:
    """Build a RAG answer prompt from retrieved chunks."""
    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        source = chunk.get("source_file", "Unknown")
        page = chunk.get("page_number", "?")
        context_parts.append(
            f"---\n[Source {i}: {source}, page {page}]\n{chunk['text']}\n---"
        )
    context = "\n\n".join(context_parts)
    return RAG_ANSWER_PROMPT.format(context=context, question=question)


# ---------------------------------------------------------------------------
# Query routing
# ---------------------------------------------------------------------------

# Patterns that strongly indicate a data/SQL question.
SQL_INDICATORS = [
    "how much", "how many", "total", "top ", "compare",
    "trend", "by year", "by state", "by specialty",
    "which companies", "who received", "payments to",
    "spending on", "average", "count of", "sum of",
    "highest", "lowest", "most", "least",
    "breakdown", "distribution", "per year",
    "in 2018", "in 2019", "in 2020", "in 2021",
    "in 2022", "in 2023", "in 2024",
]

# Patterns that strongly indicate a policy/documentation question.
POLICY_INDICATORS = [
    "what is open payments", "what are open payments",
    "open payments program", "open payments work",
    "about open payments", "explain open payments",
    "who is required to report", "who must report",
    "reporting requirements", "reporting threshold",
    "covered recipient definition", "what is a covered recipient",
    "what is a covered", "define covered recipient",
    "sunshine act", "section 6002", "cfr 403",
    "delay in publication", "dispute process",
    "what counts as", "is it required",
    "exemption", "exempt from",
    "de minimis", "penalty", "enforcement",
    "compliance", "what does the law",
    "how does open payments work",
    "what are the rules", "reporting entity",
    "methodology", "data dictionary",
    "what does cms", "cms require",
    "transfer of value", "what is a transfer",
    "applicable manufacturer", "what is an applicable",
    "group purchasing organization", "what is a gpo",
    "teaching hospital", "what qualifies",
    "noncovered recipient", "non-covered recipient",
    "program year", "publication cycle",
    "open payments registration",
    "affordable care act", "aca ", "section 6002",
    "physician payments sunshine act",
    "what types of payments", "what kind of payments",
    "who reports", "who has to report",
    "what must be reported", "what is reported",
    "dispute resolution", "review and dispute",
    "attestation", "data submission",
]


def _normalize_for_routing(text: str) -> str:
    """Normalize text for routing: lowercase, strip special chars (™®© etc)."""
    import unicodedata
    # Replace trademark/copyright symbols and other special marks with space.
    cleaned = ""
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("S") or cat.startswith("M"):  # Symbols, Marks
            cleaned += " "
        else:
            cleaned += ch
    # Collapse multiple spaces and lowercase.
    return " ".join(cleaned.lower().split())


def classify_question(
    question: str, rag_available: bool = False
) -> str:
    """Classify a question as 'sql', 'rag', or 'hybrid'.

    Returns 'sql' if RAG is not available, regardless of question type.
    """
    if not rag_available:
        return "sql"

    q_lower = _normalize_for_routing(question)

    sql_score = sum(1 for p in SQL_INDICATORS if p in q_lower)
    policy_score = sum(1 for p in POLICY_INDICATORS if p in q_lower)

    if policy_score >= 2 and sql_score == 0:
        return "rag"
    if policy_score >= 1 and sql_score >= 1:
        return "hybrid"
    if policy_score >= 1 and sql_score == 0:
        return "rag"
    return "sql"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    parser = argparse.ArgumentParser(description="RAG over CMS documentation")
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Parse PDFs, embed, and build the vector store",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild the vector store from scratch",
    )
    parser.add_argument(
        "--query",
        type=str,
        help="Run a test query against the vector store",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show vector store status",
    )
    args = parser.parse_args()

    config = _load_config()
    rag = DocumentRAG(config)

    if args.ingest or args.rebuild:
        count = rag.ingest(force_rebuild=args.rebuild)
        print(f"\nIngested {count} chunks into vector store.")

    elif args.query:
        if not rag.is_available():
            print("Vector store is empty. Run --ingest first.")
            sys.exit(1)
        results = rag.query(args.query)
        print(f"\nTop {len(results)} results for: {args.query}\n")
        for i, r in enumerate(results, 1):
            print(f"--- Result {i} (score: {r['score']:.3f}) ---")
            print(f"Source: {r['source_file']}, page {r['page_number']}")
            print(f"Category: {r['category']}")
            print(r["text"][:500])
            print()

    elif args.status:
        if rag.is_available():
            col = rag._get_collection()
            print(f"Vector store: {col.count()} chunks indexed")
        else:
            print("Vector store is empty or not initialized.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
