# 工場の前段（GitHub ログインと HTTPS）

工場をインターネットに置くとき（パイロットの EC2 など）に、操業コンソールの前に GitHub のログインを置き、AI 越しの口（事業部の窓口）を HTTPS で開く。判断は [ADR 0035](../../docs/adr/0035-edge-login-proxy.md)。

Mac の中で動かすとき（`http://localhost:8790`）は使わない。`compose.yaml` と工場のコードは何も変えず、`compose.edge.yaml` を重ねたときだけ効く。

```
利用者のブラウザ ──HTTPS──▶ edge（Caddy）──┬─ console.<EDGE_DOMAIN> ─ oauth2-proxy で確かめる ─▶ console:8790
                    ▲ IP 制限                ├─ proto.<EDGE_DOMAIN>   ─ 同じログイン         ─▶ console:8791（動くプロトタイプ）
利用者の Claude ─────┘（セキュリティグループ）└─ desk.<EDGE_DOMAIN>    ─ ログインは通さない   ─▶ desk:8792（窓口のトークンで確かめる）
（窓口の MCP、stdio）
```

| 入口 | 通る人 | 誰かの記録 |
|---|---|---|
| `console.<EDGE_DOMAIN>`・`proto.<EDGE_DOMAIN>` | `EDGE_GITHUB_USERS` に書いた GitHub アカウントでログインした人 | oauth2-proxy のログ（`docker compose logs oauth2-proxy` の `AuthSuccess`）。コンソールの操作の記録は今は `CONSOLE_OPERATOR` のまま |
| `desk.<EDGE_DOMAIN>` | 窓口のトークン（`fdk_...`）を持つ人。1 人 1 本 | 窓口の events（`desk.opened` などの `by`、`desk.message` の `author`）にトークンの名前 |

どの入口も、まずクラウドのファイアウォールの IP 制限で不特定多数を遮る。

## 用意するもの

1. **ドメイン**: 工場用の名前を 1 つ決め（例 `factory.example.com`）、DNS に `console`・`proto`・`desk` の 3 つ（かワイルドカード `*.factory.example.com`）の A レコードを EC2 の固定の IP（Elastic IP）に向ける
2. **GitHub の OAuth App**: GitHub の Settings → Developer settings → OAuth Apps → New OAuth App
   - Homepage URL: `https://console.<EDGE_DOMAIN>`
   - Authorization callback URL: `https://console.<EDGE_DOMAIN>/oauth2/callback`
   - 作ったら Client ID と、Generate a new client secret の値を控える（秘密。`.env` にだけ置く）
   - 持ち主は工場を運用する人の個人アカウントか組織。どちらでも通す人は `EDGE_GITHUB_USERS` で決まる
3. **セキュリティグループ**（受信）
   - 443/tcp: 通す人の送信元の IP だけ（自宅・事務所の固定 IP など）
   - 80/tcp: `0.0.0.0/0`。Let's Encrypt が証明書の持ち主を確かめる（HTTP-01）ために要る。80 で返すのは確かめの応答と HTTPS への転送だけ
   - 22/tcp（SSH）は運用する人の IP だけ。ほかのポート（5433・7070・8787・8790〜8794）は開けない（`compose.yaml` はどれも `127.0.0.1` で待つ）

## `.env` に足す

```bash
# 前段を重ねる（docker compose と更新役が両方を読む）
COMPOSE_FILE=compose.yaml:compose.edge.yaml

EDGE_DOMAIN=factory.example.com
EDGE_GITHUB_CLIENT_ID=...
EDGE_GITHUB_CLIENT_SECRET=...
# ログインの Cookie の鍵。次で作る:
#   python3 -c 'import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
EDGE_COOKIE_SECRET=...
# 通す GitHub アカウント（カンマ区切り。大文字小文字はそのまま）
EDGE_GITHUB_USERS=account-a,account-b
# セキュリティグループと同じ送信元（空白区切り）。二重目の制限
EDGE_ALLOW_CIDRS=198.51.100.10/32 203.0.113.0/24
```

`CONSOLE_ALLOWED_HOSTS`・`DESK_ALLOWED_HOSTS`・`PROTOTYPE_PUBLIC_URL`・`CONSOLE_URL`（通知のリンク）は `compose.edge.yaml` が `EDGE_DOMAIN` から足すので、`.env` に書かなくてよい。

## 上げる

```bash
cd orchestrator
docker compose config -q            # 足りない値があればここで止まる
docker compose up -d
docker compose logs -f edge         # 証明書を取れたか（certificate obtained successfully）
```

`https://console.<EDGE_DOMAIN>` を開くと GitHub のログインに回り、許した人だけがコンソールに戻る。許していないアカウントは 500（missing github user）で止まり、Cookie は出ない。

## AI 越しの口（1 人 1 本の窓口のトークン）

コンソールのログインとは別に、窓口（ADR 0030）のトークンを 1 人 1 本出す。名前は人ごとに分ける（共用しない）。窓口の記録（`desk.opened`・`desk.request_confirmed` などの `by`、`desk.message` の `author`）にその名前が残り、誰が頼んだかを後で追える。

```bash
# 健康監視をする運用主（すべての相談を見て、優先度・保留を変えられる）
docker compose exec dispatcher factoryctl desk token create <運用主の名前> --role operator
# アプリの作成を指示する人（対象の製品について相談を開き、自分の依頼のチケットだけを動かせる）
docker compose exec dispatcher factoryctl desk token create <依頼者の名前> --role requester --product owner/repo
docker compose exec dispatcher factoryctl desk token list
docker compose exec dispatcher factoryctl desk token revoke <名前>   # 失くした・やめたとき
```

トークンは発行したときに 1 回だけ出る。本人に安全な経路（パスワード管理の共有など）で渡し、チャットやリポジトリに書かない。本人は手元の Claude に窓口の MCP を登録し、`FACTORY_DESK_URL` を前段に向ける（[desk-mcp/README.md](../desk-mcp/README.md)）。

```bash
claude mcp add factory-desk -e FACTORY_DESK_URL=https://desk.<EDGE_DOMAIN> \
  -e FACTORY_DESK_TOKEN_FILE=$HOME/.config/factory/desk.token -- python3 /path/to/factory_desk_mcp.py
```

送信元の IP がセキュリティグループに入っていないと、窓口にも届かない（手元の Claude も同じ機械から繋ぐため）。

## 人を足す・外す

- 足す: `EDGE_GITHUB_USERS` に足して `docker compose up -d oauth2-proxy`。AI 越しに頼むなら窓口のトークンも出す。送信元の IP をセキュリティグループと `EDGE_ALLOW_CIDRS` に足す
- 外す: `EDGE_GITHUB_USERS` から抜いて `docker compose up -d oauth2-proxy`、窓口のトークンを revoke。ログイン済みの Cookie は `EDGE_COOKIE_EXPIRE`（既定 24h）まで残るので、すぐ締め出すときは `EDGE_COOKIE_SECRET` も作り直す（全員がログインし直しになる）

## ロードバランサーを使うとき

ACM の証明書でロードバランサー（ALB）が TLS を終えるなら、Caddy は HTTP で受ける。

- `.env` に `EDGE_SCHEME=http`、`EDGE_HTTPS_PORT` は使わない
- ALB のリスナー 443 → EC2 の 80。IP 制限は ALB のセキュリティグループで行い、EC2 の 80 は ALB からだけ受ける
- `EDGE_ALLOW_CIDRS` は既定（すべて）のままにする（Caddy から見える送信元は ALB になるため）

## 外す

前段を外す（個別のエンドポイントに認証を入れたあと、か Mac に戻すとき）:

1. `.env` の `COMPOSE_FILE` から `compose.edge.yaml` を抜く（`EDGE_*` は残っていても何も起きない）
2. `docker compose up -d --remove-orphans`（edge と oauth2-proxy が止まって消え、コンソール・窓口の開いてよいホスト名と通知のリンクは `compose.yaml` の値に戻る）
3. 証明書のボリュームを捨てるなら `docker volume rm factory_edgedata factory_edgeconfig`

## 守っていること

- 利用者から来た `X-Auth-Request-*` のヘッダーは前段で捨て、ログインを確かめた後だけ oauth2-proxy の値を付ける。今のコンソールはこのヘッダーを読まないが、後で各エンドポイントが「前段が確かめた人」を信じるときの前提になる
- oauth2-proxy が `X-Forwarded-*` を信じるのは compose の網の中からだけ（`OAUTH2_PROXY_TRUSTED_PROXY_IPS`）。oauth2-proxy のポートは外に出さない
- Cookie は `SameSite=None; Secure`。動くプロトタイプは CSP の sandbox で不透明なオリジンになり、そこからの CSS・画像の要求に Cookie が付かないと前段で止まるため。コンソールの操作は画面に埋めた合言葉のヘッダーで他サイトからの要求を断っている（`console.py`）
- GitHub に求める権限は `user:email` だけ（組織・リポジトリは見ない）

## 確かめ方（手元）

GitHub の代わりに偽の OAuth の相手を立てて、ログインの流れを通せる（oauth2-proxy の `OAUTH2_PROXY_LOGIN_URL`・`OAUTH2_PROXY_REDEEM_URL`・`OAUTH2_PROXY_VALIDATE_URL` を偽の相手に向け、Caddy は `local_certs` で）。ADR 0035 の作成時に確かめたこと:

- 未ログインの console・proto は `https://console.<EDGE_DOMAIN>/oauth2/start?rd=...` に回る
- 許したアカウントはログイン後に console・proto（CSS）に届き、上流に `X-Auth-Request-User` が付く（利用者が送った偽の値は上書き）
- 許していないアカウントはログインで止まり、Cookie が出ない
- desk はログインなしで上流に届き、`Authorization` がそのまま渡る
- `EDGE_ALLOW_CIDRS` の外からは 3 つとも 403
