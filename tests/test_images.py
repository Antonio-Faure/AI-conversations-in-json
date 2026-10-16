"""Tests du telechargement local des images (src/utils/images.py)."""

from __future__ import annotations

import base64
from types import SimpleNamespace

from src.utils.images import (
    build_fetch_body,
    decode_fetch_result,
    download_conversation_files,
    download_conversation_images,
    extract_image_urls,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-data"


def _conv(texte: str, platform: str = "gemini"):
    return SimpleNamespace(platform=platform, messages=[SimpleNamespace(texte=texte)])


def test_extract_image_urls_unicite_et_ordre():
    text = "![a](https://x/1.png) texte ![b](https://x/2.png) ![c](https://x/1.png)"
    assert extract_image_urls(text) == ["https://x/1.png", "https://x/2.png"]
    assert extract_image_urls("pas d'image") == []


def test_download_reecrit_et_met_en_cache(tmp_path):
    calls = []

    def loader(url):
        calls.append(url)
        return PNG, "image/png"

    conv = _conv("![a](https://x/1.png) et ![b](https://x/2.png)")
    stats = download_conversation_images(conv, loader, tmp_path / "images")
    assert stats == {"downloaded": 2, "cached": 0, "failed": 0}
    assert "https://" not in conv.messages[0].texte
    assert conv.messages[0].texte.count("images/") == 2
    assert len(list((tmp_path / "images").glob("*.png"))) == 2

    # deuxieme conversation avec les memes URL : cache, aucun nouvel appel
    before = len(calls)
    conv2 = _conv("![a](https://x/1.png)")
    stats2 = download_conversation_images(conv2, loader, tmp_path / "images")
    assert stats2 == {"downloaded": 0, "cached": 1, "failed": 0}
    assert len(calls) == before
    assert conv2.messages[0].texte.startswith("![a](images/")


def test_download_rejette_html(tmp_path):
    conv = _conv("![a](https://x/page)")
    stats = download_conversation_images(
        conv, lambda url: (b"<html>login</html>", "text/html"), tmp_path / "images"
    )
    assert stats["failed"] == 1
    assert conv.messages[0].texte == "![a](https://x/page)"


def test_download_preserve_les_liens_de_prose(tmp_path):
    """Une URL d'image citee dans la prose reste un lien web (capacite `link`)."""
    conv = _conv(
        "Voir [https://x/1.png](https://x/1.png) puis ![a](https://x/1.png)"
    )
    stats = download_conversation_images(
        conv, lambda url: (PNG, "image/png"), tmp_path / "images"
    )
    assert stats == {"downloaded": 1, "cached": 0, "failed": 0}
    text = conv.messages[0].texte
    assert "[https://x/1.png](https://x/1.png)" in text
    assert "![a](images/" in text and text.endswith(".png)")


def test_download_loader_none(tmp_path):
    conv = _conv("![a](https://x/page)")
    stats = download_conversation_images(conv, lambda url: None, tmp_path / "images")
    assert stats["failed"] == 1


def test_files_telecharge_et_reecrit(tmp_path):
    conv = _conv("Voici [notes.txt](https://x/notes.txt) et [doc.pdf](https://x/doc.pdf).")
    stats = download_conversation_files(
        conv, lambda url: (b"contenu", "text/plain"), tmp_path / "files"
    )
    assert stats == {"downloaded": 2, "cached": 0, "failed": 0}
    assert "https://" not in conv.messages[0].texte
    assert conv.messages[0].texte.count("files/") == 2
    names = {p.suffix for p in (tmp_path / "files").glob("*")}
    assert ".txt" in names and ".pdf" in names


def test_files_ignore_les_liens_web(tmp_path):
    conv = _conv("Voir [OpenAI](https://openai.com) et [docs](https://x/docs).")
    stats = download_conversation_files(conv, lambda url: (b"x", "text/html"), tmp_path / "files")
    assert stats["downloaded"] == 0 and stats["failed"] == 0
    assert "https://" in conv.messages[0].texte


def test_files_rejette_html(tmp_path):
    conv = _conv("[doc.pdf](https://x/doc.pdf)")
    stats = download_conversation_files(
        conv, lambda url: (b"<html>login</html>", "text/html"), tmp_path / "files"
    )
    assert stats["failed"] == 1
    assert conv.messages[0].texte == "[doc.pdf](https://x/doc.pdf)"


def test_decode_fetch_result():
    data = base64.b64encode(PNG).decode()
    assert decode_fetch_result({"ok": True, "data": data, "ct": "image/png"}) == (
        PNG,
        "image/png",
    )
    assert decode_fetch_result({"ok": False}) is None
    assert decode_fetch_result(None) is None
    assert decode_fetch_result({"ok": True, "data": None}) is None


def test_build_fetch_body_injecte_url():
    body = build_fetch_body("https://x/y.png?a=1&b=2")
    assert 'const url = "https://x/y.png?a=1&b=2";' in body
    assert "__URL__" not in body
    assert "credentials" in body


# -- URL de medias « seuls » (images / audio / video) -------------------------


def test_media_url_kind_par_extension():
    from src.utils.images import media_url_kind

    assert media_url_kind("https://x/photo.jpg") == "image"
    assert media_url_kind("https://x/photo.JPG?token=1") == "image"
    assert media_url_kind("https://x/clip.mp3") == "audio"
    assert media_url_kind("https://x/clip.ogg#t=1") == "audio"
    assert media_url_kind("https://x/movie.mp4") == "video"
    assert media_url_kind("https://x/article.html") is None
    assert media_url_kind("https://youtube.com/watch?v=abc") is None


def test_extract_media_urls_contextes():
    from src.utils.images import extract_media_urls

    text = (
        "![a](https://x/1.png) puis [doc](https://x/son.mp3) "
        "et https://x/photo.jpg fin"
    )
    assert extract_media_urls(text) == [
        ("md-image", "https://x/1.png"),
        ("md-link", "https://x/son.mp3"),
        ("bare", "https://x/photo.jpg"),
    ]
    assert extract_media_urls("") == []


def test_media_links_telecharge_et_reecrit(tmp_path):
    from src.utils.images import download_conversation_media_links

    def loader(url):
        if url.endswith(".mp3"):
            return b"ID3fake-audio", "audio/mpeg"
        return PNG, "image/png"

    conv = _conv(
        "![alt](https://x/sans-extension) puis https://x/photo.jpg "
        "et [son](https://x/clip.mp3) et https://x/page.html"
    )
    stats = download_conversation_media_links(
        conv, loader, tmp_path / "images", tmp_path / "files"
    )
    assert stats["candidates"] == 3
    assert stats["downloaded"] == 3
    texte = conv.messages[0].texte
    assert "![alt](images/" in texte  # image markdown (meme sans extension)
    assert "![photo.jpg](images/" in texte  # URL nue image -> embed
    assert "[son](files/" in texte and ".mp3)" in texte  # lien audio -> local
    assert "https://x/page.html" in texte  # lien web intact
    assert len(list((tmp_path / "images").glob("*"))) == 2
    assert len(list((tmp_path / "files").glob("*.mp3"))) == 1


def test_media_links_rejette_html_et_dry_run(tmp_path):
    from src.utils.images import download_conversation_media_links

    conv = _conv("https://x/fake.jpg")
    stats = download_conversation_media_links(
        conv,
        lambda url: (b"<html>login</html>", "text/html"),
        tmp_path / "images",
        tmp_path / "files",
    )
    assert stats["failed"] == 1
    assert conv.messages[0].texte == "https://x/fake.jpg"

    conv2 = _conv("![a](https://x/1.png)")
    stats2 = download_conversation_media_links(
        conv2,
        lambda url: (PNG, "image/png"),
        tmp_path / "images",
        tmp_path / "files",
        dry_run=True,
    )
    assert stats2["downloaded"] == 1
    assert conv2.messages[0].texte == "![a](https://x/1.png)"  # rien ecrit
    assert not (tmp_path / "images").exists()
