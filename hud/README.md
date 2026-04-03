# Framework Task Comparison: HUD

This `hud/` directory contains the HUD implementation of the shared orders incident task.

## What it does

- exposes mock Linear tools backed by fixture JSON
- exposes mock GitHub tools backed by fixture JSON plus a local bare git repo
- copies vendored `orders-api` source into the runtime workspace
- grades with a hidden pricing test plus GitHub/Linear workflow checks

The agent works on the repo at `/home/ubuntu/workspace/orders_api` in container runs, or `.runtime/dev/workspace/orders_api` in local module-mode development.

## Repo layout

- `env.py`: thin HUD entrypoint
- `task_catalog.py`: task metadata and prompt template
- `runtime.py`: workspace setup, local bare repo setup, and grading
- `tasks.py`: task instances created via `scenario.task()`
- `source/orders_api`: vendored baseline app code
- `source/tests`: vendored grader file
- `servers/`: vendored mock GitHub and Linear MCP services

## Local development

Run these commands from `hud/`.

```bash
uv sync
uv run hud dev env:env --port 8080
```

In another terminal:

```bash
uv run hud scenario setup linear_example --url http://localhost:8080/mcp
```

Run the task objects directly:

```bash
uv run python run_tasks.py
```

Or through the CLI:

```bash
uv run hud eval tasks.py claude --model claude-sonnet-4-5
```

## Notes

The app source is vendored directly in this repo. The runtime initializes a local bare `origin` from that source so the agent can still use normal `git checkout`, `git commit`, and `git push` flows without any separate git transport server.
