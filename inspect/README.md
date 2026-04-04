# Inspect AI

Minimal Inspect AI version of the orders-incident demo.

Run the cheap baseline:

```bash
uv sync
uv run inspect eval orders_incident.py --model mockllm/model
```

Run a real model:

```bash
export OPENAI_API_KEY=...
uv run inspect eval orders_incident.py --model openai/gpt-5.2
```

View saved logs:

```bash
uv run inspect view
```

The working copy lives at `workspace/orders_api` inside the sample sandbox.
