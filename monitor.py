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
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

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
BLOCKED_ALERT_AFTER = int(os.getenv("BLOCKED_ALERT_AFTER", "288"))  # 288 passages x 5 min = 24 h
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
KNOWN = {"DISPO", "PRECOMMANDE", "INDISPO"}

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
)
ADD_TO_CART = ("ajouter au panier", "ajout au panier", "acheter maintenant")


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
    hits = clean_dispo_hits(text)
    cart = any(p in text for p in ADD_TO_CART)
    neg = any(p in text for p in NEGATIVE_PHRASES)
    extrait = f"texte : …{hits[0]}…" if hits else "texte"
    if neg and not cart:
        return "INDISPO", "texte"
    # Sans bouton panier, « disponible » seul ne suffit plus (ex. « disponible en drive »)
    if hits and cart:
        return "DISPO", extrait
    return "INCONNU", extrait


def analyze(html, base_url=""):
    soup = BeautifulSoup(html, "html.parser")
    cart = find_cart_link(soup, base_url)
    signals, prices = structured_signals(soup)
    price = min(prices) if prices else None
    if signals:
        for st in ("DISPO", "PRECOMMANDE", "INDISPO"):
            if st in signals:
                return st, "schema", price, cart
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
        try:
            r = http.post(NTFY_SERVER, json={
                "topic": NTFY_TOPIC, "title": title, "message": message,
                "click": cart_url or url, "priority": 5, "tags": ["rotating_light"],
                "actions": [{"action": "view", "label": lbl, "url": u, "clear": True} for lbl, u in buttons],
            }, timeout=15)
            sent |= r.status_code < 300
        except Exception as e:
            print(f"  ! ntfy : {e}")
    if TG_TOKEN and TG_CHAT:
        try:
            r = http.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT, "text": f"{title}\n{message}",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": [[{"text": lbl, "url": u}] for lbl, u in buttons]},
            }, timeout=15)
            sent |= r.status_code < 300
        except Exception as e:
            print(f"  ! telegram : {e}")
    if not sent:
        print("  ! aucune notification envoyée (NTFY_TOPIC / TELEGRAM_* non configurés ?)")


# ---------------------------------------------------------------- boucle
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
    print(f"Veille — {len(urls)} pages — {now_iso()}")

    for i, (url, label, cart_manual) in enumerate(urls):
        status, source, price, cart_found = check(url)
        cart_url = cart_manual or cart_found or template_cart_link(url)
        cart_src = "manuel" if cart_manual else "détecté" if cart_found else "modèle" if cart_url else ""
        shop = retailer(url)
        prev = state.get(url, {})
        last_known = prev.get("last_known")

        if status in KNOWN:
            if status in AVAILABLE and last_known not in AVAILABLE:
                prix = f" — {price:.2f} €" if price else ""
                verb = "en précommande" if status == "PRECOMMANDE" else "DISPONIBLE"
                notify(f"{shop.upper()} : {verb}", f"{label}{prix}", url, cart_url)
            last_known = status

        blocked_streak = prev.get("blocked_streak", 0) + 1 if status == "BLOQUE" else 0
        if NOTIFY_BLOCKED and blocked_streak == BLOCKED_ALERT_AFTER:
            notify(f"{shop.upper()} : bloqué depuis 24 h", label, url)

        state[url] = {
            "label": label, "status": status, "source": source, "price": price, "cart": cart_url,
            "last_known": last_known, "blocked_streak": blocked_streak, "checked": now_iso(),
        }
        rows.append((shop, label, status, source, price, cart_src))
        print(f"  {status:<12} {shop:<14} {label}  [{source}]" + (f"  {price:.2f} €" if price else "") + (f"  panier:{cart_src}" if cart_src else ""))

        if i < len(urls) - 1:
            time.sleep(random.uniform(1.5, 3.5))

    # purge des URL retirées de urls.txt
    active = {u for u, _ in urls}
    state = {u: v for u, v in state.items() if u in active}
    save_state(state)

    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"### Veille stock — {now_iso()}\n\n| Statut | Enseigne | Produit | Source | Prix | Lien panier |\n|---|---|---|---|---|---|\n")
            for shop, label, status, source, price, cart_src in rows:
                src = str(source).replace("|", "/")
                f.write(f"| {status} | {shop} | {label} | {src} | {f'{price:.2f} €' if price else ''} | {cart_src} |\n")


if __name__ == "__main__":
    main()
