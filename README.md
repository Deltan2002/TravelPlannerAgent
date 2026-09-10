# AI Travel Planner

A FastAPI, LangGraph, and React travel planner with destination research, budget-aware
itineraries, transport estimates, Redis caching, and human review.

The application works without paid credentials in demo mode. Live mode uses Serper,
Open-Meteo, Frankfurter, and optionally the OpenAI Responses API.

## What it does

- Researches destinations and returns sources, local guidance, safety notes, and weather.
- Searches for direct or connected transport from the traveler's current city.
- Separates priced and unpriced route legs so incomplete costs remain visible.
- Produces a within-budget plan and an optional stretch plan.
- Generates a weather-aware packing list.
- Scores each plan with an explainable readiness audit.
- Pauses in LangGraph for approval, rejection, or modification.
- Provides a small React workspace for creating, inspecting, and reviewing plans.
- Saves workflow checkpoints in SQLite and provider results in Redis.

## Architecture

~~~mermaid
flowchart LR
    UI[React UI] --> API[FastAPI]
    API --> GRAPH[LangGraph]
    GRAPH --> VALIDATE[Validate request]
    VALIDATE --> RESEARCH[Research agent]
    RESEARCH --> SEARCH[Search and transport]
    RESEARCH --> WEATHER[Weather or climate]
    RESEARCH --> PLAN[Itinerary agent]
    PLAN --> TOOLS[Budget and packing]
    TOOLS --> AUDIT[Readiness audit]
    AUDIT --> REVIEW{{Human review}}
    REVIEW -->|Approve| FINAL[Final plan]
    REVIEW -->|Reject| RESEARCH
    REVIEW -->|Modify| PLAN
    GRAPH <--> SQLITE[(SQLite)]
    SEARCH -. cache .-> REDIS[(Redis)]
    WEATHER -. cache .-> REDIS
    PLAN -. cache .-> REDIS
~~~

Every plan ID is also a LangGraph thread ID. SQLite checkpoints the workflow before the
review interrupt, allowing a plan to survive an application restart.

## Quick start

Python 3.11 through 3.13 is supported.

~~~bash
git clone https://github.com/Deltan2002/TravelPlannerAgent.git
cd TravelPlannerAgent
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
docker compose up -d redis
uvicorn app.main:app --reload
~~~

Open:

- React UI: <http://127.0.0.1:3000> when using Docker
- Swagger UI: <http://127.0.0.1:8000/docs>
- Health check: <http://127.0.0.1:8000/health>

For live research and OpenAI generation, update .env:

~~~dotenv
APP_MODE=live
SERPER_API_KEY=your_serper_key
LLM_PROVIDER=openai
OPENAI_API_KEY=your_openai_key
OPENAI_MODEL=gpt-5-mini
~~~

## Docker

~~~bash
docker compose up --build
~~~

Docker Compose starts the API and Redis. The data directory preserves SQLite checkpoints,
the redis-data volume preserves cached entries, and the React UI is available at
<http://127.0.0.1:3000>.

For frontend-only development, keep the API on port 8000 and run:

~~~bash
cd frontend
npm install
npm run dev
~~~

Open <http://127.0.0.1:5173>. Vite forwards `/api` requests to FastAPI.

## Create a plan

~~~bash
curl -X POST http://127.0.0.1:8000/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "current_location": "Bengaluru, India",
    "destination": "Kyoto, Japan",
    "start_date": "2026-11-10",
    "end_date": "2026-11-17",
    "budget_min": 150000,
    "budget_max": 300000,
    "currency": "INR",
    "interests": ["temples", "food", "photography"],
    "travelers": 1,
    "preferences": ["public transport", "central hotel"],
    "transport_modes": ["flight", "train", "bus"],
    "allow_transport_connections": true,
    "include_premium_fares": false
  }'
~~~

The call runs research and planning synchronously, pauses for review, and returns HTTP 201
with a plan ID and status URL.

Swagger includes connected Bengaluru-to-Kyoto and direct London-to-Paris examples.

## Review a plan

Read the current state:

~~~bash
curl http://127.0.0.1:8000/plan/PLAN_ID
~~~

Approve the within-budget plan:

~~~json
{"action": "approve"}
~~~

Approve the stretch plan:

~~~json
{"action": "approve", "plan_choice": "stretch"}
~~~

Reject and repeat research:

~~~json
{
  "action": "reject",
  "feedback": "Find stronger transport evidence."
}
~~~

Modify itinerary details without repeating research:

~~~json
{
  "action": "modify",
  "feedback": "Make day two slower.",
  "modifications": {
    "hotel_preference": "Quiet hotel near the station",
    "day_changes": [
      {
        "day": 2,
        "replace_activities_with": ["Tea ceremony", "Riverside walk"],
        "note": "Keep the afternoon relaxed."
      }
    ]
  }
}
~~~

Submit these bodies to POST /plan/{id}/review. Retrieve an approved plan from
GET /plan/{id}/final.

A finalized plan cannot be modified. Create a new plan when changes are required after
approval.

## How planning works

### Transport

The live search includes the origin, destination, requested dates, modes, and currency.
Results are filtered for route relevance, explicit prices, trip direction, cabin class,
and source quality.

Important response fields:

| Field | Meaning |
|---|---|
| fare_basis | Whether per-person pricing was explicit or assumed |
| round_trip_basis | Explicit round trip or an explicit one-way fare doubled |
| fare_date_basis | Exact dates, partial dates, or dates not shown |
| price_scope | Complete route or primary leg only |
| priced_legs | Legs included in the displayed total |
| unpriced_legs | Legs shown without inventing a fare |
| connection_schedule_status | Whether connected legs were schedule-matched |

Premium economy, business, and first-class fares are excluded unless
include_premium_fares is true.

Transport prices are search-derived estimates, not live inventory. A
fare_date_basis value of dates_not_shown means the search used the requested dates, but
the source did not confirm availability for those dates.

### Budget

budget_min and budget_max represent the complete trip budget for all travelers, including
origin transport.

- The within-budget plan targets the midpoint and never exceeds budget_max.
- The stretch plan is capped at 15% above budget_max, is generated independently with
  enhanced destination-specific options, and requires explicit approval.
- Selected transport is deducted before allocating destination spending.

The remaining amount is allocated as:

| Category | Share |
|---|---:|
| Lodging | 35% |
| Food | 20% |
| Activities | 25% |
| Local transport | 15% |
| Contingency | 5% |

These values are spending allocations, not verified booking quotes.

### Weather and packing

- Trips starting within 15 days use an Open-Meteo live forecast.
- Later trips use the same dates from the previous five years as a climate estimate.
- Provider failures return generic seasonal guidance without invented measurements.
- Packing suggestions respond to rain, heat, cold, trip length, and user interests.

### Readiness audit

Each available scenario receives draft_readiness or stretch_readiness with a score out of
100 and ten deterministic checks.

| Check | Weight |
|---|---:|
| Date coverage | 15 |
| Budget compliance | 15 |
| Activity spending | 10 |
| Complete transport route | 15 |
| Fare-date evidence | 10 |
| Connection schedule | 10 |
| Fare source evidence | 10 |
| Weather confidence | 5 |
| Activity sources | 5 |
| Preference coverage | 5 |

- pass receives full points.
- warning receives half points and still allows approval.
- blocker receives zero points and prevents approval.

The complete audit also appears inside awaiting_input.readiness for the human reviewer.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | /health | Application health |
| POST | /plan | Create and generate a plan |
| GET | /plan/{id} | Current plan and workflow state |
| POST | /plan/{id}/review | Approve, reject, or modify |
| GET | /plan/{id}/final | Approved final plan |

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| APP_MODE | demo | demo or live provider mode |
| DATABASE_PATH | data/travel_planner.sqlite3 | SQLite checkpoint database |
| SERPER_API_KEY | empty | Live search credential |
| LLM_PROVIDER | deterministic | deterministic or openai |
| OPENAI_API_KEY | empty | OpenAI credential |
| OPENAI_MODEL | gpt-5-mini | Responses API model |
| OPENAI_TIMEOUT_SECONDS | 120 | Per-call OpenAI read timeout |
| HTTP_TIMEOUT_SECONDS | 20 | Other provider timeout |
| REDIS_URL | empty | Redis connection URL |
| CACHE_TTL_SECONDS | 900 | Provider-cache lifetime |
| MAX_REVISIONS | 5 | Reject/modify limit |

Redis caches successful search, weather, currency, and identical OpenAI requests. Cache
keys are hashed, and a Redis failure does not fail plan creation. Every POST /plan still
creates a new workflow and plan ID.

OpenAI writes only the research narrative and itinerary content. Python attaches and
validates transport, weather, budget, packing, and source data. If OpenAI is unavailable,
the deterministic generator completes the plan.

## Project structure

| Path | Responsibility |
|---|---|
| app/main.py | FastAPI routes and Swagger examples |
| app/service.py | Dependency setup and plan lifecycle |
| app/workflow/graph.py | LangGraph nodes, routing, and human interrupt |
| app/agents/ | Research and itinerary agents |
| app/tools/web_search.py | Search and transport extraction |
| app/tools/destination_context.py | Forecast and historical climate |
| app/tools/currency.py | Reference currency conversion |
| app/tools/planning.py | Budget, packing, and readiness tools |
| app/models.py | Request, response, and validation models |
| app/prompts.py | OpenAI prompts |
| app/cache.py | Redis cache wrapper |
| frontend/src/App.jsx | React trip form, results, readiness, and review workspace |
| frontend/src/api.js | Browser API client |
| frontend/nginx.conf | Production UI hosting and API proxy |

## Current limitations

- Transport comes from search snippets rather than booking APIs.
- Fares with dates_not_shown are not confirmed for the requested dates.
- Connected legs are not guaranteed to form a valid schedule.
- Hotel, restaurant, and activity availability is not live.
- POST /plan waits synchronously for generation.
- SQLite and the in-process review lock are intended for one application instance.
- Authentication and automated tests are not currently included.

## Recommended improvements

1. **Background processing:** Return HTTP 202 and run planning in a worker such as Celery,
   RQ, ARQ, or BullMQ.
2. **Production database:** Replace SQLite with PostgreSQL for multiple instances.
3. **Authentication and ownership:** Associate plans with authenticated users.
4. **External API reliability:** Add retries, backoff, rate limits, and fallback providers.
5. **Prompt validation:** Sanitize PII, resist prompt injection, and constrain tool use.
6. **Research quality:** Prioritize official sources and store citation dates.
7. **Travel providers:** Add live flight, rail, hotel, restaurant, and route APIs.
8. **User interface:** Add accounts, saved-plan lists, and richer loading progress to the
   basic React workspace.
9. **Monitoring:** Add structured logs, traces, metrics, alerts, and cost tracking.
10. **Concurrent review:** Add plan versions and database-backed concurrency checks.
11. **Automated testing:** Add unit, endpoint, workflow, persistence, contract, and load tests.
12. **Security and privacy:** Use secret management, encryption, redaction, and retention rules.
13. **Quality evaluation:** Extend readiness checks with opening hours, duration overlap,
    realistic travel times, accessibility, and deeper preference evaluation.

## Code quality

~~~bash
ruff check .
~~~

With the API running, scripts/demo.py creates a plan, reads it, approves it, and retrieves
the final result.
