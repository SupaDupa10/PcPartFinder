# Allgemeine Hinweise zur Hardware-Bewertung

Diese Datei wird **nicht** als Preisquelle verwendet. Sie dient nur als Ort für
Notizen, die beim manuellen Prüfen von Angeboten helfen. Der Suchplaner und die
Bewertung lesen ausschließlich die strukturierten Dateien (`.yaml`, `.json`).

## Warum werden keine Marktpreise mitgeliefert?

Weil sie sonst erfunden wären. Diese Anwendung erfindet keine Zahlen:

* **Beobachtete Angebotspreise** kommen ausschließlich aus tatsächlich
  abgerufenen Anzeigen. Sie sind Angebotspreise, keine erzielten
  Verkaufspreise — dieser Unterschied wird überall ausgewiesen.
* **Manuell gepflegtes Preiswissen** kommt aus `hardware_knowledge.yaml`.
  Diese Werte trägst du selbst ein, wenn du sie vertreten kannst.
* Wenn beides fehlt, lautet das Ergebnis **„Marktwert unsicher"** und nicht
  etwa ein geschätzter Betrag.

## Wie entsteht der Deal-Score?

Der Score ist rein deterministisch und reproduzierbar. Gleiche Eingaben und
gleiche Gewichte ergeben immer denselben Score. Jede Zahl ist im Detail
nachrechenbar.

Die Kriterien A–J sind in `backend/scoring/deal.py` dokumentiert und ihre
Gewichte in der Oberfläche unter „Einstellungen" einstellbar.

## Praktische Hinweise zum Gebrauchtmarkt

Diese Punkte sind Erfahrungswerte und bewusst als Hinweise, nicht als Regeln
formuliert:

* **Ex-Mining-Grafikkarten** sind in den Segmenten RX 570–580, RX 6600–6800 XT
  und RTX 3060–3090 verbreitet. Häufige Indizien: viele Karten gleichzeitig im
  Angebot, ungewöhnlich niedriger Preis, keine Originalverpackung, Hinweise auf
  Dauerbetrieb.
* **Lüfterverschleiß** ist bei lang genutzten Karten der häufigste Defekt und im
  Text meist nicht erwähnt. Fehlende Angaben sind daher ein Risikosignal, kein
  Qualitätsmerkmal.
* **Auktionen** werden von dieser Anwendung grundsätzlich ausgeschlossen.
  Angebote, deren Format nicht eindeutig als Festpreis erkennbar ist, werden
  ebenfalls verworfen statt geraten.
* **„Ohne Rechnung"** reduziert den Wiederverkaufswert und die
  Garantieaussichten deutlich.
* Bei **Netzteilen** ist das Alter entscheidend: Kondensatoren altern. Ein
  günstiges 8 Jahre altes Netzteil ist kein Schnäppchen.

## Eigene Datei verwenden

In den Einstellungen kann unter *Hardware-Wissensbasis* ein zusätzlicher Pfad
angegeben werden (`KNOWLEDGE_USER_FILE`). Eigene Dateien werden zusammen mit dem
Starterkatalog geladen. Ein fehlerhafter Eintrag führt nicht zum Abbruch — die
Anwendung meldet ihn im Bereich „Wissensbasis" und arbeitet mit dem Rest weiter.
