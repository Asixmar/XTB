#!/usr/bin/env python3
"""
Portfel IKE — monitor.
Dwa tryby (ustawiane zmienną środowiskową MODE):
  MODE=weekly  -> raz w tygodniu: brief z najważniejszymi newsami + tygodniowa zmiana ceny
  MODE=alert   -> codziennie: sprawdza ruch ceny i przy zmianie >= PROGU wysyła push

Powiadomienia: domyślnie Telegram (bot wysyła wiadomość jak czat -> push na iPhone jak SMS).
Alternatywy (Pushover / ntfy) są niżej, w funkcji notify() — wystarczy odkomentować.

Uruchamiane automatycznie przez GitHub Actions (patrz .github/workflows/monitor.yml).
"""

import os
import csv
import json
import sys
import datetime as dt

import requests
import yfinance as yf

# ------------------------- USTAWIENIA -------------------------

THRESHOLD_PCT = 20.0        # próg alertu (%). Zmień na 15 / 10 jeśli chcesz częściej.
LOOKBACKS = [1, 5]          # okna do sprawdzania ruchu: 1 sesja i ~1 tydzień (5 sesji)
SUPPRESS_DAYS = 3           # nie powtarzaj tego samego alertu dla spółki przez X dni
NEWS_PER_TICKER = 2         # ile newsów na spółkę w tygodniowym briefie
NEWS_MAX_AGE_DAYS = 8       # newsy starsze niż tyle dni pomijamy
STATE_FILE = "alert_state.json"
HOLDINGS_FILE = "holdings.csv"

# --------------------------------------------------------------


def load_holdings():
    """Wczytuje holdings.csv: kolumny ticker,name (name opcjonalne)."""
    rows = []
    with open(HOLDINGS_FILE, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip()
            if not t:
                continue
            rows.append({"ticker": t, "name": (r.get("name") or t).strip()})
    if not rows:
        print("Brak spółek w holdings.csv"); sys.exit(0)
    return rows


# ------------------------- POWIADOMIENIA -------------------------

def notify(title, message, url=None):
    """Wysyła wiadomość. Domyślnie Telegram (bot -> czat na Twoim telefonie)."""
    bot = os.environ.get("TG_BOT_TOKEN")
    chat = os.environ.get("TG_CHAT_ID")
    if not bot or not chat:
        print("!! Brak TG_BOT_TOKEN / TG_CHAT_ID — ustaw sekrety w GitHub.")
        print(f"[DRY RUN] {title}\n{message}")
        return
    text = f"{title}\n\n{message}"
    if url:
        text += f"\n{url}"
    r = requests.post(
        f"https://api.telegram.org/bot{bot}/sendMessage",
        data={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    print("Telegram:", r.status_code, r.text[:120])

    # --- Pushover (odkomentuj, jeśli wolisz zamiast Telegrama) ---
    # data = {"token": os.environ["PUSHOVER_TOKEN"], "user": os.environ["PUSHOVER_USER"],
    #         "title": title, "message": message}
    # if url: data["url"] = url
    # requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=20)

    # --- ntfy (odkomentuj, jeśli wolisz zamiast Telegrama) ---
    # topic = os.environ["NTFY_TOPIC"]  # np. "moj-portfel-xyz123"
    # requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
    #               headers={"Title": title, "Click": url or ""}, timeout=20)


def send_long(title, lines, url=None):
    """Dzieli długi brief na kilka wiadomości (limit Telegrama to 4096 znaków)."""
    chunk, size, part = [], 0, 1
    for ln in lines:
        if size + len(ln) > 3500 and chunk:
            notify(f"{title} ({part})", "\n".join(chunk), url)
            chunk, size, part = [], 0, part + 1
        chunk.append(ln); size += len(ln) + 1
    if chunk:
        suffix = f" ({part})" if part > 1 else ""
        notify(f"{title}{suffix}", "\n".join(chunk), url)


# ------------------------- DANE -------------------------

def price_history(ticker):
    """Zwraca listę dziennych zamknięć (najstarsze -> najnowsze) lub []."""
    try:
        h = yf.Ticker(ticker).history(period="1mo", interval="1d")
        closes = [float(x) for x in h["Close"].dropna().tolist()]
        return closes
    except Exception as e:
        print(f"  cena {ticker}: błąd {e}")
        return []


def pct_change(closes, lookback):
    if len(closes) <= lookback:
        return None
    old, new = closes[-1 - lookback], closes[-1]
    if not old:
        return None
    return (new - old) / old * 100.0


def recent_news(ticker):
    """Defensywnie wyciąga newsy (schemat yfinance bywa różny w wersjach)."""
    try:
        items = yf.Ticker(ticker).news or []
    except Exception:
        return []
    out = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=NEWS_MAX_AGE_DAYS)
    for it in items:
        c = it.get("content", it)  # nowszy yfinance zagnieżdża w "content"
        title = c.get("title") or it.get("title")
        if not title:
            continue
        # data publikacji (różne pola)
        ts = it.get("providerPublishTime")
        when = None
        if ts:
            when = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
        else:
            pd = c.get("pubDate") or c.get("displayTime")
            if pd:
                try:
                    when = dt.datetime.fromisoformat(pd.replace("Z", "+00:00"))
                except Exception:
                    when = None
        if when and when < cutoff:
            continue
        link = ""
        if isinstance(c.get("canonicalUrl"), dict):
            link = c["canonicalUrl"].get("url", "")
        link = link or it.get("link") or ""
        out.append({"title": title.strip(), "link": link})
        if len(out) >= NEWS_PER_TICKER:
            break
    return out


# ------------------------- TRYBY -------------------------

def run_weekly(holdings):
    lines = []
    for h in holdings:
        t = h["ticker"]
        closes = price_history(t)
        wk = pct_change(closes, 5)
        head = h["name"]
        if wk is not None:
            arrow = "🟢" if wk >= 0 else "🔴"
            head += f"  {arrow} {wk:+.1f}% (tydz.)"
        lines.append(head)
        news = recent_news(t)
        if news:
            for n in news:
                lines.append(f"  • {n['title']}")
        else:
            lines.append("  • brak świeżych newsów")
        lines.append("")
    if not lines:
        return
    today = dt.date.today().strftime("%d.%m")
    send_long(f"📊 Przegląd portfela {today}", lines)


def run_alert(holdings):
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            state = json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            state = {}
    today = dt.date.today().isoformat()
    changed = False

    for h in holdings:
        t = h["ticker"]
        closes = price_history(t)
        if len(closes) < 2:
            continue
        hit = None
        for lb in LOOKBACKS:
            ch = pct_change(closes, lb)
            if ch is not None and abs(ch) >= THRESHOLD_PCT:
                # bierzemy największy ruch
                if hit is None or abs(ch) > abs(hit[1]):
                    hit = (lb, ch)
        if not hit:
            continue

        # dedup: nie powtarzaj tego samego alertu przez SUPPRESS_DAYS
        last = state.get(t, {}).get("date")
        if last:
            try:
                gap = (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days
                if gap < SUPPRESS_DAYS:
                    continue
            except Exception:
                pass

        lb, ch = hit
        window = "dziś" if lb == 1 else f"{lb} sesji"
        arrow = "🚀" if ch >= 0 else "⚠️"
        price = closes[-1]
        notify(
            f"{arrow} {h['name']}: {ch:+.1f}%",
            f"Duży ruch ({window}). Kurs: {price:.2f}.\nSprawdź co się dzieje w {t}.",
        )
        state[t] = {"date": today, "pct": round(ch, 1)}
        changed = True

    if changed:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    mode = os.environ.get("MODE", "alert").lower()
    holdings = load_holdings()
    print(f"Tryb: {mode}, spółek: {len(holdings)}")
    if mode == "weekly":
        run_weekly(holdings)
    else:
        run_alert(holdings)


if __name__ == "__main__":
    main()
