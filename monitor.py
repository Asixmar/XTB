"""
Miesięczny raport portfela -> Telegram
=======================================

Raz w miesiącu dla każdej spółki z holdings.csv:
  1. Kurs: zmiana w ostatnim miesiącu, 3 największe dzienne ruchy, 52-tyg. max/min.
  2. Newsy z największych serwisów finansowych (Bloomberg, WSJ, Reuters, FT, CNBC,
     MarketWatch, Barron's, Yahoo Finance) — przez kanały RSS (Google News + Yahoo).
  3. Analitycy: konsensus, średnia cena docelowa, ostatnie podwyżki/obniżki rekomendacji
     (z zaznaczeniem największych banków: Goldman, Morgan Stanley, JPMorgan itd.).
  4. AI (Claude) wybiera newsy, które najmocniej mogą ruszyć ceną, i streszcza,
     co sądzą analitycy — po polsku.
  5. Wysyła wszystko na Telegram.

Sekrety (GitHub -> Settings -> Secrets -> Actions):
  TG_BOT_TOKEN, TG_CHAT_ID   – bot Telegrama (wymagane)
  ANTHROPIC_API_KEY          – klucz do Claude (zalecane; bez niego raport bez streszczeń AI)

Test lokalny bez wysyłania:  DRY_RUN=1 python monitor.py
"""

import csv
import datetime as dt
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus

import feedparser
import pandas as pd
import requests
import yfinance as yf

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "31"))     # okres newsów i zmiany kursu
ANALYST_DAYS = int(os.environ.get("ANALYST_DAYS", "90"))       # okres zmian rekomendacji
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
REPORT_FILE = "report.txt"

# Serwisy, z których bierzemy newsy (domena -> nazwa wyświetlana)
SOURCES = {
    "bloomberg.com": "Bloomberg",
    "wsj.com": "Wall Street Journal",
    "reuters.com": "Reuters",
    "ft.com": "Financial Times",
    "cnbc.com": "CNBC",
    "marketwatch.com": "MarketWatch",
    "barrons.com": "Barron's",
    "finance.yahoo.com": "Yahoo Finance",
}

# Największe domy maklerskie / banki inwestycyjne — ich zmiany rekomendacji są wyróżniane
TOP_FIRMS = [
    "goldman", "morgan stanley", "jp morgan", "jpmorgan", "bank of america", "bofa",
    "citi", "ubs", "barclays", "wells fargo", "deutsche", "jefferies", "evercore",
    "bernstein", "rbc", "bmo", "hsbc", "td cowen", "cowen", "piper sandler",
    "raymond james", "wedbush", "oppenheimer", "mizuho", "baird", "truist",
    "needham", "keybanc", "stifel", "cantor", "bnp", "societe generale", "macquarie",
]

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


# ---------------------------------------------------------------- pomocnicze
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def retry(label, fn, tries=3, wait=4):
    """Wywołuje fn() z ponowieniem — Yahoo potrafi chwilowo odrzucać zapytania z GitHuba."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            print(f"   ! {label} (próba {i + 1}/{tries}): {e}")
            if i < tries - 1:
                time.sleep(wait * (i + 1))
    return None


def num(x, nd=2):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if pd.isna(v):
        return None
    return round(v, nd)


def is_top_firm(name):
    n = (name or "").lower()
    return any(f in n for f in TOP_FIRMS)


def norm_title(t):
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def load_holdings(path="holdings.csv"):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip()
            if not t:
                continue
            rows.append({
                "ticker": t,
                "name": (r.get("name") or t).strip(),
                "query": (r.get("query") or "").strip(),
                "type": (r.get("type") or "stock").strip().lower(),
            })
    if not rows:
        sys.exit("!! holdings.csv jest pusty albo ma złe nagłówki (wymagane: ticker,name).")
    return rows


# ---------------------------------------------------------------- kurs
def price_stats(tk):
    h = retry("historia kursu", lambda: tk.history(period="1y", auto_adjust=False))
    if h is None or h.empty:
        return None
    close = h["Close"].dropna()
    last = float(close.iloc[-1])
    cutoff = close.index[-1] - pd.Timedelta(days=LOOKBACK_DAYS)
    before = close[close.index <= cutoff]
    start = float(before.iloc[-1]) if len(before) else float(close.iloc[0])

    rets = close.pct_change().dropna()
    rets = rets[rets.index > cutoff]
    biggest = rets.abs().sort_values(ascending=False).head(3).index
    moves = [{"data": str(d.date()), "zmiana_%": round(float(rets[d]) * 100, 2)}
             for d in sorted(biggest)]

    currency = None
    try:
        currency = tk.fast_info.get("currency")
    except Exception:  # noqa: BLE001
        pass

    return {
        "kurs": round(last, 2),
        "waluta": currency,
        "zmiana_miesiac_%": round((last / start - 1) * 100, 2),
        "max_52t": round(float(close.max()), 2),
        "min_52t": round(float(close.min()), 2),
        "najwieksze_ruchy_dzienne": moves,
    }


# ---------------------------------------------------------------- newsy
def fetch_feed(url):
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def entry_time(e):
    for k in ("published_parsed", "updated_parsed"):
        if e.get(k):
            return dt.datetime(*e[k][:6], tzinfo=dt.timezone.utc)
    return None


def google_news(term):
    sites = " OR ".join(f"site:{d}" for d in SOURCES)
    q = f"({term}) ({sites}) when:{LOOKBACK_DAYS}d"
    url = ("https://news.google.com/rss/search?q=" + quote_plus(q)
           + "&hl=en-US&gl=US&ceid=US:en")
    items = []
    for e in retry("Google News RSS", lambda: fetch_feed(url)) or []:
        src = (e.get("source") or {}).get("title") or ""
        title = e.get("title", "")
        if src and title.endswith(" - " + src):
            title = title[: -len(" - " + src)]
        items.append({"tytul": title, "zrodlo": src or "?", "link": e.get("link"),
                      "czas": entry_time(e)})
    return items


def yahoo_rss(ticker):
    url = (f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(ticker)}"
           "&region=US&lang=en-US")
    items = []
    for e in retry("Yahoo RSS", lambda: fetch_feed(url)) or []:
        items.append({"tytul": e.get("title", ""), "zrodlo": "Yahoo Finance",
                      "link": e.get("link"), "czas": entry_time(e),
                      "opis": re.sub("<[^>]+>", "", e.get("summary", ""))[:300]})
    return items


def yfinance_news(tk):
    items = []
    for n in retry("yfinance news", lambda: tk.news) or []:
        c = n.get("content") or n          # nowy i stary format yfinance
        title = c.get("title")
        if not title:
            continue
        when = None
        if c.get("pubDate"):
            when = pd.to_datetime(c["pubDate"], utc=True).to_pydatetime()
        elif n.get("providerPublishTime"):
            when = dt.datetime.fromtimestamp(n["providerPublishTime"], dt.timezone.utc)
        provider = (c.get("provider") or {}).get("displayName") or n.get("publisher") or "Yahoo Finance"
        link = (c.get("canonicalUrl") or {}).get("url") or n.get("link")
        items.append({"tytul": title, "zrodlo": provider, "link": link, "czas": when,
                      "opis": (c.get("summary") or "")[:300]})
    return items


def collect_news(h, tk):
    term = h["query"] or f'"{h["name"]}"'
    raw = google_news(term) + yahoo_rss(h["ticker"]) + yfinance_news(tk)
    cutoff = now_utc() - dt.timedelta(days=LOOKBACK_DAYS)
    seen, out = set(), []
    for it in raw:
        if not it["tytul"] or (it["czas"] and it["czas"] < cutoff):
            continue
        key = norm_title(it["tytul"])[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    out.sort(key=lambda x: x["czas"] or cutoff, reverse=True)
    for it in out:
        it["czas"] = it["czas"].strftime("%Y-%m-%d") if it["czas"] else None
    return out[:45]


# ---------------------------------------------------------------- analitycy
ACTION_PL = {"up": "podwyższenie", "down": "obniżenie", "init": "rozpoczęcie wyceny",
             "main": "podtrzymanie", "reit": "powtórzenie"}


def analyst_data(tk, last_price):
    out = {}
    info = retry("info", lambda: tk.info) or {}
    out["konsensus"] = info.get("recommendationKey")
    out["konsensus_srednia_1do5"] = num(info.get("recommendationMean"))  # 1 = strong buy, 5 = sell
    out["liczba_analitykow"] = info.get("numberOfAnalystOpinions")

    pt = retry("ceny docelowe", lambda: tk.analyst_price_targets) or {}
    if pt:
        mean = num(pt.get("mean"))
        out["cena_docelowa"] = {"srednia": mean, "mediana": num(pt.get("median")),
                                "min": num(pt.get("low")), "max": num(pt.get("high"))}
        if mean and last_price:
            out["potencjal_do_sredniej_%"] = round((mean / last_price - 1) * 100, 1)

    rs = retry("rekomendacje", lambda: tk.recommendations_summary)
    if rs is not None and not rs.empty:
        out["rozklad_rekomendacji"] = rs.head(2).to_dict(orient="records")  # teraz i miesiąc temu

    ud = retry("zmiany rekomendacji", lambda: tk.upgrades_downgrades)
    actions = []
    if ud is not None and not ud.empty:
        df = ud.copy()
        df.index = pd.to_datetime(df.index, utc=True)
        df = df[df.index >= now_utc() - dt.timedelta(days=ANALYST_DAYS)].sort_index(ascending=False)
        for d, r in df.iterrows():
            firm = str(r.get("Firm", ""))
            actions.append({
                "data": str(d.date()), "firma": firm, "duzy_bank": is_top_firm(firm),
                "akcja": ACTION_PL.get(str(r.get("Action", "")), r.get("Action")),
                "z": r.get("FromGrade") or None, "na": r.get("ToGrade") or None,
                "cena_docelowa": num(r.get("currentPriceTarget")) or None,
                "poprzednia_cena_docelowa": num(r.get("priorPriceTarget")) or None,
            })
    # najpierw duże banki, potem reszta; max 25 pozycji
    actions.sort(key=lambda a: (not a["duzy_bank"], a["data"]), reverse=False)
    top = [a for a in actions if a["duzy_bank"]]
    rest = [a for a in actions if not a["duzy_bank"]]
    out["ostatnie_zmiany"] = (sorted(top, key=lambda a: a["data"], reverse=True)
                              + sorted(rest, key=lambda a: a["data"], reverse=True))[:25]
    return out


# ---------------------------------------------------------------- AI
SYSTEM = """Jesteś analitykiem rynku kapitałowego piszącym miesięczny raport dla inwestora
indywidualnego z Polski. Piszesz po polsku, konkretnie, bez lania wody.
Zasady:
- Korzystasz WYŁĄCZNIE z danych podanych w wiadomości. Niczego nie dopisujesz z pamięci.
  Jeśli czegoś brakuje — napisz krótko, że brak danych.
- Z listy newsów wybierasz te, które realnie mogą najmocniej wpłynąć na cenę
  (wyniki i prognozy, duże kontrakty, regulacje, przejęcia, zmiany w zarządzie,
  pozwy, zmiany rekomendacji, makro: Fed, inflacja). Pomijasz szum i powtórki.
- Gdy największy dzienny ruch kursu zbiega się datą z newsem — połącz je.
- Przy każdym newsie podaj źródło (np. Bloomberg, Reuters) i datę.
- Nie dajesz własnych rekomendacji kup/sprzedaj — relacjonujesz, co sądzą analitycy.
- Format: zwykły tekst pod Telegram, BEZ markdown (żadnych **, #, tabel). Możesz używać emoji
  jako nagłówków i myślników jako punktów. Maksymalnie ok. 2500 znaków."""

TEMPLATE = """Spółka/instrument: {name} ({ticker}), typ: {type}

DANE (JSON):
{data}

Napisz sekcję raportu w tym układzie:
📌 {name} ({ticker})
1) Jednym zdaniem: co było najważniejsze w tym miesiącu i jak zachował się kurs.
2) 📰 Najważniejsze newsy (3–5, od najsilniejszego wpływu na cenę): tytuł po polsku,
   źródło, data, 1 zdanie dlaczego to ważne i w którą stronę może ruszyć cenę (↑/↓/?).
   Pod 3 najważniejszymi wklej ich link w osobnej linii.
3) 🏦 Analitycy: konsensus, średnia cena docelowa i potencjał względem kursu,
   najważniejsze ostatnie zmiany (szczególnie duże banki — nazwa firmy, z czego na co,
   nowa cena docelowa), czy nastroje się poprawiają czy pogarszają.
   Dla ETF-u zamiast tego: co analitycy i media mówią o całym rynku/indeksie.
4) ⚖️ Bilans: główne szanse i główne ryzyka na najbliższe tygodnie (po 1–2)."""


def ai_section(h, data):
    import anthropic
    client = anthropic.Anthropic()
    payload = json.dumps(data, ensure_ascii=False, default=str, indent=1)
    msg = client.messages.create(
        model=MODEL, max_tokens=3000, system=SYSTEM,
        messages=[{"role": "user", "content": TEMPLATE.format(
            name=h["name"], ticker=h["ticker"], type=h["type"], data=payload)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def plain_section(h, data):
    """Wersja bez AI (gdy brak ANTHROPIC_API_KEY albo błąd API)."""
    p, a = data.get("kurs") or {}, data.get("analitycy") or {}
    lines = [f"📌 {h['name']} ({h['ticker']})"]
    if p:
        lines.append(f"Kurs {p['kurs']} {p.get('waluta') or ''} | miesiąc {p['zmiana_miesiac_%']:+.1f}%")
    big = [n for n in data.get("newsy", []) if n["zrodlo"] != "Yahoo Finance"][:5] \
        or data.get("newsy", [])[:5]
    if big:
        lines.append("📰 Newsy:")
        lines += [f"- {n['tytul']} ({n['zrodlo']}, {n['czas']})" for n in big]
    if a.get("konsensus"):
        cd = a.get("cena_docelowa") or {}
        lines.append(f"🏦 Konsensus: {a['konsensus']} ({a.get('liczba_analitykow')} analityków), "
                     f"śr. cena docelowa {cd.get('srednia')} ({a.get('potencjal_do_sredniej_%')}%)")
    for z in a.get("ostatnie_zmiany", [])[:5]:
        lines.append(f"- {z['data']} {z['firma']}: {z['akcja']} {z['z'] or ''}→{z['na'] or ''}"
                     + (f", PT {z['cena_docelowa']}" if z['cena_docelowa'] else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
def split_message(text, limit=3800):
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def send(text):
    token, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    with open(REPORT_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n\n" + "-" * 40 + "\n\n")
    if DRY_RUN:
        print("[DRY RUN]\n" + text + "\n")
        return
    if not token or not chat:
        sys.exit("!! Brak TG_BOT_TOKEN / TG_CHAT_ID — dodaj sekrety w GitHub.")
    for part in split_message(text):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": part,
                                "disable_web_page_preview": True}, timeout=30)
        print("Telegram:", r.status_code, r.text[:200])
        if not r.ok:   # czerwony X w Actions zamiast cichej porażki
            sys.exit(f"!! Telegram odrzucił wiadomość: {r.status_code} {r.text[:300]}")
        time.sleep(1)


# ---------------------------------------------------------------- main
def main():
    open(REPORT_FILE, "w").close()
    holdings = load_holdings()
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not use_ai:
        print("!! Brak ANTHROPIC_API_KEY — raport pójdzie bez streszczeń AI.")

    sections, summary_rows, ok = [], [], 0
    for h in holdings:
        print(f"== {h['ticker']}")
        try:
            tk = yf.Ticker(h["ticker"])
            price = price_stats(tk)
            news = collect_news(h, tk)
            analysts = analyst_data(tk, price["kurs"] if price else None) \
                if h["type"] != "etf" else {}
            data = {"kurs": price, "analitycy": analysts, "newsy": news}
            print(f"   kurs: {'ok' if price else 'BRAK'} | newsy: {len(news)} | "
                  f"zmiany rekomendacji: {len(analysts.get('ostatnie_zmiany', []))}")
            if price:
                summary_rows.append(f"{h['name']}: {price['kurs']} {price.get('waluta') or ''} "
                                    f"({price['zmiana_miesiac_%']:+.1f}% m/m)")
            if not price and not news:
                raise RuntimeError("brak danych z Yahoo i z newsów")

            text = None
            if use_ai:
                text = retry("Claude API", lambda: ai_section(h, data), tries=2, wait=10)
            sections.append(text or plain_section(h, data))
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"   !! {h['ticker']}: {e}")
            sections.append(f"📌 {h['name']} ({h['ticker']})\n⚠️ Nie udało się pobrać danych: {e}")

    month = now_utc().strftime("%m.%Y")
    header = (f"📊 Miesięczny raport portfela — {month}\n\n"
              + ("\n".join(summary_rows) or "brak kursów")
              + f"\n\nNewsy z ostatnich {LOOKBACK_DAYS} dni: Bloomberg, WSJ, Reuters, FT, CNBC, "
                "MarketWatch, Barron's, Yahoo Finance.\n"
                "ℹ️ Raport informacyjny, nie jest poradą inwestycyjną.")
    send(header)
    for s in sections:
        send(s)

    if ok == 0:
        sys.exit("!! Nie udało się przygotować raportu dla żadnej pozycji.")


if __name__ == "__main__":
    main()
