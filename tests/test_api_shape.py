"""Tests du module de forme canonique (value-free) des payloads API."""

from __future__ import annotations

import json

from src.utils.api_shape import (
    bucket_of,
    canonical_shape,
    compare_shapes,
    flatten_shape,
    format_tags,
    level_for,
    merge_shapes,
    shape_of,
    signature_of,
    signature_of_shape,
    types_compatible,
)

UUID = "550e8400-e29b-41d4-a716-446655440000"
SHA1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


class TestBuckets:
    def test_bornes(self):
        assert bucket_of(0) == "0"
        assert bucket_of(1) == "1"
        assert bucket_of(2) == "2-5"
        assert bucket_of(5) == "2-5"
        assert bucket_of(6) == "6-20"
        assert bucket_of(20) == "6-20"
        assert bucket_of(21) == "21-100"
        assert bucket_of(100) == "21-100"
        assert bucket_of(101) == "100+"
        assert bucket_of(10_000) == "100+"


class TestFormatTags:
    def test_uuid(self):
        assert format_tags(UUID) == ["uuid"]
        assert format_tags(UUID.upper()) == ["uuid"]

    def test_iso_date(self):
        assert format_tags("2026-10-12") == ["iso-date"]
        assert format_tags("2026-10-12T08:30:00Z") == ["iso-date"]
        assert format_tags("2026-10-12T08:30:00.123+02:00") == ["iso-date"]
        assert format_tags("2026-10-12 08:30:00") == ["iso-date"]
        assert format_tags("12/10/2026") == []

    def test_url(self):
        assert format_tags("https://example.org/a/b?c=1") == ["url"]
        assert format_tags("ftp://host/file") == ["url"]
        assert format_tags("www.example.org") == []

    def test_email(self):
        assert format_tags("user@example.com") == ["email"]

    def test_hash(self):
        assert format_tags(SHA1) == ["hash"]
        assert format_tags(SHA256) == ["hash"]
        assert format_tags("deadbeef") == []

    def test_aucun_tag(self):
        assert format_tags("Bonjour le monde") == []
        assert format_tags("") == []

    def test_plusieurs_tags_tries(self):
        # une chaine ne porte qu'un tag en pratique ; l'ordre reste deterministe
        assert format_tags(UUID) == sorted(format_tags(UUID))


class TestShapeOf:
    def test_types_scalaires(self):
        assert shape_of(1) == {"t": "int"}
        assert shape_of(1.5) == {"t": "float"}
        assert shape_of(True) == {"t": "bool"}
        assert shape_of(None) == {"t": "null"}
        assert shape_of("abc") == {"t": "str"}

    def test_objet_cle_triee(self):
        shape = shape_of({"b": 1, "a": "x"})
        assert shape["t"] == "object"
        assert list(shape["c"]) == ["a", "b"]

    def test_liste_bucket_et_item(self):
        shape = shape_of([1, 2, 3])
        assert shape == {"t": "list", "n": "2-5", "i": {"t": "int"}}

    def test_liste_vide(self):
        assert shape_of([]) == {"t": "list", "n": "0"}

    def test_liste_heterogene_type_compose(self):
        shape = shape_of([1, "a", None])
        assert shape["n"] == "2-5"
        assert shape["i"]["t"] == "int+null+str"

    def test_liste_objets_cles_unies(self):
        shape = shape_of([{"a": 1}, {"b": "x"}])
        assert shape["i"]["t"] == "object"
        assert set(shape["i"]["c"]) == {"a", "b"}

    def test_tags_chaine(self):
        shape = shape_of({"id": UUID, "url": "https://x.test/"})
        assert shape["c"]["id"] == {"t": "str", "fmt": ["uuid"]}
        assert shape["c"]["url"] == {"t": "str", "fmt": ["url"]}

    def test_max_depth_coupe(self):
        payload = {"a": {"b": {"c": {"d": 1}}}}
        shape = shape_of(payload, max_depth=2)
        assert shape["c"]["a"]["c"]["b"]["cut"] is True
        assert shape["c"]["a"]["c"]["b"]["c"] == {}

    def test_max_list_samples_echantillonne(self):
        shape = shape_of(list(range(100)), max_list_samples=5)
        assert shape["n"] == "21-100"
        assert shape["i"] == {"t": "int"}


class TestSignature:
    def test_stable(self):
        payload = {"a": [1, 2], "b": "x"}
        assert signature_of(payload) == signature_of(dict(payload))

    def test_value_free(self):
        # la signature est celle de la FORME : des valeurs differentes de meme
        # structure donnent la meme empreinte (c'est le but)
        assert signature_of({"a": [1, 2]}) == signature_of({"a": [7, 9]})

    def test_sensible_a_la_structure(self):
        base = {"a": [1, 2], "b": "x"}
        assert signature_of(base) != signature_of({"a": [1, 2], "b": "x", "c": 1})
        assert signature_of(base) != signature_of({"a": [1, 2], "b": [1]})
        # changement de bucket de cardinalite (2-5 -> 21-100)
        assert signature_of(base) != signature_of({"a": list(range(30)), "b": "x"})

    def test_signature_de_la_forme_canonique(self):
        payload = {"a": "x"}
        assert signature_of(payload) == signature_of(payload)
        assert len(signature_of(payload)) == 64
        # la signature est bien le hash de la forme (pas du payload)
        import hashlib

        shape = shape_of(payload)
        expected = hashlib.sha256(canonical_shape(shape).encode("ascii")).hexdigest()
        assert signature_of(payload) == expected


class TestFlatten:
    def test_chemins(self):
        shape = shape_of({"a": {"b": 1}, "l": [{"c": "x"}]})
        paths = flatten_shape(shape)
        assert "$" in paths
        assert "$.a.b" in paths
        assert "$.l" in paths
        assert "$.l[]" in paths
        assert "$.l[].c" in paths


class TestCompare:
    def test_identique(self):
        shape = shape_of({"a": 1, "l": [1, 2]})
        report = compare_shapes(shape, shape_of({"a": 2, "l": [3, 4]}))
        assert report["score"] == 100.0
        assert report["level"] == "OK"
        assert report["added"] == [] and report["removed"] == []

    def test_champ_ajoute_et_supprime(self):
        ref = shape_of({"a": 1, "b": 2})
        live = shape_of({"a": 1, "c": 3})
        report = compare_shapes(ref, live)
        assert report["added"] == ["$.c"]
        assert report["removed"] == ["$.b"]
        # le chemin inconnu penalise la structure ; `removed` reste informatif
        assert report["score"] < 100
        assert report["structure"] < 100

    def test_sous_ensemble_penalise_par_la_couverture(self):
        """Un payload qui n'utilise qu'une partie du vocabulaire n'est plus 100.

        Les chemins du live restent tous connus (structure 100) mais la
        couverture de la reference est partielle -> score pondere, WARN.
        """
        ref = shape_of({"a": 1, "b": {"c": "x"}, "l": [1, 2, 3]})
        live = shape_of({"a": 5})
        report = compare_shapes(ref, live)
        assert report["structure"] == 100.0
        assert report["coverage"] == 33.33
        assert report["score"] < 95
        assert report["level"] == "WARN"
        assert report["removed"]  # branches non utilisees, informatif

    def test_payload_vide_break(self):
        """`{}` / `null` ne peuvent plus scorer 100 (faux negatif historique)."""
        ref = shape_of({"a": 1, "b": {"c": "x"}, "l": [1, 2, 3]})
        for payload in ({}, None):
            report = compare_shapes(ref, shape_of(payload))
            assert report["coverage"] == 0.0
            assert report["level"] == "BREAK"
            assert report["score"] < 80

    def test_liste_riche_videe_penalisee(self):
        """`messages=[]` alors que la reference y voit des objets : BREAK."""
        ref = shape_of(
            {
                "conversation_id": "x",
                "title": "t",
                "messages": [{"id": "m1", "role": "user", "texte": "bonjour"}],
            }
        )
        live = shape_of(
            {"conversation_id": "x", "title": "t", "messages": []}
        )
        report = compare_shapes(ref, live)
        assert report["emptied_lists"] == ["$.messages"]
        assert report["level"] == "BREAK"
        assert report["score"] < 80

    def test_messages_degarnis_warn(self):
        """Items presents mais champs d'items disparus -> couverture d'items."""
        ref = shape_of(
            {
                "conversation_id": "x",
                "title": "t",
                "messages": [
                    {"id": "m1", "role": "user", "texte": "bonjour", "ts": "x"},
                    {"id": "m2", "role": "assistant", "texte": "salut", "ts": "y"},
                ],
            }
        )
        live = shape_of(
            {
                "conversation_id": "x",
                "title": "t",
                "messages": [{"id": "m1"}, {"id": "m2"}],
            }
        )
        report = compare_shapes(ref, live)
        assert report["emptied_lists"] == []
        assert report["coverage"] == 57.14  # 4 champs sur 7 (3 + 4 items)
        assert report["level"] == "WARN"

    def test_liste_optionnelle_scalaire_videe_toleree(self):
        """Une liste d'items scalaires videe (`safe_urls`) n'est pas une perte."""
        ref = shape_of({"a": 1, "safe_urls": ["https://x.test/"]})
        live = shape_of({"a": 1, "safe_urls": []})
        report = compare_shapes(ref, live)
        assert report["emptied_lists"] == []
        assert report["score"] == 100.0

    def test_null_compatible_avec_tout_type(self):
        ref = shape_of({"a": 1, "b": "x", "c": True})
        live = shape_of({"a": None, "b": None, "c": None})
        report = compare_shapes(ref, live)
        assert report["score"] == 100.0
        assert report["type_changes"] == []

    def test_type_compose_accepte_ses_membres(self):
        ref = merge_shapes([shape_of({"a": 1}), shape_of({"a": "x"})])
        assert ref["c"]["a"]["t"] == "int+str"
        assert compare_shapes(ref, shape_of({"a": 2}))["score"] == 100.0
        assert compare_shapes(ref, shape_of({"a": None}))["score"] == 100.0
        report = compare_shapes(ref, shape_of({"a": [1]}))
        assert report["type_changes"] == ["$.a"]

    def test_types_compatible(self):
        assert types_compatible({"t": "int"}, {"t": "int"})
        assert types_compatible({"t": "int"}, {"t": "null"})
        assert types_compatible({"t": "int+str"}, {"t": "str"})
        assert not types_compatible({"t": "int"}, {"t": "str"})
        assert not types_compatible({"t": "object"}, {"t": "list"})

    def test_changement_de_type(self):
        ref = shape_of({"a": [1]})
        live = shape_of({"a": "x"})
        report = compare_shapes(ref, live)
        assert report["type_changes"] == ["$.a"]
        assert report["removed"] == ["$.a[]"]
        assert report["level"] == "BREAK"

    def test_changement_de_cardinalite(self):
        ref = shape_of({"a": [1, 2]})
        live = shape_of({"a": list(range(30))})
        report = compare_shapes(ref, live)
        assert report["cardinality_changes"] == ["$.a"]
        assert report["cardinality"] < 100
        assert report["structure"] == 100.0

    def test_liste_plus_courte_compatible(self):
        ref = shape_of({"a": list(range(30))})
        live = shape_of({"a": [1]})
        report = compare_shapes(ref, live)
        assert report["cardinality_changes"] == []
        assert report["score"] == 100.0

    def test_changement_de_format(self):
        # nouveau format jamais vu dans la reference -> signale
        ref = shape_of({"id": "pas-un-uuid"})
        live = shape_of({"id": UUID})
        report = compare_shapes(ref, live)
        assert report["format_changes"] == ["$.id"]
        assert report["type_changes"] == []

    def test_format_perdu_tolere(self):
        # un tag deja vu qui disparait (valeur heterogene) : pas de bruit
        ref = shape_of({"id": UUID})
        live = shape_of({"id": "pas-un-uuid"})
        report = compare_shapes(ref, live)
        assert report["format_changes"] == []
        assert report["score"] == 100.0

    def test_gros_ecart_break(self):
        ref = shape_of({"messages": [{"author": {"role": "user"}}]})
        live = shape_of({"autre": [1, 2, 3]})
        report = compare_shapes(ref, live)
        assert report["level"] == "BREAK"
        assert report["score"] < 80

    def test_counts(self):
        report = compare_shapes(shape_of({"a": 1}), shape_of({"a": 1}))
        assert report["counts"] == {"ref_paths": 2, "live_paths": 2, "common": 2}


class TestMergeShapes:
    def test_union_des_structures(self):
        merged = merge_shapes([shape_of({"a": 1, "b": "x"}), shape_of({"a": 2, "c": True})])
        assert merged["t"] == "object"
        assert set(merged["c"]) == {"a", "b", "c"}
        # chaque payload d'origine est structurellement compatible avec l'union ;
        # la couverture reste partielle (l'union contient aussi les branches de
        # l'autre payload), donc le score n'est plus 100
        for payload in ({"a": 1, "b": "x"}, {"a": 2, "c": True}):
            report = compare_shapes(merged, shape_of(payload))
            assert report["structure"] == 100.0
            assert report["coverage"] < 100.0
            assert report["score"] < 100.0

    def test_type_compose_et_enfants_conserves(self):
        merged = merge_shapes(
            [shape_of({"d": {"x": 1}}), shape_of({"d": "texte"})]
        )
        node = merged["c"]["d"]
        assert node["t"] == "object+str"
        assert "x" in node["c"]  # les enfants de la branche objet sont gardes

    def test_liste_bucket_maxi(self):
        merged = merge_shapes([shape_of({"l": [1]}), shape_of({"l": list(range(30))})])
        assert merged["c"]["l"]["n"] == "21-100"

    def test_vide(self):
        assert merge_shapes([]) is None

    def test_signature_de_la_forme(self):
        shape = shape_of({"a": 1})
        assert signature_of_shape(shape) == signature_of({"a": 1})


class TestLevels:
    def test_seuils(self):
        assert level_for(100) == "OK"
        assert level_for(95) == "OK"
        assert level_for(94.99) == "WARN"
        assert level_for(80) == "WARN"
        assert level_for(79.99) == "BREAK"
        assert level_for(0) == "BREAK"

    def test_seuil_configurable(self):
        assert level_for(90, threshold=90) == "OK"
        assert level_for(89.9, threshold=90) == "WARN"


class TestCanonicalKey:
    """Les cles d'objet qui sont des valeurs (ids, emails...) sont masquees."""

    def test_cle_uuid_masquee(self):
        shape = shape_of({UUID: {"x": 1}})
        assert list(shape["c"]) == ["<uuid>"]
        assert shape["c"]["<uuid>"]["c"]["x"] == {"t": "int"}

    def test_cle_hash_et_email_masquees(self):
        shape = shape_of({SHA256: 1, "jean@example.com": 2})
        assert set(shape["c"]) == {"<hash>", "<email>"}

    def test_cle_composee_masquee(self):
        # cle type `<uuid>:<index>` (carte de widgets ChatGPT)
        shape = shape_of({f"{UUID}:3": {"x": 1}})
        assert list(shape["c"]) == ["<uuid>:3"]

    def test_cle_normale_conservee(self):
        assert "messages" in shape_of({"messages": []})["c"]


class TestAntiFuite:
    """Aucune valeur scalaire du payload ne doit apparaitre dans la forme."""

    SECRETS = (
        "sk-secret-token-0123456789",
        "Message prive avec un nom : Jean Dupont",
        "https://interne.example.org/api?token=abc",
        "jean.dupont@example.com",
        UUID,
        "2026-10-12T08:30:00Z",
        SHA256,
        "C:\\Users\\jean\\Documents\\notes.txt",
    )

    def _payload(self):
        return {
            "token": self.SECRETS[0],
            "messages": [
                {
                    "text": self.SECRETS[1],
                    "url": self.SECRETS[2],
                    "email": self.SECRETS[3],
                    "id": self.SECRETS[4],
                    "created_at": self.SECRETS[5],
                    "checksum": self.SECRETS[6],
                    "path": self.SECRETS[7],
                }
            ],
            # carte indexee par identifiant : la cle est une valeur du payload
            self.SECRETS[4]: {"score": 1},
        }

    def test_forme_sans_valeurs(self):
        rendered = json.dumps(shape_of(self._payload()), ensure_ascii=False)
        for secret in self.SECRETS:
            assert secret not in rendered

    def test_les_tags_sont_bien_calcules(self):
        shape = shape_of(self._payload())
        item = shape["c"]["messages"]["i"]["c"]
        assert item["id"] == {"t": "str", "fmt": ["uuid"]}
        assert item["url"] == {"t": "str", "fmt": ["url"]}
        assert item["email"] == {"t": "str", "fmt": ["email"]}
        assert item["created_at"] == {"t": "str", "fmt": ["iso-date"]}
        assert item["checksum"] == {"t": "str", "fmt": ["hash"]}
        assert item["text"] == {"t": "str"}
        assert item["path"] == {"t": "str"}
