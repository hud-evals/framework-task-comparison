FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y \
    bash \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3 /usr/local/bin/python
RUN python3 -m pip install --break-system-packages "fastmcp>=2,<3" pytest

COPY orders_api /app/orders_api
COPY linear_mcp /app/linear_mcp
COPY linear_state /app/linear_state

RUN set -eux; \
    cd /app/orders_api; \
    git init; \
    git checkout -b order_bug_baseline; \
    git config user.name "Agent Bot"; \
    git config user.email "agent@example.com"; \
    git add .; \
    git commit -m "Seed orders-api baseline"; \
    git clone --bare . /app/orders-api.git; \
    git remote add origin /app/orders-api.git

RUN python3 - <<'PY'
import json
import subprocess
from pathlib import Path

bare_repo = Path("/app/orders-api.git")
result = subprocess.run(
    ["git", "for-each-ref", "--format=%(refname) %(objectname)"],
    cwd=bare_repo,
    capture_output=True,
    text=True,
    check=True,
)
refs = {}
for line in result.stdout.strip().splitlines():
    parts = line.split()
    if len(parts) == 2:
        refs[parts[0]] = parts[1]
Path("/app/initial_refs.json").write_text(json.dumps(refs, indent=2) + "\n", encoding="utf-8")
PY
