"""操業コンソールの画面（console.py が配る）。

中身は console_static/ の静的ファイル（ADR 0059。ビルドなし、外部の CDN なし）: index.html（どのページにも返す殻）、
brand.css（ブランドの層）・tokens.css（意味の層）・components.css（部品の層）・console.css（ページの CSS）・console.js（画面の JS）、
catalog.html・catalog.js（部品のカタログ。/catalog）。

ページは URL で分ける（2026-11-07 オーナー）: / ホーム（ダッシュボード）、/inbox と /inbox/<key>（受信トレイとメッセージの詳細の 2 ペイン）、
/line（ライン）、/ship（出荷）、/products と /products/<owner>/<repo>（製品。中身は products_page.py の断片を読み込む）、/usage（使用量。中身は usage_page.py の断片を読み込む）、/help（ヘッダの数字と言葉の意味）と
/help/api・/help/cli（API と MCP・factoryctl のリファレンス。中身は GET /api/reference。コードの説明から作る。reference.py）、
/settings/<項目>（右上の歯車。ランナー・モード・ラインと流量・Mac・工場の版）。
サーバーはどのページにも同じ HTML を返し、画面の中で URL に合うページを描く。データは /api/state の 1 本（10 秒ごと）
"""
import os
import re

APP_PATH = re.compile(r"/(inbox(/[^/?#]+)?|ask(/(new|\d+))?|live|maint|line|ship|products(/[\w.-]+/[\w.-]+)?|usage|help(/(api|cli|mobile))?|settings(/[a-z-]+)?)/?")   # PAGE を返すパス（/ と /index.html のほか）

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console_static")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8"}
STATIC_NAME = re.compile(r"[a-z][a-z0-9_-]{0,40}\.(css|js)")   # /static/<名前> で配るファイル（フォルダの外や隠しファイルは読まない）


def _read(name: str) -> str:
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as f:
        return f.read()


PAGE = _read("index.html")          # TOKEN は __TOKEN__ を console.py が置き換える（meta name="console-token"）
CATALOG = _read("catalog.html")
JS = _read("console.js")


def static_file(name: str):
    """/static/<name> の中身と Content-Type。配らない名前・ないファイルは None"""
    if not STATIC_NAME.fullmatch(name):
        return None
    path = os.path.join(STATIC_DIR, name)
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read(), STATIC_TYPES[os.path.splitext(name)[1]]
