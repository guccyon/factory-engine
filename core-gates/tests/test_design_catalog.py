"""デザインシステムのカタログ（core-gates/design_catalog.py）: トークンの CSS 変数、見本の検査、組み立て"""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import design_catalog as dc

TOKENS = {"color": {"roles": {"primary": "#1E3FD0", "on_primary": "#FFFFFF", "bad": "red"},
                    "status": {"danger": {"fg": "#C0182F", "on_container": "#7A0F1E"}}},
          "typography": {"families": {"sans": "'Noto Sans CJK JP', sans-serif", "evil": "x; } body { y"},
                         "scale": {"display": {"size": 44, "weight": 900, "line_height": 1.1, "letter_spacing": "-0.02em", "use": "残りの回数"}}},
          "spacing": {"scale_px": {"md": 16, "xl": 40}}, "shape": {"radius_px": {"pill": 999}}}
COMP = {"preview_css": ".b{background:var(--color-primary)}",
        "components": [{"id": "button", "ja": "ボタン", "use": "操作を 1 つ", "lifecycle": "trial",
                        "preview": [{"label": "primary", "html": '<button class="b">予約する</button>'},
                                    {"label": "disabled", "html": "<button disabled>予約する</button>"}]}]}


def test_css_vars_follow_the_token_names_and_drop_unsafe_values():
    v = {n: val for n, val, _ in dc.css_vars(TOKENS)}
    assert v["--color-primary"] == "#1E3FD0" and v["--color-on-primary"] == "#FFFFFF" and v["--color-danger-on-container"] == "#7A0F1E"
    assert v["--space-md"] == "16px" and v["--radius-pill"] == "999px" and v["--font-sans"].startswith("'Noto")
    assert "--color-bad" not in v and "--font-evil" not in v           # 色の名前・CSS を壊す書体は入れない


def test_validate_previews():
    assert dc.validate_previews(COMP) == []
    assert dc.validate_previews({}) == ["components.yaml に components（部品の一覧）がない"]
    bad = {"components": [{"id": "a"}, {"id": "b", "preview": [{"label": "x", "html": '<img src="https://evil.example/x.png">'}]},
                          {"id": "c", "preview": [{"label": "x", "html": "<script>alert(1)</script>"}]},
                          {"id": "d", "preview": [{"html": "<b>x</b>"}]}],
           "preview_css": "@import url(x); </style>"}
    errs = "\n".join(dc.validate_previews(bad))
    for want in ("a: preview", "外の URL", "script", "d.preview[0]: label と html", "preview_css: @import", "</style>"):
        assert want in errs


def test_build_lays_out_tokens_and_components():
    files = {"tokens.yaml": yaml.safe_dump(TOKENS, allow_unicode=True), "components.yaml": yaml.safe_dump(COMP, allow_unicode=True),
             "concept_tone.yaml": yaml.safe_dump({"concept": {"name": "コートの熱"}}, allow_unicode=True),
             "governance.yaml": "version: 0.1.0\n"}
    h = dc.build(files)
    assert "--color-primary: #1E3FD0;" in h and ".type-display" in h and "コートの熱" in h and "0.1.0" in h
    assert '<figcaption>disabled</figcaption>' in h and "予約する" in h and ".b{background:var(--color-primary)}" in h
    assert "trial" in h and "http" not in h.split("<body>")[1]           # 外の資源を読まない
    assert 1200 <= dc.catalog_height(files) <= 16000


def test_cli(tmp_path):
    (tmp_path / "tokens.yaml").write_text(yaml.safe_dump(TOKENS))
    (tmp_path / "components.yaml").write_text(yaml.safe_dump(COMP, allow_unicode=True))
    assert dc.main([str(tmp_path), str(tmp_path / "c.html")]) == 0 and (tmp_path / "c.html").is_file()
    (tmp_path / "components.yaml").write_text("components: [{id: a}]\n")
    assert dc.main([str(tmp_path), str(tmp_path / "c.html")]) == 1
