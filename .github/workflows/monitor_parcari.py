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
