# Phase 0: Environment Setup — Implementation Plan

## Objective
Install all dependencies, configure Ollama with the target model, and create the project scaffolding (`config.yaml`, `requirements.txt`, virtual environment) so every subsequent phase can be developed and tested immediately.

---

## Pre-Implementation System Audit

### Current State (as of 2026-04-10)

| Component | Status | Details |
|-----------|--------|---------|
| **Python** | Installed | Python 3.14.3 (`C:\Users\skadt\AppData\Local\Python\bin\python.exe`) |
| **pip** | Installed | pip 26.0.1 |
| **Ollama** | Installed | v0.20.4 (`C:\Users\skadt\AppData\Local\Programs\Ollama\ollama.exe`) |
| **Virtual environment** | Not created | No `.venv/` directory exists in project |
| **Qwen2.5-Coder-14B** | Not pulled | Only `glm-5:cloud`, `glm-4.7-flash`, `minimax-m2.5:cloud` currently in Ollama |
| **pandas** | Installed (global) | v3.0.1 (already available) |
| **duckdb** | Not installed | |
| **pyarrow** | Not installed | |
| **langchain** | Not installed | |
| **chainlit** | Not installed | |
| **pyyaml** | Not installed | |
| **plotly** | Not installed | |
| **ollama (Python)** | Not installed | |

### Hardware (confirmed)
- CPU: AMD Ryzen 9 7900X (12-core / 24-thread)
- RAM: 32 GB DDR5
- GPU: NVIDIA RTX 4070 Ti SUPER (16 GB VRAM)
- OS: Windows 11

---

## Implementation Steps

### Step 1: Create `requirements.txt`

All Python dependencies for the project. Pinned to minimum versions.

```
duckdb>=1.0
pyarrow>=15.0
langchain>=0.3
langchain-community>=0.3
langchain-ollama>=0.2
chainlit>=1.1
ollama>=0.3
pyyaml>=6.0
plotly>=5.18
pandas>=2.0
```

**Notes:**
- `chainlit` replaces `streamlit` (Phase 3 UI decision)
- `langchain-ollama` is the dedicated Ollama integration (avoids deprecation warnings from `langchain-community`)
- `pandas` is already installed globally (v3.0.1) but will be in the venv for isolation

### Step 2: Create `config.yaml`

Central configuration file controlling all application behavior:
- Model settings (provider, name, base URL, temperature, retries, timeout)
- Data paths (source CSVs, Parquet output, DuckDB path, data dictionaries)
- Ingestion settings (compression, row group size, sample size)
- UI settings (title, display rows, SQL visibility, charts, theme)

### Step 3: Create Python Virtual Environment

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The `.venv/` directory is already in `.gitignore`.

### Step 4: Pull Qwen2.5-Coder-14B via Ollama

```bash
ollama pull qwen2.5-coder:14b
```

**Expected:**
- Download size: ~8.5 GB (Q4_K_M quantization)
- VRAM usage at inference: ~10 GB
- This leaves ~6 GB VRAM headroom for KV cache on the RTX 4070 Ti SUPER

### Step 5: Verify All Acceptance Criteria

| # | Check | Command | Expected |
|---|-------|---------|----------|
| 1 | Ollama has model | `ollama list` | Shows `qwen2.5-coder:14b` |
| 2 | Model responds | `ollama run qwen2.5-coder:14b "SELECT 1"` | Returns a SQL-related response |
| 3 | Pip install clean | `pip install -r requirements.txt` | No errors |
| 4 | All imports work | `python -c "import duckdb, langchain, chainlit, pyarrow, yaml; print('OK')"` | Prints `OK` |
| 5 | Config loads | `python -c "import yaml; cfg = yaml.safe_load(open('config.yaml')); print(cfg['model']['name'])"` | Prints `qwen2.5-coder:14b` |

### Step 6: Commit and Push

Commit `requirements.txt`, `config.yaml`, and `phase-0-plan.md` to GitHub.

---

## Potential Issues & Mitigations

| Issue | Likelihood | Mitigation |
|-------|-----------|------------|
| Python 3.14 compatibility with chainlit/langchain | Medium | These packages may not have 3.14 wheels yet. If pip install fails, fall back to `python3.12` or use `--pre` flag. May need to install Python 3.12 as an alternative. |
| Ollama model pull is slow (~8.5 GB) | Low | Depends on internet speed. Can run in background while other steps proceed. |
| VRAM conflicts with existing Ollama models | Low | Ollama unloads inactive models. The existing `glm-4.7-flash` (19 GB) will be unloaded when Qwen is loaded. |
| Chainlit requires Node.js | Low | Chainlit bundles its own frontend assets. No separate Node.js install needed. |

---

## Files Created in This Phase

| File | Purpose |
|------|---------|
| `phase-0-plan.md` | This document — Phase 0 implementation record |
| `requirements.txt` | Python dependency manifest |
| `config.yaml` | Central application configuration |
| `.venv/` | Python virtual environment (gitignored) |

---

## Implementation Results

### Step 1: requirements.txt — DONE
Created with 10 dependencies. All resolved cleanly on Python 3.14.3.

### Step 2: config.yaml — DONE
Created with all sections: model, data, ingestion, ui.

### Step 3: Virtual Environment — DONE
- Created `.venv/` with Python 3.14.3
- `pip install -r requirements.txt` completed successfully
- Key versions installed: duckdb 1.5.1, chainlit 2.11.0, langchain 1.2.15, langchain-ollama 1.1.0, pyarrow 23.0.1, pandas 3.0.2, plotly 6.7.0, ollama 0.6.1, pyyaml 6.0.3
- Import verification: `import duckdb, langchain, chainlit, pyarrow, yaml, pandas, plotly, ollama` — all passed
- Config verification: `yaml.safe_load(open('config.yaml'))['model']['name']` → `qwen2.5-coder:14b` — passed

### Step 4: Ollama Model Pull — DONE
- `ollama pull qwen2.5-coder:14b` completed successfully
- Model verification: `ollama list` shows `qwen2.5-coder:14b`
- Inference test: `ollama run qwen2.5-coder:14b "SELECT 1"` — responded correctly

### Step 5: Acceptance Criteria — ALL PASSED
See checklist below.

### Step 6: Committed and Pushed to GitHub

---

## Completion Criteria

Phase 0 is complete when:
- [x] `phase-0-plan.md` created and documents the implementation
- [x] `requirements.txt` created with all dependencies
- [x] `config.yaml` created with all settings
- [x] `.venv/` created and all packages installed
- [x] `qwen2.5-coder:14b` pulled in Ollama (9.0 GB)
- [x] All 5 acceptance checks pass
- [ ] Changes committed and pushed to GitHub
