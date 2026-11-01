# barcode-muziek

Scan de barcode van een CD, en het album speelt via YouTube Music op je HomePods.

```
barcodescanner ──UART──▶ Pico W ──HTTP──▶ barcoder.py ──▶ Discogs + MusicBrainz (tegelijk, + cache)
                                               │
                                               └──▶ Home Assistant ──▶ Music Assistant ──▶ YouTube Music
                                                                                     └──▶ AirPlay ──▶ HomePod mini(s)
```

## Waarom zo

- **Pico W → HTTP** in plaats van MQTT: één kort TCP-verzoek op je LAN, geen broker, en de Pico hoeft niet te wachten.
  De server antwoordt direct met `202` en zoekt daarna het album op.
- **Discogs en MusicBrainz worden tegelijk bevraagd**, en de eerste treffer wint. Elke barcode komt daarna in `cache.json`,
  dus een CD die je al eerder scande speelt zonder lookup.
- **Bootlegs met dezelfde barcode** worden overgeslagen: officiële releases krijgen voorrang, en daarbinnen het
  vaakst voorkomende album.
- **Music Assistant** zoekt het album op YouTube Music en streamt via AirPlay naar de HomePods. Spotify/Sonos-specifieke
  code is niet meer nodig, en de speakers wisselen is een kwestie van de entity aanpassen.
- Scan je een andere CD terwijl de vorige nog laadt, dan wint de nieuwste. Dubbele scans binnen 5 s worden genegeerd.
- Lukt het niet (album onbekend, afspelen mislukt), dan verschijnt er een notificatie in Home Assistant.

## 1. Music Assistant (eenmalig)

YouTube Music in Music Assistant vereist een **YouTube (Music) Premium**-account.

1. Start Music Assistant en de PO Token-server (nodig voor YouTube) op de Docker-host:
   ```sh
   cd music-assistant
   docker compose up -d
   ```
   Music Assistant draait met `network_mode: host`, anders vindt hij de HomePods niet. De web-UI staat op poort 8095.
   Draai je Home Assistant OS, gebruik dan in plaats hiervan de apps *Music Assistant* en *YT Music PO Token Generator*.
2. Maak bij de eerste keer openen een **admin-account** aan (een gewone gebruiker geeft rechtenfouten bij `play_media`).
3. **YouTube Music** toevoegen ([docs](https://www.music-assistant.io/music-providers/youtube-music/)):
   - Log in een incognito-/privévenster in op music.youtube.com, open de ontwikkelaarstools → Network, filter op
     `browse` en klik op een playlist in je bibliotheek.
   - Kopieer van dat verzoek de waarde van de `Cookie`-request-header. Die moet `__Secure-3PAPISID` bevatten.
   - Sluit het venster **zonder uit te loggen**.
   - In MA: *Settings → Music Sources → YouTube Music*, met je Gmail-adres en de cookie. De PO Token Server URL kan op
     de standaardwaarde blijven.
   - Stopt YouTube Music later met `401: Unauthorized` in de MA-log, haal dan een nieuwe cookie op.
4. **HomePods** ([docs](https://www.music-assistant.io/player-support/airplay/)): die verschijnen vanzelf onder de
   AirPlay-provider. Zet in de Woning-app bij AirPlay-toegang *Iedereen op hetzelfde netwerk* aan (niet
   *Alleen mensen in deze woning*).
5. Wil je op meerdere HomePods tegelijk afspelen? Maak in MA een **group player** (sync group) van de HomePods. Die
   verschijnt als eigen `media_player` in HA.
6. Koppel MA aan HA: *Instellingen → Apparaten & diensten → Music Assistant*, met `http://<docker-host>:8095` en het
   admin-account.
7. Test in HA via *Ontwikkelaarshulpmiddelen → Acties*:
   ```yaml
   action: music_assistant.play_media
   target:
     entity_id: media_player.woonkamer
   data:
     media_id: Nevermind
     artist: Nirvana
     media_type: album
   ```

## 2. Server (`server/`)

Draait op elke machine in je netwerk (je server, of naast HA).

```sh
cd server
cp .env.example .env     # vul DISCOGS_TOKEN, HA_URL, HA_TOKEN en HA_PLAYERS in
docker compose up -d --build
```

Of zonder Docker:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
.venv/bin/python barcoder.py
```

`barcoder.service` is een systemd-unit voor die laatste variant.

- `HA_TOKEN`: een long-lived access token (HA → je profiel → Beveiliging).
- `DISCOGS_TOKEN`: optioneel. Op [discogs.com/settings/developers](https://www.discogs.com/settings/developers) via
  *Generate new token* (40 tekens; de *Consumer Key* van een app werkt niet). Zonder token wordt alleen MusicBrainz
  gebruikt.

## Snelheid

Gemeten van scan tot muziek op een groep van twee HomePod minis:

| | Tijd |
|---|---|
| Eerste keer dat een CD gescand wordt | ~15 s, omdat Music Assistant het album van YouTube Music in zijn bibliotheek importeert |
| Daarna | ~5 s, vrijwel alleen het opzetten van de AirPlay 2-verbinding |

De barcode-lookup zelf (Discogs/MusicBrainz) kost ~0,3 s, en 0 s bij een barcode die al in de cache staat.

Testen zonder scanner:

```sh
curl -X POST --data 0720642442524 http://server:8080/scan
# of in de browser: http://server:8080/scan/0720642442524
# of eenmalig vanaf de command line:
.venv/bin/python barcoder.py 0720642442524
```

## 3. Pico W (`pico/`)

1. Flash MicroPython op de Pico W.
2. Kopieer `config.example.py` naar `config.py`, vul je WiFi en het IP van de server in.
3. Zet `main.py` en `config.py` op de Pico (bijv. met Thonny of `mpremote cp main.py config.py :`).

De scanner gaat net als voorheen op UART0 (GP0 TX, GP1 RX, 9600 baud). De onboard-LED:
knippert langzaam = WiFi verbinden; kort aan = scan wordt verstuurd; 5× snel = versturen mislukt.

## Wat er weg is t.o.v. de oude versie

De upload van de albumhoes naar het e-ink-display (`send_image`) zit er niet meer in. Music Assistant zet de hoes al
als `entity_picture` op de media_player, dus een HA-automatisering kan die doorsturen. Of laat het weten, dan zet ik
het terug in `barcoder.py`.
