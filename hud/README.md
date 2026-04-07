# Framework Task Comparison: HUD

Minimal HUD demo for the shared orders incident task.

Agent can see:
- a local `orders_api` checkout
- mock Linear tools backed by JSON fixtures
- grader based on the pushed branch and the Linear workflow

## Files

- `env.py`: the whole environment
- `tasks.py`: the single task definition
- `source/`: broken app plus hidden test
- `linear_mcp/`: mock Linear service
- `linear_data/`: seeded Linear ticket state

## Run It

From `hud/`:

```bash
uv sync
uv run hud eval tasks.py claude --model claude-sonnet-4-5
```

Build the environment image:

```bash
uv run hud build .
```
