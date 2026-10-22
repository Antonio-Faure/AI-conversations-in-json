#!/usr/bin/env python3
"""Mot de passe de l'interface web (hash scrypt dans un fichier local).

Le mot de passe en clair n'est jamais ecrit : seul son hash scrypt est
conserve (`secrets/web_password.txt`, permissions 600, non versionne).

    .venv/bin/python scripts/web_password.py --generate        # en cree un (l'affiche une fois)
    .venv/bin/python scripts/web_password.py --generate --force # remplace l'existant
    .venv/bin/python scripts/web_password.py --password-stdin  # mot de passe fourni sur stdin
    .venv/bin/python scripts/web_password.py --check           # verifie un mot de passe
    .venv/bin/python scripts/web_password.py --show            # chemin + empreinte du hash

`--file` change le fichier cible (defaut : secrets/web_password.txt).
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.webauth import (  # noqa: E402
    generate_password,
    load_or_create_secret,
    password_entropy_bits,
    read_password_file,
    verify_password,
    write_password_file,
)

DEFAULT_FILE = ROOT / "secrets" / "web_password.txt"
#: longueur minimale acceptee pour un mot de passe fourni a la main
MIN_LENGTH = 12


def resolve(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def ensure_secret(args) -> Path:
    """Cree le secret de signature des sessions (le service ne peut pas ecrire).

    Le dossier `secrets/` est monte en lecture seule dans le service web : les
    deux fichiers doivent donc exister avant le demarrage.
    """
    path = args.session_secret or (resolve(args.file).parent / "web_session_secret")
    path = resolve(path)
    load_or_create_secret(path)
    return path


def cmd_generate(args) -> int:
    path = resolve(args.file)
    if path.exists() and not args.force:
        print(f"[web] {path} existe deja (--force pour le remplacer)", file=sys.stderr)
        return 1
    password = generate_password()
    write_password_file(path, password)
    secret_path = ensure_secret(args)
    print("[web] mot de passe cree — notez-le maintenant, il ne sera plus affiche :\n")
    print(f"    {password}\n")
    print(f"    entropie : ~{password_entropy_bits(password)} bits")
    print(f"    fichier  : {path} (hash scrypt, permissions 600)")
    print(f"    session  : {secret_path} (secret de signature, permissions 600)")
    return 0


def cmd_password_stdin(args) -> int:
    path = resolve(args.file)
    password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_LENGTH:
        print(f"[web] mot de passe trop court (minimum {MIN_LENGTH} caracteres)",
              file=sys.stderr)
        return 1
    if path.exists() and not args.force:
        print(f"[web] {path} existe deja (--force pour le remplacer)", file=sys.stderr)
        return 1
    write_password_file(path, password)
    ensure_secret(args)
    print(f"[web] mot de passe enregistre dans {path}")
    return 0


def cmd_check(args) -> int:
    path = resolve(args.file)
    stored = read_password_file(path)
    if not stored:
        print(f"[web] aucun mot de passe dans {path}", file=sys.stderr)
        return 1
    if sys.stdin.isatty():
        password = getpass.getpass("mot de passe : ")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if verify_password(password, stored):
        print("[web] mot de passe correct")
        return 0
    print("[web] mot de passe incorrect", file=sys.stderr)
    return 1


def cmd_show(args) -> int:
    path = resolve(args.file)
    stored = read_password_file(path)
    if not stored:
        print(f"[web] aucun mot de passe dans {path}", file=sys.stderr)
        return 1
    print(f"[web] fichier : {path}")
    print(f"[web] empreinte : {stored.split('$')[-1][:16]}…  (scrypt)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE,
                        help="fichier du hash (defaut : secrets/web_password.txt)")
    parser.add_argument("--session-secret", type=Path,
                        help="fichier du secret de session (defaut : a cote du hash)")
    parser.add_argument("--force", action="store_true",
                        help="remplacer un fichier existant")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--generate", action="store_true",
                        help="generer un mot de passe fort (affiche une fois)")
    action.add_argument("--password-stdin", action="store_true",
                        help="lire le mot de passe sur l'entree standard")
    action.add_argument("--check", action="store_true",
                        help="verifier un mot de passe contre le fichier")
    action.add_argument("--show", action="store_true",
                        help="afficher le fichier et l'empreinte du hash")
    args = parser.parse_args()
    if args.generate:
        return cmd_generate(args)
    if args.password_stdin:
        return cmd_password_stdin(args)
    if args.check:
        return cmd_check(args)
    return cmd_show(args)


if __name__ == "__main__":
    raise SystemExit(main())
