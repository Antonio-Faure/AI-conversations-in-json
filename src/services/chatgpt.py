"""Scraper ChatGPT / OpenAI (chatgpt.com) via Playwright.

Particularites :
  - sidebar scrollable infinie (nav[aria-label="Chat history"])
  - timestamps absents du DOM : recuperes depuis les props React
    (`memoizedProps.message.create_time`) via page.evaluate
  - modele par message : attribut data-message-model-slug (+ props React)
"""

from __future__ import annotations

from typing import Any, Dict

from ..parsers.chatgpt import ChatGPTParser
from ..schema import ConversationRef
from .base import BaseService

#: JS de collecte des donnees internes React par message.
#: create_time est un epoch (secondes) ou {t: secondes, s: nanos} selon les builds.
EXTRACT_REACT_META_JS = """
() => {
  const out = { messages: {}, title: null };
  const fiberKey = (el) =>
    Object.keys(el).find((k) => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
  const normTime = (ct) => {
    if (ct == null) return null;
    if (typeof ct === 'number') return ct > 1e12 ? ct / 1000 : ct;
    if (typeof ct === 'object' && typeof ct.t === 'number')
      return ct.t + (ct.s || 0) / 1e9 + (ct.ns || 0) / 1e9;
    return null;
  };
  const nodes = document.querySelectorAll('[data-message-id]');
  nodes.forEach((el) => {
    const id = el.getAttribute('data-message-id');
    const entry = {
      time: null,
      model: el.getAttribute('data-message-model-slug') || null,
      role: el.getAttribute('data-message-author-role') || null,
    };
    try {
      const key = fiberKey(el);
      let fiber = key ? el[key] : null;
      for (let hops = 0; fiber && hops < 40; hops++) {
        const props = fiber.memoizedProps || {};
        const msg = props.message || (props.data && props.data.message) || props.node;
        if (msg && msg.author && msg.author.role) {
          const t = normTime(msg.create_time || msg.timestamp || msg.created_at);
          if (t) entry.time = t;
          const mm = (msg.metadata || {}).model;
          if (mm && msg.author.role === 'assistant') entry.model = mm;
          entry.role = msg.author.role;
          break;
        }
        fiber = fiber.return;
      }
    } catch (e) { /* DOM/React interne different : on garde les attributs data-* */ }
    out.messages[id] = entry;
  });
  const h = document.querySelector("[data-testid='history-title'], header h1");
  if (h) out.title = (h.textContent || '').trim() || null;
  return out;
}
"""


class ChatGPTService(BaseService):
    name = "chatgpt"
    home_url = "https://chatgpt.com/"

    sidebar_scroll_selectors = (
        "nav[aria-label='Chat history']",
        "aside nav",
        ".nav-scrollable-container",
        "aside",
    )
    sidebar_ready_selectors = (
        "nav[aria-label='Chat history'] a[href*='/c/']",
        "a[href*='/c/']",
    )
    login_url_parts = ("chatgpt.com/auth", "openai.com/auth", "signin", "login")
    login_selectors = (
        "a[data-testid='login-button']",
        "a:has-text('Log in')",
        "a[href*='/auth/login']",
        "button:has-text('Log in')",
        "input[type='password']",
    )
    #: ecran d'accueil vide ("Ready when you are.") : conversation non chargeable
    empty_chat_selectors = ("text=/ready when you are/i",)

    def build_parser(self) -> ChatGPTParser:
        return ChatGPTParser()

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        data = self.session.evaluate(EXTRACT_REACT_META_JS) or {}
        extra: Dict[str, Any] = {"messages": data.get("messages") or {}}
        if data.get("title"):
            extra["title_hint"] = data["title"]
        if ref.title:
            extra.setdefault("title_hint", ref.title)
        return extra
