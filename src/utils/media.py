"""Generation locale de medias pour les conversations d'etalonnage.

Produit un jeu standard (image, audio, video, pdf, csv, txt/md/json) sans
dependance Python nouvelle : les medias binaires passent par `ffmpeg` s'il est
disponible, les fichiers texte/PDF sont ecrits directement.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import List, Sequence

from .logging import get_logger

log = get_logger("aicv.media")

DEFAULT_TEXT = (
    "Document d'etalonnage.\n\n"
    "- premiere puce\n"
    "- deuxieme puce\n\n"
    "| Cle | Valeur |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n"
)


def ffmpeg_path() -> str:
    return shutil.which("ffmpeg") or ""


def _run_ffmpeg(args: Sequence[str]) -> bool:
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        log.warning("ffmpeg introuvable : media binaire ignore")
        return False
    cmd = [ffmpeg, "-y", "-loglevel", "error", *args]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        log.debug("ffmpeg echec (%s): %s", args, exc)
        return False


def make_text_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def make_pdf(path: Path, title: str, lines: Sequence[str]) -> Path:
    """PDF minimal valide (une page, Helvetica), sans dependance."""
    text_lines = [f"({_pdf_escape(title)}) Tj T*"] + [
        f"({_pdf_escape(line)}) Tj T*" for line in lines
    ]
    content = "BT /F1 13 Tf 60 780 Td 18 TL\n" + "\n".join(text_lines) + "\nET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
    ]
    out = ["%PDF-1.4"]
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len("\n".join(out)) + 1)
        out.append(f"{index} 0 obj\n{obj}\nendobj")
    xref_pos = len("\n".join(out)) + 1
    out.append(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f ")
    for offset in offsets:
        out.append(f"{offset:010d} 00000 n ")
    out.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\n".join(out).encode("latin-1"))
    return path


def _pdf_escape(value: str) -> str:
    return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def generate_media(out_dir: Path, with_av: bool = True) -> List[Path]:
    """Genere le jeu standard et retourne la liste des fichiers crees."""
    out_dir = Path(out_dir)
    created: List[Path] = []
    created.append(
        make_pdf(
            out_dir / "document.pdf",
            "Document d'etalonnage",
            ["Ce document teste le parsing des pieces jointes.",
             "Cle a valeur : alpha=1, beta=2, gamma=3."],
        )
    )
    created.append(
        make_text_file(
            out_dir / "donnees.csv",
            "nom,quantite,prix\nPommes,3,2\nPoires,5,3\n",
        )
    )
    created.append(make_text_file(out_dir / "notes.txt", DEFAULT_TEXT))
    created.append(make_text_file(out_dir / "notes.md", "# Titre\n\n" + DEFAULT_TEXT))
    created.append(
        make_text_file(
            out_dir / "donnees.json",
            json.dumps({"etalon": True, "valeurs": [1, 2, 3]}, ensure_ascii=False, indent=2),
        )
    )
    if not with_av:
        return created

    image = out_dir / "image.png"
    if _run_ffmpeg(["-f", "lavfi", "-i", "testsrc=size=480x320:rate=1", "-frames:v", "1", str(image)]):
        created.append(image)
    audio = out_dir / "audio.mp3"
    if _run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(audio)]):
        created.append(audio)
    video = out_dir / "video.mp4"
    if _run_ffmpeg(
        ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15", str(video)]
    ):
        created.append(video)
    return created
