# Bot de veille « erreurs de prix » → Telegram

Outil personnel qui surveille Dealabs, le réseau Pepper européen, CamelCamelCamel (et Reddit en option),
repère les erreurs de prix et les très grosses baisses, vérifie le prix réel (historique + Idealo) puis
t'envoie une alerte Telegram. **Aucun achat automatique** : tu cliques sur le lien et tu décides.

---

## 1. Arborescence

```
Bot_erreur_prix/
├── main.py              # point d'entrée (--once, --loop, --test, --dry-run, --get-chat-id, --velocity-report)
├── settings.py          # lecture de config.json + secrets (.env / variables d'environnement)
├── http_client.py       # client curl_cffi (empreinte Chrome), proxy/en-têtes via env, délais, nouvelles tentatives
├── db.py                # state.db (SQLite) : dédoublonnage, températures, cache Idealo, historique local
├── filters.py           # prix max, catégories, marques revente, détection mode, critères de déclenchement
├── verifier.py          # vérification stricte -> verdict ✅ ⚠️ ❓
├── brand_checker.py     # marque vérifiée / distributeur / réelle / introuvable (cache 30 j)
├── official_price.py    # prix sur le site officiel de la marque (cache 24 h)
├── comparators.py       # idealo.fr/.de, geizhals.eu, présence web multi-marchands (cache 12 h)
├── web_search.py        # recherche DuckDuckGo HTML (budget, cache, pause anti-robot)
├── notifier.py          # mise en forme HTML + envoi Telegram
├── models.py            # structures Deal / Verification / Alert
├── utils.py             # prix, URL, ASIN/EAN/MPN, similarité de titres
├── sources/
│   ├── base.py          # interface commune BaseSource
│   ├── pepper_common.py # parseurs JSON embarqué / RSS / HTML de la plateforme Pepper
│   ├── dealabs.py       # Dealabs (flux + groupe + topics)
│   ├── pepper_eu.py     # mydealz, hotukdeals, chollometro, pepper.it + filtre « livrable en France »
│   ├── camel.py         # CamelCamelCamel (Top Price Drops + stats produit)
│   ├── reddit.py        # Reddit (.json -> .rss -> old.reddit)
│   ├── idealo.py        # Idealo (recherche, fiche produit, historique) — vérification
│   ├── idealo_drops.py  # Idealo « bons plans » (toutes enseignes, ≥ 50 %, ≤ 200 €)
│   ├── merchant_common.py # outils communs enseignes (JSON-LD, tri par remise)
│   ├── decathlon.py     # Decathlon deals / bons plans
│   ├── boulanger.py     # Boulanger (pages catégories)
│   ├── fnac.py          # Fnac (bons plans / ventes flash)
│   └── zalando.py       # Zalando promos (tri par réduction, catégorie Mode)
├── tests/               # tests unitaires + fixtures HTML/JSON réalistes
├── config.json          # TOUS les réglages (aucun secret)
├── .env.example         # modèle des secrets -> copier en .env
├── requirements.txt
├── run_bot.bat          # lancement local en boucle (Windows)
└── .github/workflows/price-bot.yml   # exécution planifiée GitHub Actions
```

## 2. Sources et endpoints

| Source | Données utilisées (dans l'ordre de préférence) |
|---|---|
| Dealabs | JSON embarqué (`data-vue3`, composant `ThreadMainListItemNormalizer`) des pages `https://www.dealabs.com/nouveaux`, `/hot`, `/groupe/erreur-de-prix` → sinon RSS `/rss/nouveaux`, `/rss/hot`, `/rss/groupe/erreur-de-prix` → sinon HTML. Topics : `/discussions/suivi-erreurs-de-prix-1063390` et `/discussions/le-topic-des-erreurs-de-prix-1056379` |
| mydealz.de | `/new`, `/trending` (JSON embarqué) → `/rss/new`, `/rss/hot` |
| hotukdeals.com | `/new`, `/hot` → `/rss/new`, `/rss/hot` |
| chollometro.com | `/nuevos`, `/populares` → `/rss/new`, `/rss/hot` |
| pepper.it | **désactivé** : le site a fermé le 14/08/2025 |
| CamelCamelCamel | RSS `https://{fr,de,it,es}.camelcamelcamel.com/top_drops/feed` → HTML `/top_drops` ; stats produit `/product/{ASIN}` |
| Reddit (optionnel) | `https://www.reddit.com/r/{sub}/new.json` → `/new/.rss` → `https://old.reddit.com/r/{sub}/new/` |
| Dealabs pages 2 | `/nouveaux?page=2` et `/groupe/erreur-de-prix?page=2` (`pages_per_feed`, option `paginate` par flux) |
| Dealabs pages marchands | `/search?q=<enseigne>&sortBy=new` filtré sur le nom du marchand (Boulanger, Fnac, Decathlon, Zalando, Darty, Cdiscount) — `/bons-plans/<enseigne>` redirige vers une page de codes promo sans deals |
| Idealo bons plans | API publique liée depuis `https://www.idealo.fr/bons-plans` : `…/csr/api/v2/modules/dealsResult?discountFrom=50&priceTo=200…` (repli : JSON base64 de la page). Capte Amazon, Cdiscount, Darty, Rakuten, Boulanger… |
| Decathlon | `/deals`, `/deals?from=40&size=40`, `/deals/bons-plans` : JSON-LD puis vignettes `.product-card` (prix, prix barré, remise) |
| Boulanger | pages liste de catégories (`/c/ecran-pc-moniteur`, `/c/console-playstation-5`…) : `tc_vars["list_products"]` (prix TTC, % remise, vendeur, EAN) + JSON-LD `ItemList` |
| Fnac | `/Bons-plans/cl1`, `/ventes-flash` : JSON-LD puis vignettes `.Article-item` — **bloqué par DataDome** en pratique |
| Zalando | `/promo-homme/`, `/promo-femme/`, `/promo-sport-homme/`, `/promo-sport-femme/` avec `?order=sale` (tri par réduction) ; lit aussi « Dernier prix le plus bas » (plus bas 30 j) |
| Idealo (vérification) | `https://www.idealo.fr/prechcat.html?q=…` (résultats JSON encodés en base64 dans la page), fiche `/prix/{id}/…` (JSON-LD + offres), historique `/price-chart/sites/4/products/{id}/history?period=P3M` |

Le JSON embarqué des pages Pepper est préféré au RSS car c'est le seul à contenir la **température**
(indispensable pour la vélocité) et le domaine du marchand (`linkHost`, utile pour savoir si c'est Amazon.de).

## 3. Fonctionnement en bref

1. Chaque source renvoie des deals. Les températures sont enregistrées dans `state.db`.
2. Filtres : prix ≤ 200 € (conversion £→€ via `fx_to_eur`), catégories High-tech/Gaming/Mode & sport **ou** marque
   de la liste « revente » (Lego, Apple, Dyson…). Les coques/verres trempés sont exclus (`exclude_title_keywords`).
3. Déclenchement si **un** critère est rempli : mot-clé (« erreur de prix », « Preisfehler », « price error »…,
   y compris publication dans le groupe/topic « Erreur de prix »), réduction affichée ≥ 70 %, ou vélocité ≥ seuil °/min.
4. Dédoublonnage strict (URL normalisée, ASIN, EAN, hash du titre) : un produit n'est jamais signalé deux fois.
5. Vérification : Idealo (sauf vêtements/chaussures) par EAN → référence fabricant → titre nettoyé avec contrôle de
   similarité (rejette accessoires et autres modèles, ex. XM4 vs XM5). Cache 12 h. Historique : CamelCamelCamel pour
   Amazon, Idealo sinon, et à défaut l'historique local que le bot construit lui-même jour après jour.
6. Verdict ✅ / ⚠️ / ❓ puis alerte Telegram.

---

## 2 bis. Catalogue de sites (`sites_catalog`)

Les sites supplémentaires ne demandent pas de nouveau fichier Python : chaque entrée de `sites_catalog.sites`
choisit un **moteur** réutilisable (`sources/catalog.py`).

| Moteur | Fichier | Sites configurés |
|---|---|---|
| `idealo_deals` | `sources/comparators_drops.py` | idealo.de / .es / .it (tri nouveauté + réduction, ≥ 50 %, ≤ 200 €), filtré sur les boutiques qui livrent la France (`ships_to_france_shops`) |
| `geizhals_deals` | `sources/comparators_drops.py` | geizhals.eu — **403 Cloudflare** depuis la France, pause automatique |
| `tradeinn` | `sources/tradeinn.py` | runnerinn, bikeinn, trekkinn, snowinn, waveinn, dressinn… via les bons plans Idealo (le site lui-même n'expose ses produits que via un moteur de recherche privé protégé par clé, non utilisé) |
| `galaxus` | `sources/galaxus.py` | galaxus.fr : produits remisés présents dans `__NEXT_DATA__` + offres Galaxus vues sur Idealo |
| `shopify` | `sources/sneakers_generic.py` | asphaltgold, overkillshop, sneakersnstuff, size.co.uk (£ → €, plafond UK 150 €) |
| `hypernova` | `sources/sneakers_generic.py` | footshop.fr |
| `cards` | `sources/sneakers_generic.py` | kickz.com |
| `jsonld` | `sources/sneakers_generic.py` | snipes, bstn, sivasdescalzo, endclothing — **désactivés** (produits chargés en JavaScript ou URL promo introuvable) |

Chaque site a son `enabled`, son `min_interval_minutes` et peut avoir son propre `min_displayed_discount_pct`.
Les deals déclenchés sont vérifiés par ordre de priorité : sources communautaires d'abord, puis plus forte remise.

## 3 bis. Vérification stricte anti « faux bons plans »

Pour chaque deal déclenché (toutes catégories, mode comprise) :

1. **Marque** (`brand_checker.py`, cache 30 j dans `brand_cache`) : liste `brands.blocked_brands` → ⚠️ immédiat ;
   `brands.verified_brands` + `resale_brands` → vérifiée ; sinon recherche web DuckDuckGo : site officiel au nom
   exact de la marque (les sosies du type `marque-france.fr` / `-outlet` sont rejetés) ou ≥ 2 revendeurs
   indépendants → réelle ; rien → `fake_or_exclusive` → ⚠️.
2. **Site officiel** (`official_price.py`, cache 24 h) : `site:<domaine> <modèle>` puis lecture du prix
   (JSON-LD / meta / prix affiché). Prix barré > prix officiel + 10 % ou promo proche du prix officiel → ⚠️.
3. **Multi-comparateurs** (`comparators.py`, cache 12 h) : idealo.fr, idealo.de en secours, geizhals.eu
   (High-Tech), puis recherche web multi-marchands. **0 autre marchand + contrôles complets + pas de prix officiel
   → ⚠️ vendu exclusivement sur le site source.** Une recherche qui ne retrouve le produit nulle part est
   considérée comme non concluante (pas d'exclusivité prononcée).
4. **Verdict** : ✅ exige marque vérifiée/réelle + ≥ 1 autre marchand (ou prix officiel) + prix ≥ 35 % sous une
   médiane **de marché** (jamais un prix barré marchand) ou ≥ 30 % sous le prix officiel, et moins cher que le plus
   bas prix connu ailleurs. Une référence **approximative** (prix habituel Idealo, moyenne CamelCamelCamel) doit en
   plus être confirmée par le prix officiel : sinon ❓, ou report si la recherche du prix officiel n'a pas pu se faire.
5. **Envoi Telegram** (`notifications`) : ⚠️ jamais envoyés (enregistrés + loggués) ; ❓ envoyés seulement pour
   les sources communautaires (Dealabs, Pepper…). Les liens Pepper `/visit/...` sont résolus vers l'URL directe du
   produit (paramètres d'affiliation retirés) ; « Lien marchand » apparaît en premier.

**Budget** : `web_search.max_queries_per_run` (6) et `min_delay_seconds` (9) protègent DuckDuckGo ; si la page
anti-robot apparaît, la recherche est mise en pause 60 min. Les deals de sites marchands qui ont besoin de la
recherche web sont alors **reportés** au passage suivant (pas de verdict bâclé) ; les erreurs de prix
communautaires, urgentes, partent en ❓.

**Moteur** : sources lues en parallèle (`general.max_workers`), pause automatique d'un domaine après 2 blocages
403/429 consécutifs (`http.domain_cooldown_*`, persistée dans `state.db`), plafond 150 € pour les boutiques UK
(`general.uk_hosts`).

## 4. Installation sous Windows (pas à pas)

Ouvre **PowerShell** dans le dossier du projet : dans l'Explorateur, va dans le dossier, clique dans la barre
d'adresse, tape `powershell` et Entrée.

**a) Vérifier Python** — affiche les versions de Python installées :
```powershell
py --list
```
Le projet est compatible Python 3.11/3.12 et a été testé avec Python 3.14. S'il n'y a rien, installe Python 3.12
depuis python.org en cochant « Add python.exe to PATH ».

**b) Créer l'environnement virtuel** — un dossier `venv` isolé qui contiendra les bibliothèques du projet
(inutile si le dossier `venv` existe déjà) :
```powershell
py -3 -m venv venv
```

**c) Installer les dépendances** — télécharge curl_cffi, BeautifulSoup, lxml, python-dotenv, pytest dans `venv` :
```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

**d) Créer ton fichier de secrets** — copie le modèle ; `.env` n'est jamais publié sur GitHub :
```powershell
Copy-Item .env.example .env
```
```powershell
notepad .env
```
Colle ton jeton BotFather après `TELEGRAM_BOT_TOKEN=` et enregistre.

**e) Lancer les tests** — vérifie que tous les parseurs fonctionnent :
```powershell
.\venv\Scripts\python.exe -m pytest
```

## 5. Récupérer ton `chat_id`

1. Sur Telegram, ouvre la conversation avec ton bot et envoie-lui un message (ex. « bonjour »).
2. Dans PowerShell, cette commande interroge l'API Telegram `getUpdates` avec ton jeton et affiche l'identifiant :
   ```powershell
   .\venv\Scripts\python.exe main.py --get-chat-id
   ```
3. Copie le nombre affiché dans `.env` après `TELEGRAM_CHAT_ID=` (pour un groupe, il commence par `-`).

## 6. Mode test (vraies alertes simulées)

Envoie 3 alertes d'exemple sur ton Telegram (un ✅, un ⚠️, un ❓), sans interroger les sites :
```powershell
.\venv\Scripts\python.exe main.py --test
```
Pour voir le résultat d'un vrai passage **sans rien envoyer** :
```powershell
.\venv\Scripts\python.exe main.py --once --dry-run
```

## 7. Lancement local en continu

Double-clique sur `run_bot.bat` (ou lance-le depuis PowerShell avec `.\run_bot.bat`). Il crée/vérifie le venv,
installe les dépendances manquantes puis lance un passage toutes les 2 minutes. Les logs s'affichent et sont aussi
écrits dans `logs\bot.log`. Arrêt : Ctrl+C.

> Au tout premier passage, le bot **mémorise sans alerter** les deals déjà en ligne (`silent_first_run`), pour ne pas
> t'inonder. Les alertes commencent au passage suivant.

## 8. Publication sur GitHub et exécution automatique

**a) Installer Git** (une seule fois) — installe Git pour Windows via le gestionnaire de paquets de Windows :
```powershell
winget install --id Git.Git -e
```
Ferme et rouvre PowerShell ensuite, puis donne ton identité à Git (utilisée pour signer tes commits) :
```powershell
git config --global user.name "TonPseudo"
```
```powershell
git config --global user.email "ton-email@exemple.com"
```

**b) Premier envoi** — chaque commande, dans l'ordre :
```powershell
git init -b main
```
(crée le dépôt local)
```powershell
git add .
```
(prépare tous les fichiers ; `.env`, `state.db` et `venv` sont exclus par `.gitignore`)
```powershell
git status
```
(**vérifie** que `.env` n'apparaît PAS dans la liste)
```powershell
git commit -m "Bot de veille erreurs de prix"
```
```powershell
git remote add origin https://github.com/alpacino17-51/Documents-bot-prix.git
```
```powershell
git push -u origin main
```
(une fenêtre de connexion GitHub s'ouvre la première fois)

**c) Secrets GitHub** — sur la page du dépôt : *Settings → Secrets and variables → Actions → New repository secret*.
Crée `TELEGRAM_BOT_TOKEN` et `TELEGRAM_CHAT_ID` (mêmes valeurs que dans `.env`). `PROXY_URL` et
`EXTRA_HEADERS_JSON` sont optionnels.

**d) Premier passage** — onglet *Actions* → workflow *price-bot* → *Run workflow*. Ouvre l'exécution : l'étape
« Lancer un passage du bot » affiche les logs (deals lus, 403 éventuels, alertes envoyées). Ensuite GitHub le relance
seul toutes les 5 minutes environ (les tâches planifiées peuvent avoir plusieurs minutes de retard, et GitHub les
désactive après 60 jours sans activité sur le dépôt).

`state.db` est conservé d'un passage à l'autre par `actions/cache` (clé `state-db-<run_id>`, restauration par préfixe
`state-db-`) ; les anciens caches sont nettoyés automatiquement.

> ⚠️ GitHub Actions tourne sur des IP de datacenter : certains sites (CamelCamelCamel, parfois Pepper) peuvent y
> répondre 403. Le bot le signale dans les logs et continue avec les autres sources. Depuis ton PC (`run_bot.bat`),
> ces blocages sont beaucoup plus rares. Évite de faire tourner les deux en même temps : ils ont chacun leur propre
> `state.db`, tu recevrais donc les alertes en double.

## 9. Réglages utiles (`config.json`)

| Clé | Rôle |
|---|---|
| `general.max_price_eur` | prix maximum (200) |
| `general.max_alerts_per_run` | limite anti-inondation par passage |
| `sources.<nom>.enabled` | activer / désactiver une source |
| `sources.<nom>.min_interval_minutes` | ne pas interroger une source à chaque passage |
| `filters.resale_brands` | marques à fort potentiel de revente |
| `triggers.keywords` | mots-clés d'erreur de prix (FR/DE/EN/ES/IT) |
| `triggers.min_displayed_discount_pct` | réduction affichée minimale (70) |
| `triggers.resale_brand_min_discount_pct` | seuil réduit (40) pour les marques de `filters.resale_brands` |
| `brands.blocked_brands` / `brands.verified_brands` | marques rejetées d'office / reconnues sans recherche |
| `notifications.*` | envoi ou non des ⚠️ et des ❓ (sites directs / communautaires) |
| `web_search.max_queries_per_run` | recherches DuckDuckGo par passage (6) |
| `sources.<nom>.min_displayed_discount_pct` | (facultatif) seuil propre à une source, ex. 85 pour Zalando |
| `general.blocked_pause_minutes` | pause d'une source après un 403 / page anti-robot (60 min) |
| `triggers.velocity.threshold_deg_per_min` | seuil de vélocité (°/min) |
| `verification.*` | seuils des verdicts (35 %, 5 %, 30 %…) |

**Seuil de vélocité** : la valeur de départ est de 8 °/min (une erreur de prix sur Dealabs prend souvent plusieurs
centaines de degrés en moins d'une heure, soit 10 à 30 °/min ; un bon deal classique tourne autour de 1 à 3 °/min).
Après quelques heures/jours de fonctionnement, lance :
```powershell
.\venv\Scripts\python.exe main.py --velocity-report
```
Il affiche la distribution observée et propose un seuil (≈ 2 % des deals les plus explosifs).

## 10. Limites connues (constatées en conditions réelles)

- **CamelCamelCamel** : le flux RSS Top Drops fonctionne, mais les pages produit sont protégées par Cloudflare (403).
  Quand c'est le cas, l'historique vient d'Idealo ou de l'historique local. Quand camel répond, ses stats couvrent tout
  l'historique : la « médiane 90 j » est alors approximée par la moyenne (c'est indiqué dans l'alerte).
- **Historique Idealo** : l'API du graphique a répondu 404 lors des tests. Le bot le détecte, n'insiste pas pendant le
  passage et s'appuie sur l'historique local : chaque prix Idealo relevé est enregistré, et dès 5 points sur 90 jours
  la médiane locale est utilisée. Les verdicts ✅/⚠️ deviennent donc plus fréquents avec le temps ; au début, attends-toi
  à beaucoup de ❓.
- **Topics Dealabs** : les commentaires sont chargés en JavaScript, ils ne sont donc pas dans le HTML. Le module
  analyse ce qui est présent (JSON embarqué ou HTML) et ne renvoie rien sinon. Le groupe « Erreur de prix » couvre
  l'essentiel.
- **Fnac** : protégé par DataDome (403 + captcha) même depuis une IP résidentielle. Le bot le détecte, l'écrit
  proprement dans les logs, met la source en pause 60 min et ne tente aucun contournement. Le parseur est testé sur
  des fixtures mais n'a pas pu être validé sur une vraie page.
- **Boulanger** : les pages « Bons plans » / « Vente flash » chargent leurs produits en JavaScript ; on lit donc des
  pages catégories (liste modifiable dans `config.json`).
- **Decathlon** : pas de page « Fins de série » publique trouvée (404) ; `/deals` et `/deals/bons-plans` sont lues.
- **Nouvelles sources** : au premier passage de chaque source nouvellement activée, ses deals sont mémorisés sans
  alerte (comme `silent_first_run`), pour éviter une avalanche de messages.
- **Reddit** : désactivé par défaut (Reddit renvoie souvent 403/429 aux clients non connectés).
- **pepper.it** : fermé depuis le 14/08/2025, laissé désactivé.

## 11. Sécurité

- Les secrets ne sont lus que depuis les variables d'environnement / `.env` (jamais depuis `config.json`).
- `.gitignore` exclut `.env`, `state.db`, `venv/`, `__pycache__/`, `logs/`.
- Le jeton Telegram n'apparaît jamais dans les logs.
