# Evals

How often does OpsPilot name the right root cause, backed by real evidence?

## Scenarios

`scenarios.yaml` holds 8 staged incidents with ground truth: one per fault type, plus a
noisy variant, a low-traffic variant, a business (not technical) case and a healthy
control where the right answer is "unknown".

## Live mode (real faults, real tools, real LLM)

Needs the stack running (`make up`), `make demo-history`, an LLM key in `.env`, and
`SHOP_ADMIN_TOKEN` exported. A full run takes about 30 minutes (each scenario needs
healthy warm-up traffic so the previous fault falls out of the look-back window).

```bash
python -m evals.runner live                         # all scenarios
python -m evals.runner live --only bad_deploy_basic # one scenario
```

Outputs:
- `results/<timestamp>_<model>.json`: scores and summary (gitignored)
- `fixtures/<scenario>.json`: every tool call and LLM response, for replay (committed)

## Offline mode (CI gate)

Replays the recorded fixtures through the current agent code: no network, no API key,
deterministic. CI fails if accuracy drops below `baseline.json`.

```bash
python -m evals.runner offline
python -m evals.runner offline --update-baseline    # after a deliberate change
```

Note: offline replay re-runs the agent's own logic (parsing, budgets, grounding,
scoring) on recorded model behaviour. A prompt or model change needs a new live run
to measure its effect.

## Metrics

| Metric | Meaning |
| --- | --- |
| accuracy | predicted category == expected category |
| grounded_rate | every evidence snippet was found in real tool output |
| business_correct | stock exhaustion classified as `business`, not an outage |
| mean / p95 latency | seconds per investigation |
| mean_tool_calls, mean_tokens | cost and efficiency |

## Run log

Record every live run here: date, model, accuracy, what changed. Never edit results by hand.

| Date | Model | Accuracy | Grounded | Notes |
| --- | --- | --- | --- | --- |
| | | | | first live run pending |
