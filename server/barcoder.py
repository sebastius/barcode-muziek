"""Barcode van een CD -> album opzoeken (Discogs/MusicBrainz) -> afspelen via Music Assistant in Home Assistant.

Ontvangt barcodes via HTTP (POST /scan met de barcode als body, of GET /scan/<barcode>)
of als argument op de command line.
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import aiohttp
from aiohttp import web

DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
# Eén of meer media_player entities, komma-gescheiden. Voor synchroon afspelen op
# meerdere HomePods: maak in Music Assistant een player group en zet die hier.
HA_PLAYERS = [p.strip() for p in os.environ["HA_PLAYERS"].split(",") if p.strip()]
CACHE_FILE = Path(os.environ.get("CACHE_FILE", "cache.json"))
PORT = int(os.environ.get("PORT", "8080"))
DEBOUNCE_SECONDS = 5
USER_AGENT = "barcode-muziek/1.0 (https://github.com/sebastius/barcoder)"

log = logging.getLogger("barcoder")


# --- cache --------------------------------------------------------------------

def load_cache():
    try:
        return json.loads(CACHE_FILE.read_text())
    except FileNotFoundError:
        return {}


def save_cache(cache):
    tmp = CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
    tmp.replace(CACHE_FILE)


# --- album opzoeken -------------------------------------------------------------

def barcode_variants(code):
    """Scanners geven een UPC-A soms als EAN-13 met voorloopnul, of andersom."""
    variants = [code]
    if len(code) == 13 and code.startswith("0"):
        variants.append(code[1:])
    elif len(code) == 12:
        variants.append("0" + code)
    return variants


def clean_artist(artist):
    # Discogs: "Prince (2)" en "Björk*" -> "Prince", "Björk"
    return re.sub(r" \(\d+\)$", "", artist.strip()).rstrip("*").strip()


def most_common(candidates):
    """Meerdere releases (persingen, maar ook bootlegs) delen soms een barcode: neem het vaakst genoemde album."""
    if not candidates:
        return None
    return max(set(candidates), key=candidates.count)


async def lookup_discogs(session, codes):
    headers = {"Authorization": f"Discogs token={DISCOGS_TOKEN}"}
    for code in codes:
        params = {"barcode": code, "type": "release", "per_page": "25"}
        async with session.get("https://api.discogs.com/database/search", params=params, headers=headers) as r:
            r.raise_for_status()
            data = await r.json()
        candidates = []
        for result in data.get("results", []):
            # Zoekresultaten hebben de titel als "Artiest - Album"
            if " - " in result.get("title", ""):
                artist, album = result["title"].split(" - ", 1)
                candidates.append((clean_artist(artist), album.strip()))
        if candidates:
            return most_common(candidates)
    return None


async def lookup_musicbrainz(session, codes):
    params = {"query": " OR ".join(f"barcode:{c}" for c in codes), "fmt": "json", "limit": "25"}
    async with session.get("https://musicbrainz.org/ws/2/release/", params=params) as r:
        r.raise_for_status()
        data = await r.json()
    releases = [rel for rel in data.get("releases", []) if rel.get("barcode") in codes]
    official = [rel for rel in releases if rel.get("status") == "Official"]
    candidates = [
        ("".join(ac["name"] + ac.get("joinphrase", "") for ac in rel["artist-credit"]).strip(), rel["title"].strip())
        for rel in official or releases
    ]
    return most_common(candidates)


async def lookup(session, code):
    """Vraagt Discogs en MusicBrainz tegelijk; de eerste met een treffer wint."""
    codes = barcode_variants(code)
    sources = {"musicbrainz": lookup_musicbrainz}
    if DISCOGS_TOKEN:
        sources["discogs"] = lookup_discogs

    async def run(name, func):
        return name, await func(session, codes)

    tasks = [asyncio.create_task(run(name, func)) for name, func in sources.items()]
    try:
        for next_done in asyncio.as_completed(tasks):
            try:
                name, result = await next_done
            except Exception as e:
                log.warning("Lookup mislukt: %r", e)
                continue
            if result:
                log.info("Gevonden via %s: %s - %s", name, *result)
                return result
    finally:
        for task in tasks:
            task.cancel()
    return None


# --- Home Assistant ------------------------------------------------------------

async def call_ha(session, service, payload):
    async with session.post(
        f"{HA_URL}/api/services/{service.replace('.', '/')}",
        json=payload,
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as r:
        if r.status >= 400:
            raise RuntimeError(f"{service} gaf HTTP {r.status}: {await r.text()}")


async def play(session, artist, album):
    payload = {"entity_id": HA_PLAYERS, "media_type": "album", "enqueue": "replace"}
    try:
        await call_ha(session, "music_assistant.play_media", payload | {"media_id": album, "artist": artist})
    except RuntimeError as e:
        # Bijv. als de artiestnaam net anders gespeld is op YouTube Music: laat MA breder zoeken
        log.warning("%s; opnieuw met losse zoekterm", e)
        await call_ha(session, "music_assistant.play_media", payload | {"media_id": f"{artist} - {album}"})


async def notify(session, message):
    try:
        await call_ha(session, "persistent_notification.create",
                      {"title": "Barcode muziek", "message": message, "notification_id": "barcoder"})
    except Exception as e:
        log.warning("Notificatie mislukt: %r", e)


# --- verwerking ----------------------------------------------------------------

class Barcoder:
    def __init__(self, session):
        self.session = session
        self.cache = load_cache()
        self.current = None
        self.last_code = None
        self.last_time = 0.0

    def scan(self, raw):
        code = re.sub(r"[^0-9]", "", raw)
        if not code:
            return
        now = time.monotonic()
        if code == self.last_code and now - self.last_time < DEBOUNCE_SECONDS:
            log.info("Dubbele scan genegeerd: %s", code)
            return
        self.last_code, self.last_time = code, now
        # Nieuwe scan terwijl de vorige nog bezig is: de nieuwste wint
        if self.current and not self.current.done():
            self.current.cancel()
        self.current = asyncio.create_task(self.handle(code))

    async def handle(self, code):
        started = time.monotonic()
        log.info("Barcode: %s", code)
        try:
            if code in self.cache:
                artist, album = self.cache[code]["artist"], self.cache[code]["album"]
                log.info("Uit cache: %s - %s", artist, album)
            else:
                result = await lookup(self.session, code)
                if not result:
                    log.warning("Geen album gevonden voor %s", code)
                    await notify(self.session, f"Geen album gevonden voor barcode {code}")
                    return
                artist, album = result
                self.cache[code] = {"artist": artist, "album": album}
                save_cache(self.cache)
            await play(self.session, artist, album)
            log.info("Speelt: %s - %s (%.1fs)", artist, album, time.monotonic() - started)
        except asyncio.CancelledError:
            log.info("Afgebroken voor nieuwere scan: %s", code)
            raise
        except Exception as e:
            log.exception("Afspelen mislukt voor %s", code)
            await notify(self.session, f"Afspelen mislukt voor barcode {code}: {e}")


# --- HTTP server ---------------------------------------------------------------

async def handle_post(request):
    request.app["barcoder"].scan(await request.text())
    return web.Response(status=202, text="ok\n")


async def handle_get(request):
    request.app["barcoder"].scan(request.match_info["code"])
    return web.Response(status=202, text="ok\n")


async def make_session():
    # Eén sessie voor alles, zodat TCP/TLS-verbindingen hergebruikt worden
    return aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=10),
    )


async def init_app():
    app = web.Application()
    session = await make_session()
    app["barcoder"] = Barcoder(session)
    app.on_cleanup.append(lambda app: session.close())
    app.add_routes([web.post("/scan", handle_post), web.get("/scan/{code}", handle_get)])
    return app


async def run_once(code):
    async with await make_session() as session:
        barcoder = Barcoder(session)
        barcoder.scan(code)
        if barcoder.current:
            await barcoder.current


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) > 1:
        asyncio.run(run_once(sys.argv[1]))
    else:
        web.run_app(init_app(), port=PORT, access_log=None)


if __name__ == "__main__":
    main()
