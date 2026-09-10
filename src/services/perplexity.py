"""Scraper Perplexity (perplexity.ai).

Decouverte : l'API GraphQL de la library (persisted query) liste TOUT
(la liste DOM est virtualisee et la sidebar ne montre que le recent).
Fallback : collecteur DOM incrementiel si l'API change.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..parsers.perplexity import PerplexityParser
from ..schema import ConversationRef
from ..utils.logging import get_logger, log_fields
from .base import BaseService

log = get_logger("services")


class PerplexityService(BaseService):
    name = "perplexity"
    home_url = "https://www.perplexity.ai/"
    #: la home ne montre que le recents ; la library liste tout
    discover_url = "https://www.perplexity.ai/library"

    #: endpoint + hash de la persisted query LibraryRecentThreadsPaginationQuery
    GRAPHQL_ENDPOINT = "/rest/perplexity_ask/graphql"
    GRAPHQL_HASH = "e207cce86b2c9b67fca3ea7d8450d675ec86c0c429b38e42e88f3b027e7c8729"

    sidebar_scroll_selectors = (
        "aside nav",
        "[data-testid='history']",
        "aside div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = (
        "aside a[href*='/search/']",
        "a[href*='/search/']",
    )
    login_url_parts = ("perplexity.ai/.auth", "vercel", "sign-in", "login")
    login_selectors = (
        "button:has-text('Sign in')",
        "a[href*='login']",
        "input[type='password']",
    )

    def __init__(self, session, config=None):
        super().__init__(session, config)
        self._thread_models: Dict[str, str] = {}

    def build_parser(self) -> PerplexityParser:
        return PerplexityParser()

    def after_sidebar_open(self) -> None:
        """Ferme la banniere de consentement si elle revient."""
        for sel in (
            "button:has-text('Tout autoriser')",
            "button:has-text('Uniquement nécessaires')",
            "[data-testid='consent-dialog'] button",
        ):
            if self.session.click_if_present(sel, timeout_ms=1200):
                self.session.wait_ms(600)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://www.perplexity.ai/search/{ref.id}"

    # -- decouverte via API GraphQL ---------------------------------------------

    def discover_via_api(self) -> Optional[List[ConversationRef]]:
        """Liste complete des threads via la pagination GraphQL de la library.

        Retourne None si l'API ne repond pas comme attendu (fallback DOM).
        Pre-requis : la page de decouverte est deja ouverte (par _list_once).
        """
        refs: Dict[str, ConversationRef] = {}
        cursor: Optional[str] = None
        try:
            for page in range(80):  # 25/page : ~14 pages pour 350 threads
                data = self._graphql_page(cursor)
                if data is None:
                    return None
                try:
                    threads = data["data"]["viewer"]["recentGroup"]["threads"]
                except (KeyError, TypeError):
                    log_fields(log, 30, f"{self.name}: structure GraphQL inattendue")
                    return None
                edges = threads.get("edges") or []
                for edge in edges:
                    node = edge.get("node") or {}
                    cid = node.get("entryId") or node.get("slug")
                    if not cid:
                        continue
                    refs[cid] = ConversationRef(
                        service=self.name,
                        id=cid,
                        url=f"https://www.perplexity.ai/search/{cid}",
                        title=(node.get("name") or None),
                    )
                    display = node.get("displayModel") or {}
                    model_id = display.get("modelID") or node.get("modelID")
                    if model_id:
                        self._thread_models[cid] = str(model_id)
                info = threads.get("pageInfo") or {}
                log_fields(
                    log, 20, f"{self.name}: page API {page + 1}",
                    extra={"threads": len(edges), "cumul": len(refs)},
                )
                if not edges or not info.get("hasNextPage"):
                    break
                cursor = info.get("endCursor") or (edges[-1].get("cursor"))
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001
            log_fields(log, 30, f"{self.name}: decouverte API echouee ({exc})")
            return None
        return list(refs.values()) if refs else None

    def _graphql_page(self, cursor: Optional[str]):
        payload = {
            "operationName": "LibraryRecentThreadsPaginationQuery",
            "variables": {
                "count": 25, "cursor": cursor, "includeSearchPreview": False,
                "includeTemporary": None, "searchTerm": None, "sortOrder": "NEWEST",
                "sources": None, "statuses": None, "threadTypes": None,
            },
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.GRAPHQL_HASH}},
        }
        js = (
            "return fetch(" + json.dumps(self.GRAPHQL_ENDPOINT) + ", "
            "{method: 'POST', headers: {'content-type': 'application/json'}, "
            "body: " + json.dumps(json.dumps(payload)) + "}).then(r => r.text())"
        )
        out = self.session.eval_body(js)
        if not out:
            log_fields(log, 30, f"{self.name}: reponse GraphQL vide")
            return None
        try:
            return json.loads(out)
        except (TypeError, ValueError) as exc:
            log_fields(log, 30, f"{self.name}: GraphQL non-JSON ({exc}): {str(out)[:120]}")
            return None

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        model = self._thread_models.get(ref.id)
        if model:
            extra["model"] = model
        return extra
