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
WANTED = set(range(262, 310))
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
