"""
AVISOS POR TELEGRAM  ·  Radar de Acciones

Corre cada 15 minutos en GitHub y hace 3 cosas:
  1. Vincula Telegram: cuando alguien toca "Vincular Telegram" en la app y aprieta
     Iniciar en el bot, guarda su chat para mandarle avisos.
  2. Avisos de precio (con los precios de precios.json):
       🎯 llegó a tu precio de venta      👀 está a menos de 3% de llegar
       🛑 tocó tu stop                    ⚠️ está a menos de 3% del stop
       🛒 una acción en seguimiento bajó a tu precio de compra
  3. Resumen diario (lunes a viernes, después de las 19:30): tu cartera + top 3 del radar.

Cada aviso se manda UNA sola vez (queda anotado en Supabase). Si cambiás el precio
objetivo, se vuelve a activar.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET", "")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
APP_URL = os.environ.get("APP_URL", "")

AVISO_CERCA = 3          # % para avisar "estás cerca"
SUG_VENTA, SUG_STOP = 25, 10   # mismos sugeridos que la app
HORA_RESUMEN = (19, 30)  # hora Argentina
TOP_RESUMEN = 3


# ----------------------------- utilidades -----------------------------

def ahora_arg():
    return datetime.now(timezone.utc) - timedelta(hours=3)


def fmt(n, dec=None):
    if dec is None:
        dec = 0 if abs(n) >= 1000 else 2
    s = f"{abs(n):,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if n < 0 else "") + s


def pesos(n, dec=None):
    return ("-" if n < 0 else "") + "$ " + fmt(abs(n), dec)


def pct(n):
    return ("+" if n >= 0 else "") + fmt(n, 1) + "%"


def leer_json(ruta):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ----------------------------- Supabase -----------------------------

def _h(extra=None):
    h = {"apikey": SUPABASE_SECRET, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def sb_get(tabla, query="select=*"):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabla}?{query}", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def sb_patch(tabla, filtro, datos):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{tabla}?{filtro}", headers=_h(), json=datos, timeout=30)
    r.raise_for_status()


def sb_insert_ignorar(tabla, filas):
    if not filas:
        return
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{tabla}", json=filas, timeout=30,
                      headers=_h({"Prefer": "resolution=ignore-duplicates,return=minimal"}))
    r.raise_for_status()


# ----------------------------- Telegram -----------------------------

def tg(metodo, **datos):
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{metodo}", json=datos, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"ok": False}


def enviar(chat_id, texto):
    res = tg("sendMessage", chat_id=chat_id, text=texto, parse_mode="HTML", disable_web_page_preview=True)
    if not res.get("ok"):
        print(f"No se pudo enviar a {chat_id}: {res}")
    return res.get("ok", False)


def procesar_mensajes(perfiles):
    """Lee los mensajes nuevos que le mandaron al bot (vincular / desvincular / ayuda)."""
    res = tg("getUpdates", timeout=0)
    updates = res.get("result", []) if res.get("ok") else []
    por_codigo = {p["telegram_codigo"]: p for p in perfiles if p.get("telegram_codigo")}
    por_chat = {str(p["telegram_chat_id"]): p for p in perfiles if p.get("telegram_chat_id")}
    ultimo = None
    for u in updates:
        ultimo = u["update_id"]
        msg = u.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        texto = (msg.get("text") or "").strip()
        if not chat:
            continue
        if texto.startswith("/start"):
            partes = texto.split(maxsplit=1)
            codigo = partes[1].strip() if len(partes) > 1 else ""
            p = por_codigo.get(codigo)
            if not p:
                enviar(chat, "👋 Para recibir avisos, entrá a la app → <b>Cartera</b> → "
                             "<b>Vincular Telegram</b> y tocá el botón desde ahí.")
                continue
            if not p.get("aprobado"):
                enviar(chat, "⏳ Tu cuenta todavía no fue aprobada. Cuando la aprueben, volvé a tocar Vincular.")
                continue
            sb_patch("perfiles", f"id=eq.{p['id']}", {"telegram_chat_id": str(chat)})
            p["telegram_chat_id"] = str(chat)
            nombre = p.get("nombre") or p["email"].split("@")[0]
            enviar(chat, f"✅ <b>¡Listo, {nombre}!</b>\nDesde ahora te aviso acá cuando tus acciones "
                         f"lleguen a tu precio de venta, toquen el stop o cuando una acción en seguimiento "
                         f"baje a tu precio de compra.\n\nTambién te mando un resumen cada día hábil al cierre.\n\n"
                         f"Para dejar de recibir avisos mandá /stop")
            print(f"Vinculado: {p['email']}")
        elif texto.startswith("/stop"):
            p = por_chat.get(str(chat))
            if p:
                sb_patch("perfiles", f"id=eq.{p['id']}", {"telegram_chat_id": None})
                p["telegram_chat_id"] = None
                enviar(chat, "🔕 Listo, ya no vas a recibir avisos. Podés volver a vincular desde la app.")
        else:
            enviar(chat, "🤖 Soy el bot de avisos del Radar de Acciones. Todo se maneja desde la app"
                         + (f":\n{APP_URL}" if APP_URL else "."))
    if ultimo is not None:
        tg("getUpdates", offset=ultimo + 1, timeout=0)   # marca los mensajes como leídos


# ----------------------------- cartera -----------------------------

def calcular_posiciones(ops, objs, precios):
    pos = {}
    for o in sorted(ops, key=lambda o: (o["fecha"], o["id"])):
        p = pos.setdefault(o["ticker"], {"ticker": o["ticker"], "cant": 0.0, "costo": 0.0})
        cant, precio, com = float(o["cantidad"]), float(o["precio"]), float(o["comision"] or 0)
        if o["tipo"] == "compra":
            p["cant"] += cant
            p["costo"] += cant * precio + com
        else:
            ppc = p["costo"] / p["cant"] if p["cant"] else 0
            q = min(cant, p["cant"])
            p["costo"] -= q * ppc
            p["cant"] -= q
    salida = []
    for p in pos.values():
        if p["cant"] <= 1e-9:
            continue
        p["ppc"] = p["costo"] / p["cant"]
        pr = precios.get("tickers", {}).get(p["ticker"], {})
        p["precio"] = pr.get("ars")
        p["prev"] = pr.get("ars_prev")
        ob = objs.get(p["ticker"], {})
        p["objetivo"] = float(ob["vender_en"]) if ob.get("vender_en") else p["ppc"] * (1 + SUG_VENTA / 100)
        p["stop"] = float(ob["stop"]) if ob.get("stop") else p["ppc"] * (1 - SUG_STOP / 100)
        salida.append(p)
    return salida


def avisos_de_usuario(posiciones, seguidas, precios):
    """Lista de (clave, texto) con los avisos que corresponden ahora."""
    avisos = []
    for p in posiciones:
        t, precio, ppc = p["ticker"], p["precio"], p["ppc"]
        if precio is None:
            continue
        gan = (precio / ppc - 1) * 100
        obj, stop = p["objetivo"], p["stop"]
        if precio >= obj:
            avisos.append((f"{t}|objetivo|{obj:.2f}",
                           f"🎯 <b>{t} llegó a tu precio de venta</b>\n"
                           f"Precio: {pesos(precio)} (objetivo {pesos(obj)})\n"
                           f"Tu PPC: {pesos(ppc)} → ganancia <b>{pct(gan)}</b>"))
        elif precio >= obj * (1 - AVISO_CERCA / 100):
            avisos.append((f"{t}|cerca_obj|{obj:.2f}",
                           f"👀 <b>{t} está cerca de tu precio de venta</b>\n"
                           f"Precio: {pesos(precio)} · objetivo {pesos(obj)} (falta {fmt((obj / precio - 1) * 100, 1)}%)\n"
                           f"Ganancia actual: {pct(gan)}"))
        if precio <= stop:
            avisos.append((f"{t}|stop|{stop:.2f}",
                           f"🛑 <b>{t} tocó tu stop</b>\n"
                           f"Precio: {pesos(precio)} (stop {pesos(stop)})\n"
                           f"Tu PPC: {pesos(ppc)} → resultado <b>{pct(gan)}</b>"))
        elif precio <= stop * (1 + AVISO_CERCA / 100):
            avisos.append((f"{t}|cerca_stop|{stop:.2f}",
                           f"⚠️ <b>{t} está cerca de tu stop</b>\n"
                           f"Precio: {pesos(precio)} · stop {pesos(stop)}\nResultado actual: {pct(gan)}"))
    for s in seguidas:
        t, obj = s["ticker"], s.get("precio_compra")
        precio = precios.get("tickers", {}).get(t, {}).get("ars")
        if not obj or precio is None:
            continue
        obj = float(obj)
        if precio <= obj:
            nota = f"\n📝 {s['nota']}" if s.get("nota") else ""
            avisos.append((f"{t}|compra|{obj:.2f}",
                           f"🛒 <b>{t} bajó a tu precio de compra</b>\n"
                           f"Precio: {pesos(precio)} (tu precio: {pesos(obj)}){nota}"))
    return avisos


def resumen_usuario(nombre, posiciones, seguidas, precios, ranking):
    hoy = ahora_arg().strftime("%d/%m")
    lineas = [f"📊 <b>Resumen del {hoy}</b> · {nombre}"]
    con_precio = [p for p in posiciones if p["precio"] is not None]
    if con_precio:
        lineas.append("\n<b>Tu cartera</b>")
        for p in con_precio:
            dia = f" ({pct((p['precio'] / p['prev'] - 1) * 100)} hoy)" if p.get("prev") else ""
            falta = (p["objetivo"] / p["precio"] - 1) * 100
            extra = "🎯 en objetivo" if falta <= 0 else f"faltan {fmt(falta, 1)}% para vender"
            lineas.append(f"• <b>{p['ticker']}</b> {pesos(p['precio'])}{dia}\n"
                          f"   vs PPC: {pct((p['precio'] / p['ppc'] - 1) * 100)} · {extra}")
        inv = sum(p["costo"] for p in con_precio)
        val = sum(p["precio"] * p["cant"] for p in con_precio)
        g = val - inv
        lineas.append(f"\n💰 Valor: <b>{pesos(val)}</b> · ganancia <b>{'+' if g >= 0 else '-'}{pesos(abs(g))}</b> ({pct(g / inv * 100)})")
    if seguidas:
        lineas.append("\n<b>👀 Seguimiento</b>")
        for s in seguidas:
            pr = precios.get("tickers", {}).get(s["ticker"], {}).get("ars")
            if pr is None:
                continue
            obj = float(s["precio_compra"]) if s.get("precio_compra") else None
            txt = "✅ en tu precio" if obj and pr <= obj else (f"falta bajar {fmt((1 - obj / pr) * 100, 1)}%" if obj else "")
            lineas.append(f"• {s['ticker']} {pesos(pr)} {('· ' + txt) if txt else ''}")
    top = sorted(ranking, key=lambda a: -a.get("Puntaje", 0))[:TOP_RESUMEN]
    if top:
        lineas.append("\n🏆 <b>Top del radar</b>")
        lineas += [f"{i}. {a['Ticker']} · {fmt(a['Puntaje'], 0)}" for i, a in enumerate(top, 1)]
    if precios.get("ccl"):
        lineas.append(f"\n💵 Dólar CCL: {pesos(precios['ccl'], 2)}")
    if APP_URL:
        lineas.append(f"\n🔗 {APP_URL}")
    return "\n".join(lineas)


# ----------------------------- principal -----------------------------

def main():
    if not (SUPABASE_URL and SUPABASE_SECRET and TOKEN):
        raise SystemExit("Faltan secretos: SUPABASE_SECRET y/o TELEGRAM_TOKEN.")

    perfiles = sb_get("perfiles", "select=id,email,nombre,aprobado,telegram_chat_id,telegram_codigo")
    procesar_mensajes(perfiles)

    usuarios = [p for p in perfiles if p.get("aprobado") and p.get("telegram_chat_id")]
    if not usuarios:
        print("Nadie vinculado todavía.")
        return

    precios = leer_json("precios.json")
    ranking = leer_json("data.json").get("acciones", [])
    ops = sb_get("operaciones", "select=*")
    objs = sb_get("objetivos", "select=*")
    seg = sb_get("seguimiento", "select=*")
    ya = {(a["usuario"], a["clave"]) for a in sb_get("avisos_enviados", "select=usuario,clave")}

    ahora = ahora_arg()
    hoy = ahora.strftime("%Y-%m-%d")
    precios_de_hoy = precios.get("actualizado", "").startswith(ahora.strftime("%d/%m/%Y"))
    toca_resumen = ahora.weekday() < 5 and (ahora.hour, ahora.minute) >= HORA_RESUMEN
    nuevos = []

    for u in usuarios:
        uid, chat = u["id"], u["telegram_chat_id"]
        nombre = u.get("nombre") or u["email"].split("@")[0]
        mis_objs = {o["ticker"]: o for o in objs if o["usuario"] == uid}
        posiciones = calcular_posiciones([o for o in ops if o["usuario"] == uid], mis_objs, precios)
        seguidas = [s for s in seg if s["usuario"] == uid]

        if precios_de_hoy:
            for clave, texto in avisos_de_usuario(posiciones, seguidas, precios):
                if (uid, clave) not in ya and enviar(chat, texto):
                    nuevos.append({"usuario": uid, "clave": clave})
                    ya.add((uid, clave))

        clave_res = f"resumen|{hoy}"
        if toca_resumen and (uid, clave_res) not in ya and (posiciones or seguidas):
            if enviar(chat, resumen_usuario(nombre, posiciones, seguidas, precios, ranking)):
                nuevos.append({"usuario": uid, "clave": clave_res})

    sb_insert_ignorar("avisos_enviados", nuevos)
    print(f"Listo: {len(usuarios)} usuarios con Telegram · {len(nuevos)} mensajes enviados.")


if __name__ == "__main__":
    main()
