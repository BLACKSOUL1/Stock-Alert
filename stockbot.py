#!/usr/bin/env python3
"""
Stock alert bot.

Watches product pages and sends a Telegram message when an item comes back in stock.

Usage:
    python stockbot.py                 # run forever (what the VPS service does)
    python stockbot.py --once          # check every product once and exit (cron / GitHub Actions)
    python stockbot.py --check URL     # debug: show how the bot reads one page
    python stockbot.py --test-telegram # send a test message
"""
import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get("STOCKBOT_CONFIG", BASE_DIR / "config.yaml"))
STATE_FILE = Path(os.environ.get("STOCKBOT_STATE", BASE_DIR / "state.json"))
TELEGRAM_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")

IN, OUT = "in", "out"

log = logging.getLogger("stockbot")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (without overriding)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("jitter_seconds", 10)
    cfg.setdefault("request_timeout_seconds", 20)
    cfg.setdefault("notify_when_sold_out_again", False)
    cfg.setdefault("repeat_alert_minutes", 0)
    cfg.setdefault("warn_after_failures", 5)
    cfg.setdefault("preorder_counts_as_in_stock", True)
    cfg.setdefault("products", [])
    if not cfg["products"]:
        raise SystemExit("config.yaml has no products. Add at least one under 'products:'.")
    for p in cfg["products"]:
        if not p.get("url"):
            raise SystemExit(f"A product in config.yaml is missing 'url': {p}")
    return cfg


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; cannot send: %s", text)
        return False
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
            if r.status_code == 200:
                return True
            log.warning("Telegram returned %s: %s", r.status_code, r.text[:200])
            if r.status_code in (400, 401, 403, 404):  # bad token / chat id: retrying won't help
                return False
        except requests.RequestException as e:
            log.warning("Telegram request failed (attempt %d): %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))
    return False


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


BLOCK_MARKERS = (
    "captcha",
    "are you a robot",
    "robot check",
    "unusual traffic",
    "access denied",
    "verify you are human",
    "just a moment...",
)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
    )
    return s


def fetch(session: requests.Session, url: str, timeout: int, headers: dict | None = None) -> str:
    try:
        r = session.get(url, timeout=timeout, headers=headers or {})
    except requests.RequestException as e:
        raise FetchError(f"network error: {e}") from e
    if r.status_code in (403, 429, 503):
        raise FetchError(f"HTTP {r.status_code} (site may be blocking the bot or rate limiting)")
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}")
    html = r.text
    head = html[:4000].lower()
    if any(m in head for m in BLOCK_MARKERS) and len(html) < 20000:
        raise FetchError("looks like a bot-check / captcha page")
    return html


# --------------------------------------------------------------------------- #
# Stock detection
# --------------------------------------------------------------------------- #
IN_SCHEMA = {"instock", "limitedavailability", "onlineonly", "instoreonly"}
OUT_SCHEMA = {"outofstock", "soldout", "discontinued"}
PREORDER_SCHEMA = {"preorder", "presale", "backorder"}

OUT_PHRASES = (
    "out of stock",
    "sold out",
    "currently unavailable",
    "temporarily unavailable",
    "notify me when",
    "email me when available",
    "back in stock soon",
)
ADD_TO_CART_RE = re.compile(r"\b(add to (cart|bag|basket|trolley)|buy now|buy it now|add to order)\b", re.I)
OUT_BUTTON_RE = re.compile(r"\b(sold out|out of stock|unavailable|notify me|coming soon)\b", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z]", "", str(value).lower().rsplit("/", 1)[-1])


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _schema_availabilities(soup: BeautifulSoup) -> list[str]:
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            if "availability" in node and isinstance(node["availability"], str):
                found.append(_norm(node["availability"]))
    # Microdata: <link itemprop="availability" href="https://schema.org/InStock">
    for tag in soup.find_all(attrs={"itemprop": "availability"}):
        val = tag.get("href") or tag.get("content") or tag.get_text()
        if val:
            found.append(_norm(val))
    return found


def _meta_availability(soup: BeautifulSoup) -> list[str]:
    found = []
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").lower()
        if key in ("product:availability", "og:availability", "twitter:data2") and tag.get("content"):
            found.append(_norm(tag["content"]))
    return found


def _is_disabled(el) -> bool:
    cls = " ".join(el.get("class", [])).lower()
    return el.has_attr("disabled") or el.get("aria-disabled") == "true" or "disabled" in cls


# Per-site presets, applied automatically when the product URL is on that site.
# FirstCry's pages are mostly rendered by JavaScript and contain generic "Add to cart" and
# "Notify me" buttons whatever the stock is, so the generic detection would be wrong there.
# Their page source carries the live stock count as "CurSt":<number> (0 = out of stock).
DOMAIN_PRESETS = {
    "firstcry.com": {"stock_number_regex": r'"CurSt"\s*:\s*(\d+)'},
}


def preset_for(url: str) -> dict:
    host = urlparse(url).netloc.lower()
    for domain, preset in DOMAIN_PRESETS.items():
        if host == domain or host.endswith("." + domain):
            return preset
    return {}


def with_preset(product: dict) -> dict:
    """Product settings from config.yaml, on top of the site preset (config wins)."""
    return {**preset_for(product["url"]), **product}


def _clean_title(title: str) -> str:
    return re.split(r"\s+(?:for \(|Online India|Buy at )", title)[0].strip()


def detect(html: str, rule: dict, preorder_in: bool = True) -> tuple[str | None, str, str]:
    """Return (status, reason, title). status is 'in', 'out' or None (could not tell)."""
    status, reason, title, _ = detect_full(html, rule, preorder_in)
    return status, reason, title


def detect_full(html: str, rule: dict, preorder_in: bool = True) -> tuple[str | None, str, str, int | None]:
    """Return (status, reason, title, quantity). quantity is only known for stock_number_regex rules."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = _clean_title(title)

    # ---- 0. A number in the page source that means "units in stock" ----
    pattern = rule.get("stock_number_regex")
    if pattern:
        m = re.search(pattern, html)
        if not m:
            if "has been discontinued" in html.lower():
                return OUT, "the shop says this product has been discontinued", title, 0
            return None, f"stock_number_regex /{pattern}/ not found in page (blocked, or site layout changed)", title, None
        qty = int(m.group(1))
        status = IN if qty > 0 else OUT
        return status, f"stock count in page source is {qty}", title, qty

    status, reason, title2 = _detect_rules(soup, rule, preorder_in, title)
    return status, reason, title2, None


def _detect_rules(soup: BeautifulSoup, rule: dict, preorder_in: bool, title: str) -> tuple[str | None, str, str]:
    text = soup.get_text(" ", strip=True).lower()

    def has_text(phrases) -> str | None:
        for ph in phrases or []:
            if ph.lower() in text:
                return ph
        return None

    # ---- 1. Your explicit rules (most reliable; set these per product) ----
    hit = has_text(rule.get("out_of_stock_text"))
    if hit:
        return OUT, f"page contains out-of-stock text '{hit}'", title

    sel = rule.get("out_of_stock_selector")
    if sel and soup.select_one(sel):
        return OUT, f"out-of-stock selector '{sel}' matched", title

    sel = rule.get("in_stock_selector")
    if sel:
        el = soup.select_one(sel)
        if el is not None and not _is_disabled(el):
            return IN, f"in-stock selector '{sel}' matched", title
        return OUT, f"in-stock selector '{sel}' not found (or disabled)", title

    if rule.get("in_stock_text"):
        hit = has_text(rule["in_stock_text"])
        if hit:
            return IN, f"page contains in-stock text '{hit}'", title
        return OUT, "none of the in_stock_text phrases found", title

    if rule.get("out_of_stock_text") or rule.get("out_of_stock_selector"):
        return IN, "out-of-stock rules did not match, so assuming in stock", title

    # ---- 2. Automatic detection (works on many shops without any setup) ----
    avail = _schema_availabilities(soup) + _meta_availability(soup)
    if avail:
        if any(a in IN_SCHEMA for a in avail):
            return IN, f"structured data says {avail}", title
        if any(a in PREORDER_SCHEMA for a in avail):
            return (IN if preorder_in else OUT), f"structured data says {avail} (pre-order)", title
        if any(a in OUT_SCHEMA for a in avail):
            return OUT, f"structured data says {avail}", title

    buttons = soup.find_all(["button", "input", "a"])
    saw_out_button = False
    for b in buttons:
        label = b.get_text(" ", strip=True) or b.get("value", "") or b.get("aria-label", "")
        if not label or len(label) > 60:
            continue
        if ADD_TO_CART_RE.search(label) and not _is_disabled(b):
            return IN, f"found enabled button '{label}'", title
        if OUT_BUTTON_RE.search(label) or (ADD_TO_CART_RE.search(label) and _is_disabled(b)):
            saw_out_button = True
    if saw_out_button:
        return OUT, "found a sold-out / disabled button", title

    hit = has_text(OUT_PHRASES)
    if hit:
        return OUT, f"page text contains '{hit}' (low confidence; add rules to config.yaml)", title

    return None, "could not tell from the page; add in_stock_selector / out_of_stock_text in config.yaml", title


# --------------------------------------------------------------------------- #
# Main logic
# --------------------------------------------------------------------------- #
def check_product(session, product: dict, cfg: dict) -> tuple[str | None, str, str, int | None]:
    rule = with_preset(product)
    html = fetch(
        session,
        product["url"],
        cfg["request_timeout_seconds"],
        rule.get("headers"),
    )
    return detect_full(html, rule, cfg["preorder_counts_as_in_stock"])


def process(product: dict, cfg: dict, state: dict, session) -> None:
    url = product["url"]
    st = state.setdefault(url, {"status": None, "fails": 0, "warned": False, "last_alert": 0})
    host = urlparse(url).netloc
    error = None
    status, reason, title, qty = None, "", "", None
    try:
        status, reason, title, qty = check_product(session, product, cfg)
    except FetchError as e:
        error = str(e)

    name = product.get("name") or title or url

    if error or status is None:
        st["fails"] += 1
        problem = error or reason
        log.warning("[%s] %s: %s (fail #%d)", host, name, problem, st["fails"])
        if st["fails"] >= cfg["warn_after_failures"] and not st["warned"]:
            send_telegram(
                f"⚠️ Stock bot can't read this page ({st['fails']} tries in a row):\n"
                f"{name}\n{url}\nProblem: {problem}"
            )
            st["warned"] = True
        return

    if st["warned"]:
        send_telegram(f"✅ Stock bot can read this page again:\n{name}")
    st["fails"], st["warned"] = 0, False

    prev = st["status"]
    log.info("[%s] %s -> %s (%s)", host, name, status.upper(), reason)
    now = time.time()

    if status == IN:
        repeat = cfg["repeat_alert_minutes"] * 60
        due_repeat = repeat and prev == IN and now - st["last_alert"] >= repeat
        if prev != IN or due_repeat:
            left = f" ({qty} available)" if qty is not None else ""
            ok = send_telegram(f"🟢 IN STOCK{left}: {name}\n{url}")
            if ok:
                st["last_alert"] = now
            else:
                st["status"] = prev  # try again next round instead of losing the alert
                return
    elif status == OUT and prev == IN and cfg["notify_when_sold_out_again"]:
        send_telegram(f"🔴 Sold out again: {name}")

    st["status"] = status


def run_forever(cfg: dict, once: bool = False) -> None:
    state = load_state()
    session = make_session()
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    products = cfg["products"]
    if not once:
        send_telegram(f"✅ Stock bot started. Watching {len(products)} product(s).")

    next_due = {p["url"]: 0.0 for p in products}
    while not stop["flag"]:
        now = time.time()
        for p in products:
            if stop["flag"]:
                break
            if once or now >= next_due[p["url"]]:
                process(p, cfg, state, session)
                save_state(state)
                interval = p.get("interval_seconds", cfg["interval_seconds"])
                next_due[p["url"]] = time.time() + interval + random.uniform(0, cfg["jitter_seconds"])
                time.sleep(random.uniform(0.5, 2.0))  # small gap between different pages
        if once:
            return
        wait = max(1.0, min(next_due.values()) - time.time())
        # sleep in short slices so SIGTERM stops us quickly
        end = time.time() + wait
        while time.time() < end and not stop["flag"]:
            time.sleep(min(1.0, end - time.time()))
    log.info("Stopping.")


def cmd_check(url: str, cfg: dict | None) -> int:
    rule = {}
    preorder_in = True
    timeout = 20
    if cfg:
        preorder_in = cfg["preorder_counts_as_in_stock"]
        timeout = cfg["request_timeout_seconds"]
        for p in cfg["products"]:
            if p["url"] == url:
                rule = p
    rule = with_preset({**rule, "url": url})
    session = make_session()
    try:
        html = fetch(session, url, timeout, rule.get("headers"))
    except FetchError as e:
        print(f"FETCH FAILED: {e}")
        return 2
    status, reason, title, _ = detect_full(html, rule, preorder_in)
    print(f"Title : {title}")
    print(f"Status: {status.upper() if status else 'UNKNOWN'}")
    print(f"Why   : {reason}")
    return 0 if status else 1


def main() -> int:
    load_env_file(BASE_DIR / ".env")
    ap = argparse.ArgumentParser(description="Stock alert bot")
    ap.add_argument("--once", action="store_true", help="check each product once, then exit")
    ap.add_argument("--check", metavar="URL", help="show how the bot reads a single page")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.test_telegram:
        ok = send_telegram("🔔 Test message from your stock bot. If you can read this, alerts work.")
        print("Sent." if ok else "FAILED: check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        return 0 if ok else 1

    if args.check:
        cfg = load_config() if CONFIG_FILE.exists() else None
        return cmd_check(args.check, cfg)

    cfg = load_config()
    run_forever(cfg, once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
