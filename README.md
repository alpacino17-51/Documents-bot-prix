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
├── verifier.py          # historique 90 j / 30 j + Idealo -> verdict ✅ ⚠️ ❓
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
│   └── idealo.py        # Idealo (recherche, fiche produit, historique)
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
- **Reddit** : désactivé par défaut (Reddit renvoie souvent 403/429 aux clients non connectés).
- **pepper.it** : fermé depuis le 14/08/2025, laissé désactivé.

## 11. Sécurité

- Les secrets ne sont lus que depuis les variables d'environnement / `.env` (jamais depuis `config.json`).
- `.gitignore` exclut `.env`, `state.db`, `venv/`, `__pycache__/`, `logs/`.
- Le jeton Telegram n'apparaît jamais dans les logs.
