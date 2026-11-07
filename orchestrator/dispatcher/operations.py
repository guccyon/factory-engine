"""依頼で動かしてよい決定論の操作の口（ADR 0037 の決定 3・4、ADR 0039 の順番 2）。dispatcher の側。

窓口の相談役（振り分け）が「この依頼は操作 X を引数 Y で 1 回動かせば済む」と返したら、ここで宣言と照らして検査し、
確認の段（tier）を決め、確認が済んだら動かす。操作の中身は factoryctl と同じ関数を呼ぶ（2 つ書かない）。

- 宣言の正本は definitions/operations.yaml。dispatcher のコンテナには definitions/ がないので、使う値をここに写す（食い違いはテストで見る）
- 引数は型で検査し、宣言にない引数・範囲の外の値は拒む。宛先は notifications.yaml にある名前だけ
- 段は宣言だけで決まる（依頼の本文や相談役の答えで下げない）
- LLM もワーカーも操作を動かさない。動かすのは dispatcher だけ
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

TIERS = ("read", "send", "change", "destroy")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

OPS: dict[str, dict] = {
    "report.send": {
        "ja": "定期レポートを今すぐ送る（試し送り）",
        "does": "指定の種類と日付で組み立て直し、notifications.yaml の宛先に送る。毎朝の送信には数えない（factoryctl report send と同じ）",
        "examples": ["今朝のレポートを送り直して", "昨日の工場の健康のレポートを Slack にもう一度出して"],
        "params": {
            "kind": {"type": "enum", "values": ["report.factory_health", "report.product", "all"], "default": "all", "ja": "レポートの種類"},
            "date": {"type": "date", "default": "latest", "ja": "対象の日（YYYY-MM-DD。latest は今朝送った分＝前の日）"},
            "to": {"type": "destination", "optional": True, "ja": "宛先の名前（notifications.yaml の destinations。なければその種類の宛先すべて）"},
        },
        "tier": "send",
        "allowed": ["operator", "requester"],
        "impl": "report.send_now",
    },
}


def catalog() -> list[dict]:
    """相談役に渡す一覧（振り分けの材料）。impl は渡さない"""
    return [{"id": k, **{f: v[f] for f in ("ja", "does", "examples", "params", "tier")}} for k, v in OPS.items()]


def check(op_id: str, params: dict | None, *, destinations: list[str], role: str = "requester") -> tuple[dict, list[str]]:
    """宣言と照らして引数を検査し、既定値で埋める。返り値は (整えた引数, 誤り)。誤りが 1 つでもあれば動かさない"""
    spec = OPS.get(op_id)
    if spec is None:
        return {}, [f"操作の一覧にない: {op_id}"]
    if role not in spec["allowed"]:
        return {}, [f"{op_id} はこの依頼者には動かせない"]
    if spec["tier"] == "destroy":
        return {}, [f"{op_id} は取り返しがつかないので依頼では受けない（運用主が操業コンソールで行う）"]
    params = dict(params or {}) if isinstance(params, dict) else {}
    errors, out = [], {}
    unknown = sorted(set(params) - set(spec["params"]))
    if unknown:
        errors.append(f"宣言にない引数: {', '.join(unknown)}")
    for name, p in spec["params"].items():
        v = params.get(name)
        if v in (None, ""):
            if "default" in p:
                out[name] = p["default"]
            elif not p.get("optional"):
                errors.append(f"{name} が要る")
            continue
        if p["type"] == "enum" and v not in p["values"]:
            errors.append(f"{name} は {' / '.join(p['values'])} のどれか（{v!r}）")
        elif p["type"] == "date" and not (v == "latest" or (isinstance(v, str) and DATE_RE.match(v) and _date_ok(v))):
            errors.append(f"{name} は YYYY-MM-DD か latest（{v!r}）")
        elif p["type"] == "destination" and v not in destinations:
            errors.append(f"{name} は通知の設定にある宛先の名前（{' / '.join(destinations) or 'なし'}）。依頼の本文の宛先には送らない（{v!r}）")
        else:
            out[name] = v
    return out, errors


def _date_ok(v: str) -> bool:
    try:
        d = datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        return False
    return d <= datetime.now(timezone.utc).date() + timedelta(days=1)


def tier(op_id: str) -> str:
    return OPS[op_id]["tier"]


def _report_kinds(params: dict) -> list[str]:
    import report
    return list(report.builders_for()) if params.get("kind") in (None, "all") else [params["kind"]]


def _period(params: dict):
    import report
    if params.get("date") in (None, "latest"):
        return report.previous_day(datetime.now(timezone.utc))
    start = datetime.combine(datetime.strptime(params["date"], "%Y-%m-%d").date(), datetime.min.time(), tzinfo=report.TZ)
    return start, start + timedelta(days=1)


def preview(op_id: str, params: dict, config) -> dict:
    """確認で依頼者に見せる見本（何を・どこへ）。送る前に決定論で出す"""
    if op_id == "report.send":
        import notify
        start, _ = _period(params)
        dests = sorted({d.name for k in _report_kinds(params) for d in notify.targets(config, k) if not params.get("to") or d.name == params["to"]})
        return {"what": f"{'・'.join(_report_kinds(params))} の {start:%Y-%m-%d} の分を組み立て直して送る", "to": dests,
                "text": f"{OPS[op_id]['ja']}: {'・'.join(_report_kinds(params))}（{start:%Y-%m-%d} の分）を {('・'.join(dests)) or '宛先なし'} へ"}
    return {"what": OPS[op_id]["ja"], "to": [], "text": OPS[op_id]["ja"]}


def run(cur, op_id: str, params: dict, *, config=None, poster=None) -> dict:
    """操作を 1 回動かす。返り値は {ok, summary, detail}。失敗も結果として返す（呼ぶ側が会話に書く）"""
    if op_id == "report.send":
        import notify
        import report
        config = config or notify.current(cur)
        start, end = _period(params)
        detail, sent, failed = {}, [], []
        for kind in _report_kinds(params):
            r = report.send_now(cur, kind, start, end, params.get("to"), config=config, poster=poster)
            detail[kind] = r
            sent += [f"{kind}→{d}" for d in r["sent"]]
            failed += [f"{kind}→{d}" for d in r["failed"]]
        ok = bool(sent) and not failed
        summary = (f"{start:%Y-%m-%d} の分を送りました: {', '.join(sent)}" if ok else
                   f"{start:%Y-%m-%d} の分を送れたのは {', '.join(sent) or 'なし'}、送れなかったのは {', '.join(failed) or 'なし'}"
                   + ("（送る宛先がない）" if not sent and not failed else ""))
        return {"ok": ok, "summary": summary, "detail": detail}
    return {"ok": False, "summary": f"{op_id} の実行の口がない", "detail": {}}
