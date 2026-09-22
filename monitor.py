#!/usr/bin/env python3
"""
Veille de disponibilité — Pokémon 30e anniversaire.

Pour chaque URL de urls.txt :
  1. télécharge la page (empreinte navigateur Chrome via curl_cffi) ;
  2. lit la disponibilité structurée (JSON-LD schema.org, microdata, meta produit) ;
  3. à défaut, analyse le texte : « disponible » hors négation
     (« indisponible », « plus disponible », « bientôt disponible »… sont exclus) ;
  4. notifie (ntfy et/ou Telegram) uniquement au PASSAGE vers disponible.

L'état est conservé dans state.json pour éviter une alerte toutes les heures.
Usage : python monitor.py [--dry-run] [--test-notif]
"""
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as http
    FETCH_KW = {"impersonate": "chrome"}
except ImportError:  # repli si curl_cffi n'est pas installé
    import requests as http
    FETCH_KW = {}

ROOT = Path(__file__).resolve().parent
URLS_FILE = ROOT / "urls.txt"
STATE_FILE = ROOT / "state.json"

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_BLOCKED = os.getenv("NOTIFY_BLOCKED", "0") == "1"  # alerte si un site reste bloqué trop longtemps
BLOCKED_ALERT_HOURS = float(os.getenv("BLOCKED_ALERT_HOURS", "24"))

# Plafond de prix : au-delà de référence x (1 + tolérance), pas d'alerte (statut TROP_CHER)
PRICE_TOLERANCE = float(os.getenv("PRICE_TOLERANCE", "0.35"))   # alerte jusqu'à +35 %
EXCLUDE_TOLERANCE = float(os.getenv("EXCLUDE_TOLERANCE", "0.35")) # au-delà de +35 % : site écarté (EXCLU), revu 1 fois / 24 h
EXCLUDE_RECHECK_H = 24
# Grandes enseignes : alerte même si le prix est illisible. Ailleurs : pas d'alerte sans prix lisible.
TRUSTED_RETAILERS = {"fnac", "cdiscount", "carrefour", "joueclub", "king-jouet", "smythstoys",
                     "coursesu", "1001hobbies", "micromania", "lagranderecre", "amazon"}
# Prix de référence = prix officiels annoncés (tableau de sortie 30e anniversaire).
# Clé = mot présent dans le libellé. Alerte jusqu'à +35 %, exclusion au-delà (seuils recalculés automatiquement).
REFERENCE_PRICES = {
    "dresseur": 55.99,      # ETB 30 ans              
    "poster": 27.99,        # Poster Collection       
    "nymphali": 27.99,      # Pokébox Nymphali        
    "amphinobi": 27.99,     # Pokébox Amphinobi       
    "2 boosters": 11.99,    # Duopack                 
    "bundle": 35.99,        # Booster Bundle (6 boost.)
    "196214145153": 49.99,  # Classeur Collection (hypothèse)
    "tin box": 26.99,       # Tin box : pas de prix officiel dans le tableau, prix constaté conservé
}

# Espacement des visites (anti-bot)
RUN_INTERVAL_MIN = float(os.getenv("RUN_INTERVAL_MIN", "5"))        # fréquence des passages (veille.yml)
TARGET_INTERVAL_MIN = float(os.getenv("TARGET_INTERVAL_MIN", "7.5"))  # fréquence visée pour chaque page
PAUSE_MIN = float(os.getenv("PAUSE_MIN", "2"))      # bornes de la pause entre deux requêtes (secondes) ;
PAUSE_MAX = float(os.getenv("PAUSE_MAX", "10"))     # la pause réelle est calculée pour étaler les visites sur le passage
MIN_SITE_GAP = float(os.getenv("MIN_SITE_GAP", "30"))  # secondes minimum entre deux visites du même site
TIME_BUDGET = float(os.getenv("TIME_BUDGET", "240"))  # au-delà, les pages restantes passent au tour suivant
BACKOFF_MAX_MIN = 60                                  # page bloquée : 5, 10, 20, 40 puis 60 min entre deux essais
DRY_RUN = "--dry-run" in sys.argv
TEST_NOTIF = "--test-notif" in sys.argv or os.getenv("TEST_NOTIF", "") in ("1", "true")

# Modèles de lien « ajouter au panier » par enseigne. {id} = premier nombre du dernier segment de l'URL.
# À VALIDER : un modèle faux mène simplement à une page d'erreur du site.
CART_TEMPLATES = {
    "1001hobbies": "https://www.1001hobbies.fr/panier?add=1&id_product={id}&qty=1",
}
CART_HINT = re.compile(r"(add[-_]?to[-_]?cart|addtocart|ajout[-_]?(au[-_]?)?panier|[?&]add=1\b|panier\?add|cart\?add)", re.I)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}
if not FETCH_KW:
    HEADERS["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    )

AVAILABLE = {"DISPO", "PRECOMMANDE"}
KNOWN = {"DISPO", "PRECOMMANDE", "INDISPO", "TROP_CHER", "EXCLU", "PRIX_INCONNU"}

SCHEMA_MAP = {
    "instock": "DISPO",
    "limitedavailability": "DISPO",
    "onlineonly": "DISPO",
    "instoreonly": "DISPO",
    "preorder": "PRECOMMANDE",
    "presale": "PRECOMMANDE",
    "outofstock": "INDISPO",
    "soldout": "INDISPO",
    "discontinued": "INDISPO",
    "backorder": "INDISPO",
}

BLOCK_CODES = {401, 403, 429, 503}
BLOCK_MARKERS = (
    "captcha-delivery.com", "cf-chl", "challenge-platform", "just a moment",
    "access denied", "attention required", "px-captcha", "_incapsula_resource",
)

DISPO_RE = re.compile(r"(\w*)disponibles?\b", re.I)
NEG_CONTEXT = re.compile(r"\b(non|pas|plus|bient[oô]t|prochainement)\b", re.I)
NEGATIVE_PHRASES = (
    "rupture de stock", "épuisé", "epuise", "indisponible", "hors stock",
    "m'alerter", "me prévenir", "être alerté", "victime de son succès", "plus en stock",
    "bientôt disponible", "bientot disponible", "prochainement disponible",
)
ADD_TO_CART = ("ajouter au panier", "ajouter à mon panier", "ajout au panier", "acheter maintenant")
# Phrases génériques des gabarits de page, sans rapport avec le stock : retirées avant analyse
IGNORE_RE = re.compile(
    r"(une taille et une couleur disponibles?|(tailles?|couleurs?|coloris|options?|créneaux?) disponibles?"
    r"|(retrait|livraison|drive|paiement)s? disponibles?)", re.I)


# ---------------------------------------------------------------- utilitaires
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def retailer(url):
    host = urlparse(url).netloc.lower().removeprefix("www.")
    return host.split(".")[0]


def label_for(url):
    segs = [s for s in urlparse(url).path.split("/") if s]
    best = max(segs, key=len) if segs else url
    return re.sub(r"\.html?$", "", best)[:70]


def ref_price(label):
    low = label.lower()
    for key, ref in REFERENCE_PRICES.items():
        if key in low:
            return ref
    return None


def price_cap(label, tolerance=None):
    ref = ref_price(label)
    return round(ref * (1 + (PRICE_TOLERANCE if tolerance is None else tolerance)), 2) if ref else None


def load_urls():
    items, seen = [], set()
    for line in URLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        url = parts[0]
        label = parts[1] if len(parts) > 1 and parts[1] else label_for(url)
        cart = parts[2] if len(parts) > 2 and parts[2] else None
        if url in seen:
            continue
        seen.add(url)
        items.append((url, label, cart))
    return items


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def norm_avail(value):
    v = str(value).strip().lower().rsplit("/", 1)[-1]
    return re.sub(r"[\s_\-]", "", v)


# ---------------------------------------------------------------- analyse
def _types(d):
    t = d.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def _find_products(node, out):
    if isinstance(node, dict):
        if {"Product", "ProductGroup"} & set(_types(node)):
            out.append(node)
        for v in node.values():
            _find_products(v, out)
    elif isinstance(node, list):
        for x in node:
            _find_products(x, out)


def _walk_offers(node, avails, prices):
    if isinstance(node, dict):
        for k, v in node.items():
            kl = k.lower()
            if kl == "availability" and isinstance(v, str):
                avails.append(v)
            elif kl in ("price", "lowprice") and isinstance(v, (str, int, float)):
                try:
                    prices.append(float(str(v).replace(",", ".")))
                except ValueError:
                    pass
            else:
                _walk_offers(v, avails, prices)
    elif isinstance(node, list):
        for x in node:
            _walk_offers(x, avails, prices)


PRICE_RE = re.compile(r"(\d{1,4}(?:[ \u00a0.]\d{3})*(?:[.,]\d{1,2})?)\s*(?:€|eur)", re.I)


def parse_price(value):
    """'199,00 €' / '1 199.99' / 64.99 -> float, ou None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    txt = str(value).replace("\u00a0", " ").strip()
    m = re.search(r"\d[\d .,]*", txt)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "")
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".") if num.rfind(",") > num.rfind(".") else num.replace(",", "")
    else:
        num = num.replace(",", ".")
        if num.count(".") > 1:
            head, _, tail = num.rpartition(".")
            num = head.replace(".", "") + "." + tail
    try:
        v = float(num)
        return v if 0 < v < 10000 else None
    except ValueError:
        return None


def visible_price(soup):
    """Dernier recours : premier prix affiché dans un élément « price/prix » (hors ancien prix barré)."""
    for el in soup.select('[class*="price"], [class*="prix"], [id*="price"], [id*="prix"]'):
        marks = " ".join(el.get("class", [])) + " " + (el.get("id") or "")
        if re.search(r"old|regular|barr|strike|was|before|ancien|unit", marks, re.I):
            continue
        if el.find_parent(["s", "del", "strike"]):
            continue
        m = PRICE_RE.search(el.get_text(" ", strip=True))
        if m:
            v = parse_price(m.group(1))
            if v:
                return v
    return None


def structured_signals(soup):
    avails, prices = [], []
    for s in soup.find_all("script", type="application/ld+json"):
        raw = s.string or s.get_text() or ""
        try:
            data = json.loads(raw, strict=False)
        except (json.JSONDecodeError, ValueError):
            continue
        products = []
        _find_products(data, products)
        for p in products:
            _walk_offers(p.get("offers", p), avails, prices)
    for tag in soup.select('[itemprop="availability"]'):
        v = tag.get("href") or tag.get("content")
        if v:
            avails.append(v)
    for prop in ("product:availability", "og:availability"):
        m = soup.find("meta", attrs={"property": prop})
        if m and m.get("content"):
            avails.append(m["content"])
    # Prix hors JSON-LD : microdonnées et balises meta
    for tag in soup.select('[itemprop="price"], [itemprop="lowPrice"]'):
        prices.append(tag.get("content") or tag.get_text(" ", strip=True))
    for prop in ("product:price:amount", "og:price:amount"):
        m = soup.find("meta", attrs={"property": prop})
        if m and m.get("content"):
            prices.append(m["content"])
    prices = [v for v in (parse_price(p) for p in prices) if v]
    return [SCHEMA_MAP.get(norm_avail(a)) for a in avails if SCHEMA_MAP.get(norm_avail(a))], prices


def find_cart_link(soup, base_url):
    """Cherche dans la page un lien GET d'ajout au panier (formulaire GET ou lien <a>)."""
    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if (form.get("method") or "get").lower() != "get":
            continue
        if not (re.search(r"(panier|cart)", action, re.I) or CART_HINT.search(" ".join(form.get("class", [])))):
            continue
        params = {i.get("name"): i.get("value", "") for i in form.find_all("input") if i.get("name")}
        if params:
            return urljoin(base_url, action) + ("&" if "?" in action else "?") + urlencode(params)
    for a in soup.find_all("a", href=True):
        if CART_HINT.search(a["href"]):
            return urljoin(base_url, a["href"])
    return None


def template_cart_link(url):
    tpl = CART_TEMPLATES.get(retailer(url))
    if not tpl:
        return None
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"\d{4,}", last)
    return tpl.format(id=m.group(0)) if m else None


def clean_dispo_hits(text):
    """Renvoie les extraits autour des « disponible » non niés."""
    hits = []
    for m in DISPO_RE.finditer(text):
        if m.group(1):  # « indisponible »
            continue
        if NEG_CONTEXT.search(text[max(0, m.start() - 30):m.start()]):
            continue
        hits.append(text[max(0, m.start() - 30):m.end() + 20].strip())
    return hits


def text_signal(soup):
    for t in soup(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    root = soup.find("main") or soup.body or soup
    text = re.sub(r"\s+", " ", root.get_text(" ")).lower().replace("’", "'")
    text = IGNORE_RE.sub(" ", text)
    hits = clean_dispo_hits(text)
    cart = any(p in text for p in ADD_TO_CART)
    neg = [p for p in NEGATIVE_PHRASES if p in text]
    extrait = f"texte : …{hits[0]}…" if hits else "texte"
    # Une mention de rupture l'emporte : certains sites affichent le bouton panier même en rupture
    if neg:
        return "INDISPO", f"texte : « {neg[0]} »"
    # Sans bouton panier, « disponible » seul ne suffit pas (ex. « disponible en drive »)
    if hits and cart:
        return "DISPO", extrait
    return "INCONNU", extrait


def coursesu_signal(soup):
    """Coursesu : bouton panier toujours affiché, prix masqué sans magasin choisi.
    Seul signal fiable : la mention « Bientôt disponible » (ou « indisponible »)."""
    text = re.sub(r"\s+", " ", soup.get_text(" ")).lower()
    for p in ("bientôt disponible", "indisponible", "rupture de stock", "épuisé"):
        if p in text:
            return "INDISPO", f"coursesu : « {p} »"
    if "ajouter à mon panier" in text or "ajouter au panier" in text:
        return "DISPO", "coursesu : « bientôt disponible » a disparu (stock variable selon magasin)"
    return "INCONNU", "coursesu"


RETAILER_RULES = {"coursesu": coursesu_signal}


def analyze(html, base_url=""):
    soup = BeautifulSoup(html, "html.parser")
    cart = find_cart_link(soup, base_url)
    rule = RETAILER_RULES.get(retailer(base_url)) if base_url else None
    if rule:
        for t in soup(["script", "style", "noscript", "template", "svg"]):
            t.decompose()
        status, source = rule(soup)
        return status, source, None, cart
    signals, prices = structured_signals(soup)
    price = min(prices) if prices else visible_price(soup)
    if signals:
        for st in ("DISPO", "PRECOMMANDE", "INDISPO"):
            if st in signals:
                return st, "schema", price, cart
    # Page dont le contenu (prix, stock, bouton) est injecté par JavaScript : le texte brut est trompeur
    if html.count("(=") > 10 or html.count("{{") > 20:
        return "INCONNU", "page chargée en JavaScript (stock illisible)", price, cart
    status, source = text_signal(soup)
    return status, source, price, cart


def check(url):
    last_exc = None
    for attempt in range(2):
        try:
            r = http.get(url, headers=HEADERS, timeout=30, allow_redirects=True, **FETCH_KW)
            break
        except Exception as e:  # réseau, timeout…
            last_exc = e
            time.sleep(3)
    else:
        return "ERREUR", f"{type(last_exc).__name__}", None, None

    if r.status_code in BLOCK_CODES:
        return "BLOQUE", f"HTTP {r.status_code}", None, None
    if r.status_code == 404:
        return "ERREUR", "HTTP 404 (page retirée ?)", None, None
    if r.status_code >= 400:
        return "ERREUR", f"HTTP {r.status_code}", None, None

    # Redirection hors de la fiche produit (fiche retirée, renvoi vers une catégorie ou l'accueil)
    asked, final = urlparse(url), urlparse(str(r.url))
    seg = lambda u: unquote(u.path.rstrip("/").rsplit("/", 1)[-1]).lower()
    if seg(final) != seg(asked):
        return "ERREUR", f"redirigé vers {final.path[:50] or '/'} (fiche retirée ?)", None, None

    html = r.text
    status, source, price, cart = analyze(html, str(r.url))
    if status == "INCONNU" and any(m in html.lower() for m in BLOCK_MARKERS):
        return "BLOQUE", "anti-bot", None, None
    return status, source, price, cart


# ---------------------------------------------------------------- notifications
def notify(title, message, url, cart_url=None):
    if DRY_RUN:
        print(f"  [DRY-RUN] {title} — {message}" + (f" — panier : {cart_url}" if cart_url else ""))
        return
    sent = False
    buttons = ([("🛒 Ajouter au panier", cart_url)] if cart_url else []) + [("Voir la page", url)]
    if NTFY_TOPIC:
        payload = {
            "topic": NTFY_TOPIC, "title": title, "message": message,
            "click": cart_url or url, "priority": 5, "tags": ["rotating_light"],
            "actions": [{"action": "view", "label": lbl, "url": u, "clear": True} for lbl, u in buttons],
        }
        for attempt in ("avec boutons", "sans boutons"):
            if attempt == "sans boutons":
                payload.pop("actions", None)
            try:
                r = http.post(NTFY_SERVER, json=payload, timeout=15)
                if r.status_code < 300:
                    print(f"  ntfy : envoyé ({attempt}, HTTP {r.status_code})")
                    sent = True
                    break
                print(f"  ! ntfy refusé ({attempt}) : HTTP {r.status_code} — {r.text[:200]}")
                if r.status_code == 429:
                    break  # quota ntfy.sh atteint : inutile de réessayer
            except Exception as e:
                print(f"  ! ntfy ({attempt}) : {type(e).__name__} {e}")
    if TG_TOKEN and TG_CHAT:
        try:
            r = http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT, "text": f"{title}\n{message}",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": lbl, "url": u}] for lbl, u in buttons]},
            }, timeout=15)
            ok = r.status_code < 300
            sent |= ok
            print(f"  telegram : {'envoyé' if ok else 'refusé'} (HTTP {r.status_code})" + ("" if ok else f" — {r.text[:200]}"))
        except Exception as e:
            print(f"  ! telegram : {e}")
    if not sent:
        print("  ! AUCUNE notification envoyée" + ("" if (NTFY_TOPIC or TG_TOKEN) else " : secret NTFY_TOPIC absent ou vide"))
    return sent


# ---------------------------------------------------------------- boucle
def interleave(items, state=None):
    """Répartit chaque enseigne régulièrement sur tout le tour, dans un ordre aléatoire.
    items : liste de (position, (url, libellé, lien panier))."""
    groups = {}
    for pos, item in items:
        groups.setdefault(retailer(item[0]), []).append((pos, item))
    keyed = []
    for g in groups.values():
        random.shuffle(g)
        if state:  # les pages vérifiées depuis le plus longtemps passent en premier
            g.sort(key=lambda e: state.get(e[1][0], {}).get("checked", ""))
        offset = random.random()
        for k, entry in enumerate(g):
            keyed.append(((k + offset) / len(g), random.random(), entry))
    keyed.sort(key=lambda x: (x[0], x[1]))
    return [entry for _, _, entry in keyed]


def main():
    urls = load_urls()
    if TEST_NOTIF:
        url, label, cart = next(((u, l, c) for u, l, c in urls if c or template_cart_link(u)), urls[0])
        cart = cart or template_cart_link(url)
        notify(f"TEST — {retailer(url).upper()}", f"{label} — notification de test", url, cart)
        print(f"Notification de test envoyée : {label}" + (f" (panier : {cart})" if cart else " (sans lien panier)"))
        return
    state = load_state()
    rows = []
    now = time.time()
    due, later = [], []
    for pos, item in enumerate(urls):
        (due if state.get(item[0], {}).get("next_check", 0) <= now else later).append((pos, item))
    print(f"Veille — {len(due)} pages à vérifier, {len(later)} en pause (anti-bot, fiche retirée ou exclue) — {now_iso()}")

    # Rotation : chaque passage (toutes les 5 min) prend les 2/3 des pages, les plus anciennes d'abord,
    # soit une vérification toutes les 5 ou 10 min en alternance : 7,5 min en moyenne par page.
    due.sort(key=lambda e: state.get(e[1][0], {}).get("checked", ""))
    quota = math.ceil(len(due) * RUN_INTERVAL_MIN / TARGET_INTERVAL_MIN)
    selected = due[:quota]
    print(f"  Rotation : {len(selected)} pages ce passage (objectif : chaque page toutes les {TARGET_INTERVAL_MIN:g} min)")
    order = interleave(selected, state)
    # Pause calculée pour étaler les requêtes régulièrement sur ~90 % du budget de temps
    slot = TIME_BUDGET * 0.9 / max(len(order), 1)
    start = time.monotonic()
    visited = set()
    last_visit = {}
    for i, (pos, (url, label, cart_manual)) in enumerate(order):
        wait = MIN_SITE_GAP - (time.monotonic() - last_visit.get(retailer(url), -1e9))
        if wait > 0:
            time.sleep(wait)
        if time.monotonic() - start > TIME_BUDGET:
            print(f"  Budget de {TIME_BUDGET:.0f} s atteint : {len(order) - i} pages reportées au tour suivant")
            break
        last_visit[retailer(url)] = time.monotonic()
        t_req = time.monotonic()
        status, source, price, cart_found = check(url)
        took = time.monotonic() - t_req
        visited.add(url)
        cart_url = cart_manual or cart_found or template_cart_link(url)
        cart_src = "manuel" if cart_manual else "détecté" if cart_found else "modèle" if cart_url else ""
        shop = retailer(url)
        prev = state.get(url, {})
        last_known = prev.get("last_known")

        cap = price_cap(label)
        excl = price_cap(label, EXCLUDE_TOLERANCE)
        exclude = bool(excl and price and price > excl)
        if exclude:
            # Site trop cher (> +30 %) : écarté, revérifié une fois par jour au cas où le prix baisse
            status, source = "EXCLU", f"{price:.2f} € > seuil d'exclusion {excl:.2f} €"
        elif status in AVAILABLE and cap and price and price > cap:
            status, source = "TROP_CHER", f"{source} — plafond {cap:.2f} €"
        elif status in AVAILABLE and cap and not price and shop not in TRUSTED_RETAILERS:
            status, source = "PRIX_INCONNU", f"{source} — prix illisible, pas d'alerte (vérifie via le lien)"
        redirected = status == "ERREUR" and str(source).startswith("redirigé")

        if status in KNOWN:
            if status in AVAILABLE and last_known not in AVAILABLE:
                prix = f" — {price:.2f} €" if price else ""
                verb = "en précommande" if status == "PRECOMMANDE" else "DISPONIBLE"
                notify(f"{shop.upper()} : {verb}", f"{label}{prix}", url, cart_url)
            last_known = status

        # Page bloquée : on espace les essais (5, 10, 20, 40, 60 min) pour ne pas insister
        if status == "BLOQUE":
            blocked_streak = prev.get("blocked_streak", 0) + 1
            wait_min = min(5 * 2 ** (blocked_streak - 1), BACKOFF_MAX_MIN)
            next_check = now + wait_min * 60 - 60  # relatif au début du tour
            blocked_since = prev.get("blocked_since") or time.time()
            blocked_alerted = prev.get("blocked_alerted", False)
            if NOTIFY_BLOCKED and not blocked_alerted and time.time() - blocked_since >= BLOCKED_ALERT_HOURS * 3600:
                notify(f"{shop.upper()} : bloqué depuis {BLOCKED_ALERT_HOURS:.0f} h", label, url)
                blocked_alerted = True
        else:
            blocked_streak, next_check, blocked_since, blocked_alerted = 0, 0, None, False
            if exclude:
                next_check = now + EXCLUDE_RECHECK_H * 3600
            elif redirected:
                next_check = now + 3600  # fiche retirée : on revérifie toutes les heures

        state[url] = {
            "label": label, "status": status, "source": source, "price": price, "cart": cart_url,
            "last_known": last_known, "blocked_streak": blocked_streak, "next_check": next_check,
            "blocked_since": blocked_since, "blocked_alerted": blocked_alerted, "checked": now_iso(),
        }
        rows.append((pos, shop, label, status, source, price, cart_src, url, cart_url))
        print(f"  {status:<12} {shop:<14} {label}  [{source}]" + (f"  {price:.2f} €" if price else "") + (f"  panier:{cart_src}" if cart_src else ""))

        if i < len(order) - 1:
            pause = max(PAUSE_MIN, min(PAUSE_MAX, slot - took)) * random.uniform(0.8, 1.2)
            time.sleep(pause)

    # Pages non visitées ce tour-ci : on reprend leur dernier état pour le récapitulatif
    for pos, (url, label, _) in enumerate(urls):
        if url in visited:
            continue
        prev = state.get(url, {})
        nc = prev.get("next_check", 0)
        hhmm = datetime.fromtimestamp(nc, timezone.utc).strftime('%d/%m %H:%M') if nc else ""
        if nc > now and prev.get("status") == "EXCLU":
            why = f"{prev.get('source', 'trop cher')} — revu le {hhmm} UTC"
        elif nc > now:
            why = f"en pause jusqu'au {hhmm} UTC"
        else:
            why = f"au prochain passage (rotation {TARGET_INTERVAL_MIN:g} min)"
        rows.append((pos, retailer(url), label, prev.get("status", "—"), why, None, "", url, prev.get("cart")))

    # purge des URL retirées de urls.txt
    active = {u for u, *_ in urls}
    state = {u: v for u, v in state.items() if u in active}
    save_state(state)

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Veille stock — {now_iso()}\n\n| Statut | Enseigne | Produit | Source | Prix | Page | Lien panier |\n|---|---|---|---|---|---|---|\n")
            for _, shop, label, status, source, price, cart_src, url, cart_url in sorted(rows, key=lambda r: r[0]):
                src = str(source).replace("|", "/")
                page = f"[Ouvrir]({url})"
                panier = f"[{cart_src or 'panier'}]({cart_url})" if cart_url else ""
                f.write(f"| {status} | {shop} | {label} | {src} | {f'{price:.2f} €' if price else ''} | {page} | {panier} |\n")


if __name__ == "__main__":
    main()
