# Conversation d'étalonnage

Objectif : créer **une** conversation par chatbot contenant, de façon contrôlée,
tous les cas de parsing à vérifier. Ensuite chacun est scrapé seul et comparé.

## Marche à suivre

1. Dans **chaque** chatbot (ChatGPT, Claude, Gemini, Perplexity, Grok, Mistral),
   envoyer les messages ci-dessous **dans l'ordre**, un par un.
2. Le message 9 nécessite de **joindre une image** (voir ci-dessous).
3. À la fin, **renommer la conversation exactement `Conversation étalon`**.
4. Me le dire : je scraperai uniquement ces conversations avec
   `.venv/bin/python run.py --monthly --match "conversation etalon"`.
5. Tu me donnes ensuite, par chatbot, un tableau « ce que je veux voir / ce que
   je vois ». Je lance alors un sous-agent d'analyse par chatbot.

## Image à joindre au message 9

Créer (ou réutiliser) une image simple contenant **un tableau à 2-3 colonnes et
une liste à puces**, avec un mot en gras. Par exemple une capture de ceci :

| Élément | Quantité | Prix |
| ------- | -------- | ---- |
| Pommes  | 3        | 2 €  |
| Poires  | 5        | 3 €  |

- Premier point
- Deuxième point

## Messages à envoyer

**1. Formatage inline**
> Réponds uniquement par cette phrase, sans aucun commentaire, en respectant
> exactement le formatage : **gras**, *italique*, ~~barré~~, `code inline` et un
> [lien vers OpenAI](https://openai.com).

**2. Listes**
> Réponds uniquement avec trois listes : (1) une liste à puces de 3 fruits ;
> (2) une liste numérotée de 3 étapes pour préparer un café ; (3) une liste
> imbriquée (2 puces, chacune avec 2 sous-puces). Aucun autre texte.

**3. Tableau**
> Réponds uniquement avec un tableau markdown de 3 colonnes (Nom, Âge, Ville)
> et 3 lignes de données fictives. Aucun autre texte.

**4. Bloc de code**
> Réponds uniquement avec un bloc de code Python contenant une fonction
> récursive qui calcule la factorielle, le langage bien indiqué. Aucun autre
> texte.

**5. Mathématiques LaTeX**
> Réponds uniquement avec : la formule de l'énergie cinétique en LaTeX inline,
> puis l'intégrale de 0 à 1 de x au carré en LaTeX sur une ligne dédiée. Aucun
> autre texte.

**6. Titres, citation, séparateur**
> Réponds uniquement avec : un titre de niveau 1, une phrase, un titre de
> niveau 2, une phrase, une citation sur une ligne précédée de `>`, puis une
> ligne horizontale (`---`). Aucun autre texte.

**7. Recherche web + sources**
> Quelle est la capitale de l'Australie ? Donne la réponse et cite tes sources
> avec leurs liens.

**8. Génération d'image**
> Génère une image : un chat astronaute flottant devant la Terre, style
> aquarelle.

**9. Image jointe par l'utilisateur** *(joindre l'image décrite plus haut)*
> Décris précisément cette image, puis retranscris en markdown le tableau
> qu'elle contient. N'invente rien.

**10. Réponse mixte longue**
> Réponds de façon structurée : une introduction en gras, une liste à puces, un
> tableau à 2 colonnes et 2 lignes, puis un bloc de code. Tout dans une seule
> réponse.

**11. Liens et échappement**
> Réponds uniquement par une phrase contenant : un lien markdown, une URL brute
> https://example.com et un `code` contenant les caractères spéciaux `< > & / \`.
> Aucun autre texte.

## Ce que je vérifie dans le JSON scrapé

| Cas | Attendu |
| --- | --- |
| Gras / italique / barré | `**gras**`, `*italique*`, `~~barré~~` préservés |
| Code inline | `` `code inline` `` préservé |
| Lien markdown | `[texte](https://openai.com)` préservé |
| Liste à puces | `-` ou `*` en début de ligne |
| Liste numérotée | `1.` `2.` `3.` préservés |
| Liste imbriquée | indentation conservée |
| Tableau | lignes `\| ... \|` + ligne de séparation |
| Bloc de code | `code_blocks[].language` = `python` |
| Maths inline / bloc | `$...$` / `$$...$$` préservés |
| Titres | `#` et `##` en début de ligne |
| Citation | ligne commençant par `>` |
| Séparateur | ligne `---` |
| Recherche | citations/sources (liens) présentes |
| Image générée | markdown `![...](images/<hash>.<ext>)` + fichier local |
| Image jointe user | markdown `![...](images/<hash>.<ext>)` + fichier local |
| Alternance | jamais deux `user` ou deux `assistant` consécutifs |
| Doublons | pas de message dupliqué (ex. titre répété) |

## Commandes utiles

```bash
# scraper uniquement les conversations d'étalonnage (toutes plateformes)
.venv/bin/python run.py --monthly --match "conversation etalon"

# une seule plateforme
.venv/bin/python run.py --monthly --service gemini --match "conversation etalon"

# voir la conversation dans la page web
.venv/bin/python scripts/serve_web.py
```
