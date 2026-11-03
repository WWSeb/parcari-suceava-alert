import asyncio
import json
from datetime import datetime, timedelta, timezone
import os
import urllib.parse
import urllib.request
import re
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright

SITE = "https://parcari.primariasv.ro/"
ZONA = "tineretului-prieteniei"
# Pastram locurile vechi si adaugam grupa noua, in aceeasi zona.
WANTED_OLD = set(range(262, 310))   # 48 locuri, deja monitorizate
WANTED_NEW = set(range(29, 46))     # 17 locuri noi
WANTED = WANTED_OLD | WANTED_NEW   # 65 locuri in total
OUT = Path("raport-monitor")
OUT.mkdir(exist_ok=True)
found = {}
tally = {"cereri_gis": 0, "raspunsuri_json": 0, "erori_gis": 0}
pending = set()
notes = []
zones_seen = set()

def scan(data):
    if not isinstance(data, dict):
        return
    feats = data.get("features")
    if isinstance(feats, list):
        for f in feats:
            a = f.get("attributes", {}) if isinstance(f, dict) else {}
            if not isinstance(a, dict):
                continue
            z = str(a.get("cod_zona", "")).strip().lower()
            if z:
                zones_seen.add(z)
            if z != ZONA:
                continue
            try:
                num = int(str(a.get("nr_loc", "")).strip())
            except (ValueError, TypeError):
                continue
            if num in WANTED:
                found[num] = {"nr_loc": num, "stare": a.get("stare"),
                              "cod_zona": a.get("cod_zona")}
    for key, value in data.items():
        if key != "features" and isinstance(value, dict):
            scan(value)

async def inspect(response):
    uri = response.url.lower()
    if not ("proxy.jsp" in uri or "/query?" in uri or "/query/" in uri):
        return
    tally["cereri_gis"] += 1
    if response.status != 200:
        return
    try:
        raw = await response.body()
        if len(raw) > 5_000_000:
            return
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return
        tally["raspunsuri_json"] += 1
        if "error" in obj:
            tally["erori_gis"] += 1
        else:
            scan(obj)
    except Exception:
        return

def on_response(response):
    task = asyncio.create_task(inspect(response))
    pending.add(task)
    task.add_done_callback(pending.discard)

async def flush():
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)

async def take(page, label):
    try:
        await page.screenshot(path=str(OUT / (label + ".png")), timeout=15000)
    except Exception as exc:
        notes.append("captura " + label + ": " + type(exc).__name__)
    await flush()
    print(label + ": " + str(len(found)) + " locuri identificate")

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(locale="ro-RO", viewport={"width": 1440, "height": 900})
        context.on("response", on_response)
        page = await context.new_page()
        try:
            await page.goto(SITE, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            # Folosim numai butonul public al hartii, fara tokenuri GIS.
            opened = False
            for frame in page.frames:
                links = frame.get_by_text(re.compile(r"Vezi harta|Hart[aă]", re.I))
                for i in range(min(await links.count(), 8)):
                    try:
                        await links.nth(i).click(timeout=3500)
                        opened = True
                        break
                    except Exception:
                        continue
                if opened:
                    break
            print("Buton harta apasat:", opened)
            await page.wait_for_timeout(10000)
            print("Pagini deschise:", len(context.pages))
            print("Cadre:", [urlsplit(fr.url).path for fr in page.frames[:10]])
            map_page = context.pages[-1]
            await take(map_page, "01-harta-initiala")

            # Cautam obiectul public ArcGIS MapView expus in pagina.
            # Daca nu e accesibil, continuam cu interfata vizuala.
            moved = False
            for fr in map_page.frames:
                try:
                    result = await fr.evaluate("""async () => {
                      const names = ['view','mapView','mapview','myView','view2d','map_view'];
                      for (const name of names) {
                        try {
                          const v = window[name];
                          if (v && typeof v.goTo === 'function' && v.type === '2d') {
                            await v.goTo({center:[26.2845,47.6703], zoom:18}, {animate:false});
                            return name;
                          }
                        } catch (_) {}
                      }
                      return null;
                    }""")
                    if result:
                        moved = True
                        print("Zoom prin MapView:", result)
                        await map_page.wait_for_timeout(7000)
                        await take(map_page, "02-zoom-arcgis")
                        break
                except Exception as exc:
                    notes.append("MapView: " + type(exc).__name__)

            if not moved:
                # Alternativa: folosim cautarea publica din harta.
                for fr in map_page.frames:
                    try:
                        search = fr.locator('input[placeholder*="adres" i], input[placeholder*="loc" i]')
                        if await search.count() == 0:
                            continue
                        field = search.first
                        await field.fill("Strada Prieteniei, Suceava", timeout=5000)
                        await map_page.wait_for_timeout(3000)
                        await field.press("Enter", timeout=3000)
                        print("Am incercat cautarea strazii Prieteniei")
                        await map_page.wait_for_timeout(6500)
                        await take(map_page, "02-cautare-strada")
                        break
                    except Exception as exc:
                        notes.append("Cautare strada: " + type(exc).__name__)

            if len(found) < 40:
                # Plan de rezerva: zoom cu rotita in cartierul Burdujeni
                # vazut in captura hartii orasului; tinta e aproximativa.
                for fr in map_page.frames:
                    root = fr.locator('.esri-view')
                    try:
                        if await root.count() == 0:
                            continue
                        box = await root.first.bounding_box()
                        if not box:
                            continue
                        if moved:
                            px = box['x'] + box['width'] * 0.5
                            py = box['y'] + box['height'] * 0.5
                        else:
                            px = box['x'] + box['width'] * 0.55
                            py = box['y'] + box['height'] * 0.40
                        await map_page.mouse.move(px, py)
                        for i in range(5):
                            await map_page.mouse.wheel(0, -490)
                            await map_page.wait_for_timeout(1800)
                        await map_page.wait_for_timeout(5000)
                        await take(map_page, "03-zoom-manual-automat")
                        print("Zoom prin rotita efectuat")
                        break
                    except Exception as exc:
                        notes.append("Zoom rotita: " + type(exc).__name__)
        except Exception as exc:
            notes.append("Navigare: " + type(exc).__name__ + " " + str(exc)[:120])
        await flush()
        await browser.close()

asyncio.run(main())
print("Cereri GIS:", tally["cereri_gis"], "| Raspunsuri JSON:", tally["raspunsuri_json"], "| Erori GIS:", tally["erori_gis"])
print("Locuri vechi 262-309:", len(WANTED_OLD & set(found)), "din 48")
print("Locuri noi 29-45:", len(WANTED_NEW & set(found)), "din 17")
print("Locuri gasite TOTAL:", len(found), "din 65")
(OUT / "diagnostic.json").write_text(json.dumps({
    "cereri": tally,
    "locuri": [found[k] for k in sorted(found)],
    "observatii": notes,
}, ensure_ascii=False, indent=2), encoding="utf-8")

# Vechiul grup trebuie sa fie complet: daca lipseste ceva, nu trimitem alerte.
# Noul grup se activeaza doar cand sunt gasite toate cele 17 locuri,
# fara sa blocam monitorizarea veche daca nu sunt in cadrul GIS actual.
old_missing = sorted(WANTED_OLD - set(found))
if old_missing:
    raise SystemExit(
        f"VERIFICARE INCOMPLETA pentru grupul vechi: lipsesc {len(old_missing)} "
        f"locuri: {old_missing}. Nu se trimit alerte si nu se salveaza stare."
    )
new_missing = sorted(WANTED_NEW - set(found))
new_complete = not new_missing
if new_complete:
    print("GRUP NOU ACTIV: au fost identificate toate locurile 29-45.")
else:
    print(
        "ATENTIE: grupul nou 29-45 NU este activ complet: "
        f"lipsesc {len(new_missing)} din 17: {new_missing}. "
        "Vechile 48 de locuri raman monitorizate."
    )

# Doar datele integral verificate sunt eligibile pentru alerte.
observed = {}
valid_observed = WANTED_OLD | (WANTED_NEW if new_complete else set())
for num in sorted(valid_observed):
    obj = found[num]
    try:
        value = int(obj["stare"])
    except (TypeError, ValueError, KeyError):
        raise SystemExit(f"Stare invalida pentru locul {num}. Nu se trimit alerte.")
    observed[str(num)] = value

print("Situatie observata:", " | ".join(
    f"stare {st}: {sum(v == st for v in observed.values())}"
    for st in sorted(set(observed.values()))
))

state_path = Path("stare-parcari.json")
prior = None
previous_saved_at = None
if state_path.exists():
    try:
        state_obj = json.loads(state_path.read_text(encoding="utf-8"))
        prior = state_obj["stari"]
        if not isinstance(prior, dict):
            raise ValueError("stari nu este obiect")
        allowed = [{str(n) for n in WANTED_OLD}, {str(n) for n in WANTED}]
        if set(prior) not in allowed:
            raise ValueError("set de locuri necunoscut/incomplet")
        if any(type(v) is not int for v in prior.values()):
            raise ValueError("valori invalide")
        previous_saved_at = state_obj.get("ultima_salvare")
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit("Fisierul de stare este invalid. Nu se trimit alerte.") from None

# Cand noul grup este temporar imposibil de citit, pastram in fisier
# ultimele sale stari cunoscute, fara a genera alerte pe date vechi.
current = dict(prior) if prior is not None else {}
current.update(observed)

available_now = sorted(int(n) for n, st in observed.items() if st == 1)
old_preexisting = set(prior or {})
newly_initialized = sorted(int(n) for n in observed if n not in old_preexisting)
newly_green = sorted(
    int(n) for n, st in observed.items()
    if st == 1 and n in old_preexisting and prior[n] != 1
)
new_added_green = sorted(
    num for num in newly_initialized
    if observed[str(num)] == 1 and num in WANTED_NEW
)

if prior is None:
    if available_now:
        message = (
            "🟢 PARCARI DISPONIBILE la prima verificare!\n"
            "Zona: Tineretului-Prieteniei\n"
            "Locuri: " + ", ".join(map(str, available_now)) + "\n"
            "https://parcari.primariasv.ro/"
        )
    else:
        message = (
            "✅ Monitorizare pornita: " + str(len(observed)) + "/65 locuri verificate.\n"
            "Zona: Tineretului-Prieteniei\n"
            + ("Grupele 29-45 si 262-309 sunt active.\n" if new_complete
               else "Deocamdata doar grupa 262-309 este completa.\n")
            + "Momentan nu exista locuri disponibile intre cele verificate."
        )
    print("Prima verificare: initializam starea.")
else:
    parts = []
    if newly_green:
        parts.append(
            "🟢 LOCURI DEVENITE DISPONIBILE!\n"
            "Zona: Tineretului-Prieteniei\n"
            "Locuri: " + ", ".join(map(str, newly_green))
        )
        print("Locuri nou devenite disponibile:", newly_green)
    if newly_initialized and new_complete:
        parts.append(
            "✅ Monitorizare extinsa: acum urmarim 65 de locuri, "
            "grupele 29-45 si 262-309."
        )
        if new_added_green:
            parts.append(
                "🟢 La activare, sunt deja disponibile din grupa noua: "
                + ", ".join(map(str, new_added_green))
            )
        print("Locuri noi introduse in monitorizare:", newly_initialized)
    if parts:
        message = "\n\n".join(parts) + "\nVerifica pe: https://parcari.primariasv.ro/"
    else:
        message = None
        print("Nicio schimbare care necesita notificare.")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Lipsesc TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID din GitHub Secrets.")
    payload = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true"
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as res:
            result = json.loads(res.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError("Telegram nu a confirmat")
    except Exception as exc:
        raise SystemExit(
            "Notificarea Telegram a esuat: " + type(exc).__name__
        ) from None
    print("Mesaj Telegram trimis cu succes.")

if message:
    send_telegram(message)

now = datetime.now(timezone.utc)
heartbeat_due = True
if previous_saved_at:
    try:
        saved_at = datetime.fromisoformat(previous_saved_at)
        heartbeat_due = now - saved_at >= timedelta(days=21)
    except (ValueError, TypeError):
        heartbeat_due = True

# Salvam numai dupa verificarea completa a vechiului grup si dupa
# trimiterea cu succes a mesajului (daca este cazul).
if prior is None or current != prior or heartbeat_due:
    new_state = {
        "zona": "Tineretului-Prieteniei",
        "stari": current,
        "ultima_salvare": now.isoformat(timespec="seconds"),
    }
    state_path.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )
    print("Starea a fost pregatita pentru salvare in repository.")
else:
    print("Nicio schimbare. Starea din repository ramane neschimbata.")
