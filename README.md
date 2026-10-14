# Local AI Hardware Deal Hunter

Lokale Desktop-Web-App, die den deutschen Gebrauchtmarkt für PC-Hardware
(GPU, CPU, RAM, Mainboard, SSD, HDD, Netzteil, Kühler, Gehäuse, Komplett-PCs)
nach passenden Angeboten durchsucht. Du bekommst eine einfache Liste mit
direkten Links zur Originalanzeige.

Die Oberfläche in deutscher Sprache ist bewusst schlank: **Suche**,
**Ergebnisse**, **Einstellungen**.

Alles läuft lokal. Es gibt keine Cloud-Abhängigkeit, keine kostenpflichtige
KI-API und keine simulierten Ergebnisse.

---

## 1. Was die Anwendung tut

Die App hat eine einzige Kernaufgabe: **du suchst Hardware, sie liefert dir
 eine Liste passender Angebote mit direktem Link zur Originalanzeige.**

Die Oberfläche besteht deshalb bewusst nur aus drei Bereichen: **Suche**,
**Ergebnisse** und **Einstellungen**. Favoriten, Verlauf und ein Zahlen-Dashboard
gibt es absichtlich nicht.

Eine Suchleiste gibt es nicht mehr: **Gesucht wird immer nach den Komponenten
aus `Höchstpreise.txt`**, jede nur bis zu ihrem dort hinterlegten Kauf-Limit
(inkl. Versand). Was über dem Limit liegt, wird weder gespeichert noch
angezeigt; was unter dem **Mindestpreis** von einem Viertel des Limits liegt,
wird gar nicht erst gespeichert. In der Oberfläche wählst du nur noch die
Gruppe dieser Liste (z. B. Grafikkarten oder CPUs). Mehr dazu in Abschnitt 8b.

Intern läuft im Hintergrund:

1. Aus der Kauf-Limit-Liste (bzw. ersatzweise aus einem konkreten
   Produktnamen, den die API weiterhin annimmt) entsteht ein Suchplan - rein
   regelbasiert, ohne KI. Bei einem Produktnamen leitet die App Kategorie und
   Suchbegriffe selbst ab.
2. Die gewählten Plattformen werden abgefragt - in der Voreinstellung **alle
   verfügbaren** (eBay-Website und Kleinanzeigen-Website). Jede Anfrage trägt
   Höchst- und Mindestpreis; Auktionen werden ausgeschlossen (nur sofort
   kaufbare Festpreisangebote).
3. Die gefundenen **Titel** werden über **Schlagwörter** geprüft: Nennt der
   Titel die gesuchte Komponente, oder bietet er ein Teil, Zubehör oder eine
   Verpackung an? Was nicht passt - fremde Modelle, Lüfter, Halterungen, Kabel,
   Wasserblöcke, Sticker, leere Kartons - wird **gar nicht erst gespeichert**.
   Mehr dazu in Abschnitt 8c.
4. Angebote werden normalisiert, Hardwaremodellen zugeordnet, dedupliziert und
   lokal in SQLite gespeichert.
5. Jedes Angebot wird bewertet, damit der **beste Treffer** oben steht. Diese
   Zahlen bleiben unsichtbar: auf jeder Karte stehen nur Titel, Preis und der
   Link zur Anzeige.

---

## 2. Installation

Zum Mitmachen zuerst klonen, danach die Schritte unten:

```bash
git clone <REPO-URL> PcPartFinder
cd PcPartFinder
```

Zum Weitergeben an jemanden ohne Vorkenntnisse liegt `ANLEITUNG.txt` bereit —
dieselben Schritte in einfacher Sprache, als einzelne Textdatei.

Bewusst **nicht** im Repository und daher nach dem Klonen selbst anzulegen:
`.env` (Vorlage: `.env.example`), die virtuelle Umgebung `.venv` und
`frontend/dist` (entsteht beim Bauen). Auch `data/` wird nicht geteilt — darin
liegen die Datenbank und das Chrome-Profil; jeder Rechner hat seinen eigenen
Bestand. Ohne eigene Zugangsdaten oder Importdatei starten `start.bat` bzw.
`./start.sh` mit den Beispieldaten aus `samples/` (erfundene Demo-Angebote,
keine Live-Suche).

### Ein-Klick-Einrichtung (empfohlen)

**Windows:** `setup.bat` doppelklicken.
**Linux/macOS:** `bash setup.sh`.

Beides prüft zuerst, was schon vorhanden ist, und installiert Fehlendes über
`winget` (Windows) bzw. den Paketmanager (Linux/macOS):

* Python 3.11 oder neuer,
* Node.js 20 oder neuer (nur für die Oberfläche nötig),
* einen Browser, falls keiner da ist — ohne Browser gibt es keine Suche über
die eBay- oder Kleinanzeigen-Website (Abschnitt 5.0). Ist Google Chrome da,
nimmt die Suche ihn; ist schon Firefox da, wird kein Chrome installiert.
Abschaltbar mit `setup.bat -NoChrome`.

Danach werden `.venv`, die Abhängigkeiten, `.env` und `frontend/dist`
hergestellt, und die Anwendung startet. Beide Skripte sind mehrfach
ausführbar: Vorhandenes wird nur gemeldet, nichts überschrieben.

Nur prüfen, nichts ändern:

```bash
setup.bat -Check          # Windows
bash setup.sh --check     # Linux/macOS
```

### Von Hand

Voraussetzungen:

* Python 3.11 oder neuer
* Node.js 20 oder neuer (nur für die Oberfläche)

```bash
# 1. Backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # Linux/macOS

# 2. Konfiguration
cp .env.example .env        # Windows: copy .env.example .env
# .env öffnen und Werte anpassen

# 3. Oberfläche bauen
cd frontend
npm install
npm run build               # erzeugt frontend/dist
cd ..

# 4. Starten
.venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Danach im Browser öffnen: <http://127.0.0.1:8000>

Die Oberfläche wird vom Backend mit ausgeliefert. Wer am Frontend arbeitet,
kann stattdessen `npm run dev` in `frontend/` starten (Port 5173, `/api` wird
automatisch an Port 8000 weitergeleitet).

### Startskripte

* Windows: `start.bat`
* Linux/macOS: `./start.sh`

Beide prüfen, ob die virtuelle Umgebung existiert, bauen das Frontend bei
Bedarf und starten das Backend.

Ein bereits laufendes Backend wird **nicht** stillschweigend weiterverwendet:
Es läuft ohne Reload, geladener Code lässt sich also nicht austauschen. Deshalb
meldet `/api/health` den Startzeitpunkt und ob seither Quelldateien geändert
wurden (`runtime.stale`). Die Portprüfung macht daraus einen eigenen Fall:

* Port frei → starten
* Backend läuft mit aktuellem Code → es wird nichts doppelt gestartet
* Backend läuft mit **älterem** Code → Hinweis mit PID und Neustart-Befehl

Nach einer Codeänderung also:

```bash
start.bat --restart        # bzw. ./start.sh --restart
```

Das beendet das laufende Projekt-Backend (nur dieses – ein fremder Dienst auf
dem Port wird nie beendet) und startet es mit dem aktuellen Code. Ohne
`--restart` bleibt der alte Prozess aktiv: entfernte Funktionen laufen weiter,
neue Filter wirken nicht.

### Tests

```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
```

Die Tests greifen **nie** auf echte Plattformen zu. Es werden ausschließlich
lokale Fixtures (`tests/fixtures`) verwendet.

### Funktionstest aller Endpunkte

`scripts/api_smoke_test.py` startet eine **isolierte** Backend-Instanz (eigene
Datenbank, Port 8137), ruft jeden API-Endpunkt echt über HTTP auf und führt
zusätzlich einen vollständigen Suchlauf über die Website-Connectoren durch:

```bash
.venv/Scripts/python scripts/api_smoke_test.py
```

Am Ende steht eine Zusammenfassung je Gruppe und ein Rückgabewert von `0`
(alles bestanden) bzw. `1` (mindestens eine Prüfung fehlgeschlagen). Vor dem
Lauf darf kein weiteres Backend auf Port 8137 laufen.

---

## 3. Ohne KI

Die Anwendung arbeitet vollständig **ohne KI und ohne Modell-Server**. Suchplan,
Titelprüfung und Bewertung sind deterministisch: gleiche Eingaben ergeben immer
dasselbe Ergebnis, und jede Entscheidung ist nachvollziehbar (Schlagwörter,
Preisgrenzen, Zustand, Formel).

Es gibt deshalb nichts einzurichten, keine Wartezeiten und kein Risiko, dass ein
Modell mitten im Lauf ausfällt. Wie die Titelprüfung arbeitet, steht in
Abschnitt 8c.

---

## 4. Konfiguration (`.env`)

Alle Werte mit Bedeutung und Kommentar stehen in `.env.example`. Die wichtigsten:

| Variable | Bedeutung |
| --- | --- |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_MARKETPLACE_ID` | eBay Browse API |
| `EBAY_MAX_RESULTS_PER_QUERY` | Obergrenze pro Suchanfrage |
| `EBAY_WEB_ENABLED` | eBay-Suche über die öffentliche Website (ohne Zugangsdaten) |
| `EBAY_WEB_TIMEOUT_SECONDS`, `EBAY_WEB_MIN_DELAY_SECONDS`, `EBAY_WEB_MAX_CONCURRENCY` | Tempo/Stabilität der Website-Suche |
| `KLEINANZEIGEN_ENABLED` | Bewusst deaktiviert (siehe Abschnitt 6) |
| `KLEINANZEIGEN_WEB_ENABLED` | Kleinanzeigen-Suche über die öffentliche Website (ohne Zugangsdaten) |
| `KLEINANZEIGEN_WEB_HEADLESS`, `KLEINANZEIGEN_WEB_TIMEOUT_SECONDS`, `KLEINANZEIGEN_WEB_MIN_DELAY_SECONDS`, `KLEINANZEIGEN_WEB_MAX_CONCURRENCY` | Tempo/Stabilität der Kleinanzeigen-Suche |
| `LOCAL_IMPORT_PATH` | JSON-Datei mit selbst beschafften Angebotsdaten |
| `PRICE_LIST_PATH` | Kauf-Limit-Liste (Standard: `Höchstpreise.txt`); bestimmt, wonach gesucht wird und bis zu welchem Preis |
| `DATABASE_URL` | SQLite-Datei |
| `KNOWLEDGE_PATH`, `KNOWLEDGE_USER_FILE` | Wissensbasis |
| `SEARCH_MAX_QUERIES_PER_RUN`, `SEARCH_MAX_PAGES_PER_QUERY`, `SEARCH_PARALLEL_PAGES_PER_QUERY`, `SEARCH_MAX_ROUNDS` | Suchlimits (Anfragen, Seiten je Begriff, gleichzeitige Seiten) |
| `EBAY_WEB_PAGE_SIZE` | Treffer je Seite der Website-Suche (60/120/240; Standard 240 = dieselbe Liste in weniger Anfragen) |
| `RETENTION_DAYS` | Aufbewahrungsfrist für alte Angebote |

Zugangsdaten gehören **nicht** in eine eingecheckte Datei. `.env` ist in
`.gitignore` eingetragen; über die API ausgelieferte Einstellungen zeigen
Geheimnisse nur als `***gesetzt***`.

Zusätzlich gibt es in der Oberfläche unter **Einstellungen** Werte, die ohne
Neustart änderbar sind: Suchlimits, Angebotsformate, Standardzeitraum und
Aufbewahrung. Sie landen in SQLite.

---

## 4b. Echte Angebote bekommen

Es gibt vier Wege zu echten Angeboten. Der schnellste funktioniert **ohne
jede Anmeldung**: die eBay-Suche über die öffentliche Website im eigenen
Browser (Abschnitt 5.0).

| Weg | Was du tun musst | Ergebnis |
|---|---|---|
| **eBay Website (ohne Konto)** | Nichts außer einem Browser (Google Chrome oder Firefox) — `pip install playwright` | Live-Suche auf `ebay.de` über einen echten Browser (Fenster bleibt unsichtbar) |
| **eBay API** | Kostenloses Developer-Konto, Production-Key eintragen (Abschnitt 5.1) | Live-Suche über die offizielle Browse API |
| **Kleinanzeigen Website (ohne Konto)** | Nichts außer einem Browser (Google Chrome oder Firefox) — `pip install playwright` | Live-Suche auf `kleinanzeigen.de` im eigenen Browser (Abschnitt 6.2) |
| **Kleinanzeigen (Import)** | Eigene Suche im Browser, Seite speichern, `scripts/import_html.py` (Abschnitt 6.1) | Echte Kleinanzeigen-Angebote als Import |
| **Eigene Datenquelle** | Eigener Export/Partnerdatei als JSON (Abschnitt 7) | Alles, was du rechtmäßig beschafft hast |

Eine Plattform lässt sich wirklich prüfen: `POST /api/connectors/{platform}/test`
führt eine echte Mini-Suche aus. Damit siehst du sofort, ob die Quelle liefert.

---


## 5. eBay-Anbindung

Es gibt zwei unabhängige eBay-Connectoren, die du getrennt auswählen kannst:

* **eBay (Website)** — sucht `ebay.de` über den eigenen Browser; das Fenster
  läuft außerhalb des Bildschirms und minimiert, du siehst also nichts davon.
  **Keine Zugangsdaten, kein Konto nötig** (Abschnitt 5.0).
* **eBay** — die offizielle Browse API mit OAuth-Scope
  `https://api.ebay.com/oauth/api_scope` (client_credentials, kostenloses
  Developer-Konto, Abschnitt 5.1).

Sind beide nutzbar und beide ausgewählt, hat die API Vorrang, damit keine
Angebote doppelt einlaufen.

### 5.0 eBay ohne Zugangsdaten (Website-Suche im eigenen Browser)

Genau das, was du im Browser machst: auf `ebay.de` gehen und suchen. Der
Connector übernimmt das für dich.

1. **Einmalig vorbereiten** (ein Browser ist bei dir installiert):

```bash
.venv/Scripts/python -m pip install playwright
```

   Für Google Chrome ist **kein** `playwright install` nötig — es wird dein
   vorhandenes Chrome verwendet (Standard: `EBAY_WEB_BROWSER=auto`).

   **Ohne Chrome, mit Firefox:** Playwright kann das installierte Firefox nicht
   steuern — nachgemessen endete der Startversuch nach dem Zeitlimit von 30 s in
   einer Zeitüberschreitung, weil dabei auf die Steuerungsschnittstelle des
   eigenen Firefox-Builds gewartet wird. Gebraucht wird deshalb der angepasste
   Firefox-Build von Playwright:

```bash
.venv/Scripts/python -m playwright install firefox   # lädt ca. 120 MB
```

   Danach in `.env` setzen: `EBAY_WEB_BROWSER=firefox` (bei Kleinanzeigen
   entsprechend `KLEINANZEIGEN_WEB_BROWSER=firefox`). Firefox bekommt einen
   eigenen Profilordner (`data/ebay_profile-firefox`), damit sich die Profile
   der beiden Browser nicht vermischen.

   **Welcher Browser wann:** `EBAY_WEB_BROWSER` nimmt `auto` (Standard),
   `chrome`, `chromium` oder `firefox`. `auto` probiert der Reihe nach durch,
   bis einer startet: Google Chrome, dann das mitgelieferte Chromium, dann
   Firefox. Fehlt ein Browser ganz, wird er nicht erneut in jedem Profilordner
   probiert, sondern gleich der nächste genommen.

2. In der Suche die Plattform **„eBay (Website)“** auswählen und starten. Der
   Browser arbeitet dabei unsichtbar im Hintergrund (kein Fenster über deiner
   Arbeit).

3. Prüfen lässt sich das per API:
   `curl -X POST http://127.0.0.1:8000/api/connectors/ebay_web/test`. Erwartet
   wird eine Meldung mit Anzahl und Beispieltreffer (z. B. „240 Angebote für
   'grafikkarte' gelesen“).

**Wie es funktioniert und warum kein Fenster stört:** eBay blockt einfache
HTTP-Abrufe und Headless-Browser mit `HTTP 403`. Es braucht also einen echten
Browser - aber keinen sichtbaren: Chrome startet mit seinem Fenster weit
außerhalb des Bildschirms und wird direkt danach minimiert. Für eBay ist es
weiterhin ein normaler Browser, für dich ist nichts zu sehen. Wer die Suche
mitverfolgen will, setzt `EBAY_WEB_HIDDEN=false`. Der Connector nutzt ein
persistentes Chrome-Profil (echte Cookies) und macht zwischen zwei Aufrufen
eine kurze Pause.

**Eine Seite, viele Anfragen (Schnellverfahren):** Früher baute der Connector
für jede Anfrage eine neue Seite auf und schloss sie wieder. Das war langsam
und ließ Chrome bei jeder Anfrage ein Fenster zeigen - genau das flackerte
unten in der Taskleiste. Nachgemessen mit der Windows-API und identischer URL
(`rtx 3080`, 4 Runden): eine **neue Seite je Anfrage** brauchte **8,9 s** im
Median (zwei Runden endeten nach 15,6 s mit `HTTP 403` und **0 Treffern**),
ein **dauerhafter Tab** dagegen 2,2 s bei **4× 60 Treffern**.

Deshalb bleibt jetzt **ein Tab** offen. Darin läuft einmal die Startseite
(Cookies) und danach holt sich der Connector jede Trefferliste per `fetch`
**innerhalb** dieser Seite und wertet sie mit `DOMParser` aus: kein Rendern,
kein neuer Tab, kein Fensterwechsel. Gemessen an derselben Suche, inklusive
des Mindestabstands von 1,5 s:

| Quelle | `fetch` im offenen Tab | Seite laden |
| --- | --- | --- |
| eBay | **3,7 s** je Anfrage | 7,6 s je Anfrage |
| Kleinanzeigen | **1,8 s** je Anfrage | 2,6 s je Anfrage |
| Kleinanzeigen (echter Suchlauf, 10 Anfragen) | **Ø 1,8 s** | Ø 3,3 s |

Die Treffer sind dieselben: In beiden Wegen wurden **identische Angebots-URLs**
geliefert (eBay 54, Kleinanzeigen 25 im Einzeltest); nur die Reihenfolge kann
sich um einzelne beworbene Anzeigen unterscheiden.

**Weniger Anfragen, mehr Treffer, zwei Suchansichten:** Drei Stellschrauben
treiben die Abdeckung nach oben, ohne die Quelle stärker zu belasten:

* **Größere Seiten.** eBay liefert 60/120/240 Treffer je Seite;
  `EBAY_WEB_PAGE_SIZE=240` (Standard) holt in **einer** Anfrage, wofür sonst
  vier nötig wären. Die Obergrenze der Plattform (`_MAX_RESULTS_PER_QUERY`)
  gilt trotzdem. Antwortet eBay kleiner als angefragt, merkt sich der Connector
  die tatsächlich gelieferte Seitengröße und blättert damit weiter - sonst
  entstünde eine Lücke in der Liste.
* **Zwei Ansichten bei breiten Begriffen.** Die Standardsicht (Best Match) und
  "neueste zuerst" (`_sop=10`) zeigen unterschiedliche Ausschnitte derselben
  Suche. Bei breiten Begriffen ("grafikkarte") fragt der Lauf beide ab, bei
  einer engen Modellsuche nur die Standardsicht - dort ist sie bereits
  vollständig. Jede Ansicht ist eine eigene Zeile im Abdeckungsbericht.
* **Gleichzeitige Seiten.** Meldet die Quelle ihre Gesamttrefferzahl (bei
  Kleinanzeigen immer), holt der Lauf mehrere Seiten **parallel** statt
  nacheinander. Der Mindestabstand zwischen zwei Abrufen bleibt dabei exakt
  erhalten: Er wird als Zeitfenster reserviert, nicht als "Zeit seit dem
  letzten Abruf". Ohne Gesamttrefferzahl wird weiterhin Seite für Seite
  vorgegangen - sonst würde eine Anfrage für eine Seite verbraucht, die es
  womöglich gar nicht gibt.

Abschalten (dann wird wieder jede Seite geladen) mit
`EBAY_WEB_INLINE_FETCH=false` bzw. `KLEINANZEIGEN_WEB_INLINE_FETCH=false`.
Findet der Direktabruf keine Trefferliste (z. B. eine Zwischenseite), lädt der
Connector die Seite automatisch normal - das Ergebnis bleibt in jedem Fall
geprüft und wird nie geraten. Weist die Seite den `fetch` ab (`HTTP 403/429`),
pausiert nur der Direktabruf (15 s, dann jeweils verdreifacht); die Seite selbst
wird weiterhin normal geladen. Ein `403` auf einen `fetch` heißt **nicht**,
dass die Quelle gesperrt ist - im Test lieferten geladene Seiten weiterhin
Treffer. Als Blockade gilt weiterhin allein das Laden der Seite selbst.

**Ehrliche Grenzen:**

* Automatisiertes Auslesen ist von eBays Nutzungsbedingungen nicht ausdrücklich
  erlaubt. Abschalten geht jederzeit mit `EBAY_WEB_ENABLED=false`; dann bleibt
  nur die API.
* eBay drosselt schnelle Folgeanfragen. Einzelne Anfragen werden gelegentlich
  mit `403` blockiert — das wird ehrlich als Einschränkung gemeldet, der Lauf
  macht mit den übrigen Anfragen weiter. Für große Läufe ist die API robuster.
  In einem Lauf mit vielen Anfragen bestimmen diese Blockaden und ihre
  Wiederholungen die Dauer: gemessen Ø 24–30 s je eBay-Anfrage, während dieselbe
  Anfrage ohne Blockade in 2–4 s beantwortet wird. Wird ein Profil länger
  blockiert (auch die Startseite antwortet nicht mehr), hilft es, den
  Profilordner `data/ebay_profile` zu löschen: dann beginnt der Connector mit
  frischen Cookies.
* Die Ergebnisliste enthält **kein** Veröffentlichungsdatum. Der Zeitraum wird
  nur über die Sortierung „neu eingestellt“ angenähert.
* Versandkosten und Standort stehen nicht zuverlässig in der Ergebnisliste; die
  Gesamtkosten sind deshalb geschätzt.
* Die eBay-Website liefert **keinen** Verkäuferstandort und lässt sich nicht per
  URL auf einen Umkreis einschränken (`_stpos`/`_sadis` wurden live geprüft und
  verändern die Trefferliste nicht). Ein Standortwunsch wirkt bei eBay daher
  nicht; der Lauf meldet das ausdrücklich. Bei Kleinanzeigen stehen PLZ und Ort
  in den Ergebnissen, dort filtert der Umkreis tatsächlich.
* Ändert eBay den Seitenaufbau, liefert der Connector bewusst **0 Treffer und
  eine Einschränkung** statt geratener Daten.

### 5.1 Zugangsdaten in fünf Minuten (kostenlos)

1. Auf <https://developer.ebay.com/> mit einem normalen eBay-Konto anmelden
   (kostenlos, keine Kreditkarte).
2. **Application Keys** öffnen und ein Keyset anlegen bzw. das vorhandene
   *Production*-Keyset wählen.
3. Von dort zwei Werte übernehmen:
   * **App ID (Client ID)** → `EBAY_CLIENT_ID`
   * **Cert ID (Client Secret)** → `EBAY_CLIENT_SECRET`
4. In `.env` eintragen und das Backend neu starten:

```env
EBAY_CLIENT_ID=Deine-App-ID
EBAY_CLIENT_SECRET=Dein-Cert-ID
EBAY_MARKETPLACE_ID=EBAY_DE
```

5. Zugangsdaten prüfen (echte Mini-Suche, inklusive Marktplatz und
   Restkontingent):

```bash
curl -X POST http://127.0.0.1:8000/api/connectors/ebay/test
```

   Erwartet wird eine Meldung wie „eBay antwortet; 5000 Treffer für
   'grafikkarte'“.

Schlägt der Test fehl, nennt er den Grund im Klartext (z. B. HTTP 401 →
Client ID/Secret vertauscht oder Sandbox-Key statt Production-Key).

Bereits geprüfte Eigenschaften der API und wie die Anwendung damit umgeht:

* `buyingOptions` liefert `FIXED_PRICE`/`AUCTION` → darauf beruht der
  Auktionsausschluss. Zusätzlich wird immer `FIXED_PRICE` gefiltert.
* Pagination über `offset`/`limit` (max. 200 pro Seite), Gesamtzahl über
  `total`.
* **Kein** Filterfeld für das Veröffentlichungsdatum: Der Zeitraum wird nach
  dem Abruf lokal geprüft. Angebote ohne Datum werden als „Alter unbekannt"
  gekennzeichnet und **nicht** als aktuell behandelt.
* Maximal 10.000 Ergebnisse pro Suchmenge; die Anwendung weist aus, wenn diese
  Grenze erreicht ist, statt Vollständigkeit zu behaupten.
* Der Zustandsfilter unterstützt nur wenige Werte; der Zustand wird deshalb
  lokal aus dem Text geprüft. Fehlt die Berechtigung für den Filter, läuft die
  Suche ohne ihn erneut.
* Fehlende oder abgelehnte Zugangsdaten werden als Einschränkung gemeldet und
  der Start mit dieser Plattform wird abgelehnt — statt leere Ergebnisse als
  „nichts gefunden" auszugeben.

---

## 6. Kleinanzeigen: kein API-Connector, aber Website-Suche

Kleinanzeigen stellt **keine öffentliche API** für Drittanwendungen bereit und
untersagt in seinen Nutzungsbedingungen automatisiertes Auslesen. Die
Anwendung implementiert deshalb bewusst:

* **keinen** Umgehungsmechanismus für CAPTCHAs, Login- oder Zugriffssperren,
* einen austauschbaren Connector (`backend/connectors/kleinanzeigen.py`), der
  im Standardfall deaktiviert ist und die Einschränkung offen dokumentiert.

Wird `KLEINANZEIGEN_ENABLED=true` gesetzt, meldet der Connector weiterhin
ehrlich, dass ohne zulässigen Zugriffsweg keine Daten geliefert werden. Für
einen eigenen, rechtlich zulässigen Zugriffsweg (z. B. offizielle
Partnerschnittstelle) genügt eine Implementierung der in
`backend/connectors/base.py` definierten Schnittstelle.

Ergibt eine Prüfung künftig, dass ein zulässiger Weg existiert, kann er als
weiterer Connector ergänzt werden, ohne die übrige Logik zu ändern.

### 6.2 Kleinanzeigen ohne Zugangsdaten (Website-Suche im eigenen Browser)

Dasselbe Vorgehen wie bei eBay (Abschnitt 5.0): Die App sucht die normale
Kleinanzeigen-Seite — so, wie du selbst im Browser suchst. Connector:
`backend/connectors/kleinanzeigen_web.py`, Plattform **„Kleinanzeigen
(Website)“**.

1. **Einmalig vorbereiten:** `.venv/Scripts/python -m pip install playwright`
   (ein Browser ist bereits installiert, siehe Abschnitt 5.0 — für Firefox
   zusätzlich `.venv/Scripts/python -m playwright install firefox` und
   `KLEINANZEIGEN_WEB_BROWSER=firefox`).
2. In der Suche die Plattform **„Kleinanzeigen (Website)“** wählen und starten.
3. Prüfen: `curl -X POST http://127.0.0.1:8000/api/connectors/kleinanzeigen_web/test`.
   Erwartet wird eine Meldung wie „25 Angebote für 'grafikkarte' gelesen“.

**Als zweite Quelle neben eBay (Website):** Bleibt die Quelle in der Suche auf
„Alle verfügbaren" stehen (Voreinstellung), laufen beide Quellen nebeneinander.
In der Messung vom 13.10.2026 (Gruppe Grafikkarten) lieferte Kleinanzeigen 342
der 467 gespeicherten Angebote - bei 66 von 198 Suchanfragen. Die günstigsten
Treffer kommen von dort (GTX 1660 Super ab 55 €, GTX 1080 ab 80 €).

Auch hier läuft das Schnellverfahren aus Abschnitt 5.0 (`*_WEB_INLINE_FETCH`):
ein Tab, Trefferliste per `fetch` (gemessen ~0,4 s statt ~1,1 s je Anfrage).

**Was der Connector auswertet:** Suchbegriff, Preisgrenzen (`minPrice`,
`maxPrice`), Sortierung nach Datum und die Seiten (`pageNum`, fest 25 Treffer
je Seite). Er liest Titel, Preis (auch „Zu verschenken“), Ort samt PLZ,
Einstelldatum („Heute, 20:11“, „Gestern, …“ oder `08.10.2026`) und das
Vorschaubild. Kleinanzeigen kennt keine Auktionen — jedes Angebot gilt deshalb
als Festpreis, der Auktionsausschluss greift hier nicht.

**Ehrliche Grenzen:**

* Automatisiertes Auslesen ist nach den Kleinanzeigen-Nutzungsbedingungen
  untersagt. Der Connector ist abschaltbar
  (`KLEINANZEIGEN_WEB_ENABLED=false`); Nutzung auf eigene Verantwortung.
* Der **Zustand** steht nicht in der Ergebnisliste. Er wird deshalb aus
  Titel, Beschreibung und Kartentext **abgeleitet** — aber nur in den beiden
  Zuständen, die die Suche zulässt („Gebraucht, gut“, „Sehr gut / wie neu“).
  Nennt der Text keinen Zustand, bleibt er leer statt geraten. Aus dem Freitext
  wird **nie** ausgeschlossen: „Neu“ und „Defekt“ entscheidet die Ingestion mit
  ihren harten Regeln über Titel und Beschreibung. Warum so eng: gemessen an
  den 571 gespeicherten Kleinanzeigen-Angeboten ergäbe eine Ableitung über den
  ganzen Text u. a. aus „…bereit für den Einsatz in einem **neuen** System“
  ein „Neu“ und aus „Keine Fehler oder **Defekte**!“ ein „Defekt“ — beide
  hätten echte Angebote gelöscht. So werden 185 Angebote (32 %)
  aufgewertet (Ø +1,8 Punkte, auf den Betroffenen +5,6) und **kein** Angebot
  verworfen.
* Versandkosten und Verkäuferprofil fehlen in der Liste; die Gesamtkosten sind
  deshalb geschätzt.
* Die Liste enthält zusätzlich beworbene Anzeigen. Deshalb zählt für die
  Seitensteuerung die Trefferzahl aus der Überschrift, nicht die Listengröße.
* Ändert Kleinanzeigen den Seitenaufbau, liefert der Connector **0 Treffer und
  eine Einschränkung** statt geratener Daten.
* Standardmäßig läuft der Browser headless (`KLEINANZEIGEN_WEB_HEADLESS=true`)
  — es entsteht gar kein Fenster, weil die Liste serverseitig gerendert wird.
  Bei strengeren Antworten `false` setzen; auch dann bleibt das Fenster
  unsichtbar (außerhalb des Bildschirms). Sichtbar wird es nur mit
  `KLEINANZEIGEN_WEB_HIDDEN=false`.

---

## 6.1 Eigene Suche importieren (Kleinanzeigen **und** eBay ohne API-Key)

Kleinanzeigen bietet keine zulässige Schnittstelle (Abschnitt 6). Derselbe Weg
funktioniert aber auch für eBay, wenn du keinen API-Key anlegen willst: du
suchst selbst im Browser, die App liest danach nur die **gespeicherte Datei**.
Sie ruft dabei **keine** Plattform auf.

1. Suche im Browser wie gewohnt (z. B. `https://www.ebay.de/sch/i.html?_nkw=rtx+3080`
   oder eine Kleinanzeigen-Suche).
2. Speichere die Ergebnisseite: **Strg+S** → „Seite speichern unter“ (nur HTML),
   bei mehreren Seiten jeweils eine Datei.
3. Wandle sie in das Importformat der App um:

```bash
.venv/Scripts/python scripts/import_html.py seite1.html seite2.html -o data/eigene_suche.json
# weitere Seiten später ergänzen, ohne Dubletten:
.venv/Scripts/python scripts/import_html.py seite3.html -o data/eigene_suche.json --merge
```

4. In `.env` eintragen: `LOCAL_IMPORT_PATH=data/eigene_suche.json`.
5. **Kein Neustart nötig**, wenn die App schon läuft: Sie erkennt Änderungen an
   der Importdatei automatisch beim nächsten Suchlauf.

Erkannt werden eBay-Anzeigen (`/itm/…`) und Kleinanzeigen-Anzeigen
(`/s-anzeige/…`), zuerst über schema.org-Daten (`ld+json`), sonst über die Links
samt Preis (auch „EUR 210,00“), Ort und Datum. Mit `--debug` siehst du, was
gefunden wurde; `--base-url https://www.ebay.de` hilft bei relativen Links.
Dieselben Angebote laufen anschließend durch Filter, Titelprüfung und Bewertung
wie Live-Treffer.

> Grenze: Importierte Angebote sind eine Momentaufnahme deiner Suche. Wer
> automatische, tagesaktuelle eBay-Suchen will, nimmt die offizielle API
> (Abschnitt 5.1) — dort übernimmt die App die Suche komplett.

Das Werkzeug stellt **keine** Anfragen an Kleinanzeigen — es liest nur die von
dir gespeicherte Datei. Erkannt werden zuerst `application/ld+json`
(schema.org-Daten) und sonst Anzeigenlinks mit Preis in der Umgebung; mit
`--debug` siehst du, was gefunden wurde. Sieht deine gespeicherte Seite anders
aus, lässt sich das Muster anpassen (`--link-pattern`).

Grenzen: Die Angebote sind Momentaufnahmen (Preise können veraltet sein), und
ein Angebot ohne erkannten Preis wird ohne Preisvorteil bewertet. Die App
kennzeichnet importierte Angebote immer mit der Plattform `local_import`.

---


## 7. Lokaler Import (Betrieb ohne eBay-Zugangsdaten)

Damit die komplette Pipeline auch ohne Plattform-Zugangsdaten prüfbar bleibt,
gibt es einen Connector für Daten, die du selbst zulässig beschafft hast:
eigener Export, Ergebnisse einer manuellen Recherche oder eine offizielle
Partnerdatei.

```jsonc
{
  "listings": [
    {
      "id": "optional-eigene-id",
      "title": "ASUS RTX 3080 TUF Gaming 10GB",
      "url": "https://example.com/angebot/123",
      "price": 210.0,
      "shipping_cost": 6.99,
      "condition": "Gebraucht, sehr gut",
      "published_at": "2026-10-02T09:15:00Z",
      "postal_code": "80331",
      "location": "München",
      "seller_name": "beispiel",
      "buying_options": ["FIXED_PRICE"],
      "pickup_available": true,
      "description": "Freitext ..."
    }
  ]
}
```

* Pfad über `LOCAL_IMPORT_PATH` setzen (oder in den Einstellungen).
* Die Suche ist ein Textvergleich auf Titel und Beschreibung — die Abdeckung
  entspricht genau dem Inhalt der Datei. Das wird in der Oberfläche und im
  Abdeckungsbericht so ausgewiesen; importierte Angebote tragen die Plattform
  `local_import` und stammen ausdrücklich **nicht** aus einer Live-Suche.
* Alle Formatregen (Auktionen, unbekanntes Format) und Filter greifen identisch.

### 7.1 Beispieldaten für den ersten Start

Ohne eigene Datenquelle wäre beim ersten Start **keine** Plattform nutzbar:
eBay braucht Zugangsdaten, Kleinanzeigen hat keine zulässige API. Deshalb
liegen unter `samples/beispiel_angebote.json` erfundene Demo-Angebote. Die
Startskripte laden sie automatisch, **nur** wenn wirklich keine echte Quelle
vorhanden ist — keine eigene Importdatei (`LOCAL_IMPORT_PATH`) **und** keine
eBay-Zugangsdaten (`EBAY_CLIENT_ID`/`EBAY_CLIENT_SECRET`), jeweils weder in der
Umgebung noch in der `.env`. Sobald du eBay konfigurierst, mischen sich keine
Demo-Angebote mehr unter deine echten Ergebnisse. Konsole und Fehlermeldungen
weisen darauf hin.

Die Demo-Angebote sind ausdrücklich **keine** echten Anzeigen und enthalten
**keine** echten Marktpreise; alle Links zeigen auf `example.invalid`. Sie
dienen nur dazu, Filter, Bewertung und Oberfläche ohne Zugangsdaten prüfbar zu
machen. Eigene, selbst beschaffte Daten: `LOCAL_IMPORT_PATH` auf die eigene
JSON-Datei setzen. Ein weiterer (reiner Test-)Datensatz liegt unter
`tests/fixtures/local_import_sample.json`.

---

## 8. Hardware-Wissensbasis

Format: **YAML** oder **JSON** (Markdown zusätzlich für freie Notizen).

```yaml
version: "2026-10"
entries:
  - category: gpu
    model: "RTX 3080"
    manufacturer: "NVIDIA"
    product_family: "GeForce RTX"
    aliases: ["Geforce RTX 3080", "RTX3080"]
    variants: ["10 GB", "12 GB"]
    # Preisfelder NUR eintragen, wenn du sie aus eigener Beobachtung
    # vertreten kannst. Leere Felder sind ausdrücklich erlaubt.
    good_price_max: 180
    fair_price_max: 220
    overpriced_above: 260
    performance_index: 100        # optional, von dir gepflegt
    known_issues: ["Spulenfiepen bei hoher Last"]
    problematic: false            # true = Standardfilter kann ausschließen
    weight: 1.0                   # > 1.0 = bei der Suche bevorzugt
```

Regeln, die die Anwendung durchsetzt:

* **Es werden keine Preise erfunden.** Fehlt jede Preisangabe, lautet das
  Ergebnis „Marktwert unsicher" statt einer scheinbar exakten Zahl.
* Manuell gepflegtes Preiswissen und beobachtete Angebotspreise werden
  **getrennt** ausgewiesen (Feld `used_sources` im Marktwert-Bericht).
* Beobachtete Angebotspreise sind **keine** erzielten Verkaufspreise. Dieser
  Hinweis steht an jeder Marktwert-Anzeige.
* Ein fehlerhafter Eintrag wird gemeldet und übersprungen — die übrigen
  Einträge der Datei bleiben nutzbar.
* Die Wissensbasis wird von **Suchplaner** und **Bewertung** genutzt.
* Änderungen wirken nach „Wissensbasis neu laden" in der Oberfläche oder nach
  einem Neustart; ein Codeneingriff ist nie nötig.

Der mitgelieferte Katalog (`knowledge/hardware_knowledge.yaml`) enthält
**nur Modell-Identitäten** (Namen, Aliase, Modellnummern, Varianten) und
bewusst **keine** Preise.

---

## 8b. Kauf-Limits: wonach gesucht wird (`Höchstpreise.txt`)

Eine Zeile, eine Komponente, ein Preis:

```
----------------------------------------------------------------
GRAFIKKARTEN - NVIDIA
----------------------------------------------------------------
NVIDIA GeForce RTX 3080 10GB = 405 €
```

Das ist gleichzeitig **Suchauftrag und Preisgrenze**:

* **Am 13.10.2026 an den Markt angepasst:** Alle 136 Werte wurden mit Faktor
  1,5 angehoben (auf 5 € gerundet; Grund im Kopf der Datei). Auslöser war eine
  eigene Messung: Für sechs Modelle lag das günstigste Zehntel der Angebote bei
  120-350 €, während die alten Limits bei 60-170 € standen - der Markt lag
  2-3× darüber, und es wurde praktisch nichts gespeichert. Belegter Effekt im
  selben Suchlauf (Gruppe Grafikkarten, beide Quellen, 198 Anfragen): **vorher
  0 gespeicherte Angebote, nachher 467** - davon 342 von Kleinanzeigen und 125
  von eBay. Die Liste bleibt damit weiter unter dem Marktmittel: Teuerste
  gespeicherte Anzeige war das Modell mit dem höchsten Limit (RTX 5090).
* **Suchbegriff:** Der Suchbegriff wird aus dem Namen abgeleitet
  (`NVIDIA GeForce RTX 3080 10GB` → `RTX 3080 10GB`), weil Herstellerpräfixe
  in Anzeigen praktisch nie stehen. Je Komponente läuft eine Anfrage mit ihrem
  eigenen Kauf-Limit.
* **Der Preisrahmen geht an die Quelle:** Kauf-Limit als `_udhi`, Mindestpreis
  als `_udlo` (Kleinanzeigen: `maxPrice`/`minPrice`, eBay-API: `price:[..]`).
  Damit werden überteuerte Treffer gar nicht erst geladen. Der Rahmen wurde
  live vermessen (13.10.2026, fünf GPU-Modelle, zwei komplette Suchläufe): eBay
  füllt einen engen Rahmen mit **ähnlichen Artikeln** auf, wenn es zu wenige
  genaue Treffer gibt - mit Rahmen war bei **879 von 902** geprüften Titeln das
  genannte Modell ein anderes (97 %), ohne Rahmen bei **49 von 935** (5 %).
  Die brauchbaren Angebote waren in beiden Fällen gleich viele. Der Rahmen
  spart also das Lesen teurer Treffer, kostet aber Anfragen für die
  Auffüll-Artikel - die Titelprüfung verwirft sie. Mit den seit 13.10.2026 an
  den Markt angepassten Limits fällt das kaum noch ins Gewicht: Im gemessenen
  Lauf (Grafikkarten, 198 Anfragen) passten 8.531 von 14.068 geprüften Titeln
  (61 %), nur 2.660 (19 %) nannten ein anderes Modell. Die letzte Instanz bleibt
  deshalb die lokale Prüfung (nächster Punkt): Sie muss auch dann greifen,
  wenn die Quelle den Rahmen ignoriert.
* **Preisgrenze:** Der Wert gilt **inkl. Versand**. Er wird beim Speichern und
  beim Anzeigen angewendet - was darüber liegt, taucht nirgends auf.
  Beispiel: Limit 215 €, Artikel 210 € + 5,99 € Versand = 215,99 € → verworfen.
* **Mindestpreis:** Jedes Produkt hat auch eine Grenze nach unten, und die ist
  **ein Viertel des Höchstpreises** (bei 240 € also 60 €). Angebote darunter
  sind im Gebrauchtmarkt praktisch immer Zubehör, Einzelteile oder defekte
  Ware - sie werden gar nicht erst gespeichert. Die Grenze geht - wie die
  Obergrenze - an die Quelle (`_udlo` bzw. `minPrice`) und wird zusätzlich
  lokal geprüft. Ohne Höchstpreis gibt es keinen Mindestpreis: Er wird nicht
  geraten, sondern abgeleitet.
* **Mehrere Varianten:** Ohne Angabe der Speicher-/Variantengröße im Titel gilt
  das **strengste** passende Limit. Bei `RTX 3080 10GB` (405 €) und
  `RTX 3080 12GB` (450 €) wird eine Anzeige, die nur „RTX 3080" nennt, an
  405 € gemessen - es wird nichts schöngerechnet.
* **Gruppen:** Die Abschnittsüberschriften (GRAFIKKARTEN, CPUs, RAM, SSDs,
  HDDs, Mainboards, Netzteile, Kühler, Gehäuse) bilden die Auswahl in der
  Oberfläche. Unbekannte Abschnitte und Hinweistexte werden übersprungen;
  Abschnitte ohne Einträge sind kein Fehler.
* **Live gepflegt:** Die Datei wird bei jeder Suche neu eingelesen. Komponenten
  ergänzen oder Preise anpassen wirkt sofort, ohne Neustart. Was nicht in der
  Liste steht, wird **nicht** gesucht.
* **Ohne Liste:** Ist die Datei leer oder nicht lesbar, sagt die Oberfläche das
  offen, statt still nichts zu tun (`GET /api/price-limits` liefert dann
  `available: false` samt Pfad und Hinweis).

Im Preislisten-Modus kommen die Suchbegriffe direkt aus der Datei - je
Komponente eine Anfrage mit ihrem eigenen Kauf-Limit.

### Limits aus beobachteten Preisen nachführen

Die Liste war bisher reine Handarbeit - genau daran ist der Stand vom 13.10.2026
gescheitert (Werte veraltet, 0 gespeicherte Angebote in einem ganzen Lauf).
Deshalb sind seit dem 14.10.2026 zusätzlich **RAM, SSDs, HDDs, Mainboards,
Netzteile, Kühler und Gehäuse** aufgenommen. Ihre Startwerte sind bewusst
großzügige Kalibrierwerte (etwa auf Angebotsniveau), damit zunächst überhaupt
Daten einlaufen; der Limit-Berater zieht sie anschließend nach unten.

Der Limit-Berater (`backend/pricing/limit_advisor.py`) liest die bereits
**gespeicherten, aktiven Angebote** und leitet je Komponente ein Limit aus dem
**günstigsten Zehntel** dieser Preise ab. Es wird nichts geschätzt und nichts
erfunden:

* Zuordnung über dieselbe Regel, mit der ein Angebot gesucht und angezeigt wird
  (`PriceList.limit_for`). Defekte Ware (Rang 0), fremde Modelle, andere
  Formate und verschwundene Angebote zählen nicht mit.
* Unter `market_min_observations` (Standard 5, in den Einstellungen änderbar)
  vergleichbaren Angeboten gibt es **keine** Empfehlung - das Limit bleibt
  stehen.
* In beide Richtungen auf ±50 % je Durchlauf begrenzt: Eine dünne Stichprobe
  darf keine Sprünge erzeugen, mehrere Läufe konvergieren.
* Beobachtete Angebotspreise sind **keine** erzielten Verkaufspreise; der
  Hinweis steht an jeder Empfehlung.

Zwei Wege:

* **Nach Sicht:** `GET /api/price-limits/suggestions` zeigt je Komponente das
  aktuelle Limit, die Empfehlung, die Datenbasis (Anzahl, 10. Perzentil,
  Median) und eine Begründung. `POST /api/price-limits/apply` schreibt nur
  echte Änderungen in die Datei - vorher entsteht eine Sicherungskopie
  daneben, Überschriften und Kommentare bleiben unverändert. In der
  Oberfläche ist das der Knopf **„Kauf-Limits nachführen"** unter
  **Einstellungen**.
* **Automatisch:** Die Laufzeit-Einstellung `auto_update_price_limits`
  (Standard **aus**) führt die Limits am Anfang jedes Suchlaufs nach und meldet
  die Änderung als Einschränkung im Laufprotokoll.

Ehrliche Grenze: Ein bereits **zu enges** Limit heilt der Berater nicht von
selbst - was die Suche nie geladen hat, steht nicht in der Datenbank. Genau
deshalb starten die neuen Kategorien mit großzügigen Werten.

---

## 8c. Titelprüfung: Schlagwörter statt KI

Die Suche ist absichtlich breit: Sie fragt Kategoriebegriffe, Modellnamen und
Schreibvarianten ab und bekommt deshalb auch fremde Produkte, Zubehör und
Ersatzteile zurück. Die Titelprüfung entscheidet je Anzeige **rein über
Schlagwörter**, ob dort das gesuchte Produkt verkauft wird. Kein Modell, keine
Wartezeit, kein Ausfall.

Die Prüfung arbeitet in drei Schritten:

1. **Teile, Zubehör und Verpackung.** Eindeutige Wendungen greifen auch mitten
   im Titel („… GRAPHICS CARD FAN“, „Ersatzlüfter“, „Lüfter für RTX 3060“,
   „Halterung“, „Riser-Kabel“, „nur OVP“, „ohne Chip“, „ohne VRAM“, „nur
   Platine“, „für Ersatzteile“). Dazu die Verbindung „Teil + für/passend“:
   Titel wie `FD8015U12D VGA Fan for GTX1660Ti …` verkaufen einen Lüfter, kein
   Modell. Ein Teilwort am Titelanfang („Graka Lüfter Zotac RTX 2060 …“) oder
   als Hauptwort am Ende („… RX6800XT Reference Cooler“) benennt ebenfalls das
   Verkaufte. Mehrzahlformen („Wärmeleitpads“, „Kartons") zählen mit.
   Teilwörter, die nur als Bestandteil eines zusammengesetzten Wortes stehen
   („Ersatz**halterung**“, „**Riser**kabel“), zählen ebenso - sonst rutschen
   genau diese Anzeigen durch.
   Ein bloßes „Fan“ mit Zählwort („ASUS RTX 3060 **Dual** Fan“) bleibt ein
   gültiges Angebot - es ist eine Eigenschaft der Karte.
2. **Fremde Geräte.** Notebook- und PC-Baureihen („Razer Blade“, „Legion Pro“,
   „Predator Helios“, „Gaming Laptop“), Server-Baugruppen („Backplane“,
   „Integrity“), Docking- und eGPU-Gehäuse sowie Laufwerksgrößen („1TB“,
   „500GB“) - sie alle benennen ein Gerät, in dem die Karte steckt. Auch ein
   Titel, der das Modell ausdrücklich verneint („HP OMEN 45L – **Ohne RTX
   5090** – 16 GB DDR5“), verkauft die Karte nicht.
3. **Passende Komponente.** Der Titel muss die Komponente nennen - über die
   Kauf-Limit-Liste („RTX 3080“, „RTX 3080 10GB“, auch ohne Leerzeichen als
   „RTX3080“) bzw. ohne Liste die Schlagwörter des Suchbegriffs. Fremde Modelle
   und Komponenten anderer Gruppen fallen damit heraus.

**Die Beschreibung als Rückfall.** Der Titel genügt in der Regel - aber nicht
immer: Kleinanzeigen zeigt unter dem Titel häufig nur „Grafikkarte“ und nennt
das Modell im Text; die eBay-API liefert eine Kurzbeschreibung mit. Nennt der
Titel die Komponente **nicht** (und nur dann), entscheidet deshalb die
**Beschreibung** - beurteilt mit **denselben** Regeln (`titles.match_product`).
Verkauft der Titel erkennbar ein Teil, Zubehör, eine Verpackung, ein fremdes
Gerät oder ist es ein Suchgesuch, bleibt es dabei: Die Beschreibung hebt das
nicht auf. Wie viele Angebote so zugeordnet wurden, steht getrennt im Laufbericht
("davon über die Beschreibung zugeordnet") und in der Bestandsprüfung (`gerettet`).

Wichtig für den Gleichlauf: Die **Bestandsprüfung** (siehe unten) nutzt dieselbe
Funktion. Sonst würde sie eine gerade über die Beschreibung gerettete Anzeige am
Anfang des nächsten Suchlaufs sofort wieder aussortieren.

**Beigaben bleiben erlaubt.** Steht vor einem Teil- oder Verpackungswort ein
„mit“, „inkl.“, „inklusive“, „in OVP“ oder „ohne“, beschreibt es die Karte:
`MSI RTX 3070 Ti … inklusive OVP/Originalverpackung`, `… Grafikkarte mit OVP`,
`KFA2 RTX 3070 Ti … inkl. OVP & Zubehör`. Ankündigungen wie „nur Verpackung“
oder „BOX ONLY“ schlagen jede solche Ausstattungsangabe.

Ist das gesuchte Produkt selbst das Teil (Suche nach „Lüfter“), entfällt die
jeweilige Ausschlussregel - dann gehört das Teil zur Suche.

Was nicht passt, wird **gar nicht erst gespeichert** und zählt nur im
Abdeckungsbericht (`excluded_title_filter`, in der Oberfläche unter
„Titelprüfung“).

### Der gespeicherte Bestand wird mitgeprüft

Jede Verbesserung der Regeln macht frühere Läufe nicht rückgängig: Was eine
ältere, schwächere Fassung durchgelassen hat, blieb bisher für immer in der
Ergebnisliste stehen - und verdeckte genau die echten Treffer. Deshalb läuft am
**Anfang jedes Suchlaufs** eine Bestandsprüfung (`backend/search/revalidate.py`)
über alle aktiven Angebote, und sie lässt sich jederzeit von Hand anstoßen:

* `POST /api/listings/revalidate` (in der Oberfläche der Knopf **„Bestand
  prüfen“** neben der Ergebnisliste).

Aussortiertes wird auf `rejected` gesetzt, mit dem Grund (`reject_reason`) und
einer Warnung am Angebot. Der Datensatz bleibt erhalten: Die Entscheidung ist
nachvollziehbar, und eine versehentlich zu strenge Regel ist kein
Datenverlust. Zwei Dinge bleiben unangetastet - die **Merkliste** (was du selbst
markiert hast, entscheidest du) und der **lokale Import**.

Aussortierte und nicht mehr abrufbare Angebote (`rejected`, `gone`) erscheinen
nicht mehr in der Liste. Sichtbar wird die Zahl im Lauf unter „Auswahl per
Titelregel aussortiert: … aus dem Bestand entfernt“.

### Filter schon auf der Plattform

Die Suche schränkt wo möglich **auf der Website** ein, statt alles zu laden und
dan wegzuwerfen:

* **Nur gebrauchte Ware**: eBay bekommt die gebrauchten Zustände
  (`LH_ItemCondition`), also weder Neuware noch „Als Ersatzteil / defekt".
  Ohne diesen Filter bestand die Trefferliste überwiegend aus Neuware, die
  ohnehin nie gespeichert würde. (Vorher: 134 Neuware-Treffer in einem Lauf -
  jetzt 0.)
* **Nur Sofort-Kauf** (`LH_BIN`). Höchst- und Mindestpreis der Komponente gehen
  zusätzlich als Preisrahmen an die Quelle (`_udhi`/`_udlo`, bei Kleinanzeigen
  `maxPrice`/`minPrice`) **und** werden lokal erneut geprüft. Ein zu enger
  Rahmen verdrängt Treffer - live vermessen: Mit dem alten, zu niedrigen Limit
  nannten 97 % der gelieferten Titel ein anderes Modell, ohne Rahmen 5 %. Seit
  dem 13.10.2026 passen die Limits zum Markt, der Rahmen schneidet jetzt vor
  allem teure Treffer ab (siehe 8b).
* **Standort/Umkreis:** Die eBay-**Website** kann das nicht - live geprüft:
  `_stpos`/`_sadis` und `LH_PrefLoc` liefern dieselben Treffer wie ohne Filter
  (75 Artikel, 74 identisch; die Abweichung sind rotierende Werbeplätze). Hier
  greift deshalb nur der Hinweis in der Oberfläche, und der Standort wird - wo
  die Quelle ihn liefert - nach dem Abruf berücksichtigt. Die offizielle
  eBay-API kann dagegen serverseitig nach Abhol-PLZ und Umkreis filtern.

### Was vorher schon aussortiert wird

Ausschlussbegriffe, Auktionsformat, Preisgrenzen (Höchst- und Mindestpreis),
Standort und Zeitraum prüft die Anwendung vor der Titelprüfung. Dazu die
eindeutigen Fälle „nur Verpackung/Papiere“ (`nur OVP`, `ohne Karte`,
`nur Rechnung`, `leere Verpackung`) und Titel, die mit „nur <Teil>“ beginnen
(`NUR KÜHLER!! MSI RTX 3060 GAMING X 12G`): Angeboten wird das Teil, nicht das
Produkt. Steht das Teilwort dagegen mitten im Titel („MSI RTX 3060, nur Kühler
getauscht“, „mit OVP und Rechnung“), bleibt es ein gültiges Angebot.

Zwei Gruppen fallen immer durch, unabhängig vom Titel:

| Ausschluss | Signal |
| --- | --- |
| **Defekt / Ersatzteil** | Zustand etwa „Als Ersatzteil / defekt", „Nur Ersatzteile" oder Textsignale wie `defekt`, `Ersatzteil`, `funktioniert nicht`, `nicht funktionsfähig`, `läuft/startet nicht`, `kein Bildsignal`, `Lüfter defekt`, `Wasserschaden`, `abgebrochene/verbogene Pins`, `oxidiert`, `Riss`, `Bastlerartikel` |
| **Neuware** | Zustand „Neu", „Neu: Sonstige", „Neu mit/ohne Etikett", „versiegelt", „brandneu" |

Weiche Hinweise verwerfen nichts: „keine Garantie", „ungetestet", „ohne OVP",
„Kratzer", „Spulenfiepen" oder „kein Defekt" sind keine Defekte. Verneinungen
werden berücksichtigt („kein Defekt" ist ein gutes Zeichen).

Der lokale Import ist von der Titelprüfung ausgenommen: Diese Angebote hast du
selbst ausgewählt, sie werden nicht nachträglich aussortiert.

---

## 9. Deal-Score (intern, in der Oberfläche unsichtbar)

Der Score bestimmt nur die **Reihenfolge** der Ergebnisse („bester Treffer"
zuerst). Er wird in der Oberfläche nicht angezeigt.

Der Score ist rein deterministisch: gleiche Eingaben und gleiche Gewichte
ergeben immer denselben Wert (`backend/scoring/deal.py`).

| | Kriterium | Standardgewicht |
| --- | --- | --- |
| A | Preisvorteil gegenüber Marktwert | 30 |
| B | Preis-Leistung | 14 |
| C | Datenqualität | 6 |
| D | Zustand und Risiko | 18 |
| E | Gesamtkosten (Versand) | 6 |
| F | Aktualität | 6 |
| G | Modellabhängige Besonderheiten (Wissensbasis) | 6 |
| H | Standort und Abholung | 5 |
| I | Marktvergleich | 5 |
| J | Informationsqualität | 4 |

Dazu gibt es harte Gates — ein niedriger Preis allein erzeugt **keinen**
Top-Score:

* ohne erkanntes Modell: Score höchstens 45
* Modellzuordnung unklar (Konflikt zwischen Modellen): höchstens 62
* widersprüchliche Modellzuordnung: höchstens 74
* nicht mehr verfügbares Angebot: höchstens 5

Urteile: *Herausragender Deal*, *Guter Deal*, *Fairer Preis*, *Kein besonderer
Deal*, *Potenziell riskant*, *Zu wenig Daten*.

Risikosignale entstehen deterministisch aus dem Anzeigetext
(`backend/scoring/risks.py`) und berücksichtigen Verneinungen: „kein
Spulenfiepen" ist kein Spulenfiepen-Signal, „ohne Garantie" kein
Garantiesignal. Anzeigentexte werden ausschließlich als Daten behandelt;
Anweisungen darin können das Bewertungssystem nicht beeinflussen.

### Benchmark

`backend/benchmarks/deal_benchmark.py` prüft mit Beispielangeboten, ob bekannte
gute Deals höher bewertet werden als überteuerte, riskante oder unklare.
Die Beispielpreise sind reine Benchmark-Daten, keine Marktpreise.

---

## 10. Projektstruktur

```
backend/
  api/            HTTP-Endpunkte (Suche, Angebote, System, Einstellungen)
  benchmarks/     reproduzierbarer Deal-Benchmark
  connectors/     Plattform-Connectoren + Registry (einheitliche Schnittstelle)
  domain/         Normalisierung, Hardware-Erkennung, Deduplizierung, Formate
  knowledge/      Wissensbasis-Loader
  pricing/        Marktpreisermittlung (Median, Quartile, Unsicherheit)
  scoring/        Risikoanalyse, Deal-Score
  search/         Suchplan, Ingestion, Titelprüfung, Runner, Abdeckungsbericht
  services/       Datenzugriff (Repository), Laufzeit-Einstellungen
  config.py       Bootstrap- und Laufzeit-Konfiguration
  db.py           Engine, Session, schlanke Migrationen
  main.py         FastAPI-Anwendung + Auslieferung der Oberfläche
frontend/         React + TypeScript + Vite
knowledge/        Wissensbasis (YAML) und Notizen (Markdown)
scripts/          Hilfsprogramme (HTML-Import, Portprüfung, Funktionstest)
tests/            pytest-Suite + Fixtures
```

---

## 11. Betrieb

Die Oberfläche hat genau eine Aufgabe: Angebote finden und den Link dazu
zeigen. Eine Seite, kein Dashboard:

* **Welches Produkt genau?** den Produktnamen eintippen, z. B. `RTX 3080 Ti`
  oder `Ryzen 5 5600X`. Kategorien werden daraus abgeleitet, nicht mehr
  ausgewählt.
* **Budget**, **Standort (PLZ)**, **Umkreis** und **Lieferung** (Versand,
  Abholung oder beides) festlegen, dazu die **Mindestqualität** (ab welcher
  Bewertung Angebote erscheinen) und die **Quelle** - dann **Suchen**.
  Die Lieferungsauswahl gilt auch für die Ergebnisliste: Bei **Nur Versand**
  wird schon beim Abruf gefiltert, wo die Quelle das kann (Kleinanzeigen
  nutzt dafür seine eigene Einordnung "Versand möglich"); was belegt nur zur
  Abholung steht, erscheint nicht in der Liste. **Nur Abholung** zeigt nur
  Angebote mit verwertbarem Verkäuferstandort. "Versand und Abholung" lässt
  alles stehen.
* Während der Suche steht **eine Zeile** Status ("Suche läuft … X Angebote
  gefunden") mit **Abbrechen** daneben. Keine Zwischenberichte, keine Tabellen.
* **Ergebnisse:** eine Liste aus Links. Pro Zeile: Bewertung, Titel, Quelle mit
  Datum und PLZ, Gesamtpreis und "öffnen ↗". Die ganze Zeile öffnet die
  Originalanzeige in einem neuen Tab. „Mehr anzeigen" lädt weitere.
* **Einstellungen:** standardmäßig eingeklappt (eine Zeile Text). Darin:
  Suchlimits (Seiten je Begriff, gleichzeitige Seiten) und die Knöpfe
  **„Kauf-Limits nachführen"** (siehe Abschnitt 8b) sowie
  **„Alle Angebote und den Suchverlauf löschen"** (`POST /api/maintenance/clear`).

Für den normalen Aufräumfall braucht es den Knopf nicht mehr: Die
**Bestandsprüfung** (siehe Abschnitt 8c) beurteilt alles Gespeicherte mit den
aktuellen Regeln und nimmt Aussortiertes aus der Liste — am Anfang jedes
Suchlaufs und auf Wunsch über den Knopf „Bestand prüfen".

Der Löschknopf ist der bewusste, harte Neuanfang: Er leert die Angebote, den
Preisverlauf und den kompletten Suchverlauf in **einem Schritt** (nach einer
Rückfrage, ohne Papierkorb — das lässt sich nicht rückgängig machen).
**Einstellungen und Wissensbasis bleiben unverändert**, ein laufender Suchlauf
wird vorher abgebrochen und abgewartet, sonst schriebe er die eben geleerten
Tabellen sofort wieder voll. Der nächste Suchlauf füllt die Liste neu.

Es wird nie behauptet, eine Plattform sei „vollständig durchsucht". Der
Abdeckungsbericht (`/api/search/runs/{id}/coverage`) enthält dafür ein
explizites Feld `fully_searched` samt Begründung — die Oberfläche zeigt ihn
bewusst nicht an, damit sie schlicht bleibt.

**Stabilität der Browser-Nutzung** (beide Website-Connectoren):

| Verhalten | Einstellung |
| --- | --- |
| Mindestabstand zwischen zwei Seitenaufrufen | `*_WEB_MIN_DELAY_SECONDS` |
| Harte Obergrenze je Abruf (Seitenladezeit + Browserstart) | `*_WEB_TIMEOUT_SECONDS` |
| Zusätzliche Versuche nach einer Bot-Blockade (Pause + Startseite neu laden) | `*_WEB_BLOCK_RETRIES` |
| Browser nach Ruhe schließen (Windows und Speicher freigeben) | `*_WEB_IDLE_SECONDS` |
| Kein sichtbares Fenster über der Arbeit (Standard) | `*_WEB_HIDDEN` (`false` = Fenster zeigen) |
| Gar kein Fenster, reines Headless | `*_WEB_HEADLESS` (eBay blockt das oft mit `403`) |
| Trefferliste im offenen Tab per `fetch` holen (schnell) | `*_WEB_INLINE_FETCH` (`false` = jede Seite vollständig laden) |
| Welcher Browser sucht (`auto`, `chrome`, `chromium`, `firefox`) | `*_WEB_BROWSER` (Standard `auto`) |

Das Fenster bleibt unsichtbar, weil Chrome mit `--window-position=-32000,-32000`
startet und direkt ausgeblendet wird (Windows: `ShowWindow(SW_HIDE)`, damit auch
kein Eintrag in der Taskleiste bleibt). Nachgeblendet wird nur noch, wenn
wirklich ein neuer Tab entsteht - das Schnellverfahren braucht keinen.
Nicht-Windows-Systeme: minimiert außerhalb des Bildschirms.

Für Firefox gibt es die Chromium-Schalter (`--window-position`,
`--start-minimized`) nicht; dort wird das Fenster allein über die WinAPI
ausgeblendet. Minimieren über CDP fällt weg (CDP kennt nur Chromium), deshalb
gibt es als zweiten Weg `ShowWindow(SW_MINIMIZE)` über die WinAPI. Erkannt wird
der richtige Fenstertyp über die Fensterklasse: `Chrome_WidgetWin…` bzw.
`Mozilla…`; angefasst werden weiterhin nur Fenster der eigenen Browser-Prozesse.
Auf Nicht-Windows-Systemen lässt sich ein Firefox-Fenster nicht verbergen -
das wird als Warnung gemeldet statt still ignoriert. In der Prüfung (Windows,
Firefox 155 von Playwright) war bei beiden Quellen **kein einziges** der 8 bzw.
10 eigenen Firefox-Fenster sichtbar (`IsWindowVisible` für jedes Fenster der
eigenen Prozesse geprüft).

Nachgemessen mit der Windows-API (`IsWindowVisible`/`GetWindowRect`): Während
eines vollständigen Laufs über beide Website-Connectoren war in 32 Abfragen
**kein einziges** Chrome-Fenster sichtbar oder in der Taskleiste - und eBay
lieferte trotzdem Treffer (48 Treffer, 12 Anfragen, 0 Fehler). Das Ausblenden
bricht das Auslesen also nicht.

Ein abgestürzter, geschlossener oder hängender Browser beendet den Suchlauf
nicht: Der Connector baut ihn einmal sauber neu auf und liest die Seite erneut.
Ein Suchlauf läuft immer nur einmal gleichzeitig; die Oberfläche fragt den
Status währenddessen ab und bleibt bedienbar.

---

## 12. Ehrliche Einschränkungen

* **Kleinanzeigen:** keine zulässige automatisierte Schnittstelle → der
  API-Connector bleibt bewusst deaktiviert, offen dokumentiert. Nutzbar ist
  stattdessen die Website-Suche im eigenen Browser (Abschnitt 6.2), die aber
  den Nutzungsbedingungen unterliegt und abschaltbar ist.
* **eBay API:** benötigt eigene Zugangsdaten. Ohne sie ist dieser Connector
  nicht nutzbar; der Start wird mit Begründung abgelehnt statt still leer zu
  bleiben.
* **eBay Website:** funktioniert ohne Zugangsdaten, ist aber Bot-Schutz und
  Nutzungsbedingungen ausgesetzt: einzelne Anfragen können mit `403` blockiert
  werden (wird ehrlich gemeldet), und der Zeitraum wird nur über die Sortierung
  angenähert. Abschaltbar mit `EBAY_WEB_ENABLED=false`.
* **Firefox:** Playwright braucht dafür seinen eigenen Firefox-Build
  (`playwright install firefox`, ca. 120 MB); das installierte Firefox lässt
  sich nicht steuern. Unter Windows bleibt das Firefox-Fenster unsichtbar
  (WinAPI), unter Linux/macOS ist das nicht möglich - dort dann `*_WEB_HEADLESS`
  nutzen oder bei Google Chrome bleiben.
* **Erster Start ohne Zugangsdaten:** nutzbar sind jetzt die eBay-Website, die
  Kleinanzeigen-Website und der lokale Import. Die Startskripte laden zusätzlich automatisch die
  Beispieldaten aus `samples/beispiel_angebote.json` (Abschnitt 7.1).
* **Veröffentlichungsdatum:** eBay liefert es in der Suche nicht als Filterfeld
  und nicht für jedes Angebot. Unbekannte Daten werden als unbekannt geführt.
* **Entfernungen:** Es liegen keine Geodaten vor. Die Entfernung wird aus der
  PLZ-Zone grob geschätzt und in der Oberfläche als grob gekennzeichnet; ohne
  PLZ wird keine Entfernung berechnet.
* **Marktwerte:** Angebotspreise sind keine Verkaufspreise. Bei dünner Datenlage
  wird die Unsicherheit deutlich angezeigt und ggf. gar kein Marktwert genannt.
* **Kein Zugriffsschutz-Umgehen:** Die Anwendung implementiert keine
  Mechanismen, die CAPTCHAs, Loginsperren oder technische Schutzmaßnahmen
  umgehen. Die eBay-Website-Suche nutzt einen normalen Browser ohne
  Umgehungstricks (kein Headless-Fälschen, nur ein Fenster außerhalb des
  Bildschirms); wird sie blockiert, meldet sie das, statt es zu verschleiern.
* **Titelprüfung:** Sie arbeitet über Schlagwörter. Der **Titel** entscheidet;
  die **Beschreibung** springt nur ein, wenn der Titel die Komponente gar nicht
  nennt - und ausschließlich bei diesem Fall (nicht bei Teil, Verpackung,
  fremdem Gerät oder Suchgesuch). Ein Titel, der die gesuchte Komponente anders
  schreibt als erwartet, kann darunter fallen; umgekehrt bleibt ein Titel
  gültig, der ein Teil nur beiläufig nennt („mit OVP und Rechnung").
* **Fake-Erkennung:** Es gibt keine verlässliche automatische Betrugserkennung —
  nur textbasierte Risikosignale, die als solche gekennzeichnet sind.
* **Kauf-Limits:** Die Suche deckt nur die Komponenten ab, die in
  `Höchstpreise.txt` stehen; fehlt eine Zeile, wird sie nicht gesucht. Die
  Grenze inkl. Versand kann nur prüfen, was die Plattform als Versandkosten
  angibt — bei unbekannten Versandkosten zählt der Artikelpreis. Der
  Mindestpreis (ein Viertel des Höchstpreises) wird aus dem jeweils aktuellen
  Limit abgeleitet und gilt beim Suchen und Speichern; bereits gespeicherte
  Angebote werden nur gegen den Höchstpreis geprüft, wenn sie angezeigt werden.
* **Preislisten-Lauf:** Solche Suchläufe sind lang (eine Anfrage je Komponente
  und Plattform, bei breiten Begriffen zusätzlich eine zweite Suchansicht). Das
  ist beabsichtigt: dafür ist die Abdeckung genau die Liste. Der Lauf lässt
  sich jederzeit pausieren oder abbrechen.

---

## 13. Datenschutz und Nutzungsbedingungen

* Alle Daten bleiben lokal in einer SQLite-Datei.
* Gespeichert werden nur Informationen, die für Suche und Bewertung nötig sind.
* Bei eBay werden ausschließlich öffentliche Suchdaten über die offizielle API
  verwendet.
* Bitte beachte die Nutzungsbedingungen der jeweiligen Plattform, wenn du
  eigene Daten importierst.
