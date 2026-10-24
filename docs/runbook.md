# Runbook: run the demo locally

## 1. Configure

```bash
cp .env.example .env
# set POSTGRES_PASSWORD, SHOP_ADMIN_TOKEN, OPSPILOT_API_SECRET, OPSPILOT_LLM_API_KEY
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make demo-history          # simulated deploy history in var/deploy-history
```

## 2. Start the stack

```bash
make up                    # db, migrate, shop, 3 MCP servers, api, 2 workers
docker compose ps          # everything healthy?
```

| Service | URL |
| --- | --- |
| Shop | http://localhost:8000/docs |
| OpsPilot API | http://localhost:8080/docs |
| MCP servers | http://localhost:8101/mcp, :8102, :8103 (localhost only) |

## 3. Break something and investigate

```bash
set -a; source .env; set +a                 # load secrets into this shell
make load &                                  # 2 minutes of traffic
sleep 60
python -m scripts.inject on buggy_deploy     # also records "release v1.5.0"
sleep 60
curl -s -X POST localhost:8080/incidents \
  -H "X-OpsPilot-Secret: $OPSPILOT_API_SECRET" -H "Content-Type: application/json" \
  -d '{"alarm_name": "shop-5xx-rate-high", "window_minutes": 3}'
# -> {"id": "...", "status": "queued", "deduplicated": false}

curl -s localhost:8080/incidents/<id> -H "X-OpsPilot-Secret: $OPSPILOT_API_SECRET"
curl -s localhost:8080/incidents/<id>/trace -H "X-OpsPilot-Secret: $OPSPILOT_API_SECRET"
python -m scripts.inject reset
```

Other faults: `slow_db --param delay_ms=1200`, `bad_config`, `memory_leak`,
`stock_exhaustion --param product_id=2`.

## 4. Inspect a tool by hand

```bash
npx @modelcontextprotocol/inspector   # connect to http://localhost:8101/mcp (Streamable HTTP)
```

## 5. Troubleshooting

| Symptom | Check |
| --- | --- |
| Incident stays `queued` | `docker compose logs worker`: LLM key set? MCP servers healthy? |
| Report says `unknown` | Was there traffic during the fault? Is `window_minutes` long enough? |
| `401` from the API | `X-OpsPilot-Secret` must match `OPSPILOT_API_SECRET` |
| Git tools say "no git repository" | Run `make demo-history` before `make up` |
| Port 5432 in use | Stop your local Postgres or change the db port mapping |
