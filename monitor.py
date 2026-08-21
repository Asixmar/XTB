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
GEMINI_MODEL = "gemini-2.5-flash"   # stabilny i w darmowym limicie; nowszy: gemini-3.6-flash, najtańszy: gemini-3.5-flash-lite

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

def ai_digest_pl(items):
    """Buduje polski przegląd z wyjaśnieniami przez Gemini API. Zwraca tekst albo None."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None  # brak klucza -> awaryjnie użyjemy nagłówków po angielsku

    blocks = []
    for it in items:
        if it["wk"] is None:
            hdr = f"{it['name']} | (brak danych o zmianie) (tydz.)"
        else:
            emoji = "🟢" if it["wk"] >= 0 else "🔴"
            hdr = f"{it['name']} | {emoji} {it['wk']:+.1f}% (tydz.)"
        news = "\n".join(f"- {h}" for h in it["headlines"]) or "- (brak newsów)"
        blocks.append(hdr + "\n" + news)
    data = "\n\n".join(blocks)

    system = (
        "Jesteś asystentem, który objaśnia newsy giełdowe początkującemu inwestorowi. "
        "Piszesz wyłącznie po polsku, prosto, zwięźle, bez żargonu. "
        "Nie doradzasz kupna ani sprzedaży — tylko wyjaśniasz."
    )
    prompt = (
        "Dostajesz spółki z portfela, ich tygodniową zmianę ceny i nagłówki newsów po angielsku.\n"
        "Zwróć przegląd PO POLSKU. Dla każdej spółki użyj DOKŁADNIE podanego nagłówka z emoji i liczbą "
        "(nie zmieniaj liczb ani nazw), a pod nim dla każdego newsa dwie linie:\n"
        "• <krótka polska parafraza nagłówka>\n"
        "   ↳ <1-2 zdania: co ta wiadomość oznacza i jak MOŻE wpłynąć na wycenę akcji, prosto dla laika>\n"
        "Jeśli spółka nie ma newsów, wpisz tylko: • brak świeżych newsów\n"
        "Nie dodawaj żadnego wstępu, tytułu ani podsumowania na końcu.\n\n"
        f"Dane:\n{data}"
    )
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 1600, "temperature": 0.4},
            },
            timeout=60,
        )
        if r.status_code != 200:
            print("Gemini API:", r.status_code, r.text[:200])
            return None
        cand = r.json().get("candidates", [])
        if not cand:
            print("Gemini: brak odpowiedzi (możliwa blokada treści)")
            return None
        parts = cand[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        print("Gemini błąd:", e)
        return None


def run_weekly(holdings):
    items = []
    for h in holdings:
        closes = price_history(h["ticker"])
        items.append({
            "name": h["name"],
            "wk": pct_change(closes, 5),
            "headlines": [n["title"] for n in recent_news(h["ticker"])],
        })

    today = dt.date.today().strftime("%d.%m")

    ai = ai_digest_pl(items)
    if ai:
        lines = ai.splitlines() + ["", "ℹ️ To nie porada inwestycyjna — tylko wyjaśnienie newsów."]
        send_long(f"📊 Przegląd portfela {today}", lines)
        return

    # AWARYJNIE (brak GEMINI_API_KEY lub błąd API): stary format z nagłówkami po angielsku
    lines = []
    for it in items:
        head = it["name"]
        if it["wk"] is not None:
            head += f"  {'🟢' if it['wk'] >= 0 else '🔴'} {it['wk']:+.1f}% (tydz.)"
        lines.append(head)
        if it["headlines"]:
            for t in it["headlines"]:
                lines.append(f"  • {t}")
        else:
            lines.append("  • brak świeżych newsów")
        lines.append("")
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
