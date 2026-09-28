"""Scraper Gemini / Google (gemini.google.com) via Playwright.

Gemini lazy-loade l'historique complet derriere un bouton "Show all" dans la
sidebar : on le clique si present avant de scroller.
"""

from __future__ import annotations

import json
from typing import List, Tuple

from ..parsers.gemini import TURN_SELECTOR, GeminiParser, merge_turn_fragments
from ..schema import ConversationRef
from .base import BaseService, ScrapedPage

#: remise a zero de l'accumulateur JS des tours de conversation
_RESET_TURNS_JS = (
    "window.__aicvTurns = Object.create(null);"
    "window.__aicvOrder = [];"
    "return 0;"
)
#: memorise les tours visibles puis remonte le fil d'environ un ecran a chaque
#: passage. Gemini virtualise l'historique : sauter directement en haut
#: (`scrollTop = 0`) peut ne jamais rendre les fenetres intermediaires. On
#: defile donc de proche en proche, en memorisant chaque tour des qu'il apparait.
#: L'identite d'un tour est la requete utilisateur (stable d'un rendu a l'autre)
#: et non l'`id` du conteneur (regenere a chaque re-rendu virtualise).
#: Le DOM final peut manquer la reponse d'un tour encore en rendu : on ecrase
#: alors l'exemplaire memorise des que la reponse apparait, et on renvoie une
#: signature (tours repondus) pour ne pas conclure a la stabilite trop tot.
_COLLECT_TURNS_JS = """
return (function(){
  const nodes = Array.from(document.querySelectorAll(__SELECTOR__));
  const fresh = [];
  const answered = nodes.map(function(n){
    return n.querySelector('model-response message-content') ? '1' : '0';
  });
  for (const n of nodes) {
    const user = n.querySelector('user-query');
    const label = user ? (user.innerText || user.textContent || '') : '';
    const norm = label.replace(/\\s+/g, ' ').trim().slice(0, 200);
    const key = norm ? ('u\u0000' + norm) : ('id\u0000' + (n.getAttribute('id') || ''));
    const html = n.outerHTML;
    const hasAnswer = n.querySelector('model-response message-content') ? true : false;
    const previous = window.__aicvTurns[key];
    if (previous === undefined) {
      window.__aicvTurns[key] = html;
      window.__aicvOrder.push(key);
      fresh.push(key);
    } else if (hasAnswer && previous.indexOf('<model-response') === -1) {
      window.__aicvTurns[key] = html;  // version complete (reponse rendue)
    }
  }
  // Gemini scrolle dans <infinite-scroller> (pas la fenetre) : on remonte le
  // conteneur scrollable par petits pas pour declencher le lazy-load.
  let scrollHost = nodes.length ? nodes[0].parentElement : null;
  while (scrollHost) {
    if (scrollHost.scrollHeight > scrollHost.clientHeight + 5) break;
    scrollHost = scrollHost.parentElement;
  }
  let atTop = true;
  if (scrollHost) {
    const step = Math.max(120, Math.round(scrollHost.clientHeight * 0.8));
    scrollHost.scrollTop = Math.max(0, scrollHost.scrollTop - step);
    atTop = scrollHost.scrollTop <= 0;
  }
  return [fresh.length, window.__aicvOrder.length, nodes.length, answered.join(''), atTop ? 1 : 0];
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))
#: amene le fil en bas (certains fils s'ouvrent sur les premiers tours)
_SCROLL_BOTTOM_JS = """
return (function(){
  const nodes = document.querySelectorAll(__SELECTOR__);
  if (nodes.length) nodes[nodes.length - 1].scrollIntoView({block: 'end', inline: 'nearest'});
  return nodes.length;
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))
#: ordre final du DOM (le virtualiseur conserve les tours charges) : sert
#: d'ordre de reference du fil, independamment de l'ordre de decouverte.
_FINAL_TURNS_JS = """
return (function(){
  const nodes = Array.from(document.querySelectorAll(__SELECTOR__));
  return {
    order: nodes.map(function(n){ return n.outerHTML; }),
    seen: window.__aicvOrder.map(function(k){ return window.__aicvTurns[k]; })
  };
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))


class GeminiService(BaseService):
    name = "gemini"
    home_url = "https://gemini.google.com/app"

    sidebar_scroll_selectors = (
        "conversation-history",
        "mat-nav-list",
        "aside mat-nav-list",
        "aside",
    )
    sidebar_ready_selectors = (
        "conversation-history a[href*='/app/']",
        "a[href*='/app/']",
    )
    #: sidebar demarree repliee : bouton "Ouvrir la barre laterale" / "Open sidebar"
    sidebar_open_selectors = (
        "button[aria-label*='barre latérale' i]",
        "button[aria-label*='sidebar' i]",
    )
    #: ouvre la liste complete des conversations (sinon seul "Recent" est visible)
    expand_selectors = (
        "[data-test-id='history-show-all']",
        "button:has-text('Show all')",
        "button:has-text('Show more')",
        "button:has-text('Tout afficher')",
        "button:has-text('Tout afficher plus')",
    )
    login_url_parts = ("accounts.google.com", "service.login", "signin")
    # NB: pas de selecteur sur a[href*='accounts.google.com'] : quand on est
    # connecte, le menu du compte (SignOutOptions) matche et fait un faux
    # positif. La redirection vers accounts.google.com suffit (URL).
    login_selectors = (
        "button:has-text('Sign in')",
        "input[type='password']",
    )

    def build_parser(self) -> GeminiParser:
        return GeminiParser()

    def after_sidebar_open(self) -> None:
        """Ouvre la sidebar (elle peut demarrer repliee) puis la liste complete."""
        for sel in self.sidebar_open_selectors:
            if self.session.click_if_present(sel, timeout_ms=1500):
                self.session.wait_ms(2500)
                break
        for sel in self.expand_selectors:
            if self.session.click_if_present(sel):
                self.session.wait_ms(1200)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://gemini.google.com/app/{ref.id}"

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge le fil complet en remontant, puis remplace le HTML.

        Le pipeline commun scrolle vers le bas (le fil est deja en bas) : Gemini
        ne charge les anciens tours qu'en remontant le conteneur de conversation.
        On collecte donc les tours au fil du scroll vers le haut, on fusionne
        les exemplaires virtualises, et on parse l'ensemble accumule.
        """
        page = super().scrape_conversation(ref)
        seen, order = self._collect_conversation_turns()
        if seen or order:
            html = merge_turn_fragments(order, seen)
            if html:
                page.html = html
        return page

    def _collect_conversation_turns(self) -> Tuple[List[str], List[str]]:
        """Remonte le fil par increments en memorisant les tours.

        Retourne ``(seen, order)`` : l'accumulateur des tours decouverts et
        l'ordre logique du DOM final (reference pour la fusion).
        """
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = int(scroll_cfg.get("pause_ms", 700))
        max_rounds = max(20, int(scroll_cfg.get("max_rounds", 60)))
        stable_rounds = 3

        self.session.eval_body(_RESET_TURNS_JS)
        # s'assure d'abord d'avoir le bas du fil (le fil peut s'ouvrir en haut)
        for _ in range(3):
            self.session.eval_body(_SCROLL_BOTTOM_JS)
            self.session.wait_ms(pause_ms)
        last_total = -1
        last_signature = None
        stable = 0
        for _ in range(max_rounds):
            result = self.session.eval_body(_COLLECT_TURNS_JS)
            self.session.wait_ms(pause_ms)
            total = int(result[1]) if result else 0
            signature = str(result[3]) if result and len(result) > 3 else ""
            at_top = bool(result[4]) if result and len(result) > 4 else False
            # stabilite = en haut du fil ET plus de tours charges/repondus
            if at_top and total == last_total and signature == last_signature:
                stable += 1
            else:
                stable = 0
            last_total = total
            last_signature = signature
            if at_top and stable >= stable_rounds:
                break

        data = self.session.eval_body(_FINAL_TURNS_JS) or {}
        # `seen` = accumulateur (versions completes, ordre de decouverte) ;
        # `order` = DOM final (ordre logique du fil). La fusion prend `order`
        # comme reference d'ordre et complete avec `seen` les tours absents.
        seen = [f for f in data.get("seen") or [] if f]
        order = [f for f in data.get("order") or [] if f]
        return seen, order
