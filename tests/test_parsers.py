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
from src.parsers.mistral import merge_message_fragments
from src.parsers.perplexity import merge_thread_messages
from src.schema import Conversation


# ---------------------------------------------------------------------------
# Listes de conversations (sidebars)
# ---------------------------------------------------------------------------


class TestParseLinks:
    def test_chatgpt_sidebar(self, fixture_html):
        refs = ChatGPTParser().parse_links(fixture_html("chatgpt_home.html"), "https://chatgpt.com/")
        assert [r.id for r in refs] == [
            "f0000000-0000-4000-8000-fcd84216abfa",
            "f0000000-0000-4000-8000-2ee19297ee2b",
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
        assert conv.messages[0].message_id == "f0000000-0000-4000-8000-6c26793a5ab4"
        assert conv.messages[0].model is None
        assert conv.messages[1].model == "gpt-5"
        assert conv.messages[1].message_id == "f0000000-0000-4000-8000-ce6ee6bc2b7d"

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
                "f0000000-0000-4000-8000-6c26793a5ab4": {"time": 1767522600, "model": None},
                "f0000000-0000-4000-8000-ce6ee6bc2b7d": {"time": 1767522630, "model": "gpt-5"},
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

    def test_sources_citations_web(self):
        """Pastilles de source -> Message.sources (« titre — url », deduplique)."""
        conv = self._conversation(
            "<p>Voir "
            "<span data-testid='webpage-citation-pill'><a href='https://example.com/a' "
            "alt='https://example.com/a'><span class='truncate'>Example Domain</span></a></span>"
            " et "
            "<span data-testid='webpage-citation-pill'><a href='https://example.com/a'>"
            "<span class='truncate'>Example Domain</span></a></span>"
            " et "
            "<span data-testid='webpage-citation-pill'><a href='https://openai.com/b'>"
            "<span class='truncate'>OpenAI Help Center +1</span></a></span>.</p>"
        )
        assert conv.messages[0].sources == [
            "Example Domain — https://example.com/a",
            "OpenAI Help Center — https://openai.com/b",
        ]

    def test_tools_etapes_cot(self):
        """Cartes d'appel d'outil (cot-v5) -> Message.tools."""
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='assistant' data-message-id='a1'>
              <div class='flex flex-col'>
                <button aria-expanded='true'>Worked for 5s</button>
                <div class='panel'>
                  <div class='group'>
                    <button aria-label='Searched files and read the requested file'></button>
                    <div class='contents'>
                      <span data-testid='cot-v5-tool-icon-pile'>
                        <span data-testid='cot-v5-native-tool-icon'></span></span>
                      <span class='inline-flex'><span>Searched files and read the requested file</span></span>
                    </div>
                  </div>
                  <div class='group'>
                    <button aria-label='Interacted with Files'></button>
                    <div class='contents'>
                      <span data-testid='cot-v5-tool-icon-pile'>
                        <span data-testid='cot-v5-native-tool-icon'></span></span>
                    </div>
                  </div>
                </div>
              </div>
              <div class='markdown'><p>2</p></div>
            </div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        assert conv.messages[0].tools == [
            "Searched files and read the requested file",
            "Interacted with Files",
        ]
        # le raisonnement reste extrait (comportement existant)
        assert "Searched files and read the requested file" in conv.messages[0].reasoning

    def test_etalon_tools_etapes(self, fixture_html):
        """Etalon gele : 3 messages portent des etapes d'outil."""
        conv = ChatGPTParser().parse(
            fixture_html("etalons/chatgpt/chatgpt.html"),
            conversation_id="f0000000-0000-4000-8000-f2f43feb599e",
        )
        with_tools = [m for m in conv.messages if m.tools]
        assert len(with_tools) == 3
        flat = [tool for m in with_tools for tool in m.tools]
        assert "Searched files and read the requested file" in flat
        assert "Interacted with Files" in flat

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

    def test_reasoning_panneau_cot_deplie(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='assistant' data-message-id='a1'>
              <div class='flex flex-col'>
                <button aria-expanded='true'>Thought for 12s</button>
                <div class='panel'><p>Je verifie les donnees.</p>
                  <p>Puis je conclus.</p></div>
              </div>
              <div class='markdown'><p>Reponse finale</p></div>
            </div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        assert conv.messages[0].reasoning == "Je verifie les donnees.\nPuis je conclus."
        # le raisonnement n'est pas injecte dans le texte du message
        assert "Je verifie" not in conv.messages[0].texte

    def test_reasoning_panneau_replie_garde_le_libelle(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='assistant' data-message-id='a1'>
              <button aria-expanded='false'>Worked for 5s</button>
              <div class='panel'></div>
              <div class='markdown'><p>2</p></div>
            </div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        assert conv.messages[0].reasoning == "Worked for 5s"

    def test_artifacts_canvas_titre_et_contenu(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='assistant' data-message-id='a1'>
              <div class='markdown'><p>Voici l'artefact.</p></div>
              <div id='textdoc-message-abc123'>
                <div class='header'><span class='font-semibold'>Questions Jury</span></div>
                <div contenteditable='true'><h1>Questions possibles</h1>
                  <ul><li><p>Q1</p></li></ul></div>
              </div>
            </div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        message = conv.messages[0]
        assert message.artifacts == ["Questions Jury"]
        assert "# Questions possibles" in message.texte
        assert "- Q1" in message.texte

    def test_piece_jointe_file_tile_dans_texte(self):
        html = """<html><body><main>
          <div data-testid='conversation-turn-1'>
            <div data-message-author-role='user' data-message-id='u1'>
              <div class='group/file-tile' aria-label='donnees.json'>
                <div class='truncate font-semibold'>donnees.json</div></div>
              <div class='group/file-tile' aria-label='donnees.csv'>
                <div class='truncate font-semibold'>donnees.csv</div></div>
              <div class='group/file-tile' aria-label='document(1).pdf'>
                <div class='truncate font-semibold'>document(1).pdf</div></div>
              <div class='markdown'><p>Analyse</p></div>
            </div>
          </div>
        </main></body></html>"""
        conv = ChatGPTParser().parse(html, conversation_id="c")
        texte = conv.messages[0].texte
        assert "donnees.json" in texte
        assert "donnees.csv" in texte
        assert "document(1).pdf" in texte


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
            conversation_id="f0000000-0000-4000-8000-584ffecb651a",
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
        # ... mais recuperee dans `reasoning`
        assert assistant.reasoning == "Je dois organiser 7 jours…"
        assert conv.messages[0].reasoning == ""

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

    def test_markdown_complet(self):
        """Titres, listes (imbriquees/checklist), citations, `---`, tableaux."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response"><div class="prose">
              <div class="standard-markdown">
                <h2>Section</h2>
                <p><strong>gras</strong> <em>ital</em> <del>barre</del></p>
                <ul><li>un</li><li>deux<ul><li>nid</li></ul></li></ul>
                <ol><li>premier</li><li>second</li></ol>
                <ul class="contains-task-list">
                  <li class="task-list-item"><input disabled type="checkbox"/> a faire</li>
                  <li class="task-list-item"><input checked disabled type="checkbox"/> fait</li>
                </ul>
                <blockquote><p>citation</p></blockquote>
                <hr/>
                <table>
                  <thead><tr><th>Cle</th><th>Val</th></tr></thead>
                  <tbody><tr><td>a</td><td>1</td></tr></tbody>
                </table>
              </div>
            </div></div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        texte = conv.messages[0].texte
        assert "## Section" in texte
        assert "**gras**" in texte
        assert "*ital*" in texte
        assert "~~barre~~" in texte
        assert "- un" in texte
        assert "  - nid" in texte  # liste imbriquee indente
        assert "1. premier" in texte
        assert "2. second" in texte
        assert "- [ ] a faire" in texte
        assert "- [x] fait" in texte
        assert "> citation" in texte
        assert "\n---\n" in texte
        assert "| Cle | Val |" in texte
        assert "| --- | --- |" in texte

    def test_pieces_jointes_user(self):
        """Images et fichiers joints : markdown (URL absolue) + noms."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='human'>
            <div data-cds='UserMessage'>
              <div data-cds='MessageAttachments'>
                <div data-cds='MessageAttachmentsImage' data-testid='file-thumbnail'>
                  <img alt="" src="/api/files/abc/preview" width="120" height="120"/>
                  <span class="sr-only">image.png</span>
                </div>
                <div data-cds='MessageAttachmentsFile' data-testid='file-thumbnail'>
                  <span title="notes.txt"><span class="sr-only">notes.txt</span></span>
                </div>
              </div>
              <div data-testid='user-message'><p>Analyse ca.</p></div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        texte = conv.messages[0].texte
        assert "Analyse ca." in texte
        assert "![image.png](https://claude.ai/api/files/abc/preview)" in texte
        assert "notes.txt" in texte

    def test_code_langage_et_favicon_exclus(self):
        """En-tete de code retire (langage conserve), favicons non extraits."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response"><div class="prose">
              <div class="standard-markdown">
                <div data-not-prose=""><div aria-label="Code python">
                  <div class="text-text-500 font-small">python</div>
                  <div class="overflow-x-auto">
                    <pre><code class="language-python">x = 1</code></pre>
                  </div>
                </div></div>
                <p>Voir <img alt="" height="32" width="32"
                  src="https://t0.gstatic.com/faviconV2"/> la source.</p>
              </div>
            </div></div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        texte = conv.messages[0].texte
        assert "```python" in texte
        assert not texte.startswith("python")
        assert "gstatic" not in texte
        assert conv.messages[0].code_blocks[0].language == "python"

    def test_reasoning_etape_thinking_2026(self):
        """DOM transcript 2026 : etape de raisonnement -> Message.reasoning."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response">
              <div class="prose" data-cds="Prose">
                <div class="standard-markdown"><p>Voici une illustration.</p></div>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus" data-step-key="thinking-0">
                <div data-cds-row="">
                  <bdi>Checking available tools for generating the image.</bdi>
                </div>
                <span class="sr-only" role="status">
                  Checking available tools for generating the image.
                </span>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus"
                   data-step-key="toolu_01Abc">
                <div data-cds-row=""><bdi>A exécuté une commande</bdi></div>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assistant = conv.messages[0]
        assert assistant.reasoning == (
            "Checking available tools for generating the image."
        )
        assert "Checking available tools" not in assistant.texte  # hors texte
        assert "A exécuté une commande" not in assistant.texte  # statut outil exclu

    def test_reasoning_panneau_deplie_texte_integral(self):
        """Panneau de raisonnement deploye -> raisonnement integral (pas le resume).

        Le service deplie la carte de statut et l'etape ``thinking-2`` : le
        texte complet est alors dans ``[data-cds-row-panel]``, le ``<bdi>``
        n'etant qu'un resume. Le panneau de la carte parente (memoire) ne doit
        pas polluer le raisonnement.
        """
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response">
              <div class="prose" data-cds="Prose">
                <div class="standard-markdown"><p>Voici le renard.</p></div>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus" data-open="">
                <div data-cds-row="">
                  <bdi>Mémoire rappelée</bdi>
                  <button data-cds-row-toggle="" aria-expanded="true"></button>
                </div>
                <div data-cds-row-panel="">
                  <div class="standard-markdown">
                    <p>How Alex wants Claude to format responses</p>
                  </div>
                  <div data-cds="TurnStatusStep" data-step-key="thinking-1">
                    <div data-cds-row="">
                      <bdi>Finding a way to produce the image.</bdi>
                    </div>
                  </div>
                  <div data-cds="TurnStatusStep" data-step-key="thinking-2"
                       data-open="">
                    <div data-cds-row="">
                      <bdi>Adding ear details, whiskers, and snow.</bdi>
                      <button data-cds-row-toggle="" aria-expanded="true"></button>
                    </div>
                    <div data-cds-row-panel="">
                      <div class="standard-markdown">
                        <p>Designing a vivid SVG scene of a fox in snow.</p>
                      </div>
                      <div class="standard-markdown">
                        <p>Adding ear details, whiskers, and scattered snow.</p>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assistant = conv.messages[0]
        assert assistant.reasoning == (
            "Finding a way to produce the image.\n\n"
            "Designing a vivid SVG scene of a fox in snow.\n"
            "Adding ear details, whiskers, and scattered snow."
        )
        # resume court remplace par le panneau, pas duplique
        assert "Adding ear details, whiskers, and snow." not in assistant.reasoning
        assert "How Alex wants Claude" not in assistant.reasoning
        assert "Mémoire rappelée" not in assistant.reasoning
        # rien du raisonnement ne fuit dans le texte
        assert "Designing a vivid SVG scene" not in assistant.texte
        assert assistant.texte == "Voici le renard."

    def test_reasoning_carte_parente_sans_duplication(self):
        """Carte parente thinking-N : ne pas avaler le panneau des etapes filles."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response">
              <div class="prose" data-cds="Prose">
                <div class="standard-markdown"><p>Reponse.</p></div>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus"
                   data-step-key="thinking-0" data-open="">
                <div data-cds-row="">
                  <bdi>Carte parente.</bdi>
                </div>
                <div data-cds-row-panel="">
                  <div data-cds="TurnStatusStep" data-step-key="thinking-1">
                    <div data-cds-row=""><bdi>Etape fille.</bdi></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assistant = conv.messages[0]
        assert assistant.reasoning == "Carte parente.\n\nEtape fille."
        assert assistant.reasoning.count("Etape fille.") == 1

    def test_artifacts_canvas_extraits(self):
        """Cartes artefact/canvas -> Message.artifacts (titre + type)."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div class="font-claude-response">
              <div class="prose"><div class="standard-markdown">
                <p>Voici le fichier ci-dessous.</p>
              </div></div>
              <div data-sheet-kind="csv">
                <button aria-label="Afficher Scores" type="button"></button>
                <div class="text-heading">Scores</div>
                <div class="text-footnote">Tableau · CSV</div>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assistant = conv.messages[0]
        assert assistant.artifacts == ["Scores (csv)"]
        assert "Scores" not in assistant.texte  # carte hors texte
        assert "Voici le fichier" in assistant.texte

    def test_etalon_reasoning_et_artifacts(self, fixture_html):
        """Etalon gele : 1 message avec reasoning, 3 cartes artefact."""
        conv = ClaudeParser().parse(
            fixture_html("etalons/claude/claude.html"),
            conversation_id="f0000000-0000-4000-8000-12a21176e31e",
        )
        reasoning = [m for m in conv.messages if m.reasoning]
        assert len(reasoning) == 1
        assert "Checking available tools" in reasoning[0].reasoning
        assert "Checking available tools" not in reasoning[0].texte
        with_artifacts = [m for m in conv.messages if m.artifacts]
        assert len(with_artifacts) == 3
        flat = [entry for m in with_artifacts for entry in m.artifacts]
        assert flat == ["Scores (csv)", "Bonjour (txt)", "Artefact test (html)"]

    def test_etalon_horodatage_et_outils(self, fixture_html):
        """Etalon gele : horodatage de la derniere reponse + cartes d'outils."""
        conv = ClaudeParser().parse(
            fixture_html("etalons/claude/claude.html"),
            conversation_id="f0000000-0000-4000-8000-12a21176e31e",
        )
        # une seule <time> dans le transcript : rattachee a la bonne reponse
        stamped = [m for m in conv.messages if m.timestamp]
        assert len(stamped) == 1
        assert stamped[0].role == "assistant"
        assert stamped[0].timestamp == "2026-09-24T09:37:00Z"
        assert conv.started_at == "2026-09-24T09:37:00Z"
        assert conv.last_message_at == "2026-09-24T09:37:00Z"
        labels = [tool for m in conv.messages for tool in m.tools]
        assert "A exécuté une commande, fichiers partagés" in labels
        assert "Checking available tools for generating the requested image." in labels
        assert any("Artefact simple" in label for label in labels)

    def test_etalon_supp_horodatage_et_memoire(self, fixture_html):
        """Etalon supplementaire : horodatage + carte 'Memoire rappelee'."""
        conv = ClaudeParser().parse(
            fixture_html("etalons/claude/claude-supp.html"),
            conversation_id="f0000000-0000-4000-8000-34fb92a90b44",
        )
        assert conv.messages[0].tools == []
        assert conv.messages[1].timestamp == "2026-09-25T16:03:45Z"
        assert conv.messages[1].tools == ["Mémoire rappelée"]

    def test_etalon_supp_reasoning_integral(self, fixture_html):
        """Etalon supp (HTML gelee, panneau deplie) : raisonnement integral.

        Le libelle replie fait ~55 caracteres ; une fois le panneau deploye par
        le service, le texte complet (~840 caracteres) est dans le DOM et doit
        etre extrait, pas le resume.
        """
        conv = ClaudeParser().parse(
            fixture_html("etalons/claude/claude-supp.html"),
            conversation_id="f0000000-0000-4000-8000-34fb92a90b44",
        )
        reasoning = conv.messages[1].reasoning
        assert "Designing a vivid SVG scene of a fox in snow." in reasoning
        assert "Adding ear details, whiskers, and scattered falling snow." in reasoning
        assert len(reasoning) > 700
        assert conv.messages[1].metadata.get("had_thinking") is True

    def test_sources_citations_web(self):
        """Citations web inline -> Message.sources (URL dedupliquees)."""
        html = """<html><body>
          <div data-testid='transcript-row'>
            <div class="font-claude-response"><div class="prose">
              <div class="standard-markdown"><p>Selon
                <span data-not-prose=""><span><a class="group/tag"
                  href="https://exemple.fr/a">Exemple</a></span></span> et
                <span data-not-prose=""><span><a class="group/tag"
                  href="https://exemple.fr/a">Exemple</a></span></span>.
              </p></div>
            </div></div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assert conv.messages[0].sources == ["https://exemple.fr/a"]

    def test_outils_cartes_de_statut(self):
        """TurnStatus -> tools : morph-key, repli <bdi>, bruits exclus."""
        html = """<html><body>
          <div data-testid='transcript-row'>
            <div class="font-claude-response">
              <div class="prose"><div class="standard-markdown"><p>Resultat.</p></div></div>
              <div data-cds="TurnStatus" data-testid="TurnStatus">
                <span data-morph-key="done|Web recherché"></span>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus">
                <span data-morph-key="done|"></span>
                <bdi>Artefact cree artefact.html</bdi>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus">
                <span data-morph-key="done|A réfléchi pendant 26 s"></span>
              </div>
              <div data-cds="TurnStatus" data-testid="TurnStatus">
                <span data-morph-key="done|I'm calculating the rates…"></span>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        msg = conv.messages[0]
        assert msg.tools == ["Web recherché", "Artefact cree artefact.html"]
        assert "Web recherché" not in msg.texte
        assert "Artefact cree" not in msg.texte

    def test_timestamp_barre_actions_frere_du_texte(self):
        """Le <time> est dans la barre d'actions, frere du noeud de contenu."""
        html = """<html><body>
          <div data-testid='transcript-row' data-perf-row='assistant'>
            <div data-testid='assistant-message'>
              <div class="font-claude-response">
                <div class="prose"><div class="standard-markdown"><p>Reponse.</p></div></div>
              </div>
              <div data-cds='MessageActions'>
                <time data-cds='RelativeTime'
                      datetime='2026-09-24T09:37:00.093Z'>il y a 3 jours</time>
              </div>
            </div>
          </div>
        </body></html>"""
        conv = ClaudeParser().parse(html, conversation_id="abc")
        assert conv.messages[0].timestamp == "2026-09-24T09:37:00Z"


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

    def test_reponse_vide_ne_fusionne_pas_deux_requetes(self):
        # un tour dont la reponse est vide (image pure, etat de rendu) ne doit
        # pas coller la requete suivante ni perdre son alternance.
        html = (
            "<html><body>"
            '<div class="conversation-container" id="a">'
            "<user-query><div class='query-text'>Question A</div></user-query>"
            "<model-response><message-content></message-content></model-response>"
            "</div>"
            '<div class="conversation-container" id="b">'
            "<user-query><div class='query-text'>Question B</div></user-query>"
            "<model-response><message-content>Reponse B</message-content></model-response>"
            "</div>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert [m.role for m in conv.messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert "Question A" in conv.messages[0].texte
        assert conv.messages[1].texte == ""
        assert "Question B" in conv.messages[2].texte

    def test_sources_depuis_les_puces_de_citation(self):
        # grounding web (domaine) et fichiers joints (type: nom) ; une puce peut
        # couvrir plusieurs citations.
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Sources</div></user-query>"
            "<model-response><message-content><p>Reponse</p></message-content>"
            "<sources-carousel-inline>"
            "<source-inline-chip><button aria-label=\"Afficher les détails de la "
            "source pour la citation de stirrup.artificialanalysis.ai. Appuyer sur "
            "Entrée pour ouvrir la boîte de dialogue des sources.\">"
            "<span class='source-title'>stirrup.artificialanalysis.ai</span>"
            "</button></source-inline-chip>"
            "<source-inline-chip><button aria-label=\"Afficher les détails de la "
            "source pour les citations de BnF API, BnF API et Data Gouv. Appuyer "
            "sur Entrée pour ouvrir la boîte de dialogue des sources.\">"
            "<span class='source-title'>Sources</span></button></source-inline-chip>"
            "</sources-carousel-inline>"
            "</model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert conv.messages[1].sources == [
            "stirrup.artificialanalysis.ai",
            "BnF API",
            "Data Gouv",
        ]
        # le nom de fichier n'est pas un source
        html_file = (
            "<html><body>"
            "<user-query><div class='query-text'>JSON</div></user-query>"
            "<model-response><message-content>Valeur</message-content>"
            "<source-inline-chip><button aria-label=\"Afficher les détails de la "
            "source pour la citation de JSON: donnees.json. Appuyer sur Entrée "
            "pour ouvrir la boîte de dialogue des sources.\">"
            "<span class='source-title'>JSON</span></button></source-inline-chip>"
            "</model-response></body></html>"
        )
        conv_file = GeminiParser().parse(html_file, conversation_id="c")
        assert conv_file.messages[1].sources == ["JSON: donnees.json"]

    def test_sources_depuis_une_carte_avec_lien(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Lien</div></user-query>"
            "<model-response><message-content>Reponse</message-content>"
            "<sources-list><a href='https://example.com/article'>"
            "<span class='source-title'>Titre</span></a></sources-list>"
            "</model-response></body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert conv.messages[1].sources == ["Titre — https://example.com/article"]

    def test_modele_depuis_le_libelle_de_reponse(self):
        # DOM recent : le modele n'est plus dans le pied mais dans le libelle
        # d'accessibilite de la reponse.
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Q</div></user-query>"
            "<model-response><message-content>R</message-content>"
            "<h6 class='cdk-visually-hidden screen-reader-model-response-label'>"
            "Gemini a dit</h6>"
            "</model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert conv.model == "Gemini"

    def test_timestamp_depuis_time(self):
        html = (
            "<html><body>"
            "<user-query><div class='query-text'>Q</div></user-query>"
            "<model-response><message-content>R</message-content>"
            "<div class='response-footer'>"
            "<time datetime='2026-09-03T14:05:00Z'>3 sept.</time></div>"
            "</model-response>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert conv.messages[1].timestamp == "2026-09-03T14:05:00Z"
        assert conv.messages[0].timestamp is None

    def test_outils_depuis_les_marqueurs_structurels(self):
        html = (
            "<html><body>"
            '<div class="conversation-container" id="a">'
            "<user-query><div class='query-text'>Execute</div></user-query>"
            "<model-response><message-content>"
            "<pre><code class='code-result-container'>15</code></pre>"
            "<div class='attachment-container generated-images'>"
            "<generated-image></generated-image></div>"
            "<svg-code-block></svg-code-block>"
            "</message-content></model-response>"
            "</div>"
            '<div class="conversation-container" id="b">'
            "<user-query><div class='query-text'>Video</div></user-query>"
            "<div class='luminous-video-duration-pill'>0:01</div>"
            "<model-response><message-content>Decrit</message-content></model-response>"
            "</div>"
            "</body></html>"
        )
        conv = GeminiParser().parse(html, conversation_id="c")
        assert conv.messages[1].tools == [
            "Exécution de code",
            "Génération d'images",
            "Canvas",
        ]
        assert conv.messages[3].tools == ["Analyse vidéo"]


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

    def test_ordre_suit_le_dom_final(self):
        # decouverte melangee : `order` (DOM final) impose l'ordre logique du fil
        seen = [
            _turn_block("id-b", "Question B", "Reponse B"),
            _turn_block("id-a", "Question A", "Reponse A"),
        ]
        order = [
            _turn_block("id-a3", "Question A", "Reponse A"),
            _turn_block("id-b3", "Question B", "Reponse B"),
        ]
        conv = GeminiParser().parse(
            merge_turn_fragments(seen, order), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == ["user", "assistant"] * 2
        assert "Question A" in conv.messages[0].texte
        assert "Question B" in conv.messages[2].texte

    def test_clef_ignore_le_libelle_lecteur_ecran(self):
        # la requete est dupliquee par le libelle d'accessibilite : la clef doit
        # rester identique d'un exemplaire virtualise a l'autre (sinon le tour
        # n'est pas dedoublonne).
        def block(turn_id: str, hidden: str) -> str:
            return (
                f'<div class="conversation-container" id="{turn_id}">'
                "<user-query><div class='query-text'>"
                f"<h5 class='cdk-visually-hidden screen-reader-user-query-label'>{hidden}</h5>"
                "<p>Question A</p></div></user-query>"
                "<model-response><message-content>Reponse A</message-content>"
                "</model-response></div>"
            )

        fragments = [
            block("id-1", "Vous avez dit"),
            block("id-2", "Vous avez dit Vous avez dit"),
        ]
        conv = GeminiParser().parse(
            merge_turn_fragments(fragments), conversation_id="c"
        )
        assert len(conv.messages) == 2
        assert conv.messages[0].texte.strip() == "Question A"

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

    def test_sources_cartes_en_bas(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        sources = conv.messages[1].sources
        assert sources == [
            "https://aceee.org/reports/ev-2026",
            "https://eea.europa.eu/report/ev",
        ]

    def test_citations_inline_vers_sources(self):
        html = self._assistant(
            "<p>Le point clé."
            '<span class="citation inline" data-pplx-citation=""'
            ' data-pplx-citation-url="https://exemple.com/a"'
            ' aria-label="Titre A — détail">exemple</span>'
            '<span class="citation inline"'
            ' data-pplx-citation-url="https://exemple.com/b">'
            '<span aria-label="Titre B long et descriptif">exemple</span></span>'
            '<span class="citation inline"'
            ' data-pplx-citation-url="https://exemple.com/a">exemple</span>'
            "</p>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].sources == [
            "Titre A — détail — https://exemple.com/a",
            "Titre B long et descriptif — https://exemple.com/b",
        ]

    def test_citation_inline_hors_du_corps_aussi_prise(self):
        # la citation est dans une carte de source (lien) : URL seule
        html = (
            '<div data-workflow-final-text=""><div data-renderer="lm">'
            "<p>Réponse.</p></div>"
            '<div data-testid="sources">'
            '<a href="https://www.lemonde.fr/a">Le Monde</a></div></div>'
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].sources == ["https://www.lemonde.fr/a"]

    # -- panneau « Sources N » de fil (cartes collectees par le service) ------

    def test_sources_du_panneau_reparties_par_citation_inline(self):
        # l'URL citee inline recoit son titre ; la carte non attribuable reste
        # sur le dernier message cite (choix documente).
        html = self._assistant(
            "<p>Voir.</p>"
            '<span class="citation" '
            'data-pplx-citation-url="https://a.example/x">exemple</span>'
        )
        conv = PerplexityParser().parse(
            html,
            conversation_id="c",
            extra={
                "thread_sources": [
                    {"title": "Titre B", "url": "https://b.example/y"},
                    {"title": "Titre A", "url": "https://a.example/x"},
                ]
            },
        )
        sources = conv.messages[0].sources
        assert sources == [
            "Titre A — https://a.example/x",
            "Titre B — https://b.example/y",
        ]

    def test_sources_du_panneau_attribuees_par_compteur_de_tour(self):
        # pas de citation inline : le tour qui annonce « 2 sources » recoit les
        # deux cartes du panneau.
        html = (
            self._user("<span>Q1</span>")
            + self._assistant("<p>Rien.</p>")
            + self._user("<span>Q2</span>")
            + '<div data-workflow-final-text=""><div data-renderer="lm">'
            '<p>Web.</p></div>'
            '<button><img src="https://www.google.com/s2/favicons?domain=a.example"/>'
            "<span>2 sources</span></button></div>"
        )
        conv = PerplexityParser().parse(
            html,
            conversation_id="c",
            extra={
                "thread_sources": [
                    {"title": "A", "url": "https://a.example/1"},
                    {"title": "B", "url": "https://b.example/2"},
                ]
            },
        )
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        assert conv.messages[1].sources == []
        assert conv.messages[3].sources == [
            "A — https://a.example/1",
            "B — https://b.example/2",
        ]

    def test_sources_du_panneau_sans_attribution_au_dernier_assistant(self):
        html = (
            self._user("<span>Q1</span>")
            + self._assistant("<p>Rien.</p>")
            + self._user("<span>Q2</span>")
            + self._assistant("<p>Toujours rien.</p>")
        )
        conv = PerplexityParser().parse(
            html,
            conversation_id="c",
            extra={
                "thread_sources": [
                    {"title": "", "url": "https://seul.example/z"},
                ]
            },
        )
        assert conv.messages[1].sources == []
        assert conv.messages[3].sources == ["https://seul.example/z"]

    def test_sources_du_panneau_ignorees_si_vides(self):
        html = self._assistant("<p>Réponse.</p>")
        conv = PerplexityParser().parse(
            html, conversation_id="c", extra={"thread_sources": {"count": 0, "sources": []}}
        )
        assert conv.messages[0].sources == []

    def test_horodatage_visible_dans_la_bulle(self):
        html = (
            '<div class="group/partition" data-workflow-entry="0">'
            '<div data-workflow-items="populated"><div class="contents">'
            '<div class="group/user-bubble flex min-w-0 items-center justify-end gap-2">'
            '<div class="pointer-events-none opacity-0">'
            '<span class="select-none whitespace-nowrap leading-none font-sans '
            'text-xs font-normal text-tertiary">25 sept., 19:01</span>'
            '<button aria-label="Copier la requête">Copier</button></div>'
            '<div class="min-w-0 max-w-[600px]"><div data-renderer="lm">'
            "<p>Ma question.</p></div></div>"
            "</div></div></div></div>"
            + self._assistant("<p>Ma réponse.</p>")
        )
        conv = PerplexityParser().parse(
            html, conversation_id="c", extra={"now": "2026-09-30T12:00:00Z"}
        )
        assert conv.messages[0].timestamp == "2026-09-25T19:01:00Z"
        assert conv.started_at == "2026-09-25T19:01:00Z"
        assert "25 sept." not in conv.messages[0].texte
        assert "Copier" not in conv.messages[0].texte

    def test_horodatage_annee_deduite_dans_le_passe(self):
        html = self._user(
            '<div class="opacity-0">'
            '<span class="whitespace-nowrap text-tertiary">31 déc., 23:30</span>'
            "</div><span>Question.</span>"
        )
        conv = PerplexityParser().parse(
            html, conversation_id="c", extra={"now": "2026-01-05T08:00:00Z"}
        )
        assert conv.messages[0].timestamp == "2025-12-31T23:30:00Z"

    def test_horodatage_non_reconnu_ignore(self):
        html = self._user(
            '<div class="opacity-0"><span class="text-tertiary">bientôt</span></div>'
            "<span>Question.</span>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].timestamp is None

    def test_outils_recherche_depuis_etape_workflow(self):
        html = self._assistant(
            '<div class="group/step-header">'
            '<div title="Recherche terminée">Recherche terminée 3s</div></div>'
            "<p>Réponse.</p>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].tools == ["Recherche"]

    def test_started_at_depuis_en_tete(self, fixture_html):
        conv = PerplexityParser().parse(
            fixture_html("perplexity_conversation.html"), conversation_id="c"
        )
        assert conv.started_at == "2026-09-05T08:00:00Z"

    def test_titre_ne_prend_pas_un_h1_de_reponse(self):
        # le HTML accumule ne contient que les messages : le service y recopie
        # le <title> du fil, sinon un `# Titre` d'une reponse deviendrait le nom.
        html = (
            "<html><head><title>Nom du fil</title></head><body>"
            '<div class="group group/user-bubble">question</div>'
            '<div data-workflow-final-text=""><div data-renderer="lm">'
            "<h1>Titre principal</h1></div></div></body></html>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.title == "Nom du fil"

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

    # -- 2026 : pieces jointes en « chips » hors de la bulle ------------------

    @staticmethod
    def _turn_user(chips: str = "", text: str = "Décris cette image.") -> str:
        """Tour user 2026 : bulle + chips de pieces jointes cote a cote."""
        return (
            '<div class="group/partition" data-workflow-entry="0">'
            '<div data-workflow-items="populated">'
            '<div class="contents"><div class="group/user-bubble flex min-w-0 '
            'items-center justify-end gap-2">'
            '<div class="pointer-events-none opacity-0"><span>01:27</span>'
            '<button aria-label="Copier la requête">Copier</button></div>'
            '<div class="min-w-0 max-w-[600px]"><div data-renderer="lm">'
            f"<p>{text}</p></div></div>"
            "</div></div></div>"
            f'<div class="contents">{chips}</div>'
            "</div>"
        )

    _CHIP = (
        '<button data-asset-chip="true" title="{label}" aria-label="{label}">'
        '<span><svg><use xlink:href="#pplx-icon-photo"></use></svg></span>'
        '<span>{label}</span></button>'
    )
    _CHIP_IMG = (
        '<button data-asset-chip="true" title="{label}">'
        '<img alt="{label}" src="{src}"/></button>'
    )

    def test_chip_fichier_hors_bulle_garde_le_nom(self):
        html = self._turn_user(self._CHIP.format(label="notes.txt"))
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "notes.txt" in text
        assert "Décris cette image." in text
        assert "01:27" not in text and "Copier" not in text

    def test_chip_image_sans_miniature_reste_detectable(self):
        # URL S3 expiree : le chip n'a qu'un nom de fichier image
        html = self._turn_user(self._CHIP.format(label="image.jpg"))
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "![image.jpg](image.jpg)" in text

    def test_chip_image_avec_miniature_garde_l_url(self):
        html = self._turn_user(
            self._CHIP_IMG.format(
                label="image.jpg",
                src="https://ppl-ai-file-upload.s3.amazonaws.com/x/image.jpg",
            )
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert (
            "![image.jpg](https://ppl-ai-file-upload.s3.amazonaws.com/x/image.jpg)"
            in text
        )

    def test_images_generees_carrousel_hors_corps_lm(self):
        html = self._assistant(
            "<p>Voici les images.</p>"
            '<div data-testid="image-carousel-row">'
            '<div data-testid="image-carousel-img">'
            '<img alt="Chuck Bass" src="https://cdn.example/x.jpg"/></div>'
            '<div data-testid="image-carousel-img">'
            '<img alt="Blair" src="https://cdn.example/y.jpg"/></div>'
            "</div>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        text = conv.messages[0].texte
        assert "Voici les images." in text
        assert "![Chuck Bass](https://cdn.example/x.jpg)" in text
        assert "![Blair](https://cdn.example/y.jpg)" in text

    def test_reasoning_etape_nommee(self):
        html = (
            '<div data-workflow-final-text=""><div data-workflow-items="populated">'
            '<div class="flex flex-col min-w-0 text-secondary relative">'
            '<div class="group/step-header relative z-10 flex items-center gap-2">'
            '<button aria-expanded="false"><div><div title="Recherche terminée">'
            "Recherche terminée</div>"
            '<div title="Vérification des fichiers disponibles">'
            "Vérification des fichiers disponibles</div></div></button></div>"
            '<div class="relative"><div class="group/step-header">'
            '<div title="Lecture des sources">Lecture des sources</div>'
            "</div></div></div>"
            '<div data-renderer="lm"><p>Réponse.</p></div>'
            "</div></div>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        msg = conv.messages[0]
        assert msg.texte == "Réponse."
        assert "Vérification des fichiers disponibles" in msg.reasoning
        assert "Lecture des sources" in msg.reasoning
        assert "Recherche terminée" not in msg.reasoning

    def test_reasoning_vide_quand_seul_statut(self):
        html = self._assistant(
            '<div class="group/step-header">'
            '<div title="Recherche terminée">Recherche terminée</div></div>'
            "<p>Réponse.</p>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert conv.messages[0].reasoning == ""

    def test_reasoning_tour_de_progression_avant_la_reponse(self):
        # Pro Search : les etapes sont rendues dans un tour dedie, hors `lm`
        html = (
            '<div class="group/partition" data-workflow-entry="1">'
            '<div data-workflow-items="populated">'
            '<div class="group/step-header relative z-10">'
            '<div title="Recherche terminée">Recherche terminée</div>'
            '<div title="Lecture des sources">Lecture des sources</div>'
            "</div></div></div>"
            + self._assistant("<p>Réponse.</p>")
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert [m.role for m in conv.messages] == ["assistant"]
        assert "Lecture des sources" in conv.messages[0].reasoning
        assert "Recherche terminée" not in conv.messages[0].reasoning

    def test_reasoning_tour_de_progression_sans_reponse_au_dernier_assistant(self):
        # etape Pro Search du dernier tour (reponse absente/en cours) : faute de
        # message suivant, elle est rattachee au dernier message assistant.
        html = (
            self._user("<span>Q</span>")
            + self._assistant("<p>R.</p>")
            + '<div class="group/partition" data-workflow-entry="2">'
            '<div data-workflow-items="populated">'
            '<div class="group/step-header">'
            '<div title="Recherche terminée">Recherche terminée</div>'
            '<div title="Vérification des fichiers disponibles">'
            "Vérification des fichiers disponibles</div></div></div></div>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[1].reasoning == "Vérification des fichiers disponibles"

    def test_artefact_carte_hors_corps_lm(self):
        html = (
            '<div data-workflow-final-text="">'
            '<div data-workflow-items="populated">'
            '<div data-renderer="lm"><p>Le fichier YAML a été généré.</p></div>'
            '<div class="flex w-full flex-col divide-y rounded-xl bg-raised">'
            '<div class="group relative w-full p-2 flex items-center">'
            '<button aria-label="notes"></button>'
            '<div class="flex flex-col">'
            '<div class="font-sans font-bold text-sm"><span>notes</span></div>'
            '<div class="flex items-center">'
            '<div class="font-sans text-secondary text-sm">YAML</div></div></div>'
            '<button aria-label="Options de l’artefact"></button>'
            "</div></div></div></div>"
        )
        conv = PerplexityParser().parse(html, conversation_id="c")
        msg = conv.messages[0]
        assert msg.artifacts == ["notes.yaml"]
        assert "Le fichier YAML a été généré." in msg.texte
        assert "Options de l’artefact" not in msg.texte

    def test_artefacts_vides_sans_carte(self):
        conv = PerplexityParser().parse(
            self._assistant("<p>Réponse simple.</p>"), conversation_id="c"
        )
        assert conv.messages[0].artifacts == []

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

    def test_tour_de_progression_accumule_devient_reasoning(self):
        # le service memorise les tours `data-workflow-items` de Pro Search
        # (role « steps ») ; le parser les rattache a la reponse suivante.
        steps = (
            '<div data-workflow-items="populated"><div class="contents">'
            '<div class="group/step-header relative z-10">'
            '<div title="Recherche terminée">Recherche terminée</div>'
            '<div title="Vérification des fichiers disponibles">'
            "Vérification des fichiers disponibles</div></div>"
            "</div></div>"
        )
        items = [
            {"key": "u:1", "role": "user", "html": self._USER.format(text="Q1"), "pos": 100},
            {"key": "s:1", "role": "steps", "html": steps, "pos": 150},
            {"key": "a:1", "role": "assistant", "html": self._ASSIST.format(text="R1"), "pos": 200},
        ]
        conv = PerplexityParser().parse(
            merge_thread_messages(items), conversation_id="c"
        )
        assert [m.role for m in conv.messages] == ["user", "assistant"]
        assert conv.messages[1].texte == "R1"
        assert "Vérification des fichiers disponibles" in conv.messages[1].reasoning
        assert "Recherche terminée" not in conv.messages[1].reasoning


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

    def test_pieces_jointes_audio_video_en_texte(self):
        """Audio/video joints -> lien markdown, donc detectables dans `texte`."""
        payload = {
            "conversation": {"conversationId": "grok-13", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Analyse cet audio et cette vidéo.",
                 "fileAttachmentAssetMetadata": [
                     {"mimeType": "audio/mpeg", "name": "audio.mp3",
                      "key": "users/u1/audio/content"},
                     {"mimeType": "video/mp4", "name": "video.mp4",
                      "key": "users/u1/video/content"},
                 ]},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Voici.", "model": "grok-3"},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-13", extra=payload)
        text = conv.messages[0].texte
        assert "[audio.mp3](https://assets.grok.com/users/u1/audio/content)" in text
        assert "[video.mp4](https://assets.grok.com/users/u1/video/content)" in text

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

    def test_citation_inline_vers_lien_markdown(self):
        """`<grok:render ... citation_card>` -> lien markdown `[n](url)`."""
        card = json.dumps({
            "id": "d1a063",
            "type": "render_inline_citation",
            "cardType": "citation_card",
            "url": "https://www.calendardate.com/todays.htm",
            "kind": 1,
        })
        payload = {
            "conversation": {"conversationId": "grok-10", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Quelle date ? Cite la source."},
                {"responseId": "r2", "sender": "assistant", "model": "grok-3",
                 "message": ("La date actuelle est le 13."
                             '<grok:render card_id="d1a063" card_type="citation_card" '
                             'type="render_inline_citation">'
                             '<argument name="citation_id">1</argument>'
                             "</grok:render>"),
                 "cardAttachmentsJson": [card]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-10", extra=payload)
        text = conv.messages[1].texte
        assert "<grok:render" not in text
        assert "https://www.calendardate.com/todays.htm" in text
        assert text == (
            "La date actuelle est le 13."
            "[1](https://www.calendardate.com/todays.htm)"
        )

    def test_artefacts_canvas_extraits(self):
        """Les cartes `rendered_file_card` alimentent `Message.artifacts`."""
        canvas = json.dumps({
            "id": "c1", "type": "render_file",
            "cardType": "rendered_file_card", "file_name": "canvas.html",
            "mime_type": "application/octet-stream",
            "url": "users/u1/generated/x/canvas.html",
        })
        csv_card = json.dumps({
            "id": "c2", "type": "render_file",
            "cardType": "rendered_file_card", "file_name": "output.csv",
            "mime_type": "text/csv",
            "url": "users/u1/generated/y/output.csv",
        })
        image = json.dumps({
            "id": "c3", "type": "render_generated_image",
            "cardType": "generated_image_card", "prompt": "un renard",
            "image_chunk": {"imageUrl": "users/u1/generated/z/image.jpg"},
        })
        payload = {
            "conversation": {"conversationId": "grok-11", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Crée un artefact canvas et un CSV."},
                {"responseId": "r2", "sender": "assistant",
                 "message": "Voici les fichiers.", "model": "grok-3",
                 "cardAttachmentsJson": [canvas, csv_card, image]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-11", extra=payload)
        # l'image generee n'est pas un artefact (deja en markdown)
        assert conv.messages[1].artifacts == ["canvas.html", "output.csv"]

    def test_reasonnement_non_expose_par_l_api(self):
        """`steps`/`thinkingStartTime` sont des libelles UI, pas du raisonnement.

        Le payload Grok ne contient aucun texte de chaine de pensee ; on ne
        remplit donc pas `Message.reasoning` (limite documentee).
        """
        payload = {
            "conversation": {"conversationId": "grok-12", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Calcule."},
                {"responseId": "r2", "sender": "assistant", "message": "15",
                 "model": "grok-3",
                 "thinkingStartTime": "2026-09-25T16:51:53.000Z",
                 "thinkingEndTime": "2026-09-25T16:52:02.000Z",
                 "uiLayout": {"reasoningUiLayout": "UNIFIED"},
                 "steps": [
                     {"text": ["Thinking about your request"],
                      "tags": ["header"]},
                     {"text": ["<xai:tool_usage_card>Bash</xai:tool_usage_card>"],
                      "tags": ["tool_usage_card"]},
                 ]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-12", extra=payload)
        assert conv.messages[1].reasoning == ""

    def test_outils_extraits_des_steps(self):
        """`tool_usage_card` -> `Message.tools` en libelles lisibles."""
        payload = {
            "conversation": {"conversationId": "grok-tools", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human",
                 "message": "Cherche et lis un fichier."},
                {"responseId": "r2", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "steps": [
                     {"text": ["Thinking about your request"],
                      "tags": ["header"]},
                     {"text": ["<xai:tool_usage_card>\n"
                               "<xai:tool_name>web_search</xai:tool_name>\n"
                               "</xai:tool_usage_card>"],
                      "tags": ["tool_usage_card"]},
                     {"text": ["<xai:tool_usage_card>\n"
                               "<xai:tool_name>WebSearch</xai:tool_name>\n"
                               "</xai:tool_usage_card>"],
                      "tags": ["tool_usage_card"]},
                     {"text": ["<xai:tool_usage_card>\n"
                               "<xai:tool_name>ReadFile</xai:tool_name>\n"
                               "</xai:tool_usage_card>"],
                      "tags": ["tool_usage_card"]},
                     {"text": [""], "tags": ["raw_function_result"]},
                 ]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-tools", extra=payload)
        assert conv.messages[1].tools == ["Recherche web", "Lecture de fichier"]

    def test_outils_deduits_sans_carte(self):
        """Sans `tool_usage_card`, les resultats exposes trahissent l'outil."""
        payload = {
            "conversation": {"conversationId": "grok-tools2", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Cherche."},
                {"responseId": "r2", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "webSearchResults": [
                     {"url": "https://a.example/x", "title": "A"}]},
                {"responseId": "r3", "sender": "human", "message": "Sur X ?"},
                {"responseId": "r4", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "xposts": [{"username": "bob", "postId": "42"}]},
                {"responseId": "r5", "sender": "human", "message": "Une image."},
                {"responseId": "r6", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "steps": [{"tags": ["tool_usage_card"],
                            "text": ["<xai:tool_usage_card><xai:tool_name>"
                                     "InitTerminalSession</xai:tool_name>"
                                     "</xai:tool_usage_card>"]}],
                 "cardAttachmentsJson": [json.dumps({
                     "id": "gen1", "type": "render_generated_image",
                     "cardType": "generated_image_card", "prompt": "un renard"})]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-tools2", extra=payload)
        assert conv.messages[1].tools == ["Recherche web"]
        assert conv.messages[3].tools == ["Recherche X"]
        assert conv.messages[5].tools == [
            "Initialisation du terminal", "Génération d'image",
        ]

    def test_sources_citees(self):
        """`citedWebSearchResults` + cartes citation -> `Message.sources`."""
        card = json.dumps({
            "id": "d1a063", "type": "render_inline_citation",
            "cardType": "citation_card",
            "url": "https://www.calendardate.com/todays.htm",
        })
        payload = {
            "conversation": {"conversationId": "grok-src", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Source ?"},
                {"responseId": "r2", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "citedWebSearchResults": [
                     {"url": "https://fr.wikipedia.org/wiki/Napoleon", "title": "N"}],
                 "cardAttachmentsJson": [card]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-src", extra=payload)
        assert conv.messages[1].sources == [
            "https://fr.wikipedia.org/wiki/Napoleon",
            "https://www.calendardate.com/todays.htm",
        ]

    def test_sources_repli_resultats_et_xposts(self):
        """Sans citation formelle, les resultats exposes alimentent `sources`."""
        payload = {
            "conversation": {"conversationId": "grok-src2", "title": "Etalon"},
            "responses": [
                {"responseId": "r1", "sender": "human", "message": "Cherche."},
                {"responseId": "r2", "sender": "assistant", "model": "grok-3",
                 "message": "Voilà.",
                 "webSearchResults": [
                     {"url": "https://a.example/x"},
                     {"url": "https://b.example/y"},
                     {"url": "https://a.example/x"}],
                 "xposts": [{"username": "bob", "postId": "42"}]},
            ],
        }
        conv = GrokParser().parse("", conversation_id="grok-src2", extra=payload)
        assert conv.messages[1].sources == [
            "https://a.example/x",
            "https://b.example/y",
            "https://x.com/bob/status/42",
        ]

    def test_sources_absentes_restent_vides(self):
        """Un tour sans recherche ne fabrique aucune source."""
        conv = GrokParser().parse("", conversation_id="grok-1", extra=self.PAYLOAD)
        assert conv.messages[1].sources == []
        assert conv.messages[1].tools == []


class TestGrokHtmlRender:
    """Le HTML d'export (genere) reflete aussi raisonnement et artefacts."""

    def test_artefacts_et_raisonnement_rendus(self):
        from src.schema import Message
        from src.utils.html_render import conversation_to_html

        msg = Message(role="assistant", texte="Voici.",
                      reasoning="Pensee interne", artifacts=["canvas.html"])
        conv = Conversation(platform="grok", conversation_id="g1", title="T",
                            messages=[msg])
        html = conversation_to_html(conv)
        assert "Raisonnement" in html and "Pensee interne" in html
        assert "Artefacts" in html and "canvas.html" in html

    def test_outils_et_sources_rendus(self):
        from src.schema import Message
        from src.utils.html_render import conversation_to_html

        msg = Message(role="assistant", texte="Voici.",
                      tools=["Recherche web"],
                      sources=["https://a.example/x"])
        conv = Conversation(platform="grok", conversation_id="g2", title="T",
                            messages=[msg])
        html = conversation_to_html(conv)
        assert "Outils" in html and "Recherche web" in html
        assert 'href="https://a.example/x"' in html and "Sources" in html


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

    IMAGE_HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Génère une image.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div data-message-part-type="answer">
          <div class="markdown-container-style">
            <img src="/cdn-cgi/image/width=800/https://blob/chat-images/fox.jpg" alt="Generated image"/>
          </div>
        </div>
      </div>
      <div data-message-author-role="user" data-message-id="u-2">
        <div class="select-text"><span>Suite.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-2">
        <div data-message-part-type="answer">
          <div class="markdown-container-style"><p>Fin.</p></div>
        </div>
      </div>
    </main></body></html>
    """

    def test_image_seule_conserve_l_alternance_et_absolutise(self):
        """Un assistant « image seule » ne doit pas fusionner deux tours user."""
        conv = MistralParser().parse(self.IMAGE_HTML, conversation_id="chat-1")
        assert [m.role for m in conv.messages] == [
            "user", "assistant", "user", "assistant",
        ]
        image = conv.messages[1].texte
        assert "![Generated image](https://chat.mistral.ai/cdn-cgi/image/width=800/" in image

    REASONING_HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Explique.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div class="flex w-full flex-col gap-1 break-words">
          <button><span class="truncate">Réfléchi pendant 1s</span></button>
          <div data-message-part-type="reasoning">
            <div class="markdown-container-style"><p>Je pèse le pour et le contre.</p></div>
          </div>
          <div data-message-part-type="answer">
            <div class="markdown-container-style"><p>Voici la réponse.</p></div>
          </div>
        </div>
      </div>
    </main></body></html>
    """

    def test_reasoning_extrait_hors_de_la_reponse(self):
        """Le bloc « Réfléchi » va dans `reasoning`, pas dans `texte`."""
        conv = MistralParser().parse(self.REASONING_HTML, conversation_id="work-1")
        assistant = conv.messages[1]
        assert assistant.reasoning == "Je pèse le pour et le contre."
        assert "pèse le pour et le contre" not in assistant.texte
        assert "Réfléchi pendant 1s" not in assistant.reasoning
        assert assistant.texte.strip() == "Voici la réponse."

    CANVAS_HTML = """
    <html><head><title>Test Mistral</title></head><body><main>
      <div data-message-author-role="user" data-message-id="u-1">
        <div class="select-text"><span>Crée un canvas.</span></div>
      </div>
      <div data-message-author-role="assistant" data-message-id="a-1">
        <div class="flex w-full flex-col gap-1 break-words">
          <div class="border-default bg-card rounded-xl border"><div>
            <div class="flex items-center gap-2 rounded-t-xl border-b px-4 py-2 bg-card">
              <button><svg></svg>
                <span class="transition-colors truncate">Rapport</span>
              </button>
            </div>
            <div class="border-default border-b">
              <div data-review-comment-boundary="canvas"
                   data-review-comment-canva-id="c-1"
                   data-review-comment-canva-type="text/markdown"
                   data-review-comment-canva-version="0">
                <div class="canva-editor inline-canva-editor">
                  <h1>Rapport de test</h1><p>Ceci est un rapport.</p>
                </div>
              </div>
              <div data-review-comment-boundary="canvas"
                   data-review-comment-canva-id="c-1"
                   data-review-comment-canva-type="text/markdown"
                   data-review-comment-canva-version="1"></div>
            </div>
          </div></div>
          <div data-message-part-type="answer">
            <div class="markdown-container-style"><p>Canvas rapport créé.</p></div>
          </div>
        </div>
      </div>
    </main></body></html>
    """

    def test_canvas_extrait_comme_artefact_deduplique(self):
        """Le canvas -> `artifacts` (« titre (type) »), versions dédupliquées."""
        conv = MistralParser().parse(self.CANVAS_HTML, conversation_id="chat-1")
        assistant = conv.messages[1]
        assert assistant.artifacts == ["Rapport (text/markdown)"]
        assert assistant.texte.strip() == "Canvas rapport créé."

    # -- horodatages, modele, sources, outils ---------------------------------

    @staticmethod
    def _rsc_page(body: str, *payloads: str) -> str:
        """HTML de test avec un payload RSC Next.js reconstitue."""
        scripts = "".join(
            "<script>self.__next_f.push([1,%s])</script>" % json.dumps(payload)
            for payload in payloads
        )
        return (
            "<html><head><title>Test Mistral</title></head><body><main>"
            f"{body}{scripts}</main></body></html>"
        )

    def test_horodatage_visible_et_modele(self):
        """Le libelle visible -> timestamp ISO, le RSC -> `Conversation.model`."""
        body = """
          <div data-message-author-role="user" data-message-id="u-1">
            <div class="select-text"><span>Bonjour.</span></div>
            <div class="text-hint text-sm">24 sept., 14:27</div>
          </div>
          <div data-message-author-role="assistant" data-message-id="a-1">
            <div data-message-part-type="answer">
              <div class="markdown-container-style"><p>Salut.</p></div>
            </div>
            <div class="text-hint text-sm">24 sept., 14:28</div>
          </div>
        """
        payload = json.dumps(
            {
                "role": "user",
                "content": "Bonjour.",
                "id": "u-1",
                "createdAt": "$D2026-09-24T12:27:00.000Z",
            }
        ) + json.dumps(
            {
                "role": "assistant",
                "content": "Salut.",
                "id": "a-1",
                "createdAt": "$D2026-09-24T12:28:00.000Z",
            }
        )
        meta = json.dumps(
            {
                "chat": {
                    "id": "d-1",
                    "productType": "work",
                    "modelConfig": {"model_alias": "glm-5-latest-short"},
                }
            }
        ) + (
            '"vibeWorkModelConfigs":{"value":[{"model_alias":"glm-5-latest-short",'
            '"model_display_name":"GLM 5.3"}]}'
        )
        page_html = self._rsc_page(body, payload, meta)
        conv = MistralParser().parse(page_html, conversation_id="d-1")
        assert conv.model == "GLM 5.3"
        assert conv.messages[0].timestamp == "2026-09-24T14:27:00Z"
        assert conv.messages[1].timestamp == "2026-09-24T14:28:00Z"
        assert conv.started_at == "2026-09-24T14:27:00Z"
        assert conv.last_message_at == "2026-09-24T14:28:00Z"

    def test_horodatage_repli_rsc_sans_libelle(self):
        body = """
          <div data-message-author-role="user" data-message-id="u-1">
            <div class="select-text"><span>Bonjour.</span></div>
          </div>
        """
        payload = json.dumps(
            {
                "role": "user",
                "content": "Bonjour.",
                "id": "u-1",
                "createdAt": "$D2026-09-24T12:27:39.072Z",
            }
        )
        conv = MistralParser().parse(
            self._rsc_page(body, payload), conversation_id="d-1"
        )
        assert conv.messages[0].timestamp == "2026-09-24T12:27:39Z"

    def test_sources_et_outils_depuis_payload_rsc(self):
        """`references` + resultats `web_search` -> sources ; tool_calls -> tools."""
        body = """
          <div data-message-author-role="user" data-message-id="u-1">
            <div class="select-text"><span>Cherche.</span></div>
          </div>
          <div data-message-author-role="assistant" data-message-id="a-1">
            <div data-message-part-type="answer">
              <div class="markdown-container-style"><p>Réponse.</p></div>
            </div>
          </div>
        """
        payload = json.dumps(
            {
                "role": "assistant",
                "content": "Réponse.",
                "id": "a-1",
                "createdAt": "$D2026-09-24T12:28:00.000Z",
                "references": [{"url": "https://ex.com/a", "title": "Article A"}],
                "contentChunks": [
                    {
                        "type": "tool_call",
                        "name": "web_search",
                        "publicResult": {
                            "results": [
                                {"url": "https://ex.com/b", "title": "Article B"}
                            ]
                        },
                    },
                    {"type": "tool_call", "name": "code_interpreter"},
                    {"type": "canva", "id": "c-1"},
                ],
            }
        )
        conv = MistralParser().parse(
            self._rsc_page(body, payload), conversation_id="d-1"
        )
        assistant = conv.messages[1]
        assert assistant.sources == [
            "Article A — https://ex.com/a",
            "Article B — https://ex.com/b",
        ]
        assert assistant.tools == ["Recherche web", "Exécution de code", "Canvas"]

    def test_sources_repli_cartes_dom(self):
        """Sans payload RSC : les cartes « N sources » alimentent `sources`."""
        body = """
          <div data-message-author-role="user" data-message-id="u-1">
            <div class="select-text"><span>Cherche.</span></div>
          </div>
          <div data-message-author-role="assistant" data-message-id="a-1">
            <div data-message-part-type="answer">
              <div class="markdown-container-style"><p>Réponse.</p></div>
            </div>
            <div class="bg-card border-default overflow-hidden rounded-xl border-[0.5px]">
              <div class="bg-card-subtle flex h-10 items-center gap-2 px-4 py-2">
                <span class="text-muted text-sm shrink-0 leading-5 whitespace-nowrap">2 sources</span>
              </div>
              <div><a href="https://ex.com/a">Article A</a></div>
              <div><a href="https://ex.com/b">Article B</a></div>
            </div>
          </div>
        """
        conv = MistralParser().parse(
            f"<html><head><title>Test Mistral</title></head><body><main>{body}"
            "</main></body></html>",
            conversation_id="d-1",
        )
        assert conv.messages[1].sources == [
            "Article A — https://ex.com/a",
            "Article B — https://ex.com/b",
        ]

    def test_merge_message_fragments_ordre_et_dedup(self):
        """Fenetres virtualisees remontees : ordre reconstitue, sans doublon."""
        def turn(key: str, role: str) -> str:
            return f'<div data-message-author-role="{role}" data-message-id="{key}">x</div>'

        rows = {key: turn(key, "user") for key in ("m1", "m2", "m3", "m4")}
        # deux fenetres : [m2,m3] puis [m1,m2] (remontee), decouverte m2,m3,m1
        edges = [["m2", "m3"], ["m1", "m2"]]
        seq = ["m2", "m3", "m1"]
        html = merge_message_fragments(rows, edges, seq)
        assert html.index('data-message-id="m1"') < html.index('data-message-id="m2"')
        assert html.index('data-message-id="m2"') < html.index('data-message-id="m3"')
        assert html.count('data-message-id="m1"') == 1

    def test_merge_message_fragments_vide(self):
        assert merge_message_fragments({}, [], []) == ""


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
