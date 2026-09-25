# Extracteur d'avis Trustpilot

Récupère **tous** les avis Trustpilot d'une marque et les exporte en **CSV, Excel ou JSON**,
via une interface web ou en ligne de commande.

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
python -m trustpilot_scraper getstryde.co --stars 1 2             # seulement les avis négatifs
python -m trustpilot_scraper getstryde.co --max-pages 5 --delay 2
```

## Colonnes exportées

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

## Bon à savoir

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
