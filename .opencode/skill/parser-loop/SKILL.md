---
name: parser-loop
description: Boucle de correction d'un parseur de chatbot a partir d'une conversation etalon et de screenshots (scrape, compare, patch, re-scrape). Use when fixing a scraper parser (src/parsers/*.py) for ChatGPT, Claude, Gemini, Perplexity, Grok or Mistral using visual screenshots.
---

# Boucle de correction d'un parseur (conversation etalon + screenshots)

Cette skill decrit la procedure commune. L'agent qui la charge est specialise
sur **une seule** plateforme et ne touche que les fichiers de sa tete.

## Principe

Le screenshot du message **est la verite de terrain** (c'est ce que le chatbot
affiche). Le JSON standardise doit le refleter fidelement. On itere jusqu'a ce
que screenshot et JSON concordent.

## Repertoire isole

Chaque agent travaille dans son **worktree git** :
`/home/odin/Documents/code/aicv-wt/<bot>` (branche `parser/<bot>`).

- `read` / `edit` : **toujours** des chemins absolus sous ce worktree.
- `bash` : passer `workdir=/home/odin/Documents/code/aicv-wt/<bot>`.
- Python : `/home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python`.
- Config (profils/cookies partages) : `-c /home/odin/Documents/code/AI-conversations-in-json/config.yaml`.

## Perimetre STRICT

Modifiable : `src/parsers/<bot>.py`, `src/services/<bot>.py` (si necessaire),
et les tests de la tete (`tests/test_<bot>_parser.py` ou une section dediee de
`tests/test_parsers.py`).

Interdit (reserve a l'agent principal) : `src/parsers/base.py`, `src/schema.py`,
`src/orchestrator.py`, `scripts/`, `web/`, `run.py`.

Si un correctif necessite un fichier partage : **ne pas le faire**, le decrire
dans le rapport final.

Ne **jamais** changer le schema JSON ni les noms de champs : seul le contenu
extrait change.

## Boucle

1. Scraper uniquement l'etalon, avec screenshots :

   ```bash
   /home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python run.py \
     -c /home/odin/Documents/code/AI-conversations-in-json/config.yaml \
     --output /home/odin/Documents/code/aicv-wt/<bot>/exports \
     --monthly --service <bot> --match "conversation etalon" --screenshots
   ```

   (Mistral : deux etalons, `work` et `chat`, meme titre ; le pipeline les
   scrape tous les deux. Traiter les deux.)

2. Lire les 3 artefacts produits dans `exports/<bot>/` :
   - `*.json` (messages, roles, `texte`, `code_blocks`, images)
   - `*.html`
   - `screenshots/<conversation>/message-NN.png` (**tu es un modele vision :
     regarde reellement les images**)

3. Comparer. Chercher : listes (puces / numerotee / imbriquee), tableaux,
   blocs de code, LaTeX (`$...$`, `$$...$$`), gras/italique, liens, citations,
   titres, recherche/sources, **images** (generee + piece jointe) en
   `![...](images/<hash>.<ext>)` avec fichier present, tours image sans role,
   messages dupliques, roles fusionnes.

4. Corriger le parseur (+ selecteurs) et ajouter un **test unitaire** reproduisant
   le cas (HTML minimal en dur). Garder le style du fichier.

5. Re-lancer l'etape 1, verifier. Repeter jusqu'a concordance.

6. Qualite :

   ```bash
   /home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python -m pyflakes src run.py scripts tests
   /home/odin/Documents/code/AI-conversations-in-json/.venv/bin/python -m pytest -q
   ```

7. Commit sur la branche (ne **pas** push) :

   ```bash
   git add -A && git commit -m "fix(<bot>): <resume>"
   ```

## Regles de donnees

- Jamais deux messages du meme role consecutifs (le schema l'impose).
- Tableau = lignes `| ... |` + ligne de separation.
- Indentation des listes imbriquees conservee.
- `code_blocks[].language` correct ; `texte` garde les fences markdown.
- LaTeX inline `$...$` et bloc `$$...$$` preserves.
- Images distantes -> markdown relatif `images/<hash>.<ext>` (telecharge par le
  pipeline) ; ne pas laisser d'URL distante si un fichier local existe.
- Pas de boutons/actions/menus dans le texte.

## Rapport final (obligatoire)

Retourner : resume des correctifs, fichiers modifies, hash du commit,
avant/apres sur l'etalon (compteurs + exemples), tests ajoutes, points non
resolus et besoins dans les fichiers partages.
