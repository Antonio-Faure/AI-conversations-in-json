"""Tests des parsers DOM sur fixtures HTML (sans navigateur)."""

from __future__ import annotations

import json

import pytest

from src.parsers import (
    ChatGPTParser,
    ClaudeParser,
    GeminiParser,
    GrokParser,
    MistralParser,
    PARSER_CLASSES,
    PerplexityParser,
)
from src.parsers.base import ParseError
from src.parsers.chatgpt import merge_turn_snapshots, order_fragments_by_time
from src.parsers.claude import merge_transcript_rows
from src.parsers.gemini import merge_turn_fragments
from src.parsers.perplexity import merge_thread_messages
from src.schema import Conversation


# ---------------------------------------------------------------------------
# Listes de conversations (sidebars)
# ---------------------------------------------------------------------------


class TestParseLinks:
    def test_chatgpt_sidebar(self, fixture_html):
        refs = ChatGPTParser().parse_links(fixture_html("chatgpt_home.html"), "https://chatgpt.com/")
        assert [r.id for r in refs] == [
            "2f1e9c3a-7b45-4d8e-9a11-0c2d3e4f5a6b",
            "88a07f6e-5d4c-3b2a-1908-f7e6d5c4b3a2",
            "deadbeefcafe0123456789abcdef0123456789",
        ]
        assert refs[0].title == "Refactor module paiement"
        assert refs[0].url.startswith("https://")  # absolutisee
        assert refs[0].service == "chatgpt"

    def test_claude_sidebar(self, fixture_html):
        refs = ClaudeParser().parse_links(fixture_html("claude_home.html"), "https://claude.ai/chats")
        assert len(refs) == 2
        assert all("/chat/" in r.url for r in refs)

    def test_gemini_sidebar_ignore_settings(self, fixture_html):
        refs = GeminiParser().parse_links(fixture_html("gemini_home.html"), "https://gemini.google.com/app")
        ids = [r.id for r in refs]
        assert "1a2b3c4d5e6f7g8h9i0j12kl34" in ids
        assert all("settings" not in r.url for r in refs)

    def test_perplexity_sidebar_ignore_library(self, fixture_html):
        refs = PerplexityParser().parse_links(
            fixture_html("perplexity_home.html"), "https://www.perplexity.ai/"
        )
        assert len(refs) == 2
        assert all(r.id.startswith("ou-en") or r.id.startswith("meilleurs") for r in refs)


# ---------------------------------------------------------------------------
# Conversations completes
# ---------------------------------------------------------------------------


class TestChatGPTParser:
    def test_structure_complete(self, fixture_html):
        conv = ChatGPTParser().parse(
            fixture_html("chatgpt_conversation.html"), conversation_id="cid-42"
        )
        assert isinstance(conv, Conversation)
        assert conv.platform == "chatgpt"
        assert conv.conversation_id == "cid-42"
        assert conv.title == "Refactor module paiement"
        assert conv.model == "gpt-5"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_message_id_et_model_par_message(self, fixture_html):
        conv = ChatGPTParser().parse(
            fixture_html("chatgpt_conversation.html"), conversation_id="cid-42"
        )
        assert conv.messages[0].message_id == "aa11bb22-0001-4334-9556-778899aabbcc"
        assert conv.messages[0].model is None
        assert conv.messages[1].model == "gpt-5"
        assert conv.messages[1].message_id == "aa11bb22-0002-4334-9556-778899aabbcc"

    def test_code_blocks_structures(self, fixture_html):
        conv = ChatGPTParser().parse(fixture_html("chatgpt_conversation.html"), conversation_id="c")
        assert conv.messages[3].has_code
        block = conv.messages[3].code_blocks[0]
        assert block.language == "python"
        assert "test_refund" in block.code

    def test_markdown_et_nettoyage(self, fixture_html):
        conv = ChatGPTParser().parse(fixture_html("chatgpt_conversation.html"), conversation_id="c")
        first = conv.messages[0]
        assert "refactorer mon module de paiement" in first.texte
        assert "\n" in conv.messages[1].texte  # liste separee en lignes
        assert "Copier" not in conv.messages[1].texte  # boutons exclus
        assert "color:red" not in conv.messages[3].texte  # style exclu
        assert "```python" in conv.messages[3].texte  # bloc code fence

    def test_timestamps_via_meta_react(self, fixture_html):
        extra = {
            "messages": {
                "aa11bb22-0001-4334-9556-778899aabbcc": {"time": 1767522600, "model": None},
                "aa11bb22-0002-4334-9556-778899aabbcc": {"time": 1767522630, "model": "gpt-5"},
            }
        }
        conv = ChatGPTParser().parse(
            fixture_html("chatgpt_conversation.html"), conversation_id="c", extra=extra
        )
        assert conv.messages[0].timestamp == "2026-01-04T10:30:00Z"
        assert conv.started_at == "2026-01-04T10:30:00Z"
        assert conv.messages[1].model == "gpt-5"

    def test_sans_messages_erreur(self, fixture_html):
        with pytest.raises(ParseError):
            ChatGPTParser().parse("<html><body><main></main></body></html>")

    def test_images_et_tour_image_genere(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='user' data-message-id='u1'>
              <button><img alt='photo.png' width='1024'
                 src='https://chatgpt.com/backend-api/estuary/content?id=1'></button>
              <div class='markdown'><p>Regarde cette image</p></div>
            </div>
          </div>
          <div data-testid='conversation-turn-2'>
            <div data-message-author-role='assistant' data-message-id='a1'>
              <div class='markdown'><p>Bien recu</p></div></div>
          </div>
          <div data-testid='conversation-turn-3'>
            <div data-testid='image-gen-overlay-actions'></div>
            <img alt='Generated image: chat' width='1024'
                 src='https://chatgpt.com/backend-api/estuary/content?id=2'>
          </div>
          <div data-testid='conversation-turn-4'>
            <div data-message-author-role='user' data-message-id='u2'>
              <div class='markdown'><p>Merci</p></div></div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        # le tour image (assistant) est conserve -> pas de fusion des users
        assert [m.role for m in conv.messages] == ["user", "assistant", "user"]
        assert "![photo.png]" in conv.messages[0].texte
        assert "Generated image" in conv.messages[1].texte
        assert "Bien recu" in conv.messages[1].texte

    @staticmethod
    def _conversation(markdown: str) -> Conversation:
        html = (
            "<html><body><main>"
            "<div data-testid='conversation-turn-1'>"
            "<div data-message-author-role='assistant' data-message-id='a1'>"
            f"<div class='markdown'>{markdown}</div></div></div>"
            "</main></body></html>"
        )
        return ChatGPTParser().parse(html, conversation_id="c")

    def test_markdown_blocs_et_inline(self):
        conv = self._conversation(
            "<h1>Test</h1><h2>Sous-test</h2>"
            "<p><strong>Premier</strong> <em>deuxième</em> <del>troisième</del></p>"
            "<ul><li><p>Pomme</p></li><li><p>Poire</p></li></ul>"
            "<ol><li><p>Lire</p></li><li><p>Analyser</p></li></ol>"
            "<ul class='contains-task-list'>"
            "<li class='task-list-item'><p><input disabled type='checkbox'/> Faire</p></li>"
            "<li class='task-list-item'><p><input checked disabled type='checkbox'/> Terminé</p></li>"
            "</ul>"
            "<p><code>sorted(key=...)</code></p>"
            "<blockquote><p>Ceci est un test.</p></blockquote><hr/>"
            "<table><thead><tr><th>A</th><th>1</th></tr></thead>"
            "<tbody><tr><td>B</td><td>2</td></tr></tbody></table>"
        )
        texte = conv.messages[0].texte
        assert "# Test" in texte
        assert "## Sous-test" in texte
        assert "**Premier** *deuxième* ~~troisième~~" in texte
        assert "- Pomme" in texte and "- Poire" in texte
        assert "1. Lire" in texte and "2. Analyser" in texte
        assert "- [ ] Faire" in texte and "- [x] Terminé" in texte
        assert "`sorted(key=...)`" in texte
        assert "> Ceci est un test." in texte
        assert "---" in texte
        assert "| A | 1 |" in texte
        assert "| --- | --- |" in texte
        assert "| B | 2 |" in texte

    def test_code_language_entete(self):
        conv = self._conversation(
            "<pre class='overflow-visible!'><div class='flex w-full font-sans'>"
            "<div class='flex max-w-[75%] items-center'>Python</div>"
            "<div class='flex flex-row'><button>Run</button></div></div>"
            "<pre class='cm-content'><code><span>x = 2</span></code></pre></pre>"
            "<pre class='overflow-visible!'><pre class='cm-content'>"
            "<code>print(\"test\")</code></pre></pre>"
        )
        blocks = conv.messages[0].code_blocks
        assert blocks[0].language == "python"
        assert blocks[0].code == "x = 2"
        assert blocks[1].language == ""
        assert blocks[1].code == 'print("test")'

    def test_latex_inline_et_bloc(self):
        conv = self._conversation(
            "<p><span data-math-source='E = mc^2' role='math'>"
            "<span class='katex'><annotation encoding='application/x-tex'>E = mc^2</annotation>"
            "</span></span></p>"
            "<span data-math-source='\\frac{1}{2}' style='display: block;'></span>"
        )
        texte = conv.messages[0].texte
        assert "$E = mc^2$" in texte
        assert "$$\\frac{1}{2}$$" in texte
        assert "\\(" not in texte

    def test_citation_pill_source(self):
        conv = self._conversation(
            "<ol><li><p>Example Domain "
            "<span data-testid='webpage-citation-pill'><a href='https://example.com/'>"
            "<span><img alt='' width='128' height='128' "
            "src='https://www.google.com/s2/favicons?domain=example.com'/></span>"
            "<span class='truncate'>Example Domain</span></a></span></p></li></ol>"
        )
        texte = conv.messages[0].texte
        assert "1. Example Domain [Example Domain](https://example.com/)" in texte
        assert "favicons" not in texte
        assert "![image]" not in texte

    def test_tour_avec_data_turn_sans_role(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1' data-turn='user'>
            <div class='markdown'><p>Question</p></div></div>
          <div data-testid='conversation-turn-2' data-turn='assistant'>
            <div class='markdown'><p>Réponse</p></div></div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[1].texte == "Réponse"


class TestChatGPTTurnMerge:
    """Fusion incrementale des fenetres de tours (virtualisation ChatGPT)."""

    @staticmethod
    def _turn(message_id: str, role: str, text: str) -> str:
        return (
            "<div data-testid='conversation-turn-x'>"
            f"<div data-message-author-role='{role}' data-message-id='{message_id}'>"
            f"<div class='markdown'><p>{text}</p></div></div></div>"
        )

    def test_remonte_sans_doublon_et_ordre_chronologique(self):
        html = {
            "m:a": self._turn("a", "user", "A"),
            "m:b": self._turn("b", "assistant", "B"),
            "m:c": self._turn("c", "user", "C"),
            "m:d": self._turn("d", "assistant", "D"),
        }
        # fenetres vues en remontant le fil : recents -> anciens
        snapshots = [["m:d"], ["m:c", "m:d"], ["m:b", "m:c", "m:d"], ["m:a", "m:b"]]
        fragments = merge_turn_snapshots(snapshots, html)
        assert fragments == [html["m:a"], html["m:b"], html["m:c"], html["m:d"]]

    def test_composants_disjoints_du_plus_recent_au_plus_ancien(self):
        html = {
            "m:a": self._turn("a", "user", "A"),
            "m:b": self._turn("b", "assistant", "B"),
            "m:c": self._turn("c", "user", "C"),
            "m:d": self._turn("d", "assistant", "D"),
        }
        # saut de fenetre : deux composants non relies, le second plus ancien
        snapshots = [["m:d"], ["m:c", "m:d"], ["m:b"], ["m:a", "m:b"]]
        fragments = merge_turn_snapshots(snapshots, html)
        assert fragments == [html["m:a"], html["m:b"], html["m:c"], html["m:d"]]

    def test_parse_apres_fusion_roles_alternees(self):
        html = {
            "m:u1": self._turn("u1", "user", "Question 1"),
            "m:a1": self._turn("a1", "assistant", "Reponse 1"),
            "m:u2": self._turn("u2", "user", "Question 2"),
            "m:a2": self._turn("a2", "assistant", "Reponse 2"),
        }
        snapshots = [["m:a2"], ["m:u2", "m:a2"], ["m:u1", "m:a1", "m:u2"]]
        fragments = merge_turn_snapshots(snapshots, html)
        document = "<html><body>" + "".join(fragments) + "</body></html>"
        conv = ChatGPTParser().parse(document, conversation_id="c")
        assert [m.role for m in conv.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert [m.texte for m in conv.messages] == [
            "Question 1",
            "Reponse 1",
            "Question 2",
            "Reponse 2",
        ]


class TestOrderFragmentsByTime:
    """Reordonnancement chronologique par timestamp React."""

    @staticmethod
    def _turn(message_id: str, text: str) -> str:
        return (
            "<div data-testid='conversation-turn-x'>"
            f"<div data-message-author-role='user' data-message-id='{message_id}'>"
            f"<div class='markdown'><p>{text}</p></div></div></div>"
        )

    def test_reordonne_selon_les_timestamps(self):
        # l'ordre du scroll (trou) a place c avant b : les timestamps corrigent.
        fragments = [self._turn("a", "A"), self._turn("c", "C"), self._turn("b", "B")]
        meta = {"a": {"time": 10.0}, "b": {"time": 20.0}, "c": {"time": 30.0}}
        ordered = order_fragments_by_time(fragments, meta)
        assert ordered == [
            self._turn("a", "A"),
            self._turn("b", "B"),
            self._turn("c", "C"),
        ]

    def test_sans_meta_ordre_inchange(self):
        fragments = [self._turn("a", "A"), self._turn("b", "B")]
        assert order_fragments_by_time(fragments, {}) == fragments

    def test_fragment_sans_timestamp_garde_sa_place(self):
        fragments = [self._turn("a", "A"), "<div>image</div>", self._turn("b", "B")]
        meta = {"a": {"time": 10.0}, "b": {"time": 20.0}}
        ordered = order_fragments_by_time(fragments, meta)
        assert ordered == fragments


class TestClaudeParser:
    def test_structure_complete(self, fixture_html):
        conv = ClaudeParser().parse(
            fixture_html("claude_conversation.html"),
            conversation_id="6f1c2d3e-4a5b-6c7d-8e9f-0a1b2c3d4e5f",
        )
        assert conv.platform == "claude"
        assert conv.title == "Préparer un entretien technique"
        assert conv.model == "Claude Sonnet 4"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_thinking_exclu_et_signale(self, fixture_html):
        conv = ClaudeParser().parse(fixture_html("claude_conversation.html"), conversation_id="c")
        assistant = conv.messages[1]
        assert "organiser 7 jours" not in assistant.texte  # reflexion retiree
        assert assistant.metadata.get("had_thinking") is True

    def test_timestamps_et_code(self, fixture_html):
        conv = ClaudeParser().parse(fixture_html("claude_conversation.html"), conversation_id="c")
        assert conv.messages[1].timestamp == "2026-09-03T14:05:00Z"
        assert conv.started_at == "2026-09-03T14:05:00Z"
        assert conv.last_message_at == "2026-09-03T14:12:30Z"
        assert "```bash" in conv.messages[3].texte
        assert conv.messages[0].timestamp is None  # pas de time dans le parent direct

    def test_dom_transcript_2026(self):
        """Nouveau DOM Claude (transcript-row) : reponses via font-claude-response."""
        html = """<html><body>
          <div data-testid="chat-header-title">Review dossier</div>
          <div data-testid='transcript-list'>
            <div data-testid='transcript-row'>
              <div data-testid='user-message'><div>Question sur le cache offline</div></div>
            </div>
            <div data-testid='transcript-row'>
              <h2 class="sr-only">Réponse de Claude</h2>
              <div class="font-claude-response"><div class="prose">
                <div class="standard-markdown">
                  <div>A réfléchi pendant 26 s</div>
                  <p>Rester simple : cache via service worker.</p>
                </div>
              </div></div>
            </div>
            <div data-testid='transcript-row'>
              <div data-testid='user-message'><div>Merci, et pour le deploiement ?</div></div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assert [m.role for m in conv.messages] == ["user", "assistant", "user"]
        assert "cache via service worker" in conv.messages[1].texte
        assert "Réponse de Claude" not in conv.messages[1].texte  # heading sr-only exclu
        assert "réfléchi" not in conv.messages[1].texte  # label thinking exclu
        assert conv.title == "Review dossier"

    def test_dom_transcript_2026_user_cds(self):
        """DOM transcript 2026 reel : tours user balises par data-cds=UserMessage."""
        html = """<html><body>
          <div data-testid="chat-header-title">Etalonnage</div>
          <div data-testid='transcript-list'>
            <div data-testid='transcript-row' data-perf-row='human'>
              <div data-cds='UserMessage'><div>Question courte</div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='assistant'>
              <div class="font-claude-response"><div class="prose">
                <div class="standard-markdown"><p>Reponse simple.</p></div>
              </div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='human'>
              <div data-cds='UserMessage'><div>Deuxieme question</div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='assistant'>
              <div class="font-claude-response"><div class="prose">
                <div class="standard-markdown"><p>Deuxieme reponse.</p></div>
              </div></div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
        assert conv.messages[0].texte == "Question courte"
        assert conv.messages[2].texte == "Deuxieme question"
        assert "Deuxieme reponse" in conv.messages[3].texte

    def test_code_inline_conserve_backticks(self):
        """Le code inline (<code> hors <pre>) reste en markdown `code`."""
        html = """<html><body>
          <div data-testid='transcript-list'>
            <div data-testid='transcript-row' data-perf-row='human'>
              <div data-cds='UserMessage'><div>
                <p>Ecris la variable <code>total</code> en code en ligne.</p>
              </div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='assistant'>
              <div class="font-claude-response"><div class="prose">
                <div class="standard-markdown"><p>La variable <code>total</code> vaut 3.</p></div>
              </div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='human'>
              <div data-cds='UserMessage'><div><p>Montre un bloc.</p></div></div>
            </div>
            <div data-testid='transcript-row' data-perf-row='assistant'>
              <div class="font-claude-response"><div class="prose">
                <div class="standard-markdown"><pre><code class="language-python">total = 3</code></pre></div>
              </div></div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert conv.messages[0].texte == "Ecris la variable `total` en code en ligne."
        assert conv.messages[1].texte == "La variable `total` vaut 3."
        # les blocs <pre> restent des fences, sans backtick inline parasite
        assert "```python" in conv.messages[3].texte
        assert "`total`" not in conv.messages[3].texte


def _claude_transcript_row(index: int, role: str, text: str) -> str:
    """Rangee de transcript minimale (comme le DOM virtualise 2026)."""
    if role == "user":
        inner = f"<div data-cds='UserMessage'><div><p>{text}</p></div></div>"
    else:
        inner = (
            "<div class='font-claude-response'><div class='prose'>"
            f"<div class='standard-markdown'><p>{text}</p></div></div></div>"
        )
    return (
        f"<div data-testid='transcript-row' data-index='{index}' "
        f"data-perf-row='{role}'>{inner}</div>"
    )


class TestClaudeMergeRows:
    """Fusion des rangees virtualisees du transcript Claude."""

    def test_trie_par_index_et_dedup(self):
        rows = [
            _claude_transcript_row(3, "assistant", "Reponse 3"),
            _claude_transcript_row(0, "user", "Question 0"),
            _claude_transcript_row(2, "user", "Question 2"),
            _claude_transcript_row(1, "assistant", "Reponse 1"),
            _claude_transcript_row(4, "user", "Question 4"),
            _claude_transcript_row(1, "assistant", "Reponse 1"),  # doublon
        ]
        conv = ClaudeParser().parse(
            merge_transcript_rows(rows), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant", "user",
        ]
        assert [m.texte for m in conv.messages] == [
            "Question 0", "Reponse 1", "Question 2", "Reponse 3", "Question 4",
        ]

    def test_dedup_garde_le_rendu_le_plus_complet(self):
        rows = [
            _claude_transcript_row(0, "user", "Question"),
            _claude_transcript_row(0, "user", "Question avec details supplementaires"),
            _claude_transcript_row(1, "assistant", "Reponse"),
        ]
        conv = ClaudeParser().parse(
            merge_transcript_rows(rows), conversation_id="c"
        )
        assert len(conv.messages) == 2
        assert "details supplementaires" in conv.messages[0].texte

    def test_header_reinjecte_pour_le_modele(self):
        header = (
            "<button data-testid='model-selector-dropdown' "
            "aria-label='Modèle : Sonnet 5 Extra'></button>"
        )
        html = merge_transcript_rows(
            [
                _claude_transcript_row(0, "user", "Question"),
                _claude_transcript_row(1, "assistant", "Reponse"),
            ],
            header_html=header,
        )
        conv = ClaudeParser().parse(html, conversation_id="c")
        assert conv.model == "Sonnet 5 Extra"
        assert len(conv.messages) == 2

    def test_fragments_vides(self):
        assert merge_transcript_rows([]) == ""
        assert merge_transcript_rows(["<div>sans rangee</div>"]) == ""


class TestGeminiParser:
    def test_structure_complete(self, fixture_html):
        conv = GeminiParser().parse(
            fixture_html("gemini_conversation.html"), conversation_id="cid-gemini"
        )
        assert conv.platform == "gemini"
        assert conv.title == "Plan de voyage Japon"
        assert conv.model == "Gemini 2.5 Pro"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_contenu_et_ordre(self, fixture_html):
        conv = GeminiParser().parse(fixture_html("gemini_conversation.html"), conversation_id="c")
        assert "10 jours au Japon" in conv.messages[0].texte
        assert "Tokyo 3j" in conv.messages[1].texte
        assert "JR Pass" in conv.messages[3].texte

    def test_liste_a_puces_en_markdown(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Liste</div></user-query>"
            "<model-response><message-content><ul>"
            "<li><p>Pomme</p></li><li><p>Banane</p></li><li><p>Orange</p></li>"
            "</ul></message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert "- Pomme" in conv.messages[1].texte
        assert "- Orange" in conv.messages[1].texte

    def test_liste_numerotee_imbriquee_en_markdown(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Etapes</div></user-query>"
            "<model-response><message-content><ol><li><p>Un</p>"
            "<ul><li><p>Detail</p></li></ul></li><li><p>Deux</p></li>"
            "</ol></message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert "1. Un" in conv.messages[1].texte
        assert "  - Detail" in conv.messages[1].texte
        assert "2. Deux" in conv.messages[1].texte

    def test_tableau_en_markdown(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Tableau</div></user-query>"
            "<model-response><message-content><table>"
            "<thead><tr><th>Nom</th><th>Age</th></tr></thead>"
            "<tbody><tr><td>Alice</td><td>28</td></tr>"
            "<tr><td>Bob</td><td>34</td></tr></tbody></table>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        table = conv.messages[1].texte
        assert "| Nom | Age |" in table
        assert "| --- | --- |" in table
        assert "| Alice | 28 |" in table
        assert "| Bob | 34 |" in table

    def test_piece_jointe_image_en_markdown(self):
        html = (
            "<html><body>"
            "<user-query>"
            "<div class='file-preview-container'>"
            "<img class='preview-image' src='https://lh3.googleusercontent.com/gg/ABC'"
            " alt=\"Aperçu de l'image importée\">"
            "</div>"
            "<div class='query-text'>Decris l'image</div>"
            "</user-query>"
            "<model-response><message-content>Une image</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert "![Aperçu de l'image importée](https://lh3.googleusercontent.com/gg/ABC)" in (
            conv.messages[0].texte
        )
        assert "Decris l'image" in conv.messages[0].texte

    def test_langage_et_sortie_des_blocs_code(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Code</div></user-query>"
            "<model-response><message-content><code-block>"
            "<div class='code-block-decoration header-formatted'><span>Python</span></div>"
            "<pre><code class='code-container formatted' data-test-id='code-content'>"
            "print(1)</code></pre>"
            "<div class='code-block-decoration header'><span>Résultat du code</span></div>"
            "<mat-divider></mat-divider>"
            "<pre><code class='code-result-container' "
            "data-test-id='code-output-stdout-stderr'>1</code></pre>"
            "</code-block></message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "```python\nprint(1)\n```" in text
        assert "Résultat du code" not in text
        assert [b.language for b in conv.messages[1].code_blocks] == ["python", ""]
        assert conv.messages[1].code_blocks[1].code == "1"

    def test_entete_ui_nest_pas_un_langage(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Mermaid</div></user-query>"
            "<model-response><message-content><code-block>"
            "<div class='code-block-decoration header-formatted'>"
            "<span>Extrait de code</span></div>"
            "<pre><code class='code-container'>graph LR</code></pre>"
            "</code-block></message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "Extrait de code" not in text
        assert "```\ngraph LR\n```" in text

    def test_titres_en_markdown(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Structure</div></user-query>"
            "<model-response><message-content>"
            "<h1>Titre</h1><h2>Sous-titre</h2><h3>Sous-sous-titre</h3>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "# Titre" in text
        assert "## Sous-titre" in text
        assert "### Sous-sous-titre" in text

    def test_citation_gras_italique_barre_et_code_inline(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Styles</div></user-query>"
            "<model-response><message-content>"
            "<blockquote><p>Le code est de la poésie logique</p></blockquote>"
            "<p><b>A</b> <i>B</i> <s>C</s> <code>git status</code></p>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "> Le code est de la poésie logique" in text
        assert "**A** *B* ~~C~~" in text
        assert "`git status`" in text

    def test_latex_data_math_en_dollars(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>LaTeX</div></user-query>"
            "<model-response><message-content><p>Soit "
            "<span class='math-inline' data-math='E=mc^2'>rendu</span>.</p>"
            "<div class='math-block' data-math='a^2 + b^2 = c^2'>rendu</div>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "$E=mc^2$" in text
        assert "$$a^2 + b^2 = c^2$$" in text

    def test_fichier_genere_sans_icone_ni_bouton(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>CSV</div></user-query>"
            "<model-response><message-content>"
            "<generated-file>"
            "<img alt='Icône CSV' "
            "src='https://drive-thirdparty.googleusercontent.com/32/type/text/csv'>"
            "<div class='file-name-lr' title='produits.csv'>produits</div>"
            "<div class='file-type-lr'>CSV</div>"
            "<button>Ouvert</button>"
            "</generated-file>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        text = conv.messages[1].texte
        assert "produits.csv" in text
        assert "drive-thirdparty" not in text
        assert "Ouvert" not in text

    def test_piece_jointe_fichier_garde_le_nom(self):
        html = (
            "<html><body>"
            "<user-query>"
            "<div class='file-preview-container'>"
            "<div data-test-id='uploaded-file'>"
            "<button aria-label='Projet Helios.pdf'>"
            "<div class='extension-label'>PDF</div>"
            "<div class='filename-label'>Projet Helios</div>"
            "</button></div></div>"
            "<div class='query-text'>Résume le fichier</div>"
            "</user-query>"
            "<model-response><message-content>Résumé</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert "Projet Helios.pdf" in conv.messages[0].texte
        assert "Résume le fichier" in conv.messages[0].texte

    def test_ligne_de_separation_horizontale(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Haut/Bas</div></user-query>"
            "<model-response><message-content>"
            "<p>Haut</p><hr><p>Bas</p>"
            "</message-content></model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert "Haut\n\n---\n\nBas" in conv.messages[1].texte


def _turn_block(turn_id: str, question: str, answer: str) -> str:
    return (
        f'<div class="conversation-container" id="{turn_id}">'
        f"<user-query><div class='query-text'>{question}</div></user-query>"
        f"<model-response><message-content>"
        f"<div class='model-response-text'>{answer}</div>"
        f"</message-content></model-response>"
        f"</div>"
    )


class TestGeminiMergeTurns:
    """Fusion des tours virtualises (Gemini re-rend des fenetres entieres)."""

    def test_dedup_garde_la_derniere_occurrence_complete(self):
        fragments = [
            _turn_block("id-a1", "Question A", ""),  # exemplaire incomplet
            _turn_block("id-a2", "Question A", "Reponse A"),
            _turn_block("id-b1", "Question B", "Reponse B"),
        ]
        conv = GeminiParser().parse(
            merge_turn_fragments(fragments), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == ["user", "assistant"] * 2
        assert "Question A" in conv.messages[0].texte
        assert "Reponse A" in conv.messages[1].texte
        assert "Reponse B" in conv.messages[3].texte

    def test_dedup_choisit_la_reponse_la_plus_tardive(self):
        fragments = [
            _turn_block("id-a", "Question A", "Reponse A"),
            _turn_block("id-b", "Question B", "Reponse B"),
            _turn_block("id-a2", "Question A", "Reponse A bis"),
        ]
        conv = GeminiParser().parse(
            merge_turn_fragments(fragments), conversation_id="c"
        )
        assert len(conv.messages) == 4
        assert "Reponse B" in conv.messages[1].texte
        assert "Reponse A bis" in conv.messages[3].texte
        # A n'apparait qu'une fois
        assert sum("Question A" in m.texte for m in conv.messages) == 1

    def test_fragments_vides(self):
        assert merge_turn_fragments([]) == ""
        assert merge_turn_fragments(["<div>sans tour</div>"]) == ""


class TestPerplexityParser:
    def test_structure_complete(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="cid-perp"
        )
        assert conv.platform == "perplexity"
        assert conv.title.startswith("Où en sont les voitures")
        assert conv.model == "sonar-pro"
        assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]

    def test_sources_dans_metadata(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        sources = conv.messages[1].metadata["sources"]
        assert sources == ["aceee.org", "eea.europa.eu"]

    def test_started_at_depuis_en_tete(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        assert conv.started_at == "2026-09-05T08:00:00Z"

    # DOM 2026 sans data-testid : bulles Tailwind `group/user-bubble` et
    # tours `data-workflow-final-text` (en-tete de workflow + corps `lm`).
    _DOM_2026 = """
    <div class="group group/user-bubble flex items-start justify-end gap-2">
      <div class="flex flex-col items-end gap-1 max-w-[600px]">
        <div class="inline-flex flex-col items-end">
          <div class="min-w-[48px] select-none p-3 bg-subtle rounded-2xl">
            <span class="min-w-0 font-sans text-base text-primary select-text">
              <span class="block max-w-full whitespace-pre-line break-words">
                Crée un lien Markdown vers
                <span role="button" title="https://exemple.com">https://exemple.com</span>
                avec le texte "Exemple".
              </span>
            </span>
          </div>
        </div>
        <div class="mt-1 flex h-6 items-center justify-end opacity-0
                    group-hover/user-bubble:pointer-events-auto">
          <span class="text-tertiary text-xs select-none whitespace-nowrap">01:27</span>
          <button aria-label="Copier la requête">Copier</button>
        </div>
      </div>
    </div>
    <div class="flex flex-col min-w-0 group/final-text gap-1 mt-4"
         data-workflow-final-text="">
      <div class="flex flex-col min-w-0 gap-4">
        <div class="w-full"><div class="contents"><div class="flex flex-col min-w-0">
          <div class="group/step-header relative z-10 flex items-center gap-2">
            <div class="min-w-0 w-full flex items-center gap-2">
              <span class="flex min-w-0 items-center gap-1">
                <div class="font-sans text-secondary text-sm select-none">Recherche terminée</div>
              </span>
            </div>
          </div>
        </div></div></div>
        <div class="w-full"><div class="w-full flex flex-col"><div class="contents">
          <div class="break-words min-w-0 flex-1"><div>
            <div class="prose leading-relaxed" data-renderer="lm">
              <p>[Exemple](https://exemple.com)</p>
            </div>
          </div></div>
        </div></div></div>
      </div>
    </div>
    """

    def test_dom_2026_une_bulle_par_message(self):
        conv = PerplexityParser().parse(self._DOM_2026, conversation_id="c")
        # la barre d'outils (jetons `group-hover/user-bubble:...`) ne doit pas
        # etre selectionnee comme un second message user
        assert [m.role for m in conv.messages] == ["user", "assistant"]

    def test_dom_2026_sans_horodatage_dans_la_requete(self):
        conv = PerplexityParser().parse(self._DOM_2026, conversation_id="c")
        user = conv.messages[0].texte
        assert "01:27" not in user
        assert "Copier" not in user
        # URL saisie rendue en `span[role=button][title]` : conservee
        assert "https://exemple.com" in user

    def test_dom_2026_assistant_sans_libelle_workflow(self):
        conv = PerplexityParser().parse(self._DOM_2026, conversation_id="c")
        assistant = conv.messages[1].texte
        assert "Recherche terminée" not in assistant
        assert "[Exemple](https://exemple.com)" in assistant

    def test_dom_2026_separateur_horizontal_en_markdown(self):
        html = self._DOM_2026.replace(
            "<p>[Exemple](https://exemple.com)</p>", "<hr/>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[1].texte == "---"

    # -- markdown riche (images, listes, tableaux, code, LaTeX) ---------------

    @staticmethod
    def _assistant(body: str) -> str:
        return (
            '<div data-workflow-final-text=""><div data-renderer="lm">'
            f"{body}</div></div>"
        )

    @staticmethod
    def _user(body: str) -> str:
        return (
            '<div class="group group/user-bubble flex items-start justify-end">'
            f"{body}</div>"
        )

    def test_image_piece_jointe_s3_en_markdown(self):
        html = self._user(
            '<div class="inline-flex">'
            '<span class="whitespace-pre-line">Décris cette image.</span>'
            '<button><div class="size-6 overflow-hidden">'
            '<img alt="Pièce jointe" class="aspect-square opacity-0" '
            'src="https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/x/image.jpg">'
            "</div></button>"
            '<div class="mt-1 opacity-0 group-hover/user-bubble:opacity-100">'
            '<span>01:27</span><button aria-label="Copier">Copier</button></div>'
            "</div>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "![Pièce jointe](https://ppl-ai-file-upload.s3.amazonaws.com" in text
        assert "Décris cette image." in text
        assert "01:27" not in text and "Copier" not in text

    def test_piece_jointe_fichier_garde_le_nom(self):
        html = self._user(
            '<span class="whitespace-pre-line">Analyse ce fichier.</span>'
            '<button class="reset"><div><svg></svg></div>'
            '<div class="line-clamp-1">notes.txt</div></button>'
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert "notes.txt" in conv.messages[0].texte

    def test_liste_imbriquee_en_markdown(self):
        html = self._assistant(
            "<ul><li>Parent<ul><li>Enfant 1</li><li>Enfant 2</li></ul></li>"
            "<li>Autre</li></ul>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "- Parent" in text
        assert "  - Enfant 1" in text
        assert "  - Enfant 2" in text
        assert "- Autre" in text

    def test_liste_numerotee_respecte_start(self):
        html = self._assistant('<ol start="3"><li>Trois</li><li>Quatre</li></ol>')
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].texte == "3. Trois\n4. Quatre"

    def test_checklist_cases_a_cocher(self):
        html = self._assistant(
            '<ul><li class="list-none"><span data-pplx-task-checkbox="true">'
            '<button aria-checked="true"></button></span>Fait</li>'
            '<li class="list-none"><span data-pplx-task-checkbox="true">'
            '<button aria-checked="false"></button></span>À faire</li></ul>'
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].texte == "- [x] Fait\n- [ ] À faire"

    def test_tableau_en_markdown(self):
        html = self._assistant(
            "<table><thead><tr><th>Colonne 1</th><th>Colonne 2</th></tr></thead>"
            "<tbody><tr><td>Ligne 1</td><td>Donnée A</td></tr></tbody></table>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "| Colonne 1 | Colonne 2 |" in text
        assert "| --- | --- |" in text
        assert "| Ligne 1 | Donnée A |" in text

    def test_latex_inline_et_display(self):
        inline = (
            '<span class="katex"><span class="katex-mathml"><math><semantics>'
            '<annotation encoding="application/x-tex">E = mc^2</annotation>'
            "</semantics></math></span></span>"
        )
        display = (
            '<span class="katex-display"><span class="katex">'
            '<span class="katex-mathml"><math><semantics>'
            '<annotation encoding="application/x-tex">\\int_0^1 x</annotation>'
            "</semantics></math></span></span></span>"
        )
        conv = PerplexityParser().parse(
            self._assistant(f"<p>Soit {inline}.</p><p>{display}</p>"),
            conversation_id="c",
        )
        text = conv.messages[0].texte
        assert "$E = mc^2$" in text
        assert "$$\\int_0^1 x$$" in text

    def test_code_language_depuis_figcaption(self):
        html = self._assistant(
            '<pre class="not-prose"><figure><figcaption><span>python</span>'
            '<button aria-label="Copier le code">Copier</button></figcaption>'
            '<code><span>print(1)</span></code></figure></pre>'
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        blocks = conv.messages[0].code_blocks
        assert blocks and blocks[0].language == "python"
        assert "print(1)" in blocks[0].code

    def test_deux_reponses_identiques_restent_distinctes(self):
        body = "Oui."
        html = (
            self._user("<span>Question 1 ?</span>")
            + self._assistant(body)
            + self._user("<span>Question 2 ?</span>")
            + self._assistant(body)
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert [m.texte for m in conv.messages] == [
            "Question 1 ?", "Oui.", "Question 2 ?", "Oui.",
        ]


class TestPerplexityMergeThread:
    """Fusion des messages accumules en remontant le fil virtualise."""

    _USER = (
        '<div class="group group/user-bubble flex items-start justify-end gap-2">'
        "<span>{text}</span></div>"
    )
    _ASSIST = (
        '<div class="flex flex-col min-w-0 group/final-text gap-1 mt-4" '
        'data-workflow-final-text=""><div class="prose" data-renderer="lm">'
        "<p>{text}</p></div></div>"
    )

    def _items(self):
        # ordre de decouverte remontant le fil : les positions absolues
        # permettent de retrouver l'ordre chronologique.
        return [
            {"key": "a:2", "role": "assistant", "html": self._ASSIST.format(text="R2"), "pos": 300},
            {"key": "u:1", "role": "user", "html": self._USER.format(text="Q1"), "pos": 100},
            {"key": "a:1", "role": "assistant", "html": self._ASSIST.format(text="R1"), "pos": 200},
            {"key": "u:2", "role": "user", "html": self._USER.format(text="Q2"), "pos": 250},
        ]

    def test_trie_par_position_et_parse(self):
        conv = PerplexityParser().parse(
            merge_thread_messages(self._items()), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert [m.texte for m in conv.messages] == ["Q1", "R1", "Q2", "R2"]

    def test_items_vides(self):
        assert merge_thread_messages([]) == ""
        assert merge_thread_messages([{"key": "", "html": ""}]) == ""

    def test_reponses_identiques_cles_distinctes_non_fusionnees(self):
        body = self._ASSIST.format(text="R")
        items = [
            {"key": "u:1", "role": "user", "html": self._USER.format(text="Q1"), "pos": 100},
            {"key": "a:1", "role": "assistant", "html": body, "pos": 200},
            {"key": "u:2", "role": "user", "html": self._USER.format(text="Q2"), "pos": 250},
            {"key": "a:2", "role": "assistant", "html": body, "pos": 300},
        ]
        conv = PerplexityParser().parse(
            merge_thread_messages(items), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert [m.texte for m in conv.messages] == ["Q1", "R", "Q2", "R"]

    def test_cle_de_collecte_stable_par_noeud(self):
        # plus de cle `role + texte[:200]` qui fusionnait deux tours identiques
        from src.services.perplexity import _COLLECT_THREAD_JS

        assert "WeakMap" in _COLLECT_THREAD_JS
        assert "text.slice(0, 200)" not in _COLLECT_THREAD_JS


class TestTextOfMarkdown:
    """Conversion des noeuds riches en markdown (liens, images, code)."""

    parser = ChatGPTParser()  # text_of est herite de BaseParser

    def test_lien_distant_en_markdown(self):
        html = '<p>Voir <a href="https://example.com/doc">la doc</a> pour plus.</p>'
        text = self.parser.text_of(self.parser.make_soup(html).p)
        assert "[la doc](https://example.com/doc)" in text
        assert "pour plus" in text

    def test_lien_sans_texte_garde_l_url(self):
        html = '<a href="https://example.com">https://example.com</a>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "[https://example.com](https://example.com)" in text

    def test_lien_ancre_reduit_au_texte(self):
        html = '<a href="#section-2">Section 2</a>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "Section 2" in text and "]" not in text

    def test_lien_label_avec_crochets(self):
        html = '<a href="https://x.io/a">Notes [1]</a>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "[Notes \\[1\\]](https://x.io/a)" in text

    def test_href_avec_parentheses_encodees(self):
        html = '<a href="https://x.io/f(a,b)">fichier</a>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "](https://x.io/f%28a,b%29)" in text

    def test_image_distante_en_markdown(self):
        html = '<img src="https://img.example.com/p.png" alt="photo">'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "![photo](https://img.example.com/p.png)" in text

    def test_image_blob_ignoree(self):
        html = '<img src="blob:https://chatgpt.com/xyz" alt="aperçu">'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "blob:" not in text and "aperçu" not in text

    def test_image_dans_lien(self):
        html = '<a href="https://x.io"><img src="https://x.io/i.png" alt="logo"></a>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "[![logo](https://x.io/i.png)](https://x.io)" in text

    def test_katex_inline_ferme_le_dollar(self):
        html = (
            '<p>Soit <span class="katex"><span class="katex-mathml"><math>'
            '<semantics><annotation encoding="application/x-tex">E = mc^2</annotation>'
            "</semantics></math></span><span class=\"katex-html\">E = mc2</span></span>.</p>"
        )
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "$E = mc^2$" in text

    def test_katex_display_double_dollar(self):
        html = (
            '<div class="katex-display"><span class="katex">'
            '<span class="katex-mathml"><math><semantics>'
            '<annotation encoding="application/x-tex">\\int_0^1 x^2</annotation>'
            "</semantics></math></span></span></div>"
        )
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "$$\\int_0^1 x^2$$" in text

    def test_bouton_avec_lien_exclu(self):
        html = '<button><a href="https://x.io">Copier</a></button>'
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "x.io" not in text

    def test_marqueur_citation_exclu_du_texte(self):
        html = (
            "<p>pip est la méthode courante."
            '<span class="citation inline"><span>reddit</span><span>+1</span></span></p>'
        )
        text = self.parser.text_of(self.parser.make_soup(html))
        assert "courante." in text and "reddit" not in text and "+1" not in text


class TestGrokParser:
    """Grok n'a pas de DOM : le parser travaille sur les donnees de l'API."""

    PAYLOAD = {
        "conversation": {
            "conversationId": "grok-1",
            "title": "Test Grok",
            "createTime": "2026-09-01T10:00:00.000Z",
            "modifyTime": "2026-09-01T10:05:00.000Z",
        },
        "responses": [
            {"responseId": "r1", "sender": "human", "message": "Salut",
             "createTime": "2026-09-01T10:00:00.000Z", "model": ""},
            {"responseId": "r2", "sender": "assistant",
             "message": "Bonjour !\n```python\nprint(1)\n```",
             "createTime": "2026-09-01T10:00:05.000Z", "model": "grok-3"},
        ],
    }

    def test_structure(self):
        conv = GrokParser().parse("", conversation_id="grok-1", extra=self.PAYLOAD)
        assert conv.platform == "grok"
        assert conv.title == "Test Grok"
        assert conv.model == "grok-3"
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[0].message_id == "r1"
        assert conv.messages[1].has_code
        assert conv.messages[1].code_blocks[0].language == "python"

    def test_sans_reponses_erreur(self):
        with pytest.raises(ParseError):
            GrokParser().parse("", conversation_id="x", extra={"responses": []})

    def test_reponse_vide_avec_stream_error_garde_l_alternance(self):
        """Cas reel du quota : l'assistant vide ne doit pas fusionner les users.

        Sans la reponse d'erreur, le parser saute le tour vide et les deux
        messages `user` consecutifs sont fusionnes (le test 30 disparait).
        """
        payload = {
            "conversation": {"conversationId": "grok-2", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Voici un JSON invalide.", "model": ""},
                {"responseId": "r2", "sender": "assistant", "message": "",
                 "model": "grok-3",
                 "streamErrors": [{
                     "message": "You've reached your usage limit. Please try "
                                "again later.",
                     "severity": "STREAM_ERROR_SEVERITY_NORMAL",
                     "usageLimitReached": {"midTurn": False}}]},
                {"responseId": "r3", "sender": "human",
                 "message": "Texte : Alice 30, Bob 25, Charlie 35.", "model": ""},
                {"responseId": "r4", "sender": "assistant",
                 "message": "- Bob\n- Alice\n- Charlie", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-2", extra=payload)
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert "usage limit" in conv.messages[1].texte
        assert "Charlie" in conv.messages[3].texte
        # le message user du 2e tour n'a pas ete absorbe par le 1er
        assert "Alice" in conv.messages[2].texte
        assert "Alice" not in conv.messages[0].texte

    def test_stream_error_dans_metadata(self):
        payload = {
            "conversation": {"conversationId": "grok-3", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Salut",
                 "model": ""},
                {"responseId": "r2", "sender": "assistant", "message": "",
                 "model": "grok-3",
                 "metadata": {"request_metadata": {"stream_errors": [
                     {"message": "Rate limit reached."}]}}},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-3", extra=payload)
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[1].texte == "Rate limit reached."

    def test_fichier_genere_garde_l_alternance(self):
        """Tour assistant reduit a une carte `rendered_file_card` (sans texte).

        Cas reel des tests 37/47 : Grok fournit le fichier demande mais le
        `message` est vide. Sans conserver la carte, le tour est saute et les
        deux messages `user` encadrants sont fusionnes.
        """
        card = json.dumps({
            "type": "render_file",
            "cardType": "rendered_file_card",
            "file_name": "resultat.txt",
            "mime_type": "text/plain",
            "file_size": 18,
            "url": "users/x/generated/y/resultat.txt",
        })
        payload = {
            "conversation": {"conversationId": "grok-4", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Crée un fichier TXT nommé resultat.txt.", "model": ""},
                {"responseId": "r2", "sender": "assistant", "message": "",
                 "model": "grok-3", "cardAttachmentsJson": [card]},
                {"responseId": "r3", "sender": "human",
                 "message": "Texte long : Section A début.", "model": ""},
                {"responseId": "r4", "sender": "assistant",
                 "message": "Voici le texte long.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-4", extra=payload)
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert "resultat.txt" in conv.messages[1].texte
        assert conv.messages[1].metadata.get("generated_files")
        # le message user suivant n'a pas ete absorbe par le precedent
        assert "Texte long" in conv.messages[2].texte
        assert "Texte long" not in conv.messages[0].texte

    def test_tour_assistant_vide_garde_l_alternance(self):
        """Assistant vide sans erreur ni fichier : tour conserve (alternance)."""
        payload = {
            "conversation": {"conversationId": "grok-5", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Premier message.", "model": ""},
                {"responseId": "r2", "sender": "assistant", "message": "",
                 "model": "grok-3"},
                {"responseId": "r3", "sender": "human",
                 "message": "Deuxieme message.", "model": ""},
                {"responseId": "r4", "sender": "assistant",
                 "message": "Reponse.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-5", extra=payload)
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert conv.messages[1].texte == ""
        assert "Deuxieme" in conv.messages[2].texte
        assert "Deuxieme" not in conv.messages[0].texte

    def test_pieces_jointes_image_et_fichier(self):
        """Image jointe -> markdown `![...]`, fichier -> lien markdown."""
        payload = {
            "conversation": {"conversationId": "grok-6", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Décris cette image et ce fichier.",
                 "model": "",
                 "fileAttachmentAssetMetadata": [
                     {"assetId": "a1", "mimeType": "image/png",
                      "name": "image.png",
                      "key": "users/u1/a1/content"},
                     {"assetId": "a2", "mimeType": "text/plain",
                      "name": "notes.txt",
                      "key": "users/u1/a2/content"},
                 ]},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Voilà.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-6", extra=payload)
        text = conv.messages[0].texte
        assert "![image.png](https://assets.grok.com/users/u1/a1/content)" in text
        assert "[notes.txt](https://assets.grok.com/users/u1/a2/content)" in text
        assert text.startswith("Décris cette image")

    def test_piece_jointe_seule_garde_l_alternance(self):
        """Tour utilisateur sans texte mais avec image : ne pas le sauter."""
        payload = {
            "conversation": {"conversationId": "grok-7", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "",
                 "fileAttachmentAssetMetadata": [
                     {"mimeType": "image/jpeg", "name": "photo.jpg",
                      "key": "users/u1/photo/content"},
                 ]},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Reponse.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-7", extra=payload)
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[0].texte == \
            "![photo.jpg](https://assets.grok.com/users/u1/photo/content)"

    def test_piece_jointe_repli_metadata(self):
        """Repli sur `fileAttachmentsMetadata` si les assets riches manquent."""
        payload = {
            "conversation": {"conversationId": "grok-9", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Voici.",
                 "fileAttachmentsMetadata": [
                     {"fileName": "doc.pdf", "fileMimeType": "application/pdf",
                      "fileUri": "users/u1/doc/content"},
                 ]},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Recu.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-9", extra=payload)
        assert "[doc.pdf](https://assets.grok.com/users/u1/doc/content)" \
            in conv.messages[0].texte

    def test_image_generee_en_markdown(self):
        """Image generee (URL absolue ou cle) ajoutee en markdown."""
        payload = {
            "conversation": {"conversationId": "grok-8", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Genere."},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Voici.", "model": "grok-3",
                 "generatedImageUrls": [
                     "https://assets.grok.com/gen/img.png",
                     "users/u1/generated/2.png",
                 ]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-8", extra=payload)
        text = conv.messages[1].texte
        assert "![image](https://assets.grok.com/gen/img.png)" in text
        assert "![image](https://assets.grok.com/users/u1/generated/2.png)" in text


class TestGrokServiceFetch:
    """Le pipeline d'images a besoin d'un `fetch` authentifie (cookies)."""

    class _FakeHttp:
        def __init__(self, status, data):
            self.calls = []
            self._status = status
            self._data = data

        def get(self, url, accept="application/json"):
            self.calls.append(url)
            return self._status, self._data

    def test_fetch_renvoie_bytes_et_mime(self):
        from src.services.grok import _CookieFetchSession

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        http = self._FakeHttp(200, png)
        session = _CookieFetchSession(http)
        data, mime = session.fetch("https://assets.grok.com/x/content")
        assert data == png
        assert mime == "image/png"
        assert http.calls == ["https://assets.grok.com/x/content"]

    def test_fetch_echec_renvoie_none(self):
        from src.services.grok import _CookieFetchSession

        session = _CookieFetchSession(self._FakeHttp(403, b"no"))
        assert session.fetch("https://assets.grok.com/x/content") is None

    def test_delegue_les_autres_methodes(self):
        from src.services.grok import _CookieFetchSession

        class Inner:
            def evaluate(self, expr):
                return expr

        session = _CookieFetchSession(self._FakeHttp(200, b""), Inner())
        assert session.evaluate("1+1") == "1+1"
        # wait_ms/close restent des no-op (pas de navigateur)
        assert session.wait_ms(12) is None



class TestMistralParser:
    """Cas reel de l'etalon : une reponse assistant reduite a un <hr>."""

    HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Affiche un separateur horizontal.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div data-message-part-type="answer">
          <div class="markdown-container-style"><hr/></div>
        </div>
      </div>
      <div data-message-author-role="user" data-message-id="u-2">
        <div class="select-text"><span>Affiche le code en ligne.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-2">
        <div data-message-part-type="answer">
          <div class="markdown-container-style"><p>Voici <code>print('Hello')</code>.</p></div>
        </div>
      </div>
    </main></body></html>
    """

    def test_hr_seul_conserve_l_alternance(self):
        conv = MistralParser().parse(self.HTML, conversation_id="work-1")
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert conv.messages[1].texte == "---"
        assert "print('Hello')" in conv.messages[3].texte

    MARKDOWN_HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Structure.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div data-message-part-type="answer">
          <div class="markdown-container-style">
            <h1>Titre</h1><h2>Sous-titre</h2>
            <ul><li>un</li><li>deux<ul><li>deux a</li></ul></li></ul>
            <ol start="3"><li>trois</li><li>quatre</li></ol>
            <table>
              <tr><th>A</th><th>B</th></tr>
              <tr><td>1</td><td>2</td></tr>
            </table>
            <blockquote><p>citation</p></blockquote>
            <hr/>
          </div>
        </div>
      </div>
    </main></body></html>
    """

    def test_listes_tableaux_titres_et_separateur(self):
        conv = MistralParser().parse(self.MARKDOWN_HTML, conversation_id="work-1")
        text = conv.messages[1].texte
        assert "# Titre" in text
        assert "## Sous-titre" in text
        assert "- un\n- deux\n  - deux a" in text
        assert "3. trois\n4. quatre" in text
        assert "| A | B |\n| --- | --- |\n| 1 | 2 |" in text
        assert "> citation" in text
        assert text.rstrip().endswith("---")

    @staticmethod
    def _rsc_html(files):
        """HTML minimal avec un payload RSC Next.js portant les pieces jointes."""
        message = {
            "role": "user",
            "content": "Question",
            "files": files,
            "id": "u-1",
        }
        script = "self.__next_f.push([1,%s])" % json.dumps(json.dumps(message))
        return (
            "<html><head><title>Test Mistral</title></head><body><main>"
            '<div data-message-author-role="user" data-message-id="u-1">'
            '<div class="select-text"><span>Question</span></div></div>'
            f"<script>{script}</script>"
            "</main></body></html>"
        )

    def test_pieces_jointes_depuis_payload_rsc(self):
        html = self._rsc_html(
            [
                {"type": "image", "url": "https://blob/chat-images/a?x=1", "name": "image.png"},
                {"type": "document", "url": "https://blob/chat-documents/b", "name": "document.pdf"},
                {"type": "audio", "url": "https://blob/chat-documents/c", "name": "audio.mp3"},
            ]
        )
        conv = MistralParser().parse(html, conversation_id="work-1")
        text = conv.messages[0].texte
        assert "![image.png](https://blob/chat-images/a?x=1)" in text
        assert "[document.pdf](https://blob/chat-documents/b)" in text
        assert "[audio.mp3](https://blob/chat-documents/c)" in text

    def test_pieces_jointes_repli_dom(self):
        html = """
        <html><head><title>Test Mistral</title></head><body><main>
          <div data-message-author-role="user" data-message-id="u-1">
            <div class="select-text"><span>Question</span></div>
            <div class="flex-wrap justify-end">
              <div><img src="https://blob/chat-images/a.png"/></div>
              <div><p class="group/text-truncator relative grid">
                <span class="line-clamp-2">notes.txt</span>
              </p></div>
            </div>
          </div>
        </main></body></html>
        """
        conv = MistralParser().parse(html, conversation_id="work-1")
        text = conv.messages[0].texte
        assert "![image](https://blob/chat-images/a.png)" in text
        assert "notes.txt" in text

    RICH_HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Tableau et maths.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div data-message-part-type="answer">
          <div class="markdown-container-style">
            <p>Voici <span class="katex"><span class="katex-mathml"><math>
              <semantics><annotation encoding="application/x-tex">E = mc^2</annotation>
              </semantics></math></span><span class="katex-html">E = mc2</span></span>.</p>
            <div data-rich-table-inner-html="&lt;table&gt;&lt;thead&gt;&lt;tr&gt;&lt;th&gt;Nom&lt;/th&gt;&lt;th&gt;Age&lt;/th&gt;&lt;/tr&gt;&lt;/thead&gt;&lt;tbody&gt;&lt;tr&gt;&lt;td&gt;Alice&lt;/td&gt;&lt;td&gt;25&lt;/td&gt;&lt;/tr&gt;&lt;/tbody&gt;&lt;/table&gt;"
                 data-rich-table-title="Tableau">
              <div role="table" class="grid rich-table">
                <div role="columnheader"><button><span>Nom</span></button></div>
                <div role="columnheader"><button><span>Age</span></button></div>
                <div role="columnheader" class="bg-card sticky end-0"><button>x</button></div>
                <div role="cell"><span>Alice</span></div>
                <div role="cell"><span>25</span></div>
                <div role="cell" class="bg-card sticky end-0"></div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </main></body></html>
    """

    def test_tableau_rich_ui_et_latex_inline(self):
        conv = MistralParser().parse(self.RICH_HTML, conversation_id="work-1")
        text = conv.messages[1].texte
        assert "$E = mc^2$" in text  # $ fermant (base l'oublie)
        assert "Tableau" in text
        assert "| Nom | Age |" in text
        assert "| --- | --- |" in text
        assert "| Alice | 25 |" in text


class TestTousLesParsers:
    @pytest.mark.parametrize("service", sorted(set(PARSER_CLASSES) - {"grok"}))
    def test_export_valide_et_serialisable(self, service, fixture_html):
        parser = PARSER_CLASSES[service]()
        conv = parser.parse(
            fixture_html(f"{service}_conversation.html"), conversation_id=f"cid-{service}"
        )
        conv.validate()
        data = conv.to_dict()
        assert data["platform"] == service
        assert all("code_blocks" in m for m in data["messages"])
        import json

        json.loads(json.dumps(data))  # roundtrip sans exception
