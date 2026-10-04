# prag-watch

Ein kleiner Wächter für ausverkaufte Kinovorstellungen. Ursprünglich gebaut für
**The Odyssey** von Christopher Nolan als 70-mm-IMAX-Filmkopie im Cinema City Flora in Prag,
jetzt umgestellt auf **Dune: Part Three** („Duna: část třetí“, Film-ID `8105s2r`) als
70-mm-IMAX im selben Haus. Start ist am 17.12.2026, eine Vorpremiere am 15.12.2026 ist schon
freigeschaltet und fast ausverkauft.
Neue Spieltage kamen dort unregelmäßig, und kurz danach waren nicht nur die guten Plätze weg,
sondern alle.

Das Script fragt alle zehn Minuten den öffentlichen Spielplan ab, protokolliert auf zehn Minuten
genau, **wann** neue Tage freigeschaltet werden, und schlägt Alarm, sobald es etwas zu holen gibt.
Bei mir hat es an einem Mittwoch kurz nach 22 Uhr die Uhr zum Klingeln gebracht. Zwei neue
Spieltage, Saal komplett frei.

Entstanden in ein paar Runden mit Claude Code. Die eigentliche Arbeit steckte in der Frage,
**wofür man sich nachts wecken lässt**. Dazu unten mehr.

## Was es tut

1. **Erkennt den Moment der Freischaltung.** Ein Tag zählt erst dann als neu, wenn er tatsächlich
   Vorstellungen des gesuchten Films trägt. Das Kino listet Termine auch dann, wenn dort nur
   Konzertfilme oder Wiederaufführungen laufen. Das ist keine Freischaltung.
2. **Beobachtet, wie schnell frische Tage volllaufen.** Aus dem Log lässt sich ablesen, ob ein frisch
   freigeschalteter Schwung nach einer Stunde noch zu holen ist oder nach zehn Minuten schon
   halb weg.

## Was es bewusst nicht tut

- **Kein Login, kein Umgehen von Schutzmaßnahmen.** Es spricht ausschließlich die öffentliche
  `quickbook`-API an, dieselbe, die auch die Website des Kinos benutzt.
- **Die Sitzplatz-API bleibt außen vor.** `tickets.cinemacity.cz/api/seats/*` liegt hinter einem
  Bot-Filter und antwortet mit 403. Das respektiert der Wächter. Er arbeitet mit der
  Verfügbarkeitsquote, die ihm die öffentliche API ohnehin nennt. Welche Reihe frei ist, schaut
  man beim Alarm selbst im Browser nach.
- **Es bucht nichts.** Es weckt nur.

### Zum Zehn-Minuten-Takt

Ein Aufruf ist rund ein Kilobyte. Das sind sechs Kilobyte pro Stunde, also weniger als ein
einzelner Seitenaufruf im Browser. Ein Vier-Stunden-Raster wäre höflicher, kann den
Freischaltzeitpunkt aber nur auf plus/minus vier Stunden eingrenzen, und damit lässt sich weder
ein Muster erkennen noch eine Reaktion planen.

Wenn Du das Script übernimmst: **lass den Takt, wo er ist, und stell `watch_until` ein.** Danach
hört es von selbst auf. Der Spielplanserver eines Kinos ist nicht dafür gebaut, auf Dauer im
Zehn-Minuten-Takt befragt zu werden.

## Für einen anderen Film anpassen

Die Konstanten stehen oben in `prag_watch.py`:

```python
TENANT = "10101"
CINEMA = "1052"          # Praha Flora, OC FLORA
FILM_NAME_HINT = "duna: cast treti"   # tschechischer Titel, ohne Akzente
ATTR_70MM = "70-mm"
```

Für einen anderen Film tauschst Du `FILM_NAME_HINT` gegen dessen tschechischen Titel.
Groß-/Kleinschreibung und Akzente spielen keine Rolle. Den Link zur Filmseite holt sich das
Script selbst aus der API. Nimm den vollen Titel: „dun“ trifft auch „Dunkerk“, und „duna“
würde auch eine 70-mm-Wiederaufführung von Teil 1 oder 2 treffen. Für ein anderes
Cinema-City-Haus reicht `CINEMA`. Für eine andere Kinokette taugt die Struktur als Vorlage, die
API-Aufrufe muss man neu schreiben.

## Einrichtung

```bash
git clone <dieses-repo> prag-watch && cd prag-watch
cp config.example.json config.json
python3 prag_watch.py setup-pushover   # optional, siehe unten
python3 prag_watch.py check            # einmal prüfen
python3 prag_watch.py test             # Testalarm über alle Kanäle
python3 prag_watch.py report           # Auswertung des Logs
```

Python 3 genügt, keine Abhängigkeiten.

**Prüf als Erstes `watch_until` in der `config.json`.** Das ist der Tag, an dem der Wächter
aufhören soll. Die Vorlage steht auf `2027-02-28`. Liegt der Tag in der Vergangenheit, meldet
`check` nur „watch_until passed“ und tut sonst nichts. Mit `restricted_from` und
`travel_weekdays` grenzt Du ein, an welchen Wochentagen Du überhaupt nach Prag fahren kannst.
Die Vorlage lässt alle Tage zu.

## Die zwei Alarmstufen

| Stufe | Auslöser | Verhalten |
|---|---|---|
| **Weckruf** | neue Spieltage freigeschaltet, unabhängig von der Auslastung | Pushover Priorität 2, Sirene, 20 Minuten bis „Acknowledge“ |
| **Weckruf** | Saal unter 25 Prozent belegt, also 75 Prozent noch frei (`wake_ratio` 0.75) | dito |
| **Info** | über 35 Prozent frei, aber unter der Weckschwelle | normale Priorität, nachts lautlos |

Der entscheidende Gedanke dahinter: Geweckt wird für **den Moment, in dem man handeln kann**,
nicht für jeden freien Platz. Das sind genau zwei Fälle, eine frische Freischaltung und ein Saal,
der plötzlich fast leer ist. Alles andere ist Information und kein Notfall.

Neue Spieltage wecken **immer**, auch wenn ein Schwung schon halb vorverkauft auftaucht und die
75-Prozent-Schwelle gar nicht reißt. Es ist trotzdem der einzige Moment, in dem die guten Reihen
noch zu haben sind. Ein Fehlweckruf kostet einen Tipp auf „Acknowledge“, ein verpasster kostet
die Reise.

## Benachrichtigungen

Alles in `config.json`, die ist gitignored, weil dort Tokens liegen.

- **[Pushover](https://pushover.net/)** (einmalig etwa 5 Euro) ist der einzige Kanal, der
  zuverlässig durch einen Schlaf-Fokus kommt. `python3 prag_watch.py setup-pushover` trägt die
  Zugangsdaten lokal ein, so wandert der Token nie durch einen Chat.
- **[ntfy.sh](https://ntfy.sh/)** ist kostenlos. App installieren, Topic abonnieren, Topic-Namen
  in die Config. Der Topic-Name ist das einzige Geheimnis, nimm also etwas Unratbares, sonst liest
  jeder mit.
- **macOS lokal** als Zweitkanal. Nutzlos, wenn der Mac schläft.

Alle konfigurierten Kanäle feuern parallel, ein fehlschlagender Kanal bricht den Aufruf nicht ab.

### Die Stolperfalle, die mich einen Abend gekostet hat

Der Push kam an und **vibrierte nur**, obwohl die Pushover-API ein Receipt zurückmeldete, die
Nachricht also nachweislich als Priorität 2 rausging. Es braucht zwei getrennte Ebenen, und die
zweite findet man nicht:

1. **iOS-Einstellungen, Mitteilungen, Pushover:** *Critical Alerts* einschalten. Das ist nur die
   Erlaubnis.
2. **In der Pushover-App selbst, unter Settings** (der fehlende Schritt):
   - *Critical Alerts for emergency-priority* an
   - *Volume for Critical Alerts* auf `1 (Loud)`
   - *Always use default* aus lassen, sonst überschreibt die App den Ton aus der Config

Merksatz: Die iOS-Berechtigung allein bewirkt nichts. Alle Systemschalter können richtig stehen
und es bleibt trotzdem stumm.

Gegenprobe bitte mit stummem iPhone, aktivem Schlaf-Fokus und Handy im Nebenzimmer, und mit
`test pushover`, damit kein gleichzeitiger Mac-Ton das Ergebnis verfälscht.

Und noch eine: **Ein Weckruf hört nicht auf, wenn man die Mitteilung öffnet.** Priorität 2 endet
erst mit „Acknowledge“ in der App. Ein Tipp auf den Buchungslink zählt nicht.

## Dauerbetrieb in der Cloud (claude.ai-Routine)

Für den Betrieb ohne eigenen Rechner gibt es `cloud_run.sh` und `config.cloud.json`. Eine
claude.ai-Routine startet stündlich eine frische Session und arbeitet dort drei Schritte ab:

1. `./cloud_run.sh check` holt `state.json` und `log.jsonl` aus dem Branch `watch-state` und
   prüft einmal. Alarme landen in `outbox.jsonl`.
2. Jeder Eintrag aus dem Postausgang geht per Gmail-Connector als E-Mail raus, mit Buchungslink.
3. `./cloud_run.sh persist` schreibt den Zustand zurück in den Branch `watch-state`.

Weil der Container zwischen zwei Läufen gelöscht wird, überlebt der Zustand nur in diesem Branch.
Den Branch nicht löschen! Ohne ihn startet der Wächter kalt und lernt den Spielplan still neu.

## Dauerbetrieb auf dem Mac

Beide Dateien tragen den Platzhalter `__INSTALL_DIR__`. Die drei Befehle ersetzen ihn durch den
echten Pfad, laden den Job und prüfen, ob er läuft:

```bash
sed "s|__INSTALL_DIR__|$PWD|g" local.prag-watch.plist > ~/Library/LaunchAgents/local.prag-watch.plist
launchctl load ~/Library/LaunchAgents/local.prag-watch.plist
launchctl list | grep prag-watch
```

**Ein schlafender Mac fragt nichts ab.** Falls Dein Rechner in den Ruhezustand geht, hilft der
zweite Job. Er hält `caffeinate -s` dauerhaft am Leben, verhindert nur den Systemschlaf, lässt
das Display schlafen und wirkt ausschließlich am Netzteil:

```bash
cp local.prag-watch-awake.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/local.prag-watch-awake.plist
pmset -g assertions | grep caffeinate
```

Nach der Buchung beide wieder abräumen:

```bash
launchctl unload ~/Library/LaunchAgents/local.prag-watch*.plist
rm ~/Library/LaunchAgents/local.prag-watch*.plist
```

## Dateien

| Datei | Zweck |
|---|---|
| `prag_watch.py` | der Wächter, keine Abhängigkeiten |
| `config.example.json` | Vorlage, kopieren nach `config.json` |
| `local.prag-watch.plist` | launchd-Job, alle zehn Minuten |
| `local.prag-watch-awake.plist` | hält den Mac wach |

Zur Laufzeit entstehen `state.json` (was zuletzt gesehen wurde), `log.jsonl` (das Messprotokoll, nur
Änderungen plus stündlicher Lebenszeichen-Eintrag) und die beiden Logdateien. Alle vier sind gitignored.

Eine Lücke von mehr als einer Stunde im Log heißt: der Wächter lief nicht.

## Lizenz

MIT. Ohne Gewähr, dass Du damit an Karten kommst.

---

Gebaut von Thomas Latus, [Modulr](https://www.modulr.design).
