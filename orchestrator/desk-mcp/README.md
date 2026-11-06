# 事業部の窓口の MCP サーバー（stdio）

ADR 0030 の順番 3。依頼者の手元の Claude Code・Claude Desktop から、事業部の窓口（`orchestrator/dispatcher/desk_api.py`）に依頼と問い合わせを持ち込むための薄い包み。判断も状態も持たず、道具ごとに窓口の API を 1 回呼ぶ（API が正本）。

- 依存は python3（3.9 以上）の標準ライブラリだけ。このファイル 1 つを手元に写せば動く
- トークンは窓口の運用主が発行する: `docker compose exec dispatcher factoryctl desk token create <名前> --role requester --product owner/repo`
- 窓口は既定で Mac の中だけで開く（`http://localhost:8792`）。工場をクラウドに置くときは前段（ADR 0035、`orchestrator/edge/README.md`）を通して `FACTORY_DESK_URL=https://desk.<EDGE_DOMAIN>` に向ける。トークンは 1 人 1 本（共用しない）。リモートの MCP と OAuth は ADR 0030 の順番 5

## 登録

Claude Code:

```bash
claude mcp add factory-desk -e FACTORY_DESK_TOKEN_FILE=$HOME/.config/factory/desk.token -- python3 /path/to/factory_desk_mcp.py
```

Claude Desktop（`claude_desktop_config.json`）:

```json
{"mcpServers": {"factory-desk": {"command": "python3", "args": ["/path/to/factory_desk_mcp.py"],
  "env": {"FACTORY_DESK_TOKEN_FILE": "/Users/<you>/.config/factory/desk.token"}}}}
```

`FACTORY_DESK_URL`（既定 `http://localhost:8792`）・`FACTORY_DESK_TOKEN`（ファイルの代わりに直接）も使える。

## 道具

| 道具 | API | 使いどころ |
|---|---|---|
| `desk_open` | `POST /api/desk/conversations` | 依頼（request）か問い合わせ（inquiry）を開く。依頼者の言葉はそのまま渡す |
| `desk_reply` | `POST .../<id>/messages` | 相談役の問いに答える・下書きの直したいところを言う |
| `desk_read` | `GET .../<id>?after&wait` | 問い・下書き・作ったチケットを読む。`wait`（60 秒まで）で返事を待つ |
| `desk_confirm` | `POST .../<id>/confirm` | 依頼者が同意した下書きを投入する（確認 1 回） |
| `desk_list` | `GET /api/desk/conversations` | 自分の相談の一覧 |
| `desk_ticket` | `GET /api/desk/tickets/<ref>` | チケットの段・順番・PR |
| `desk_set_priority` | `POST /api/desk/tickets/<ref>/priority` | 明確な指示（「#812 を先に」）。壁打ちを通さない |

流れの例: `desk_open`（依頼）→ `desk_read`（wait 60）で問いを受けて依頼者に見せる → `desk_reply` → … → 下書きが来たら依頼者に見せ、同意を得て `desk_confirm` → `desk_read` で作ったチケットを確かめる → 以後は `desk_ticket`。

## テスト

```bash
python3 -m pytest -q orchestrator/desk-mcp   # TEST_DATABASE_URL があれば、本物の窓口の API を立てて stdio で 1 本通す
```
