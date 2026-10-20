"""
AVISOS POR TELEGRAM  ·  Radar de Acciones

Corre cada 15 minutos en GitHub y hace 3 cosas:
  1. Vincula Telegram: cuando alguien toca "Vincular Telegram" en la app y aprieta
     Iniciar en el bot, guarda su chat para mandarle avisos.
  2. Avisos de precio (con los precios de precios.json):
       🎯 llegó a tu precio de venta      👀 está a menos de 3% de llegar
       🛑 tocó tu stop                    ⚠️ está a menos de 3% del stop
       🛒 una acción en seguimiento bajó a tu precio de compra
       🚀/📉 una acción tuya se movió más que tu % elegido en el día
       📅 una empresa tuya presenta balance en los próximos días
     El stop puede ser fijo o dinámico (sube solo cuando la acción sube).
  3. Resumen diario (lunes a viernes, después de las 19:30): tu cartera + top 3 del radar.

Cada aviso se manda UNA sola vez (queda anotado en Supabase). Si cambiás el precio
objetivo, se vuelve a activar.
"""

import json
from cripto import es_cripto, precios_vivo
import secrets
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
DIAS_AVISO_BALANCE = 3   # avisar balances con esta anticipación
MAX_NOTICIAS_RESUMEN = 5
CAIDA_NOTA = 15         # avisar si la nota del radar cae esta cantidad de puntos
DIAS_CAIDA = 5          # ...comparando contra el máximo de las últimas N actualizaciones
SEMANAL = (6, (11, 0))   # domingo 11:00 hora Argentina
DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


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
            if not p and str(chat) in por_chat:
                enviar(chat, "✅ Ya estás vinculado. Te sigo avisando acá.")
                continue
            if not p:
                enviar(chat, "👋 Para recibir avisos, entrá a la app → <b>Cartera</b> → "
                             "<b>Vincular Telegram</b> y tocá el botón desde ahí.")
                continue
            if not p.get("aprobado"):
                enviar(chat, "⏳ Tu cuenta todavía no fue aprobada. Cuando la aprueben, volvé a tocar Vincular.")
                continue
            # Código nuevo: el link viejo de "Vincular Telegram" deja de servir
            nuevo_codigo = secrets.token_hex(4)
            sb_patch("perfiles", f"id=eq.{p['id']}", {"telegram_chat_id": str(chat), "telegram_codigo": nuevo_codigo})
            p["telegram_chat_id"] = str(chat)
            por_codigo.pop(codigo, None)
            por_chat[str(chat)] = p
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
        p["dinamico"] = ob.get("stop_tipo") == "dinamico" and ob.get("stop_pct")
        if p["dinamico"]:
            # Stop dinámico: queda X% debajo del precio más alto alcanzado
            maximo = max(float(ob.get("max_precio") or 0), p["ppc"], p["precio"] or 0)
            p["max_nuevo"] = maximo if maximo > float(ob.get("max_precio") or 0) + 1e-9 else None
            p["maximo"] = maximo
            p["stop"] = maximo * (1 - float(ob["stop_pct"]) / 100)
            p["stop_clave"] = f"din{float(ob['stop_pct']):g}@{maximo:.2f}"
        else:
            p["stop"] = float(ob["stop"]) if ob.get("stop") else p["ppc"] * (1 - SUG_STOP / 100)
            p["stop_clave"] = f"{p['stop']:.2f}"
        salida.append(p)
    return salida


def avisos_de_usuario(posiciones, seguidas, precios, pct_mov=0, hoy=""):
    """Lista de (clave, texto) con los avisos que corresponden ahora."""
    avisos = []

    # Movimientos fuertes del día (cartera + seguimiento), uno por dirección por día
    if pct_mov and pct_mov > 0:
        vistos = set()
        for t, origen in [(p["ticker"], "cartera") for p in posiciones] + [(s["ticker"], "seguimiento") for s in seguidas]:
            if t in vistos:
                continue
            vistos.add(t)
            pr = precios.get("tickers", {}).get(t, {})
            precio, prev = pr.get("ars"), pr.get("ars_prev")
            if not precio or not prev:
                continue
            var = (precio / prev - 1) * 100
            if abs(var) >= pct_mov:
                sube = var > 0
                donde = "de tu cartera" if origen == "cartera" else "de tu seguimiento"
                cuando, antes = ("en 24 h", "hace 24 h estaba a") if es_cripto(t) else ("hoy", "ayer cerró a")
                avisos.append((f"{t}|mov|{hoy}|{'sube' if sube else 'baja'}",
                               f"{'🚀' if sube else '📉'} <b>{t} {'sube' if sube else 'cae'} {pct(var)} {cuando}</b> ({donde})\n"
                               f"Precio: {pesos(precio)} · {antes} {pesos(prev)}"))

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
        tipo_stop = "stop dinámico" if p.get("dinamico") else "stop"
        if precio <= stop:
            avisos.append((f"{t}|stop|{p['stop_clave']}",
                           f"🛑 <b>{t} tocó tu {tipo_stop}</b>\n"
                           f"Precio: {pesos(precio)} (stop {pesos(stop)})\n"
                           f"Tu PPC: {pesos(ppc)} → resultado <b>{pct(gan)}</b>"))
        elif precio <= stop * (1 + AVISO_CERCA / 100):
            avisos.append((f"{t}|cerca_stop|{p['stop_clave']}",
                           f"⚠️ <b>{t} está cerca de tu {tipo_stop}</b>\n"
                           f"Precio: {pesos(precio)} · stop {pesos(stop)}\nResultado actual: {pct(gan)}"))
    # Balances: aviso unos días antes (cartera + seguimiento)
    if hoy:
        hoy_d = datetime.strptime(hoy, "%Y-%m-%d").date()
        for t in sorted({p["ticker"] for p in posiciones} | {s["ticker"] for s in seguidas}):
            fecha = precios.get("tickers", {}).get(t, {}).get("balance")
            if not fecha:
                continue
            f = datetime.strptime(fecha, "%Y-%m-%d").date()
            dias = (f - hoy_d).days
            if 0 <= dias <= DIAS_AVISO_BALANCE:
                cuando = "hoy" if dias == 0 else "mañana" if dias == 1 else f"en {dias} días"
                avisos.append((f"{t}|balance|{fecha}",
                               f"📅 <b>{t} presenta resultados {cuando}</b> ({DIAS_SEMANA[f.weekday()]} {f.strftime('%d/%m')})\n"
                               f"Los días de balance el precio suele moverse fuerte."))

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


def comprables(ranking):
    """Solo acciones/ETFs con CEDEAR en Argentina (si no se sabe, se dejan). Sin cripto."""
    return [a for a in ranking if a.get("CEDEAR") is not False and a.get("Mercado") != "Cripto"]


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
    # Noticias del día de tus acciones
    noticias = []
    for t in [p["ticker"] for p in posiciones] + [s["ticker"] for s in seguidas]:
        for n in precios.get("tickers", {}).get(t, {}).get("noticias", []):
            if all(n["url"] != x[1]["url"] for x in noticias):
                noticias.append((t, n))
    if noticias:
        lineas.append("\n📰 <b>Noticias de tus acciones</b>")
        for t, n in noticias[:MAX_NOTICIAS_RESUMEN]:
            titulo = n["titulo"].replace("<", "").replace(">", "")
            ic = {"buena": "🟢 ", "mala": "🔴 ", "neutra": "⚪ "}.get(n.get("sentimiento"), "")
            lineas.append(f"• {ic}<b>{t}</b>: <a href=\"{n['url']}\">{titulo}</a>")

    # Próximos balances
    prox = []
    for t in sorted({p["ticker"] for p in posiciones} | {s["ticker"] for s in seguidas}):
        fecha = precios.get("tickers", {}).get(t, {}).get("balance")
        if fecha:
            f = datetime.strptime(fecha, "%Y-%m-%d").date()
            if (f - ahora_arg().date()).days <= 14:
                prox.append((f, t))
    if prox:
        lineas.append("\n📅 <b>Próximos balances</b>")
        lineas += [f"• {t}: {DIAS_SEMANA[f.weekday()]} {f.strftime('%d/%m')}" for f, t in sorted(prox)]

    top = sorted(comprables(ranking), key=lambda a: -a.get("Puntaje", 0))[:TOP_RESUMEN]
    if top:
        lineas.append("\n🏆 <b>Top del radar</b>")
        lineas += [f"{i}. {a['Ticker']} · {fmt(a['Puntaje'], 0)}" for i, a in enumerate(top, 1)]
    if precios.get("ccl"):
        lineas.append(f"\n💵 Dólar CCL: {pesos(precios['ccl'], 2)}")
    if APP_URL:
        lineas.append(f"\n🔗 {APP_URL}")
    return "\n".join(lineas)


def _valor_hasta(hist, fecha):
    """Último valor de un historial {fecha: valor} en o antes de 'fecha' (YYYY-MM-DD)."""
    claves = [f for f in hist if f <= fecha]
    return hist[max(claves)] if claves else None


def precio_hace_una_semana(t, precios):
    """Estima el precio en pesos de hace 7 días: movimiento de la acción en dólares x movimiento del CCL."""
    pr = precios.get("tickers", {}).get(t, {})
    hace7 = (ahora_arg().date() - timedelta(days=7)).isoformat()
    # 1) Si ya tenemos el precio en pesos guardado, usamos ese (exacto)
    real = _valor_hasta(pr.get("ars_hist") or {}, hace7)
    if real:
        return real
    # 2) Si no, lo estimamos (solo para CEDEARs/ADRs conocidos del radar)
    if t not in CONOCIDOS:
        return None
    ars, uh, ch = pr.get("ars"), pr.get("usd_hist") or {}, precios.get("ccl_hist") or {}
    if not (ars and uh and ch):
        return None
    u_ahora, u_antes = uh[max(uh)], _valor_hasta(uh, hace7)
    c_ahora, c_antes = ch[max(ch)], _valor_hasta(ch, hace7)
    if not (u_antes and c_antes):
        return None
    return ars / ((u_ahora / u_antes) * (c_ahora / c_antes))


CONOCIDOS = set()   # tickers del radar (EE.UU., ADRs argentinos y ETFs); se completa en main()


def resumen_semanal(nombre, posiciones, ops_usuario, precios, ranking):
    hoy = ahora_arg().date()
    lineas = [f"🗓 <b>Tu semana</b> · {nombre}\n<i>del {(hoy - timedelta(days=7)).strftime('%d/%m')} al {hoy.strftime('%d/%m')}</i>"]
    con_precio = [p for p in posiciones if p["precio"] is not None]
    if con_precio:
        filas = []
        for p in con_precio:
            antes = precio_hace_una_semana(p["ticker"], precios)
            if antes:
                filas.append((p, (p["precio"] / antes - 1) * 100, (p["precio"] - antes) * p["cant"]))
        val = sum(p["precio"] * p["cant"] for p in con_precio)
        inv = sum(p["costo"] for p in con_precio)
        g = val - inv
        if filas:
            g_sem = sum(x[2] for x in filas)
            base = sum(x[0]["precio"] * x[0]["cant"] - x[2] for x in filas)
            icono = "📈" if g_sem >= 0 else "📉"
            lineas.append(f"\n{icono} En la semana: <b>{'+' if g_sem >= 0 else '-'}{pesos(abs(g_sem))}</b> ({pct(g_sem / base * 100)})")
        lineas.append(f"💰 Tu cartera vale <b>{pesos(val)}</b>\n   Ganancia total: {'+' if g >= 0 else '-'}{pesos(abs(g))} ({pct(g / inv * 100)})")
        if len(filas) >= 2:
            filas.sort(key=lambda x: -x[1])
            m, w = filas[0], filas[-1]
            lineas.append(f"\n🥇 Mejor: <b>{m[0]['ticker']}</b> {pct(m[1])}")
            lineas.append(f"🥴 Peor: <b>{w[0]['ticker']}</b> {pct(w[1])}")
        if filas:
            lineas.append("\n<b>Cada una en la semana</b>")
            lineas += [f"• {p['ticker']} {pct(v)}" for p, v, _ in filas]

    # Tus operaciones de la semana
    desde = (hoy - timedelta(days=7)).isoformat()
    recientes = sorted([o for o in ops_usuario if o["fecha"] >= desde], key=lambda o: o["fecha"])
    if recientes:
        lineas.append("\n🧾 <b>Tus operaciones</b>")
        lineas += [f"• {'🟢 Compraste' if o['tipo'] == 'compra' else '🔴 Vendiste'} {float(o['cantidad']):g} {o['ticker']} a {pesos(float(o['precio']))}" for o in recientes]

    # Lo que viene: balances de los próximos 7 días
    prox = []
    for p in posiciones:
        fecha = precios.get("tickers", {}).get(p["ticker"], {}).get("balance")
        if fecha:
            f = datetime.strptime(fecha, "%Y-%m-%d").date()
            if 0 <= (f - hoy).days <= 7:
                prox.append((f, p["ticker"]))
    if prox:
        lineas.append("\n📅 <b>Esta semana presentan balance</b>")
        lineas += [f"• {t}: {DIAS_SEMANA[f.weekday()]} {f.strftime('%d/%m')}" for f, t in sorted(prox)]

    top = sorted(comprables(ranking), key=lambda a: -a.get("Puntaje", 0))[:TOP_RESUMEN]
    if top:
        lineas.append("\n🏆 <b>Top del radar</b>")
        lineas += [f"{i}. {a['Ticker']} · {fmt(a['Puntaje'], 0)}" for i, a in enumerate(top, 1)]
    cruces = sorted([a for a in comprables(ranking) if a.get("Días desde cruce") is not None and a["Días desde cruce"] <= 7],
                    key=lambda a: -a.get("Puntaje", 0))[:3]
    if cruces:
        lineas.append("\n🔥 <b>Cruces alcistas de la semana</b>")
        lineas += [f"• {a['Ticker']} · nota {fmt(a['Puntaje'], 0)}" for a in cruces]
    if precios.get("ccl"):
        ch = precios.get("ccl_hist") or {}
        antes = _valor_hasta(ch, (hoy - timedelta(days=7)).isoformat())
        extra = f" ({pct((precios['ccl'] / antes - 1) * 100)} en la semana)" if antes else ""
        lineas.append(f"\n💵 Dólar CCL: {pesos(precios['ccl'], 2)}{extra}")
    if APP_URL:
        lineas.append(f"\n🔗 {APP_URL}")
    return "\n".join(lineas)


def avisos_noticias(posiciones, seguidas, precios):
    """Noticias de IMPACTO GRANDE (muy buenas o muy malas) de tus acciones, según la IA."""
    import hashlib
    avisos = []
    tickers = sorted({p["ticker"] for p in posiciones} | {s["ticker"] for s in seguidas})
    for t in tickers:
        for n in precios.get("tickers", {}).get(t, {}).get("noticias", []):
            if n.get("impacto") != "grande" or n.get("sentimiento") not in ("buena", "mala"):
                continue
            buena = n["sentimiento"] == "buena"
            titulo = n["titulo"].replace("<", "").replace(">", "")
            expl = (n.get("explicacion") or "").replace("<", "").replace(">", "")
            clave = f"{t}|noticia|{hashlib.md5(n['url'].encode()).hexdigest()[:12]}"
            avisos.append((clave,
                           f"{'🟢' if buena else '🔴'} <b>Noticia muy {'buena' if buena else 'mala'} de {t}</b>\n"
                           f"<a href=\"{n['url']}\">{titulo}</a>\n\n🤖 <b>Qué significa:</b> {expl}"))
    return avisos


def avisos_nota(posiciones, historial):
    """Avisa si una acción que tenés bajó mucho en el radar (o se cayó del ranking)."""
    fechas = sorted(historial)
    if len(fechas) < 2:
        return []
    ultima, previas = fechas[-1], fechas[-1 - DIAS_CAIDA:-1]
    hoy_rk = historial[ultima]
    avisos = []
    for p in posiciones:
        t = p["ticker"]
        antes = [(historial[f][t], f) for f in previas if t in historial[f]]
        if not antes:
            continue
        maximo, f_max = max(antes)
        ahora = hoy_rk.get(t)
        if ahora is None:
            if t in historial[previas[-1]] and historial[previas[-1]][t] >= 60:
                avisos.append((f"{t}|notacae|{ultima}",
                               f"⚠️ <b>{t} salió del radar</b>\n"
                               f"Tenía nota {fmt(historial[previas[-1]][t], 0)} y hoy su gráfico se debilitó tanto que quedó afuera del ranking.\n"
                               f"Conviene revisarla."))
        elif maximo - ahora >= CAIDA_NOTA:
            dia = datetime.strptime(f_max, "%Y-%m-%d").strftime("%d/%m")
            avisos.append((f"{t}|notacae|{ultima}",
                           f"⚠️ <b>{t} bajó en el radar: {fmt(maximo, 0)} → {fmt(ahora, 0)}</b>\n"
                           f"Perdió {fmt(maximo - ahora, 0)} puntos desde el {dia}. Puede ser que la empresa o el gráfico se estén debilitando.\n"
                           f"Revisala en la app antes de decidir."))
    return avisos


def chequeo_salud(precios, datos_radar, ahora):
    """Problemas del bot para avisarle al admin: (clave, texto)."""
    problemas = []
    habil = ahora.weekday() < 5
    def fecha_de(txt):
        try:
            return datetime.strptime(txt, "%d/%m/%Y %H:%M")
        except Exception:
            return None
    # Precios: con la bolsa abierta tienen que actualizarse cada 30 min
    fp = fecha_de(precios.get("actualizado", ""))
    if habil and (12, 0) <= (ahora.hour, ahora.minute) <= (17, 30):
        horas = (ahora.replace(tzinfo=None) - fp).total_seconds() / 3600 if fp else 99
        if horas > 2.5:
            problemas.append((f"salud|precios|{ahora:%Y-%m-%d}",
                              f"🚨 <b>Los precios no se actualizan</b> desde {precios.get('actualizado') or 'hace mucho'}.\n"
                              f"Revisá en GitHub → Actions → <b>Precios CEDEARs</b> si hay una ❌."))
    # Ranking: corre a las 18:30 los días hábiles
    fr = fecha_de(datos_radar.get("actualizado", ""))
    if habil and (ahora.hour, ahora.minute) >= (20, 0):
        if not fr or fr.date() != ahora.date():
            problemas.append((f"salud|ranking|{ahora:%Y-%m-%d}",
                              f"🚨 <b>El ranking de hoy no se actualizó</b> (último: {datos_radar.get('actualizado') or 'nunca'}).\n"
                              f"Revisá en GitHub → Actions → <b>Actualizar ranking</b> si hay una ❌."))
    # IA de noticias
    ns = [n for v in precios.get("tickers", {}).values() for n in (v.get("noticias") or [])]
    if habil and len(ns) >= 10 and not any(n.get("sentimiento") for n in ns) and ahora.hour >= 12:
        problemas.append((f"salud|ia|{ahora:%Y-%m-%d}",
                          "🚨 <b>La IA no está calificando las noticias.</b>\n"
                          "Puede ser la clave GEMINI_KEY o el límite del plan gratis. Revisá el log de <b>Precios CEDEARs</b> (buscá «IA»)."))
    return problemas


# ----------------------------- principal -----------------------------

def main():
    if not (SUPABASE_URL and SUPABASE_SECRET and TOKEN):
        raise SystemExit("Faltan secretos: SUPABASE_SECRET y/o TELEGRAM_TOKEN.")

    perfiles = sb_get("perfiles", "select=id,email,nombre,aprobado,es_admin,telegram_chat_id,telegram_codigo,aviso_movimiento")
    procesar_mensajes(perfiles)

    usuarios = [p for p in perfiles if p.get("aprobado") and p.get("telegram_chat_id")]

    precios = leer_json("precios.json")
    datos_radar = leer_json("data.json")
    ranking = datos_radar.get("acciones", [])
    historial_radar = datos_radar.get("historial", {})
    CONOCIDOS.update(a["Ticker"] for a in ranking)
    ops = sb_get("operaciones", "select=*")
    aprobados = [p for p in perfiles if p.get("aprobado")]
    objs = sb_get("objetivos", "select=*")
    seg = sb_get("seguimiento", "select=*")
    ya = {(a["usuario"], a["clave"]) for a in sb_get("avisos_enviados", "select=usuario,clave")}

    ahora = ahora_arg()
    hoy = ahora.strftime("%Y-%m-%d")
    precios_de_hoy = precios.get("actualizado", "").startswith(ahora.strftime("%d/%m/%Y"))

    # Cripto: precio en vivo cada 15 min, también de noche y los fines de semana
    vivo = precios_vivo({x["ticker"] for x in ops + seg if es_cripto(x["ticker"])}, dolar_respaldo=precios.get("ccl"))
    precios.setdefault("tickers", {})
    for t, v in vivo.items():
        if v.get("ars"):
            precios["tickers"].setdefault(t, {}).update({k: v[k] for k in ("usd", "usd_prev", "ars", "ars_prev")})

    def al_dia(t):
        """¿El precio de este ticker es de ahora? Acciones: si precios.json es de hoy. Cripto: si hay precio en vivo."""
        return t in vivo if es_cripto(t) else precios_de_hoy
    toca_resumen = ahora.weekday() < 5 and (ahora.hour, ahora.minute) >= HORA_RESUMEN
    toca_semanal = ahora.weekday() == SEMANAL[0] and (ahora.hour, ahora.minute) >= SEMANAL[1]
    nuevos = []

    # Stop dinámico: actualizar el precio máximo de todos (tengan o no Telegram)
    if precios_de_hoy or vivo:
        for u in aprobados:
            mis_objs = {o["ticker"]: o for o in objs if o["usuario"] == u["id"]}
            for p in calcular_posiciones([o for o in ops if o["usuario"] == u["id"]], mis_objs, precios):
                if p.get("max_nuevo") and al_dia(p["ticker"]):
                    sb_patch("objetivos", f"usuario=eq.{u['id']}&ticker=eq.{p['ticker']}", {"max_precio": round(p["max_nuevo"], 4)})
                    mis_objs[p["ticker"]]["max_precio"] = p["max_nuevo"]

    for u in usuarios:
        uid, chat = u["id"], u["telegram_chat_id"]
        nombre = u.get("nombre") or u["email"].split("@")[0]
        mis_objs = {o["ticker"]: o for o in objs if o["usuario"] == uid}
        posiciones = calcular_posiciones([o for o in ops if o["usuario"] == uid], mis_objs, precios)
        seguidas = [s for s in seg if s["usuario"] == uid]

        # Solo con precios de ahora (acciones: bolsa de hoy; cripto: precio en vivo)
        pos_al_dia = [p for p in posiciones if al_dia(p["ticker"])]
        seg_al_dia = [s for s in seguidas if al_dia(s["ticker"])]
        if pos_al_dia or seg_al_dia:
            for clave, texto in avisos_de_usuario(pos_al_dia, seg_al_dia, precios, float(u.get("aviso_movimiento") or 0), hoy):
                if (uid, clave) not in ya and enviar(chat, texto):
                    nuevos.append({"usuario": uid, "clave": clave})
                    ya.add((uid, clave))

        for clave, texto in avisos_nota(posiciones, historial_radar) + avisos_noticias(posiciones, seguidas, precios):
            if (uid, clave) not in ya and enviar(chat, texto):
                nuevos.append({"usuario": uid, "clave": clave})
                ya.add((uid, clave))

        clave_res = f"resumen|{hoy}"
        if toca_resumen and (uid, clave_res) not in ya and (posiciones or seguidas):
            if enviar(chat, resumen_usuario(nombre, posiciones, seguidas, precios, ranking)):
                nuevos.append({"usuario": uid, "clave": clave_res})

        clave_sem = f"semanal|{hoy}"
        if toca_semanal and (uid, clave_sem) not in ya and posiciones:
            mis_ops = [o for o in ops if o["usuario"] == uid]
            if enviar(chat, resumen_semanal(nombre, posiciones, mis_ops, precios, ranking)):
                nuevos.append({"usuario": uid, "clave": clave_sem})

    # Salud del bot: solo le avisa al admin
    for u in usuarios:
        if u.get("es_admin"):
            for clave, texto in chequeo_salud(precios, datos_radar, ahora):
                if (u["id"], clave) not in ya and enviar(u["telegram_chat_id"], texto):
                    nuevos.append({"usuario": u["id"], "clave": clave})
                    ya.add((u["id"], clave))

    sb_insert_ignorar("avisos_enviados", nuevos)
    if not usuarios:
        print("Nadie vinculado a Telegram todavía.")
    print(f"Listo: {len(usuarios)} usuarios con Telegram · {len(nuevos)} mensajes enviados.")


if __name__ == "__main__":
    main()
