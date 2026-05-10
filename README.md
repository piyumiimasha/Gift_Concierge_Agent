# Gift Concierge Agent

An AI-powered gift recommendation assistant for [kapruka.com](https://www.kapruka.com) — Sri Lanka's leading online gifting platform. Built as AEE Bootcamp Mini Project 03.

The agent finds personalised gifts, remembers recipient preferences, checks delivery across all Sri Lankan districts, and ensures gift safety through a reflection loop.

---

## Architecture Overview

```
User Message
     │
     ▼
┌─────────────────────────────────────────────────────┐
│                  GiftOrchestrator                   │
│                                                     │
│  1. Load ST history     ← SupabaseSTStore           │
│  2. Load profiles       ← SupabaseProfileStore      │
│  3. Classify intent     ← IntentRouter (Groq)       │
│                                                     │
│  ┌──────────┬─────────────────┬──────────────────┐  │
│  │  search  │preference_update│ logistics_check  │  │
│  └────┬─────┴────────┬────────┴────────┬─────────┘  │
│       │              │                 │             │
│  CatalogAgent   inline handler   LogisticsAgent     │
│  (RAG+CRAG)     (LLM extract)   (rules+narration)  │
│                                                     │
│  4. Persist both turns  → SupabaseSTStore           │
│  5. Return OrchestratorResponse                     │
└─────────────────────────────────────────────────────┘
```

---

## The 4 Parts

### Part 1 — Playwright Crawler
Headless browser scraper that builds the product catalog from kapruka.com.

```
PlaywrightCrawler
  ├── 20 categories (cakes, flowers, chocolates, clothing, electronics, …)
  ├── JavaScript load-more pagination (click + DOM mutation wait)
  ├── Auto-detects product card CSS selectors
  ├── Progressive saves after each category
  └── Output: data/catalog.json  (~650 products)
```

### Part 2 — 3-Tier Cognitive Memory Stack

```
Tier 1 — Short-Term Memory (Supabase)
  └── Ring buffer of ConversationTurns
      ├── Auto-prune by count (max 20 turns per session)
      └── Auto-prune by TTL  (1 hour expiry)

Tier 2 — Long-Term RAG (Qdrant Cloud)
  └── Product catalog as semantic vectors
      ├── OpenRouter text-embedding-3-small (1536 dims)
      ├── Cosine similarity search
      └── Category payload index for filtered search

Tier 3 — Semantic Profiles (Supabase)
  └── RecipientProfile per user/recipient
      ├── preferences, dislikes, budget_lkr
      ├── past_gifts, upcoming_occasions
      └── Case-insensitive name lookup
```

### Part 3 — Specialist Orchestration

```
IntentRouter  →  4 intents (search / preference_update / logistics_check / chitchat)
                 Groq JSON mode — no regex, fully deterministic

CatalogAgent  →  RAG pipeline
                 1. Enrich query with profile preferences
                 2. Map occasion keywords to category filter
                 3. Qdrant semantic search (k=8, threshold=0.30)
                 4. Post-filter dislikes + past gifts
                 5. Groq LLM picks 2–3 best, writes warm recommendation

LogisticsAgent → Hybrid: rule-based lookup + LLM narration
                 25 Sri Lankan districts across 4 delivery zones
                 (same-day / next-day / 2-day / extended)

Orchestrator  →  Inline handlers for preference_update + chitchat
                 Lazy sub-component init, full LangFuse tracing
```

### Part 4 — Reflection Loop (CRAG)

```
CatalogAgent.recommend() — extended with Draft → Reflect → Revise

  Step 1  DRAFT   Groq LLM generates initial recommendation from RAG results
  Step 2  REFLECT Groq LLM critiques draft against recipient dislikes/allergies
                  → "NO_VIOLATIONS" if safe
                  → lists specific violations if unsafe
  Step 3  REVISE  (only if violations found)
                  Groq LLM rewrites recommendation avoiding flagged products

Reflection only runs when profile.dislikes or profile.notes is non-empty.
Metadata fields: reflection_triggered, violations_found
```

---

## Project Structure

```
Gift_Concierge_Agent/
│
├── data/
│   └── catalog.json                  # 650 scraped kapruka products
│
├── notebooks/
│   └── test_orchestrator.ipynb       # End-to-end test notebook
│
├── scripts/
│   ├── init_supabase.py              # Create DB tables
│   └── ingest_catalog.py             # Embed + upsert catalog to Qdrant
│
├── src/
│   ├── agents/
│   │   ├── orchestrator.py           # Main entry point — GiftOrchestrator
│   │   ├── router.py                 # IntentRouter (Groq JSON mode)
│   │   ├── catalog_agent.py          # RAG + Reflection Loop
│   │   ├── logistics_agent.py        # Delivery feasibility agent
│   │   ├── prompts/
│   │   │   ├── agent_prompts.py      # Router/Catalog/Logistics/Chitchat/Reflect/Revise
│   │   │   └── memory_prompts.py     # Distill + Recall prompts
│   │   └── tools/
│   │       └── playwright_crawler.py # Kapruka scraper
│   │
│   ├── memory/
│   │   ├── schemas.py                # ConversationTurn, RecipientProfile, CatalogProduct
│   │   ├── st_store.py               # Tier 1 — Supabase ring buffer
│   │   ├── rag_store.py              # Tier 2 — Qdrant semantic search
│   │   ├── profile_store.py          # Tier 3 — Supabase recipient profiles
│   │   └── embedder.py               # OpenRouter text-embedding-3-small
│   │
│   ├── services/
│   │   └── delivery_zones.py         # 25 Sri Lankan districts, rule-based lookup
│   │
│   └── infastructure/
│       ├── observability.py          # LangFuse tracing + prompt management
│       └── db/
│           ├── supabase_client.py    # Supabase singleton client
│           ├── sql_client.py         # SQLAlchemy engine for DDL
│           └── supabase_schema.py    # Table DDL (st_turns, recipient_profiles)
│
└── .env                              # API keys (not committed)
```

---

## Tech Stack

### LLM & AI
| Tool | Purpose |
|---|---|
| **Groq** `llama-3.3-70b-versatile` | All LLM chat calls — routing, recommendations, logistics narration, reflection, preference extraction |
| **OpenRouter** `text-embedding-3-small` | Product catalog embeddings (1536 dims) and query embeddings at search time |

### Vector Database
| Tool | Purpose |
|---|---|
| **Qdrant Cloud** | Stores 650 product vectors, cosine similarity search, category payload index for filtered queries |

### Database
| Tool | Purpose |
|---|---|
| **Supabase** (PostgreSQL) | Short-term conversation turns (`st_turns` table), recipient profiles (`recipient_profiles` table) |
| **SQLAlchemy** | DDL execution (Supabase REST client doesn't support multi-statement DDL) |

### Observability
| Tool | Purpose |
|---|---|
| **LangFuse** | Distributed tracing for every LLM call, prompt versioning (local fallbacks when prompts not in dashboard), cost tracking |
| **Loguru** | Structured application logging |

### Web Scraping
| Tool | Purpose |
|---|---|
| **Playwright** | Headless Chromium browser, JS-rendered page scraping, load-more pagination |

### Infrastructure
| Tool | Purpose |
|---|---|
| **python-dotenv** | Environment variable management |
| **Jupyter** | Interactive testing notebook |
| **ipykernel** | Jupyter kernel for `.venv` |

---

## Memory Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    3-Tier Memory Stack                          │
│                                                                 │
│  Tier 1: Short-Term (Supabase st_turns)                        │
│  ┌──────────────────────────────────────────┐                  │
│  │  session ring buffer  max 20 turns       │                  │
│  │  TTL: 1 hour          oldest-first order │                  │
│  └──────────────────────────────────────────┘                  │
│                                                                 │
│  Tier 2: Long-Term RAG (Qdrant kapruka_catalog)                │
│  ┌──────────────────────────────────────────┐                  │
│  │  650 product vectors  1536 dims          │                  │
│  │  cosine similarity    category index     │                  │
│  └──────────────────────────────────────────┘                  │
│                                                                 │
│  Tier 3: Semantic Profiles (Supabase recipient_profiles)       │
│  ┌──────────────────────────────────────────┐                  │
│  │  per user/recipient   JSONB fields       │                  │
│  │  preferences[]        dislikes[]         │                  │
│  │  budget_lkr           past_gifts[]       │                  │
│  └──────────────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Intent Routing

| Intent | Trigger example | Handler |
|---|---|---|
| `search` | "Find a birthday gift for my wife" | `CatalogAgent.recommend()` |
| `preference_update` | "My mum loves orchids, budget LKR 5000" | Inline — LLM extracts JSON → `profile_store.upsert()` |
| `logistics_check` | "Can you deliver to Jaffna by Saturday?" | `LogisticsAgent.check()` |
| `chitchat` | "Hi! What can you help me with?" | Inline — Groq with profile context |

---

## Delivery Zones (Sri Lanka)

| Zone | Districts | Delivery | Surcharge |
|---|---|---|---|
| Same-Day | Colombo, Gampaha, Kalutara | 0–1 day | LKR 0 |
| Next-Day | Kandy, Galle, Matara, Kurunegala, Ratnapura, Kegalle, Badulla, Nuwara Eliya | 1–2 days | LKR 250 |
| 2-Day | Anuradhapura, Polonnaruwa, Matale, Ampara, Hambantota, Monaragala, Puttalam, Trincomalee | 2–3 days | LKR 400 |
| Extended | Jaffna, Vavuniya, Mannar, Mullaitivu, Kilinochchi, Batticaloa | 3–5 days | LKR 600 |

---

## Setup

### 1. Prerequisites
- Python 3.10+
- Accounts: Supabase, Qdrant Cloud, Groq, OpenRouter, LangFuse

### 2. Install dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Environment variables
Create a `.env` file:
```env
GROQ_API_KEY=...
OPENROUTE_API_KEY=...
SUPABASE_URL=...
SUPABASE_SERVICE_KEY=...
SUPABASE_DB_URL=postgresql://postgres:...
QDRANT_URL=...
QDRANT_API_KEY=...
QDRANT_COLLECTION_NAME=kapruka_catalog
LANGFUSE_PUBLIC_KEY=...
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://us.cloud.langfuse.com
```

### 4. Initialise the database
```bash
python scripts/init_supabase.py
```

### 5. Scrape the catalog
```bash
python -m src.agents.tools.playwright_crawler
```

### 6. Ingest catalog into Qdrant
```bash
python scripts/ingest_catalog.py
```

### 7. Run the test notebook
Open `notebooks/test_orchestrator.ipynb` and run all cells.

---

## Catalog Categories

`cakes` · `flowers` · `chocolates` · `clothing` · `electronics` · `fashion` · `softtoys` · `grocery` · `hampers` · `greetingcards` · `sports` · `baby` · `jewellery` · `cosmatics` · `customisedgifts` · `pharmacy` · `home_lifestyle` · `combogifts` · `books` · `fruitbaskets`
