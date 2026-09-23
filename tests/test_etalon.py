"""Tests des metriques/controle de regression d'etalonnage."""

from __future__ import annotations

from src.utils.etalon import conversation_metrics, detect_capabilities, regressions

PAYLOAD = {
    "conversation_id": "c",
    "messages": [
        {
            "role": "user",
            "texte": "voici ![img](images/x.png)\n- un\n- deux\n1. trois",
            "code_blocks": [],
        },
        {
            "role": "assistant",
            "texte": "| a | b |\n| --- | --- |\n| 1 | 2 |\n$E=mc^2$\n```python\nprint(1)\n```",
            "code_blocks": [{"language": "python", "code": "print(1)"}],
        },
        {"role": "assistant", "texte": "suite", "code_blocks": []},
    ],
}


def test_metriques_comptent_les_capacites():
    metrics = conversation_metrics(PAYLOAD)
    assert metrics["messages"] == 3
    assert metrics["consecutive_roles"] == 1
    assert metrics["images"] == 1
    assert metrics["tables"] == 1
    assert metrics["lists"] == 2
    assert metrics["code_langs"] == 1
    assert metrics["latex"] == 2


def test_metriques_conversation_vide():
    metrics = conversation_metrics({})
    assert all(metrics[key] == 0 for key in metrics)


def test_regression_detectee_sous_le_seuil():
    baseline = {"messages": 100, "images": 5, "tables": 4, "latex": 20}
    current = {"messages": 50, "images": 1, "tables": 4, "latex": 20}
    lost = regressions(current, baseline)
    assert any(item.startswith("messages") for item in lost)
    assert any(item.startswith("images") for item in lost)
    assert not any(item.startswith("tables") for item in lost)


def test_detecte_les_capacites_presentes():
    caps = detect_capabilities(PAYLOAD)
    assert {"list_bullet", "list_numbered", "table", "image_markdown",
            "latex_inline", "code_block", "code_languages"} <= caps
    assert "hr" not in caps and "blockquote" not in caps


def test_detecte_refus_et_unicode():
    payload = {"messages": [{"role": "assistant", "texte": "Désolé, je ne peux pas 😀 مرحبا"}]}
    caps = detect_capabilities(payload)
    assert "refusal_error" in caps
    assert "emoji_unicode" in caps


def test_regression_consecutive_roles_seulement_si_augmente():
    assert regressions({"consecutive_roles": 2}, {"consecutive_roles": 0}) == []
    assert regressions({"consecutive_roles": 2}, {"consecutive_roles": 1})
