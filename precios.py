"""
PRECIOS DE CEDEARs (en pesos) + DÓLAR CCL  ->  precios.json

Corre cada 30 minutos en GitHub. Para cada ticker que usan los usuarios baja:
  - precio actual del CEDEAR en pesos (bolsa argentina, ej: WDC.BA)
  - precio actual de la acción en dólares (EE.UU.)
  - historial diario de 2 años del precio en dólares
Si hay usuario de IOL configurado, el precio en pesos sale de IOL (igual que en tu cuenta)
y Yahoo queda de respaldo. Y además el dólar CCL (actual + historial), calculado con acciones argentinas
que cotizan en las dos bolsas.

No guarda nada personal: solo precios.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

ARCHIVO = "precios.json"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SECRET = os.environ.get("SUPABASE_SECRET", "")
IOL_USUARIO = os.environ.get("IOL_USUARIO", "").strip()
IOL_CLAVE = os.environ.get("IOL_CLAVE", "")
IOL_API = "https://api.invertironline.com"
IOL_PLAZO = "t1"   # 24 hs, el plazo que muestra IOL por defecto

# Pares para calcular el CCL: (acción en pesos, ADR en dólares, acciones por ADR)
PARES_CCL = [("GGAL.BA", "GGAL", 10), ("YPFD.BA", "YPF", 1), ("PAMP.BA", "PAM", 25), ("BMA.BA", "BMA", 10)]


def ahora_arg():
    return datetime.now(timezone.utc) - timedelta(hours=3)


def simbolo_cedear(ticker):
    """WDC -> WDC.BA · BRK-B -> BRKB.BA"""
    return ticker.replace("-", "").replace(".", "") + ".BA"


def tickers_en_uso():
    if not SUPABASE_URL or not SUPABASE_SECRET:
        raise SystemExit("Faltan SUPABASE_URL o SUPABASE_SECRET en los secretos de GitHub.")
    r = requests.post(f"{SUPABASE_URL}/rest/v1/rpc/tickers_en_uso",
                      headers={"apikey": SUPABASE_SECRET, "Content-Type": "application/json"},
                      json={}, timeout=30)
    r.raise_for_status()
    datos = r.json()
    return sorted({(d if isinstance(d, str) else next(iter(d.values()))).upper() for d in datos})


# ----------------------------- IOL -----------------------------

def iol_token():
    """Pide un token a IOL (solo lectura de precios; el código nunca opera)."""
    if not IOL_USUARIO or not IOL_CLAVE:
        print("IOL: sin usuario/clave, uso Yahoo.")
        return None
    try:
        r = requests.post(f"{IOL_API}/token", timeout=30, data={
            "username": IOL_USUARIO, "password": IOL_CLAVE, "grant_type": "password"})
        if r.status_code != 200:
            print(f"IOL: no pude iniciar sesión ({r.status_code}): {r.text[:200]}")
            return None
        return r.json().get("access_token")
    except Exception as e:
        print(f"IOL: error al iniciar sesión ({e})")
        return None


def _num(d, *claves):
    for c in claves:
        v = d.get(c)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def iol_precio_previo(item):
    """(precio actual, cierre anterior) de una respuesta de IOL."""
    precio = _num(item, "ultimoPrecio", "ultimo", "precio")
    previo = _num(item, "cierreAnterior", "ultimoCierre", "precioCierreAnterior")
    if precio and not previo:
        var = item.get("variacionPorcentual", item.get("variacion"))
        if isinstance(var, (int, float)) and var > -100:
            previo = precio / (1 + var / 100)
    return precio, previo


def iol_panel_cedears(token):
    """Todas las cotizaciones de CEDEARs en una sola consulta: {SIMBOLO: (precio, previo)}."""
    try:
        r = requests.get(f"{IOL_API}/api/v2/Cotizaciones/cedears/todos/argentina",
                         headers={"Authorization": f"Bearer {token}"}, timeout=40)
        if r.status_code != 200:
            print(f"IOL panel: respuesta {r.status_code}")
            return {}
        datos = r.json()
        items = datos.get("titulos", datos) if isinstance(datos, dict) else datos
        salida = {}
        for it in items or []:
            sim = str(it.get("simbolo", "")).upper()
            precio, previo = iol_precio_previo(it)
            if sim and precio:
                salida[sim] = (precio, previo)
        print(f"IOL panel: {len(salida)} CEDEARs")
        return salida
    except Exception as e:
        print(f"IOL panel: error ({e})")
        return {}


def iol_cotizacion(token, simbolo):
    """Cotización de un CEDEAR puntual (si no vino en el panel)."""
    try:
        r = requests.get(f"{IOL_API}/api/v2/bCBA/Titulos/{simbolo}/Cotizacion",
                         params={"model.simbolo": simbolo, "model.mercado": "bCBA", "model.plazo": IOL_PLAZO},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if r.status_code != 200:
            return None, None
        return iol_precio_previo(r.json())
    except Exception:
        return None, None


def cierres(df, simbolo):
    """Serie de cierres diarios de un símbolo (vacía si no hay)."""
    try:
        s = df[simbolo]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        s = s.dropna()
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s
    except Exception:
        return pd.Series(dtype=float)


def actual_y_previo(serie_diaria, serie_intradia):
    """Último precio (intradía si hay) y cierre del día hábil anterior."""
    if serie_diaria.empty:
        return None, None
    hoy = pd.Timestamp(ahora_arg().date())
    actual = float(serie_intradia.iloc[-1]) if not serie_intradia.empty else float(serie_diaria.iloc[-1])
    anteriores = serie_diaria[serie_diaria.index < hoy]
    previo = float(anteriores.iloc[-1]) if not anteriores.empty else None
    return actual, previo


def main():
    tickers = tickers_en_uso()
    print(f"Tickers en uso: {tickers}")

    simbolos = set()
    for t in tickers:
        simbolos |= {t, simbolo_cedear(t)}
    for ars, usd, _ in PARES_CCL:
        simbolos |= {ars, usd}
    simbolos = sorted(simbolos)

    kw = dict(group_by="ticker", auto_adjust=False, progress=False, threads=True)
    diario = yf.download(simbolos, period="2y", interval="1d", **kw)
    intradia = yf.download(simbolos, period="5d", interval="15m", **kw)

    def serie_i(s):
        """Precios intradía de HOY (si la bolsa abrió hoy)."""
        x = cierres(intradia, s)  # índice ya normalizado a la fecha local de la bolsa
        return x[x.index == pd.Timestamp(ahora_arg().date())] if not x.empty else x

    # ---- Dólar CCL (mediana de varios pares) ----
    ccl_series, ccl_hoy, ccl_prev = [], [], []
    for ars, usd, ratio in PARES_CCL:
        a, u = cierres(diario, ars), cierres(diario, usd)
        if a.empty or u.empty:
            continue
        ccl_series.append((a * ratio / u).dropna())
        aa, ap = actual_y_previo(a, serie_i(ars))
        ua, up = actual_y_previo(u, serie_i(usd))
        if aa and ua:
            ccl_hoy.append(aa * ratio / ua)
        if ap and up:
            ccl_prev.append(ap * ratio / up)
    ccl_hist = pd.concat(ccl_series, axis=1).median(axis=1).dropna() if ccl_series else pd.Series(dtype=float)
    ccl = float(pd.Series(ccl_hoy).median()) if ccl_hoy else (float(ccl_hist.iloc[-1]) if not ccl_hist.empty else None)
    ccl_anterior = float(pd.Series(ccl_prev).median()) if ccl_prev else None

    # ---- Precios de IOL (en pesos) ----
    precios_iol = {}
    token = iol_token()
    if token:
        panel = iol_panel_cedears(token)
        for t in tickers:
            sim = simbolo_cedear(t).replace(".BA", "")
            pr = panel.get(sim) or iol_cotizacion(token, sim)
            if pr and pr[0]:
                precios_iol[t] = pr
        print(f"IOL: precio para {len(precios_iol)} de {len(tickers)} tickers")

    # ---- Cada ticker ----
    salida = {}
    for t in tickers:
        ced = simbolo_cedear(t)
        ars, ars_prev = actual_y_previo(cierres(diario, ced), serie_i(ced))
        fuente = "Yahoo"
        if t in precios_iol:
            ars, ars_prev = precios_iol[t][0], precios_iol[t][1] or ars_prev
            fuente = "IOL"
        usd_d = cierres(diario, t)
        usd, usd_prev = actual_y_previo(usd_d, serie_i(t))
        if ars is None and usd is None:
            print(f"  {t}: sin precios")
        salida[t] = {
            "simbolo": ced,
            "fuente": fuente,
            "ars": round(ars, 2) if ars else None,
            "ars_prev": round(ars_prev, 2) if ars_prev else None,
            "usd": round(usd, 4) if usd else None,
            "usd_prev": round(usd_prev, 4) if usd_prev else None,
            "usd_hist": {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in usd_d.items()},
        }

    datos = {
        "actualizado": ahora_arg().strftime("%d/%m/%Y %H:%M"),
        "ccl": round(ccl, 2) if ccl else None,
        "ccl_prev": round(ccl_anterior, 2) if ccl_anterior else None,
        "ccl_hist": {d.strftime("%Y-%m-%d"): round(float(v), 2) for d, v in ccl_hist.items()},
        "tickers": salida,
    }
    with open(ARCHIVO, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Listo: {len(salida)} tickers · CCL {datos['ccl']}")


if __name__ == "__main__":
    main()
