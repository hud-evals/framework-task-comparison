# Verifiers Demo

Minimal Prime Verifiers port of the current HUD `orders-incident` demo.

## Layout

```text
verifiers/
  README.md
  pyproject.toml
  src/orders_incident_verifiers/
    __init__.py
    orders_incident.py
    assets/
      linear_data/
      orders_api/
      tests/
```

## Local Smoke Test

```bash
cd /Users/rose/dev/hud/framework-task-comparison/verifiers
uv sync
uv run orders-incident-verifiers
```

That smoke path sets up the baseline workspace, runs the hidden grader on the untouched repo, and should report `reward: 0.0`.

## Prime Run

`prime` is external to this package. Install the package in editable mode first:

```bash
cd /Users/rose/dev/hud/framework-task-comparison/verifiers
uv sync
uv pip install -e .
export OPENAI_API_KEY=...
prime eval run orders-incident-verifiers \
  -b https://api.openai.com/v1 \
  -k OPENAI_API_KEY \
  -m gpt-5.2 \
  -n 1 \
  -r 1 \
  --skip-upload
```

The grader checks:
- the hidden pricing test on the pushed commit
- whether a branch was pushed to `origin`
- whether the agent both commented on `ENG-450` and marked it `Done`

The run here can be viewed in the CLI in the `verifiers/` folder with `prime eval tui`
