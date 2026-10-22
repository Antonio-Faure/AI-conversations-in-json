"""Authentification de l'interface web : mot de passe + sessions signees.

Le mot de passe n'est jamais stocke en clair : seul son hash scrypt est
conserve, dans un fichier local en 600 (`secrets/web_password.txt`, non
versionne). Les sessions sont des jetons signes HMAC, sans etat serveur :
redemarrer le serveur ne deconnecte pas, et rien n'est ecrit sur disque par
une connexion.

Aucune dependance externe (hashlib, hmac, secrets, time).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

#: parametres scrypt (n = 2**14 : ~16 Mio de memoire, quelques ms par essai)
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
SALT_BYTES = 16
#: bornes defensives (un fichier de hash modifie ne doit pas faire exploser la RAM)
SCRYPT_MAX_N = 1 << 18
SCRYPT_MAX_R = 32
SCRYPT_MAX_P = 16

#: longueur du secret de signature des sessions
SECRET_BYTES = 32

#: alphabet sans caracteres ambigus (ni 0/O, ni 1/l/I) pour la saisie manuelle
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_GROUPS = 6
PASSWORD_GROUP_SIZE = 5

#: nom du cookie de session et duree de vie par defaut (30 jours)
SESSION_COOKIE = "aicv_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600

#: prefixe du format de hash
PREFIX = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Hash scrypt d'un mot de passe, au format `scrypt$n$r$p$sel$empreinte`."""
    if not isinstance(password, str) or not password:
        raise ValueError("mot de passe vide")
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )
    return "$".join(
        (PREFIX, str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P), _b64(salt), _b64(digest))
    )


def verify_password(password: str, stored: str | None) -> bool:
    """Verifie un mot de passe contre son hash (comparaison a temps constant)."""
    if not password or not stored:
        return False
    try:
        prefix, n_raw, r_raw, p_raw, salt_raw, digest_raw = stored.split("$")
        if prefix != PREFIX:
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if not (2 <= n <= SCRYPT_MAX_N and 1 <= r <= SCRYPT_MAX_R
                and 1 <= p <= SCRYPT_MAX_P):
            return False
        expected = _unb64(digest_raw)
        if not expected:
            return False
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt_raw),
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError, MemoryError, OverflowError, AttributeError):
        return False
    return hmac.compare_digest(computed, expected)


def generate_password(
    groups: int = PASSWORD_GROUPS, size: int = PASSWORD_GROUP_SIZE
) -> str:
    """Mot de passe aleatoire fort, en groupes lisibles (`xxxxx-xxxxx-...`)."""
    return "-".join(
        "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(size))
        for _ in range(groups)
    )


def password_entropy_bits(password: str) -> float:
    """Entropie theorique d'un mot de passe issu de `generate_password` (bits)."""
    import math

    size = len(PASSWORD_ALPHABET)
    return round(len(password.replace("-", "")) * math.log2(size), 1)


def write_private(path: Path, text: str) -> None:
    """Ecrit un fichier local en 600 (cree le dossier parent au besoin)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 0o600 des la creation : jamais de fenetre en 644 (umask permissif)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        path.chmod(0o600)
    except OSError:  # systeme de fichiers sans permissions POSIX
        pass


def write_password_file(path: Path, password: str) -> str:
    """Ecrit le hash du mot de passe (fichier 600) et renvoie ce hash."""
    digest = hash_password(password)
    write_private(path, digest + "\n")
    return digest


def read_password_file(path: Path) -> str | None:
    """Hash stocke dans le fichier (None si absent, illisible ou vide)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def load_or_create_secret(path: Path) -> bytes:
    """Secret de signature des sessions (cree au besoin, fichier 600)."""
    try:
        raw = path.read_bytes().strip()
    except OSError:
        raw = b""
    if len(raw) >= 16:
        return raw
    secret = secrets.token_bytes(SECRET_BYTES).hex().encode("ascii")
    write_private(path, secret.decode("ascii") + "\n")
    return secret


def _sign(secret: bytes, payload: str) -> str:
    # utf-8 : un jeton malforme (non ASCII) echoue par la signature, sans lever
    return _b64(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest())


def make_token(
    secret: bytes, ttl_seconds: int = SESSION_TTL_SECONDS, now: float | None = None
) -> str:
    """Jeton de session signe : `<expiration>.<nonce>.<signature>`."""
    expires = int((time.time() if now is None else now) + ttl_seconds)
    payload = f"{expires}.{_b64(secrets.token_bytes(12))}"
    return f"{payload}.{_sign(secret, payload)}"


def token_parts(token: str | None) -> tuple[str, int] | None:
    """(nonce, expiration) d'un jeton, sans verifier la signature."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        return parts[1], int(parts[0])
    except (ValueError, TypeError):
        return None


def verify_token(
    token: str | None, secret: bytes, now: float | None = None
) -> bool:
    """Jeton valide (signature correcte et non expire) ?"""
    if not token or not secret or not isinstance(token, str):
        return False
    try:
        expires, nonce, signature = token.split(".")
        if not hmac.compare_digest(_sign(secret, f"{expires}.{nonce}"), signature):
            return False
        return int(expires) > int(time.time() if now is None else now)
    except (ValueError, TypeError, UnicodeError):
        return False


def session_key(secret: bytes, password_hash: str) -> bytes:
    """Cle de signature derivee du secret **et** du hash du mot de passe.

    Changer le mot de passe change la cle : toutes les sessions en cours sont
    donc invalidees, sans etat stocke.
    """
    return hmac.new(
        secret, (password_hash or "").encode("utf-8"), hashlib.sha256
    ).digest()


class LoginThrottle:
    """Limite les tentatives de connexion, par client.

    Apres `max_failures` echecs dans `window`, le client est bloque `lockout`
    secondes. La cle est l'adresse reelle du pair (le proxy de confiance ecrase
    `X-Forwarded-For`) : il n'y a **pas** de plafond global, qui permettrait a
    un attaquant de bloquer la connexion de l'utilisateur legitime. Les
    dictionnaires sont bornes (`max_keys`) pour qu'un afflux de cles
    fabriquees ne fasse pas croitre la memoire.
    """

    def __init__(
        self,
        max_failures: int = 5,
        window: float = 300.0,
        lockout: float = 120.0,
        max_keys: int = 2048,
    ):
        self.max_failures = max_failures
        self.window = window
        self.lockout = lockout
        self.max_keys = max_keys
        self._failures: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Secondes a attendre avant une nouvelle tentative (0 = autorise)."""
        now = time.time() if now is None else now
        with self._lock:
            until = self._locked.get(key, 0.0)
            if until > now:
                return max(1, int(until - now + 0.999))
            self._locked.pop(key, None)
            self._prune(key, now)
            return 0

    def fail(self, key: str, now: float | None = None) -> int:
        """Enregistre un echec ; renvoie les secondes de blocage (0 = libre)."""
        now = time.time() if now is None else now
        with self._lock:
            if len(self._failures) >= self.max_keys:
                self._failures.clear()
                self._locked = {
                    k: v for k, v in self._locked.items() if v > now
                }
            stamps = self._prune(key, now)
            stamps.append(now)
            if len(stamps) >= self.max_failures:
                self._failures.pop(key, None)
                self._locked[key] = now + self.lockout
                return int(self.lockout)
            self._failures[key] = stamps
            return 0

    def reset(self, key: str) -> None:
        """Connexion reussie : oublie les echecs du client."""
        with self._lock:
            self._failures.pop(key, None)
            self._locked.pop(key, None)

    def _prune(self, key: str, now: float) -> list[float]:
        """Tentatives recentes du client (purge les plus anciennes)."""
        stamps = [t for t in self._failures.get(key, []) if now - t < self.window]
        if not stamps:
            self._failures.pop(key, None)
        return stamps
