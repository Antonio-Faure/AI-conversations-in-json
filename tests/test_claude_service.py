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
