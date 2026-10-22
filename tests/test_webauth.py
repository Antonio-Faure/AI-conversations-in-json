"""Tests de l'authentification web (hash scrypt, sessions signees, limitation)."""

from __future__ import annotations

import time

import pytest

from src.utils.webauth import (
    PASSWORD_ALPHABET,
    LoginThrottle,
    generate_password,
    hash_password,
    load_or_create_secret,
    make_token,
    password_entropy_bits,
    read_password_file,
    verify_password,
    verify_token,
    write_password_file,
)


def test_hash_scrypt_verifie_le_mot_de_passe():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse", stored)
    assert not verify_password("mauvais", stored)
    assert not verify_password("", stored)


def test_hash_sale_aleatoirement():
    first = hash_password("secret")
    second = hash_password("secret")
    assert first != second
    assert verify_password("secret", first)
    assert verify_password("secret", second)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        "",
        "nimportequoi",
        "bcrypt$16384$8$1$AA$AA",
        "scrypt$0$8$1$AA$AA",
        "scrypt$524288$8$1$AA$AA",   # n au-dela de la borne (refus rapide)
        "scrypt$16384$8$1$AA$AA",
        "scrypt$16384$8$1$AA$",
    ],
)
def test_verification_refuse_un_hash_invalide(stored):
    assert not verify_password("secret", stored)


def test_generation_lisible_et_forte():
    password = generate_password()
    assert password.count("-") == 5
    assert len(password.replace("-", "")) == 30
    assert set(password.replace("-", "")) <= set(PASSWORD_ALPHABET)
    assert password_entropy_bits(password) > 150
    assert generate_password() != generate_password()


def test_fichier_de_mot_de_passe(tmp_path):
    path = tmp_path / "secrets" / "web_password.txt"
    digest = write_password_file(path, "motdepasse-test")
    assert read_password_file(path) == digest
    assert "motdepasse-test" not in path.read_text(encoding="utf-8")
    assert verify_password("motdepasse-test", read_password_file(path))
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_fichier_absent(tmp_path):
    assert read_password_file(tmp_path / "absent") is None


def test_secret_cree_une_seule_fois(tmp_path):
    path = tmp_path / "web_session_secret"
    first = load_or_create_secret(path)
    second = load_or_create_secret(path)
    assert first == second
    assert len(first) >= 16
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_jeton_valide_expire_et_falsifie():
    secret = b"x" * 32
    token = make_token(secret, 60)
    assert verify_token(token, secret)
    assert not verify_token(token, b"y" * 32)          # autre secret
    # signature alteree de facon deterministe (l'ancien test etait flaky)
    altered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert not verify_token(altered, secret)
    assert not verify_token(token + ".x", secret)
    assert not verify_token(None, secret)
    assert not verify_token("", secret)
    expired = make_token(secret, 60, now=time.time() - 120)
    assert not verify_token(expired, secret)
    assert verify_token(expired, secret, now=time.time() - 90)


def test_limitation_des_tentatives():
    throttle = LoginThrottle(max_failures=3, window=60, lockout=30)
    assert throttle.retry_after("ip", now=100) == 0
    assert throttle.fail("ip", now=100) == 0
    assert throttle.fail("ip", now=101) == 0
    assert throttle.fail("ip", now=102) == 30
    assert throttle.retry_after("ip", now=110) > 0
    assert throttle.retry_after("ip", now=132) == 0  # fin du blocage
    throttle.reset("ip")
    assert throttle.retry_after("ip", now=200) == 0


def test_echecs_hors_fenetre_oublies():
    throttle = LoginThrottle(max_failures=2, window=10, lockout=30)
    throttle.fail("ip", now=0)
    # le premier echec est trop ancien : le second ne declenche pas le blocage
    assert throttle.fail("ip", now=100) == 0
    assert throttle.retry_after("ip", now=101) == 0


def test_jeton_non_ascii_refuse():
    secret = b"x" * 32
    for token in ("é.a.b", "1.é.x", "1.a.é", "é" * 40):
        assert not verify_token(token, secret)


def test_cle_de_session_depend_du_mot_de_passe():
    from src.utils.webauth import session_key

    secret = b"s" * 32
    first = session_key(secret, hash_password("a"))
    third = session_key(secret, hash_password("b"))
    assert first != third
    token = make_token(first, 60)
    assert verify_token(token, first)
    # changer le mot de passe invalide les sessions (cle differente)
    assert not verify_token(token, third)
    assert not verify_token(token, secret)


def test_parts_du_jeton():
    from src.utils.webauth import token_parts

    secret = b"x" * 32
    token = make_token(secret, 60)
    nonce, expires = token_parts(token)
    assert len(nonce) >= 8
    assert expires > time.time()
    assert token_parts("nimporte") is None
    assert token_parts("a.b") is None
    assert token_parts("a.b.c.d") is None
    assert token_parts(None) is None


def test_limiteur_borne_ses_cles():
    throttle = LoginThrottle(max_failures=2, max_keys=10)
    for i in range(50):
        throttle.fail(f"k{i}", now=100)
    assert len(throttle._failures) <= 10
