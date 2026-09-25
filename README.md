# Extracteur d'avis Trustpilot & Amazon

Récupère **tous** les avis Trustpilot d'une marque, ou Amazon d'un produit, et les exporte en
**CSV, Excel ou JSON**, via une interface web ou en ligne de commande.

Les avis sont lus dans le JSON `__NEXT_DATA__` embarqué dans chaque page Trustpilot,
puis dédupliqués par identifiant (pas de doublons liés à la pagination).

## Installation

```bash
python -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Interface web

```bash
python -m trustpilot_scraper serve
```

Puis ouvre <http://127.0.0.1:8000> :

1. Saisis un domaine (`getstryde.co`) ou une URL Trustpilot (`https://fr.trustpilot.com/review/getstryde.co`).
2. (Optionnel) filtre par notes, langue, nombre de pages max.
3. Suis la progression en direct ; tu peux arrêter à tout moment et garder les avis déjà récupérés.
4. Consulte les stats (note moyenne, répartition des étoiles, réponses de la marque),
   recherche / filtre / trie les avis, et exporte en CSV, Excel ou JSON.

Avec Docker :

```bash
docker build -t trustpilot-scraper .
docker run -p 8000:8000 trustpilot-scraper
```

## Ligne de commande

```bash
python -m trustpilot_scraper getstryde.co                         # -> getstryde_co_reviews.csv
python -m trustpilot_scraper getstryde.co -o avis.xlsx            # Excel
python -m trustpilot_scraper getstryde.co -o avis.json --lang fr  # avis en français, JSON
python -m trustpilot_scraper getstryde.co --lang all              # avis de toutes les langues
python -m trustpilot_scraper getstryde.co --stars 1 2             # seulement les avis négatifs
python -m trustpilot_scraper getstryde.co --max-pages 5 --delay 2
```

## Amazon

Amazon réserve la liste complète des avis aux utilisateurs **connectés** : l'outil pilote un vrai
navigateur (Chrome ou Edge) avec un profil dédié, dans lequel tu te connectes **une seule fois**.

> ⚠️ La récupération automatique d'avis est contraire aux conditions d'utilisation d'Amazon, qui peut
> afficher des captchas ou restreindre le compte utilisé. Utilise de préférence un **compte secondaire**.

```bash
# 1. Connexion (une fois) : une fenêtre s'ouvre, connecte-toi (coche « Rester connecté »)
python -m trustpilot_scraper amazon-login                     # --domain amazon.com, amazon.de...

# 2. Extraction : lien de la fiche produit ou ASIN
python -m trustpilot_scraper amazon https://www.amazon.fr/dp/B0C1234567 -o avis.xlsx
python -m trustpilot_scraper amazon B0C1234567 --stars 1 2    # seulement les avis négatifs
python -m trustpilot_scraper amazon B0C1234567 --show-browser # voir la fenêtre (captcha)
```

Dans l'interface web : onglet **Amazon**, bouton **Se connecter à Amazon**, puis colle le lien du produit.

- Pendant l'extraction, une fenêtre de navigateur s'ouvre **hors de l'écran** (Amazon bloque plus
  facilement les navigateurs invisibles). Ne la ferme pas.
- Amazon n'affiche que 10 pages (100 avis) par recherche : l'outil parcourt alors les avis **note par
  note**, et pour les notes très fournies trie aussi par « utiles » (jusqu'à ~200 avis par note,
  soit ~1 000 par produit). Au-delà, un avertissement le signale.
- En cas de **captcha**, relance avec `--show-browser` (ou coche « Afficher le navigateur ») et résous-le
  dans la fenêtre : l'extraction reprend toute seule.
- La connexion est stockée dans `~/.trustpilot_scraper/amazon-profile` (cookies inclus) :
  ne partage pas ce dossier. Supprime-le pour te déconnecter.
- L'image Docker ne gère pas Amazon (il faut une fenêtre de navigateur).

Colonnes Amazon : `id`, `date`, `rating`, `title`, `text`, `name`, `country`, `verified` (achat vérifié),
`likes` (votes « utile »), `variant` (taille, couleur…), `date_text` (date telle qu'affichée), `url`.

## Colonnes exportées (Trustpilot)

| colonne | contenu |
|---|---|
| `id` | identifiant Trustpilot de l'avis |
| `date` / `experience_date` | date de publication / date de l'expérience |
| `rating` | note (1 à 5) |
| `title`, `text` | titre et texte de l'avis |
| `name`, `country`, `reviews_count` | auteur, pays, nombre d'avis qu'il a publiés |
| `verified` | avis vérifié |
| `language`, `likes` | langue, nombre de « utile » |
| `reply`, `reply_date` | réponse de l'entreprise |
| `url` | lien direct vers l'avis |

## Utilisation en Python

```python
from trustpilot_scraper import TrustpilotScraper, ScrapeOptions

scraper = TrustpilotScraper("getstryde.co", ScrapeOptions(stars=[1, 2]))
reviews = scraper.run()
print(scraper.business["trust_score"], len(reviews))
```

## Modes de récupération et erreur « HTTP 403 »

Trustpilot est protégé par un pare-feu anti-robots (CloudFront) qui peut refuser les requêtes HTTP simples.
Trois modes sont disponibles (`--engine` en ligne de commande, « Mode » dans l'interface) :

| mode | fonctionnement |
|---|---|
| `auto` (défaut) | essaie d'abord en HTTP (rapide) ; si Trustpilot répond 403, bascule sur un vrai navigateur |
| `http` | requêtes HTTP uniquement (`curl_cffi`, qui imite Chrome) |
| `browser` | charge chaque page dans un vrai Chrome ou Edge piloté par Playwright |

Le mode navigateur utilise Chrome ou Edge s'ils sont installés (Edge est toujours présent sur Windows) :
aucun téléchargement n'est nécessaire. Sinon, installe Chromium avec `python -m playwright install chromium`.
Ajoute `--show-browser` pour voir la fenêtre (utile si une vérification manuelle apparaît).

```bash
python -m trustpilot_scraper getstryde.co --engine browser --max-pages 2
```

Si même le mode navigateur est bloqué, ton adresse IP est probablement bloquée pour un temps :
attends, augmente `--delay`, ou change de connexion (partage 4G, VPN).

## Bon à savoir

- Trustpilot n'affiche que **10 pages** (200 avis) par recherche. Quand l'outil atteint cette limite,
  il récupère automatiquement les avis **note par note** (10 pages par note) puis supprime les doublons.
  Seule une note qui dépasse à elle seule 200 avis peut rester incomplète (un avertissement le signale) :
  combine alors avec un filtre de langue.

- Une pause (1,5 s par défaut) est respectée entre deux pages ; en cas d'erreur 429/5xx, la page est
  retentée avec un délai croissant, puis ignorée (signalée en avertissement) si elle échoue encore.
- Trustpilot peut modifier son HTML ou limiter l'accès ; si `__NEXT_DATA__` disparaît, l'outil le signale.
- Respecte les conditions d'utilisation de Trustpilot et la réglementation sur les données personnelles
  (RGPD) pour l'usage que tu fais des avis collectés.

## Tests

```bash
pip install pytest httpx
python -m pytest
```
