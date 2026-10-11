"""
PRECIOS DE CEDEARs (en pesos) + DÓLAR CCL  ->  precios.json

Corre cada 30 minutos en GitHub. Para cada ticker que usan los usuarios baja:
  - precio actual del CEDEAR en pesos (bolsa argentina, ej: WDC.BA)
  - precio actual de la acción en dólares (EE.UU.)
  - historial diario de 2 años del precio en dólares
y además el dólar CCL (actual + historial), calculado con acciones argentinas
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

    # ---- Cada ticker ----
    salida = {}
    for t in tickers:
        ced = simbolo_cedear(t)
        ars, ars_prev = actual_y_previo(cierres(diario, ced), serie_i(ced))
        usd_d = cierres(diario, t)
        usd, usd_prev = actual_y_previo(usd_d, serie_i(t))
        if ars is None and usd is None:
            print(f"  {t}: sin precios")
        salida[t] = {
            "simbolo": ced,
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
