# 使い捨てワーカー（1 タスク 1 コンテナ）。git・gh・Claude Code CLI だけを持つ。状態は持たない。
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates jq \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.310
RUN useradd -m -u 1001 worker
USER worker
WORKDIR /work
COPY --chown=worker:worker entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
