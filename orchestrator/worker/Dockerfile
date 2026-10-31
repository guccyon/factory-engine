# 使い捨てワーカー（1 タスク 1 コンテナ）。git・gh・Claude Code CLI だけを持つ。状態は持たない。
FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates jq \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.310
# YAML の検証用（ワーカーの環境に YAML パーサーがないと PR #4 / #11 / #12 で報告された）
RUN ARCH=$(dpkg --print-architecture) && curl -fsSL "https://github.com/mikefarah/yq/releases/download/v4.47.1/yq_linux_${ARCH}" -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq
# 検査ジョブ（inspect.sh）が core-gates/shadow_review.py を動かすための python3 と PyYAML
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-yaml && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1001 worker && mkdir -p /handoff && chown worker:worker /handoff   # チケットの引き継ぎのボリュームを付けるところ（ADR 0017）
USER worker
WORKDIR /work
COPY --chown=worker:worker entrypoint.sh /usr/local/bin/entrypoint.sh
# clarify（intake → clarify の最小経路）とその要求者ペルソナ
COPY --chown=worker:worker clarify.sh /usr/local/bin/clarify.sh
COPY --chown=worker:worker personas/requesters.yaml /usr/local/share/factory/requesters.yaml
# 検査ジョブ（工場の PR に L4 の shadow 判定をかける。ADR 0004）
COPY --chown=worker:worker inspect.sh /usr/local/bin/inspect.sh
# judge のジョブ（仮決めと検査の指摘を出口で裁く。ADR 0006）
COPY --chown=worker:worker judge.sh /usr/local/bin/judge.sh
# PR 本文だけの指摘を本文の追記で直すジョブ（2026-10-27）
COPY --chown=worker:worker body_fix.sh /usr/local/bin/body_fix.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
