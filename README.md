# Portfel IKE — monitor (push na iPhone)

Raz w tygodniu dostajesz **brief z newsami** o swoich spółkach, a codziennie skrypt sprawdza ceny i przy ruchu **≥ 20%** wysyła natychmiastowy **push jak SMS**. Działa za darmo na GitHub Actions — nie musisz trzymać komputera włączonego.

Ważne: to narzędzie **informacyjne**, nie porada inwestycyjna. Skrypt **nie ma dostępu do Twojego konta** — czyta tylko listę tickerów, którą sam ustawiasz.

---

## Co ustawiasz raz (ok. 10 minut)

### 1. Stwórz bota na Telegramie
- Zainstaluj **Telegram** na iPhone (jeśli jeszcze nie masz) i włącz powiadomienia w ustawieniach telefonu — to one dadzą push na ekranie blokady jak SMS.
- W Telegramie napisz do **@BotFather**, wyślij `/newbot`, nadaj nazwę i login bota. Dostaniesz **token bota** (długi ciąg typu `123456:ABC...`) — skopiuj go.
- **Kliknij Start / napisz cokolwiek do swojego nowego bota** — inaczej bot nie może wysłać Ci wiadomości.
- Zdobądź swój **chat ID**: napisz do **@userinfobot**, a on odeśle Twój numeryczny **Id**. (Alternatywnie wejdź na `https://api.telegram.org/bot<TWOJ_TOKEN>/getUpdates` po napisaniu do bota i odczytaj `chat.id`.)

### 2. Wrzuć projekt na GitHub
- Załóż darmowe konto GitHub, utwórz **prywatne** repozytorium.
- Wgraj do niego wszystkie pliki z tego folderu (zachowaj strukturę, w tym `.github/workflows/monitor.yml`).

### 3. Dodaj sekrety
W repo: **Settings → Secrets and variables → Actions → New repository secret**. Dodaj dwa:
- `TG_BOT_TOKEN` → token bota od @BotFather
- `TG_CHAT_ID` → Twój numeryczny chat ID

### 4. Ustaw swoje spółki
Edytuj `holdings.csv` — wpisz tickery **w formacie Yahoo Finance** i nazwy:

```
ticker,name
AVAV,AeroVironment
RHM.DE,Rheinmetall
CSPX.L,iShares Core S&P 500
```

Jak znaleźć ticker: wpisz spółkę na finance.yahoo.com i przepisz symbol z adresu.
Podpowiedź co do giełd: Niemcy `.DE`, Londyn `.L`, Paryż `.PA`, Amsterdam `.AS`, Warszawa `.WA`, USA — bez sufiksu.

### 5. Test
W repo: zakładka **Actions → Portfel monitor → Run workflow** (zostaw tryb `weekly`).
Po chwili powinien przyjść push na telefon. Gotowe — dalej działa sam wg harmonogramu.

---

## Harmonogram (czas UTC — zmień w `monitor.yml`, jeśli chcesz)
- **Alert 20%**: dni robocze 22:00 UTC (po zamknięciu USA).
- **Brief tygodniowy**: poniedziałek 07:00 UTC (~09:00 w Polsce).

## Dostrajanie (`monitor.py`, sekcja USTAWIENIA)
- `THRESHOLD_PCT` — próg alertu (domyślnie 20). Ustaw 15 lub 10, jeśli chcesz częściej.
- `LOOKBACKS` — okna ruchu: `[1, 5]` = jedna sesja i ~tydzień.
- `SUPPRESS_DAYS` — ile dni nie powtarzać tego samego alertu.
- `NEWS_PER_TICKER` — ile newsów na spółkę w briefie.

## Inny kanał zamiast Telegrama?
W `monitor.py`, w funkcji `notify()`, są gotowe (zakomentowane) wersje dla **Pushover** i **ntfy** — odkomentuj wybraną i dodaj odpowiednie sekrety.

## Uwagi
- Ceny i newsy pochodzą z Yahoo Finance (za darmo, ale nieoficjalnie — sporadyczne przerwy się zdarzają).
- Alert łapie ruch, który w oknie 1 lub 5 sesji przekroczy próg; bardzo powolny „pełzający" wzrost o 20% w dłuższym czasie może się nie załapać.
- Chcesz, żeby brief był streszczany po ludzku (przez model AI), a nie same nagłówki? Da się dołożyć — powiedz, to podeślę wersję z podsumowaniem.
