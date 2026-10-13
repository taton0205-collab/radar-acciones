"""
IMPORTAR OPERACIONES DE IOL  ->  cartera del ADMIN en la app

Lee tus compras y ventas terminadas en InvertirOnline y las carga en tu cartera.
- Solo LEE: nunca compra, vende ni mueve plata.
- Solo para la cuenta del admin (la de IOL_USUARIO). Nadie más.
- Solo CEDEARs y acciones (bonos, FCI y cauciones se saltean).
- No repite: cada operación de IOL se guarda con su número.
"""

import os
from datetime import date

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET", "")
IOL_USUARIO = os.environ.get("IOL_USUARIO", "").strip()
IOL_CLAVE = os.environ.get("IOL_CLAVE", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
IOL_API = "https://api.invertironline.com"
DESDE = "2015-01-01"
TIPOS_OK = ("cedear", "accion")   # tipos de título que se importan

# Símbolo en IOL -> ticker que usa la app
A_TICKER = {"YPFD": "YPF", "PAMP": "PAM", "TECO2": "TEO", "TGSU2": "TGS", "CRES": "CRESY",
            "IRSA": "IRS", "DISN": "DIS", "BRKB": "BRK-B"}

SB = {"apikey": SUPABASE_SECRET, "Authorization": f"Bearer {SUPABASE_SECRET}", "Content-Type": "application/json"}


def sb_get(tabla, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{tabla}", headers=SB, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def iol_token():
    r = requests.post(f"{IOL_API}/token", timeout=30, data={
        "username": IOL_USUARIO, "password": IOL_CLAVE, "grant_type": "password"})
    if r.status_code != 200:
        raise SystemExit(f"IOL: no pude iniciar sesión ({r.status_code})")
    return r.json()["access_token"]


def num(*vals):
    for v in vals:
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def tipo_titulo(token, simbolo, cache={}):
    """'cedears', 'acciones', 'titulospublicos', ... (minúscula)."""
    if simbolo not in cache:
        try:
            r = requests.get(f"{IOL_API}/api/v2/bCBA/Titulos/{simbolo}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
            cache[simbolo] = str(r.json().get("tipo", "")).lower() if r.status_code == 200 else ""
        except Exception:
            cache[simbolo] = ""
    return cache[simbolo]


def avisar(chat_id, texto):
    if TELEGRAM_TOKEN and chat_id:
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": chat_id, "text": texto}, timeout=20)
        except Exception:
            pass


def main():
    if not (SUPABASE_URL and SUPABASE_SECRET and IOL_USUARIO and IOL_CLAVE):
        raise SystemExit("Faltan secretos (Supabase o IOL).")

    admins = sb_get("perfiles", {"select": "id,telegram_chat_id", "es_admin": "eq.true"})
    if len(admins) != 1:
        raise SystemExit(f"Tiene que haber exactamente 1 admin (hay {len(admins)}).")
    admin = admins[0]

    token = iol_token()
    r = requests.get(f"{IOL_API}/api/v2/operaciones", headers={"Authorization": f"Bearer {token}"}, timeout=60,
                     params={"filtro.estado": "terminadas", "filtro.fechaDesde": DESDE,
                             "filtro.fechaHasta": date.today().isoformat(), "filtro.pais": "argentina"})
    r.raise_for_status()
    ops = r.json()
    print(f"IOL: {len(ops)} operaciones terminadas")

    # Lo que ya está en la app
    mias = sb_get("operaciones", {"select": "iol_numero,ticker,tipo,fecha,cantidad", "usuario": f"eq.{admin['id']}"})
    ya_importadas = {m["iol_numero"] for m in mias if m["iol_numero"]}
    manuales = {(m["ticker"], m["tipo"], m["fecha"], float(m["cantidad"])) for m in mias if not m["iol_numero"]}

    nuevas, salteadas = [], 0
    for op in ops:
        numero = op.get("numero")
        tipo = str(op.get("tipo", "")).lower()
        tipo = "compra" if "compra" in tipo else "venta" if "venta" in tipo else None
        simbolo = str(op.get("simbolo", "")).upper().strip()
        cantidad = num(op.get("cantidadOperada"), op.get("cantidad"))
        precio = num(op.get("precioOperado"), op.get("precio"))
        fecha = str(op.get("fechaOperada") or op.get("fechaOrden") or "")[:10]
        if not (numero and tipo and simbolo and cantidad and precio and fecha) or numero in ya_importadas:
            continue
        if not any(t in tipo_titulo(token, simbolo) for t in TIPOS_OK):
            salteadas += 1
            continue
        ticker = A_TICKER.get(simbolo, simbolo)
        if (ticker, tipo, fecha, cantidad) in manuales:
            continue   # ya la habías cargado a mano
        nuevas.append({"usuario": admin["id"], "ticker": ticker, "tipo": tipo, "fecha": fecha,
                       "cantidad": cantidad, "precio": precio, "comision": 0, "iol_numero": numero})

    print(f"Nuevas: {len(nuevas)} · salteadas (bonos/FCI/otros): {salteadas}")
    if nuevas:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/operaciones", params={"on_conflict": "iol_numero"},
                          headers={**SB, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                          json=nuevas, timeout=30)
        r.raise_for_status()
        lineas = [f"{'🟢 Compra' if n['tipo'] == 'compra' else '🔴 Venta'} {n['cantidad']:g} {n['ticker']} a ${n['precio']:,.0f} ({n['fecha']})"
                  for n in sorted(nuevas, key=lambda x: x["fecha"])]
        for n in nuevas[:20]:
            print("  +", n["ticker"], n["tipo"], n["cantidad"], n["precio"], n["fecha"])
        avisar(admin.get("telegram_chat_id"),
               f"📥 Importé {len(nuevas)} operación(es) de IOL a tu cartera:\n" + "\n".join(lineas[-15:]))


if __name__ == "__main__":
    main()
