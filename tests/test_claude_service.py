"""Tests de la collecte du transcript Claude (deploiement du raisonnement)."""

from __future__ import annotations

from src.services.claude import ClaudeService

#: rangee assistant dont le panneau thinking est deploye (comme apres un clic)
EXPANDED_TURN = (
    "<div data-testid='transcript-row' data-index='1' data-perf-row='assistant'>"
    "<div class='font-claude-response'><div class='prose'>"
    "<div class='standard-markdown'><p>Reponse.</p></div></div>"
    "<div data-cds='TurnStatus' data-testid='TurnStatus' data-open=''>"
    "<div data-cds='TurnStatusStep' data-step-key='thinking-1' data-open=''>"
    "<div data-cds-row=''><bdi>Resume court.</bdi></div>"
    "<div data-cds-row-panel=''><div class='standard-markdown'>"
    "<p>Raisonnement integral deploye.</p></div></div>"
    "</div></div></div></div>"
)


class FakeSession:
    """Session factice : compte les clics de deploiement avant capture."""

    def __init__(self):
        self.expand_calls = 0
        self.collect_calls = 0
        self.expanded = False
        self.collect_saw_expanded = False

    def eval_body(self, body):
        if "window.__aicvClaude = {rows" in body:
            return 0
        if "window.__aicvClaudeScroller = el" in body:
            return {"tag": "DIV", "client": 800, "height": 4000}
        if "data-cds-row-toggle" in body:
            self.expand_calls += 1
            if self.expand_calls == 1:
                self.expanded = True
                return 1
            return 0
        if "acc.rows[key] = html" in body:
            self.collect_calls += 1
            if self.expanded:
                self.collect_saw_expanded = True
            return {"dom": 1, "total": 1, "setsize": 1, "top": 0}
        if "return {order: acc.order, rows: acc.rows}" in body:
            return {"order": ["i:1"], "rows": {"i:1": EXPANDED_TURN}}
        if "s.scrollTop = s.scrollHeight" in body:
            return 0
        if "s.scrollTop = Math.max(0" in body:
            return 0
        return None

    def wait_ms(self, ms):
        pass


def test_collecte_deploie_le_raisonnement_avant_capture():
    session = FakeSession()
    service = ClaudeService(session, {"scroll": {"stable_rounds": 1}})
    fragments = service._collect_conversation_rows()
    assert session.expand_calls >= 1
    assert session.collect_saw_expanded is True
    assert fragments and "Raisonnement integral deploye." in fragments[0]


def _chunked_row(index: int, role: str, text: str) -> str:
    return (
        f"<div data-testid='transcript-row' data-index='{index}' "
        f"data-perf-row='{role}'>{text}</div>"
    )


class ChunkedSession:
    """Simule le chargement par chunks du transcript Claude.

    Avant d'atteindre le haut du fil, seul le dernier chunk est monte : 2
    rangees avec des `data-index` locaux (0..1) et un `aria-setsize` tronque
    (2). Une fois en haut, le fil complet est charge : 4 rangees en index
    globaux (0..3) et `aria-setsize=4`.
    """

    def __init__(self):
        self.rows = {}
        self.at_top = False
        self.top_scrolls = 0

    def eval_body(self, body):
        if "window.__aicvClaude = {rows" in body:
            self.rows = {}
            self.at_top = False
            return 0
        if "window.__aicvClaudeScroller = el" in body:
            return {"tag": "DIV", "client": 800, "height": 4000}
        if "acc.rows[key] = html" in body:
            total = 4 if self.at_top else 2
            for i in range(total):
                role = "user" if i % 2 == 0 else "assistant"
                self.rows[f"i:{i}"] = _chunked_row(i, role, f"Message {i}")
            return {"dom": total, "total": len(self.rows), "setsize": total, "top": 0}
        if "return {order: acc.order, rows: acc.rows}" in body:
            return {"order": list(self.rows), "rows": self.rows}
        if "s.scrollTop = s.scrollHeight" in body:
            return 0
        if "s.scrollTop = Math.max(0" in body:
            return 0
        if "s.scrollTop = 0" in body:  # _SCROLL_TOP_JS
            self.top_scrolls += 1
            self.at_top = True
            return 0
        if "parseInt(holder.getAttribute('aria-setsize')" in body:  # _PEEK_SETSIZE_JS
            return 4 if self.at_top else 2
        if "data-cds-row-toggle" in body:
            return 0
        return None

    def wait_ms(self, ms):
        pass


def test_collecte_charge_tout_le_fil_avant_collecte():
    """Sans remonter en haut, seul le dernier chunk (index locaux) est collecte.

    Claude monte d'abord le dernier chunk avec des index locaux et un
    aria-setsize tronque : la collecte doit forcer le chargement complet
    (index globaux) pour ne pas perdre les messages anciens ni melanger les
    index.
    """
    session = ChunkedSession()
    service = ClaudeService(session, {"scroll": {"stable_rounds": 1}})
    fragments = service._collect_conversation_rows()
    assert session.top_scrolls >= 1
    indices = sorted(
        int(fragment.split("data-index='")[1].split("'")[0]) for fragment in fragments
    )
    assert indices == [0, 1, 2, 3]
    assert len(fragments) == 4

