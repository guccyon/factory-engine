"""窓口の MCP の道具の写し（ヘルプの「API と MCP」用）。手で直さない。

正本は orchestrator/desk-mcp/factory_desk_mcp.py。直したら python3 dispatcher/reference.py --sync-mcp で作り直す
"""
import json

SNAPSHOT = json.loads(r'''{
 "server": {
  "name": "factory-desk",
  "version": "0.5.0"
 },
 "setup": "事業部の窓口の MCP サーバー（stdio）。ADR 0030 の順番 3。\n\n依頼者の手元の Claude Code・Claude Desktop から窓口の API（orchestrator/dispatcher/desk_api.py）を呼ぶための薄い包み。\n判断も状態も持たない。道具ごとに API を 1 回呼び、返ってきた JSON をそのまま返す（API が正本）。\n相談役（プランナー）の返事は非同期に来るので、会話を読む道具は最大の待ち時間（wait、60 秒まで）を渡せる。\n\n依存: python3（3.9 以上、標準ライブラリだけ）。どこに置いても動く（工場のリポジトリの外に写してよい）。\n\n設定（環境変数）:\n  FACTORY_DESK_URL         窓口の API（既定 http://localhost:8792）\n  FACTORY_DESK_TOKEN       窓口のトークン（factoryctl desk token create で発行。fdk_...）\n  FACTORY_DESK_TOKEN_FILE  トークンを置いたファイル（FACTORY_DESK_TOKEN がなければこれを読む）\n\nClaude Code への登録の例:\n  claude mcp add factory-desk -e FACTORY_DESK_TOKEN_FILE=$HOME/.config/factory/desk.token -- python3 /path/to/factory_desk_mcp.py",
 "tools": [
  {
   "name": "desk_open",
   "description": "事業部の窓口に相談を開く。kind=request（依頼: 何かを進めてほしい。相談役のプランナーが問いを返し、確定したらタスクのまとまりの下書きを出す）か kind=inquiry（問い合わせ: 進み具合や理由を聞く）。返事は非同期なので、開いたあと desk_read で wait を付けて読む",
   "inputSchema": {
    "type": "object",
    "properties": {
     "kind": {
      "type": "string",
      "enum": [
       "request",
       "inquiry"
      ]
     },
     "product": {
      "type": "string",
      "description": "対象の製品（owner/repo）"
     },
     "body": {
      "type": "string",
      "description": "依頼者の言葉（言い換えずにそのまま）"
     },
     "level": {
      "type": "string",
      "enum": [
       "p0",
       "p1",
       "p2",
       "p3"
      ],
      "description": "希望の優先度（p0 緊急・p1 急ぎ・p2 通常・p3 後回し）"
     },
     "due": {
      "type": "string",
      "description": "希望の期限（例 2026-11-30、今月中）"
     },
     "notify": {
      "type": "array",
      "items": {
       "type": "string"
      },
      "description": "提出の知らせの受け取り方（依頼者が選んだときだけ）。工場の通知の設定にある宛先の名前か desk（窓口の会話だけ）。省けば既定（Slack）"
     }
    },
    "required": [
     "kind",
     "product",
     "body"
    ]
   }
  },
  {
   "name": "desk_reply",
   "description": "相談に返事・言い足しを書く（相談役の問いへの答え、下書きの直したいところ）。書くと相談役が次の周回で動く。工場が結果を提出した後は answer で受取確認に答える（accept 受け取った・more 続きを頼む・return 差し戻す。more と return は body に一言）。工場が依頼を断ったとき（提出が断り）は、accept 納得した・more 別案で進める（別の依頼として開く。body に案の番号や一言を添えてよい）・return 断りを差し戻す（body に理由）。answer は依頼者が答えを決めたときだけ付ける",
   "inputSchema": {
    "type": "object",
    "properties": {
     "conversation_id": {
      "type": "integer"
     },
     "body": {
      "type": "string"
     },
     "answer": {
      "type": "string",
      "enum": [
       "accept",
       "more",
       "return"
      ]
     }
    },
    "required": [
     "conversation_id"
    ]
   }
  },
  {
   "name": "desk_read",
   "description": "会話を読む。after（最後に見た発言の id）より後の発言と、最新の下書き（draft）・作ったチケットを返す。status に今のステップ・次に起きること・いつ頃か（by）・振り分けの結果・途中の様子・提出・断りの理由と代わりの案・答えの選択肢がまとまっている。依頼者にはまず status を伝える。wait（秒、60 まで）を渡すと新しい発言が来るまで待つ。次に読むときは返ってきた last_id を after に渡す",
   "inputSchema": {
    "type": "object",
    "properties": {
     "conversation_id": {
      "type": "integer"
     },
     "after": {
      "type": "integer",
      "default": 0
     },
     "wait": {
      "type": "integer",
      "minimum": 0,
      "maximum": 60,
      "default": 0
     }
    },
    "required": [
     "conversation_id"
    ]
   }
  },
  {
   "name": "desk_confirm",
   "description": "下書きを確認して投入する（依頼者の確認 1 回）。draft_id は desk_read の draft.draft_id。依頼者が内容と優先度に同意したときだけ呼ぶ。工場が次の周回で Issue を起票し、会話に作ったチケットが書かれる。決まった操作に振り分けた依頼（状態 awaiting_confirm）は、draft_id の代わりに answer（run 実行する・cancel やめる）で答える",
   "inputSchema": {
    "type": "object",
    "properties": {
     "conversation_id": {
      "type": "integer"
     },
     "draft_id": {
      "type": "integer"
     },
     "answer": {
      "type": "string",
      "enum": [
       "run",
       "cancel"
      ]
     },
     "level": {
      "type": "string",
      "enum": [
       "p0",
       "p1",
       "p2",
       "p3"
      ],
      "description": "チケットに付ける優先度（省くと相談を開いたときの希望、それもなければ p2）"
     }
    },
    "required": [
     "conversation_id"
    ]
   }
  },
  {
   "name": "desk_list",
   "description": "自分の相談の一覧（運用主はすべて）。新しい順",
   "inputSchema": {
    "type": "object",
    "properties": {}
   }
  },
  {
   "name": "desk_ticket",
   "description": "チケットの状態（段・優先度・保留・依存待ち・順番待ちの何番目か・作業中のジョブ・PR）。ticket は工場のチケットの id か owner/repo#123",
   "inputSchema": {
    "type": "object",
    "properties": {
     "ticket": {
      "type": "string"
     }
    },
    "required": [
     "ticket"
    ]
   }
  },
  {
   "name": "desk_set_priority",
   "description": "明確な指示（「#812 を先に」）でチケットの優先度を変える。壁打ちを通さずすぐ効く。依頼者が変えられるのは自分の依頼から作ったチケットだけ",
   "inputSchema": {
    "type": "object",
    "properties": {
     "ticket": {
      "type": "string"
     },
     "level": {
      "type": "string",
      "enum": [
       "p0",
       "p1",
       "p2",
       "p3"
      ]
     },
     "reason": {
      "type": "string"
     },
     "conversation_id": {
      "type": "integer",
      "description": "相談の中の指示なら、その相談の id"
     }
    },
    "required": [
     "ticket",
     "level"
    ]
   }
  },
  {
   "name": "inbox_list",
   "description": "受信トレイ（工場が人の判断を待っているもの）のうち、このトークンで答えてよい件の一覧。待ちの長い順。各件の key を inbox_read・inbox_answer に渡す。answerable が false の件はここでは答えられない（url の先で答える）",
   "inputSchema": {
    "type": "object",
    "properties": {
     "include_answered": {
      "type": "boolean",
      "default": false,
      "description": "回答済で反映を待っている件も入れる"
     }
    }
   }
  },
  {
   "name": "inbox_read",
   "description": "受信トレイの 1 件を読む。detail に問い・今の案・他の案・judge の見立て・影響・推す案、answer に選択肢（options と extra の id）・推奨・答えたときの作用（effect）・取り返しがつかないか（irreversible）が入る。判断の材料が足りなければ、url の PR の差分や製品のリポジトリを調べて本人の問いに答える",
   "inputSchema": {
    "type": "object",
    "properties": {
     "key": {
      "type": "string"
     }
    },
    "required": [
     "key"
    ]
   }
  },
  {
   "name": "inbox_answer",
   "description": "受信トレイの 1 件に答える。option は inbox_read の answer.options か extra の id。本人がはっきり選んだときだけ呼び、自分で選ばない。extra の note_required の選択肢と印（flag）は note（一言）が要る。irreversible の件（本番に出すなど）は、本人がその答えを明示して承認したときだけ confirm: true を付ける。先に別の入口で答えられていたら、誰が何と答えたかが返る",
   "inputSchema": {
    "type": "object",
    "properties": {
     "key": {
      "type": "string"
     },
     "option": {
      "type": "string"
     },
     "note": {
      "type": "string",
      "description": "理由・直し方の一言（本人の言葉）"
     },
     "raw": {
      "type": "string",
      "description": "本人の答えの元の文（記録に残す）"
     },
     "on": {
      "type": "string",
      "description": "違和感の印（flag）を付ける案・画面の id"
     },
     "confirm": {
      "type": "boolean",
      "description": "取り返しのつかない作用を本人が明示して承認した"
     }
    },
    "required": [
     "key",
     "option"
    ]
   }
  },
  {
   "name": "desk_preferences",
   "description": "自分の好みの一覧（工場が窓口と作業で踏まえる、あなた個人の好み。例「表より箇条書き」「数字は税抜き」）。id・好み・効く業務（空ならすべて）・確かさ（confirmed 本人が言った／hypothesis 工場の推し量り）。本人に見せ、直す・消すかを聞く",
   "inputSchema": {
    "type": "object",
    "properties": {
     "product": {
      "type": "string",
      "description": "製品（owner/repo）で絞る"
     }
    }
   }
  },
  {
   "name": "desk_preference_set",
   "description": "好みを足すか直す。id を省くと足す（product と statement が要る）、id（P-NNNN）を渡すとその好みを直す。本人がはっきり言った好みだけを、本人の言葉に近い形で書く。直すと確認済みになる",
   "inputSchema": {
    "type": "object",
    "properties": {
     "id": {
      "type": "string",
      "description": "直す好みの id（P-NNNN）。足すときは省く"
     },
     "product": {
      "type": "string"
     },
     "statement": {
      "type": "string",
      "description": "好み（400 字まで）"
     },
     "work": {
      "type": "array",
      "items": {
       "type": "string"
      },
      "description": "効く業務（例 desk.converse 窓口のやりとり、analyze.report 報告書、dev.design 画面のデザイン）。省くとすべての作業"
     }
    }
   }
  },
  {
   "name": "desk_preference_delete",
   "description": "好みを消す（行ごと消える）。本人が消してと言ったときだけ呼ぶ",
   "inputSchema": {
    "type": "object",
    "properties": {
     "id": {
      "type": "string"
     }
    },
    "required": [
     "id"
    ]
   }
  }
 ]
}''')
