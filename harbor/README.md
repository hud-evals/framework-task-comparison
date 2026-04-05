# Harbor

Minimal Harbor port of the current orders-incident demo.

Baseline run:

```bash
cd /Users/rose/dev/hud/framework-task-comparison
harbor run --path harbor --agent nop -y
```

Real run:

```bash
cd /Users/rose/dev/hud/framework-task-comparison
export OPENAI_API_KEY=...
harbor run --path harbor --agent codex --model openai/gpt-5.2 -y
```

Inspect runs:

```bash
cd /Users/rose/dev/hud/framework-task-comparison
harbor view jobs
```
