# OpsPilot

**An AI on-call agent that investigates production incidents.** When an alarm fires,
OpsPilot reads logs, metrics and recent deploys through MCP tools and returns a
root-cause report in which every piece of evidence is copied from real tool output.
It never changes anything: it investigates and recommends, and a human acts.

![CI](https://github.com/damodharg6/opspilot/actions/workflows/ci.yml/badge.svg)

**Showcase:** [opspilot-showcase.netlify.app](https://opspilot-showcase.netlify.app): watch
four real investigations replay step by step (buggy deploy, slow database, stock
exhaustion, memory leak), plus the architecture, the AI roadmap and a
[plain-English blog](https://opspilot-showcase.netlify.app/blog/).
**End-to-end guide (PDF):** [docs/OpsPilot-end-to-end.pdf](docs/OpsPilot-end-to-end.pdf).

## Showcase

The site in [`showcase/`](showcase/) is static and hosted on Netlify (config and security
headers in [netlify.toml](netlify.toml)). The demos it replays are recordings of real runs
of the full stack (API, queue, workers, MCP servers, LLM), captured by `make record-demos`.

```bash
make up && make showcase        # http://localhost:8090, with a "Run live" button per demo
make record-demos               # refresh the recordings after a prompt or model change
```

Run locally, the site gets a **Run live** button: it stages the incident on your machine
(healthy traffic, fault on, alarm) and streams the investigation into the page. The
browser never sees the API secret; [scripts/showcase_server.py](scripts/showcase_server.py)
holds it and only listens on `127.0.0.1`.

Deploy (Netlify free tier): preview first, then production.

```bash
npx netlify-cli deploy --dir showcase --no-build          # draft URL to check
npx netlify-cli deploy --dir showcase --no-build --prod   # opspilot-showcase.netlify.app
```

## How it works

```mermaid
flowchart LR
    ALARM["CloudWatch alarm"] -->|SNS, Lambda| API["Incidents API<br/>202 + queue"]
    API --> DB[("Postgres<br/>queue, reports, traces")]
    W["Workers x N"] -->|SKIP LOCKED| DB
    W <-->|tool calls| MCP["MCP servers<br/>logs, metrics, git"]
    W <--> LLM["LLM<br/>Groq, Gemini, OpenAI, Ollama"]
    MCP --> SIG[("CloudWatch Logs + Metrics<br/>deploy history")]
```

1. A demo **shop** service (FastAPI + Postgres) writes one JSON log line per request.
2. A **fault switch** breaks it realistically: slow DB, bad config, memory leak, buggy deploy, or stock exhaustion (a business issue, not an outage).
3. An **alarm** creates an incident. The API returns `202` and queues it.
4. A **worker** claims it and runs the **agent loop**: the LLM calls read-only MCP tools (`count_errors`, `search_logs`, `get_metric`, `recent_commits`, `commit_diff`) under hard budgets for tool calls, tokens and time.
5. The report is **schema-validated** and **grounded**: evidence that does not appear in real tool output is dropped and confidence is lowered.
6. An **eval suite** replays staged incidents and gates CI on accuracy.

### Example trace

From an end-to-end run of the real services (shop, MCP servers over HTTP, API,
Postgres queue, worker) with a **scripted** model standing in for the LLM:

| Step | Tool | Result |
| --- | --- | --- |
| 1 | `count_errors(group_by=error_type)` | 51 errors, all `KeyError` |
| 2 | `recent_commits(hours=2)` | `release v1.5.0: apply promotion discounts to order totals` |
| 3 | `search_logs("status>=500 path=/orders")` | 51 matching lines, newest first |

The scripted answer included one fabricated evidence line on purpose; the grounding
check removed it and lowered confidence from `high` to `medium`.

## Evaluation

8 staged scenarios with ground truth (one per fault, a noisy variant, a low-traffic
variant, a business case, and a healthy control). See [evals/README.md](evals/README.md).

| Date | Model | Accuracy | Grounded | Notes |
| --- | --- | --- | --- | --- |
| 2026-10-24 | gemini-3.5-flash, gemini-3.5-flash-lite | 4/4 | yes: 3 unproven snippets dropped | Showcase subset (bad deploy, slow DB, stock exhaustion, memory leak), run through the real API, queue and workers; recordings in [showcase/data](showcase/data) |

The full 8-scenario suite (`make evals`) has not been run live yet; the row above covers 4 of
its scenarios.

## Run it locally

```bash
cp .env.example .env            # fill in secrets + an LLM API key (Groq/Gemini free tiers work)
make install                    # pip install -e ".[dev]"
make demo-history
make up                         # docker compose: db, shop, 3 MCP servers, api, 2 workers
```

Then follow [docs/runbook.md](docs/runbook.md) to break the shop and watch an investigation.

```bash
make test                       # unit + MCP + agent tests (Postgres tests skipped)
make test-pg                    # everything, against a throwaway Postgres in Docker
make lint
```

`make test-pg` needs Docker running and port 5435 free. It starts a disposable
`postgres:16-alpine` container, waits until it accepts connections, runs the whole
suite with `TEST_DATABASE_URL` pointing at it, and stops the container afterwards,
even when tests fail or you press Ctrl-C. To use your own database instead:
`TEST_DATABASE_URL=postgresql+psycopg://... make test`. Override the port with
`make test-pg PG_TEST_PORT=5436`.

## Deploy to AWS

Terraform for EC2, CloudWatch metric filters and alarms, SNS, an in-VPC Lambda alarm
forwarder, SSM secrets, budget alerts and a GitHub OIDC deploy role:
[infra/README.md](infra/README.md).

## Design

- [docs/architecture.md](docs/architecture.md): components, incident lifecycle, design
  decisions and trade-offs, failure modes, scaling path, AI roadmap, security model
- [docs/OpsPilot-end-to-end.pdf](docs/OpsPilot-end-to-end.pdf): the whole workflow end to
  end in one document, from alarm to stored report, with file and function references
- [docs/engineering-rules.md](docs/engineering-rules.md): how the project is built and changed

Highlights:
- **Async API + Postgres job queue** with dedup (partial unique index), `SKIP LOCKED`
  claims, leases, fencing and retries with backoff
- **Stateless MCP servers** over streamable HTTP with pluggable local/AWS backends
- **Provider-agnostic LLM layer** with its own retry policy and token accounting
- **Safety**: read-only tools, whitelisted query language, output caps, secret
  redaction, untrusted-output wrapping, grounding check
- **CI/CD**: lint, tests with a Postgres service, offline eval gate, Terraform
  validate, Docker build; deploys via GitHub OIDC + SSM (no SSH keys)

## Project layout

```text
common/        JSON logging, correlation ids, redaction
shop/          demo service + fault injection
mcp_servers/   logs, metrics, git MCP servers; backends/local.py, backends/aws.py
opspilot/      llm/ (providers), agent/ (loop, prompts, report, grounding),
               api/ (incidents API), worker/ (queue consumer), db/ (models, queue)
migrations/    Alembic
evals/         scenarios, runner (live + offline replay), scoring
scripts/       load generator, fault injector, simulated deploy history, smoke check,
               demo recorder and showcase server
showcase/      static showcase site (Netlify) + recorded demo runs in data/
infra/         Terraform, Lambda alarm forwarder
```

## Limitations

- The incidents are staged in a demo service; real systems are noisier.
- Fault state lives in one shop process (fine for one container).
- Offline evals replay recorded model behaviour; a prompt or model change needs a
  new live run to be measured.
- The deploy history in the demo is simulated (a separate git repo), clearly labelled.

## Tech stack

Python 3.12 · FastAPI · SQLAlchemy 2 · Alembic · Postgres 16 · MCP Python SDK ·
OpenAI-compatible LLM APIs · Docker Compose · AWS (EC2, CloudWatch, SNS, Lambda, SSM,
IAM, Budgets) · Terraform · GitHub Actions · pytest · ruff
