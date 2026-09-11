"""Rendu HTML autonome d'une conversation (source de verite : donnees API).

Pour les plateformes dont la page est rendue cote client (Grok), on reconstruit
un HTML complet et lisible a partir du JSON standardise.
"""

from __future__ import annotations

from html import escape

from ..schema import Conversation

_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;
margin:0 auto;padding:24px;line-height:1.5;color:#1a1a1a;background:#fff}
h1{font-size:1.4rem;margin:0 0 4px}
.meta{color:#666;font-size:.85rem;margin-bottom:24px}
.msg{border:1px solid #e5e5e5;border-radius:10px;padding:12px 16px;margin:16px 0}
.msg.user{background:#f6f8ff;border-color:#dbe4ff}
.msg.assistant{background:#fafafa}
.role{font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em}
.ts{color:#999;font-size:.75rem;margin-left:8px}
pre.texte{white-space:pre-wrap;word-wrap:break-word;margin:8px 0 0;font:inherit}
pre.code{background:#0d1117;color:#e6edf3;padding:12px;border-radius:8px;overflow:auto}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9rem}
"""


def conversation_to_html(conv: Conversation) -> str:
    parts = [
        "<!DOCTYPE html>",
        '<html lang="fr"><head><meta charset="utf-8">',
        f"<title>{escape(conv.title or conv.conversation_id)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{escape(conv.title or conv.conversation_id)}</h1>",
        '<div class="meta">',
        f"platform: {escape(conv.platform)} | conversation_id: {escape(conv.conversation_id)}",
    ]
    if conv.model:
        parts.append(f" | model: {escape(str(conv.model))}")
    if conv.started_at:
        parts.append(f" | started_at: {escape(conv.started_at)}")
    if conv.last_message_at:
        parts.append(f" | last_message_at: {escape(conv.last_message_at)}")
    parts.append("</div>")

    for msg in conv.messages:
        role = escape(msg.role)
        parts.append(f'<div class="msg {role}">')
        ts = f'<span class="ts">{escape(msg.timestamp)}</span>' if msg.timestamp else ""
        model = f' <span class="ts">({escape(str(msg.model))})</span>' if msg.model else ""
        parts.append(f'<div class="role">{role}{model}{ts}</div>')
        parts.append(f'<pre class="texte">{escape(msg.texte)}</pre>')
        for block in msg.code_blocks:
            lang = escape(block.language) if block.language else ""
            cls = f' class="language-{lang}"' if lang else ""
            parts.append(f'<pre class="code"><code{cls}>{escape(block.code)}</code></pre>')
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
