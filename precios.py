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
from cripto import CRIPTO, es_cripto, precios_vivo, volumen_relativo as volumen_cripto
from zoneinfo import ZoneInfo

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


# Argentinas: el ADR de Nueva York tiene otro nombre en la bolsa local
SIMBOLO_LOCAL = {"YPF": "YPFD", "PAM": "PAMP", "TEO": "TECO2", "TGS": "TGSU2", "CRESY": "CRES", "IRS": "IRSA", "DIS": "DISN"}


def simbolo_cedear(ticker):
    """WDC -> WDC.BA · BRK-B -> BRKB.BA · YPF -> YPFD.BA"""
    t = SIMBOLO_LOCAL.get(ticker, ticker)
    return t.replace("-", "").replace(".", "") + ".BA"


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


# ----------------------------- balances y noticias -----------------------------

def proximo_balance(ticker):
    """Fecha (YYYY-MM-DD) del próximo balance de la empresa, si Yahoo la tiene."""
    try:
        cal = yf.Ticker(ticker).calendar
        fechas = cal.get("Earnings Date") if isinstance(cal, dict) else cal.loc["Earnings Date"].tolist()
        hoy = ahora_arg().date()
        futuras = sorted(pd.Timestamp(f).date() for f in (fechas or []) if pd.Timestamp(f).date() >= hoy)
        return futuras[0].isoformat() if futuras else None
    except Exception:
        return None


# ----------------------------- noticias (Google Noticias) -----------------------------
import re as _re
import xml.etree.ElementTree as _ET
from email.utils import parsedate_to_datetime as _parse_fecha

_SUFIJOS = r"\b(inc|corp|corporation|co|company|ltd|plc|sa|s\.a\.|n\.v\.|nv|holdings?|group|class [abc]|the|incorporated|limited|trust|series \d+|etf)\b\.?"


_FINANZAS = (r"acci[oó]n|stock|shares|bolsa|cotiza|ganancia|beneficio|resultado|balance|trimestr|quarter|earnings|"
             r"analista|analyst|precio objetivo|price target|dividend|inversor|investor|mercado|market|wall street|"
             r"ventas|revenue|sube|suben|cae|caen|baja|bajan|rally|compra|venta|upgrade|downgrade|%|\$|us\$|u\$s|etf|nasdaq|nyse|merval|byma")


def nombre_corto(empresa, ticker):
    """'Walmart Inc.' -> 'Walmart' · 'Alphabet Inc. Class A' -> 'Alphabet'"""
    n = _re.sub(r"\bS\.?A\.?(?=\s|$)|\bN\.?V\.?(?=\s|$)|\bS\.?A\.?B\.?", " ", (empresa or "").replace(",", " "))
    n = _re.sub(_SUFIJOS, "", n, flags=_re.I)
    n = _re.sub(r"[^\w&' .-]", " ", n)
    genericas = {"grupo", "financiero", "financial", "sociedad", "anonima", "anónima", "compania", "compañía",
                 "spdr", "state", "street", "ishares", "invesco", "vaneck", "global", "international", "de", "y"}
    palabras = [w for w in n.split() if w.lower() not in genericas]
    n = " ".join(palabras[:2]).strip(" .-")
    return n if len(n) >= 3 else ticker


def noticias_google(ticker, empresa="", maximo=5, dias=7):
    """Noticias en español (y si no hay, en inglés) de Google Noticias, filtradas por la empresa."""
    es_etf = not empresa or "etf" in empresa.lower() or "trust" in empresa.lower()
    nombre = ticker if es_etf else nombre_corto(empresa, ticker)
    claves = {nombre.lower()} | ({ticker.lower()} if len(ticker) >= 3 else set()) | ({nombre.split()[0].lower()} if len(nombre.split()) > 1 and len(nombre.split()[0]) >= 5 else set())
    salida, vistos = [], set()
    extra = "ETF" if es_etf else "acciones"
    for consulta, idioma in ((f'"{nombre}" {extra}', ("es-419", "AR", "AR:es-419")),
                             (f'"{nombre}" {"ETF" if es_etf else "stock"}', ("en-US", "US", "US:en")),
                             (f'"{ticker}" acciones', ("es-419", "AR", "AR:es-419"))):
        try:
            r = requests.get("https://news.google.com/rss/search", timeout=20,
                             params={"q": f"{consulta} when:{dias}d", "hl": idioma[0], "gl": idioma[1], "ceid": idioma[2]},
                             headers={"User-Agent": "Mozilla/5.0"})
            items = _ET.fromstring(r.content).iter("item") if r.status_code == 200 else []
        except Exception:
            items = []
        encontrados = []
        for it in items:
            titulo, fuente = it.findtext("title") or "", it.findtext("source") or ""
            if fuente and titulo.endswith(" - " + fuente):
                titulo = titulo[: -len(fuente) - 3]
            t_low = titulo.lower()
            if not any(_re.search(r"\b" + _re.escape(c) + r"\b", t_low) for c in claves):
                continue   # no habla de esta empresa
            if ticker.lower() not in t_low and not _re.search(_FINANZAS, t_low):
                continue   # menciona el nombre pero no parece una noticia de mercado
            clave = _re.sub(r"\W+", "", t_low)[:30]
            if clave in vistos:
                continue
            vistos.add(clave)
            try:
                fecha = _parse_fecha(it.findtext("pubDate")).isoformat()
            except Exception:
                fecha = ""
            encontrados.append({"titulo": titulo, "url": it.findtext("link"), "fuente": fuente, "fecha": fecha})
        encontrados.sort(key=lambda n: n["fecha"], reverse=True)
        salida += encontrados[: maximo - len(salida)]
        if len(salida) >= maximo:
            break
    return salida


def noticias_recientes(ticker, horas=30, maximo=3):
    """Últimas noticias de la empresa (de las últimas `horas`)."""
    salida = []
    try:
        limite = datetime.now(timezone.utc) - timedelta(hours=horas)
        for n in yf.Ticker(ticker).news or []:
            c = n.get("content", n)
            titulo = c.get("title")
            url = ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or c.get("link"))
            fecha = c.get("pubDate") or ""
            if not fecha and c.get("providerPublishTime"):
                fecha = datetime.fromtimestamp(c["providerPublishTime"], timezone.utc).isoformat()
            try:
                if fecha and pd.Timestamp(fecha).tz_convert("UTC") < limite:
                    continue
            except Exception:
                pass
            if titulo and url:
                salida.append({"titulo": titulo, "url": url, "fuente": (c.get("provider") or {}).get("displayName", ""), "fecha": fecha})
            if len(salida) >= maximo:
                break
    except Exception:
        pass
    if not salida:
        empresa = ""
        try:
            empresa = yf.Ticker(ticker).info.get("shortName", "")
        except Exception:
            pass
        limite_txt = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        salida = [n for n in noticias_google(ticker, empresa, maximo, dias=2) if not n["fecha"] or n["fecha"] >= limite_txt]
    return salida


def guardar_hist(hist, fecha, valor, dias=400):
    """Agrega el precio de hoy al historial en pesos y deja solo los últimos `dias`."""
    hist = dict(hist or {})
    if valor:
        hist[fecha] = round(valor, 2)
    return {k: hist[k] for k in sorted(hist)[-dias:]}


VOL_DIAS = 20   # días para el volumen "normal"


def volumenes(df, simbolo):
    """Serie de volumen de un símbolo (vacía si no hay)."""
    try:
        v = df[simbolo]["Volume"] if isinstance(df.columns, pd.MultiIndex) else df["Volume"]
        return v.dropna()
    except Exception:
        return pd.Series(dtype=float)


def volumen_relativo(diario, intradia, simbolo):
    """Cuántas veces más se opera hoy que un día normal, a esta misma hora (bolsa de EE.UU.).
    Volumen de hoy (dato diario de Yahoo, se va actualizando) / (promedio de 20 días × parte del día que ya pasó).
    La parte del día sale de cómo se repartió el volumen cada 15 min en los días anteriores (mediana).
    None si hoy no abrió o es muy temprano."""
    vi, vd = volumenes(intradia, simbolo), volumenes(diario, simbolo)
    if vi.empty or vd.empty:
        return None
    ny = ZoneInfo("America/New_York")
    ahora_ny = datetime.now(ny)
    vd.index = pd.to_datetime(vd.index).tz_localize(None).normalize()
    hoy = vd[vd.index.date == ahora_ny.date()]
    previos = vd[vd.index.date < ahora_ny.date()].tail(VOL_DIAS)
    if hoy.empty or len(previos) < VOL_DIAS // 2:
        return None   # hoy no abrió la bolsa o falta historia
    idx = vi.index.tz_convert(ny) if vi.index.tz is not None else vi.index.tz_localize(ny)
    fechas, horas = idx.date, idx.time
    partes = []   # qué parte del volumen del día ya se había operado a esta hora, en los días anteriores
    for d in sorted(set(fechas[fechas < ahora_ny.date()])):
        del_dia = fechas == d
        total = float(vi.values[del_dia].sum())
        if total > 0:
            partes.append(float(vi.values[del_dia & (horas <= ahora_ny.time())].sum()) / total)
    if not partes:
        return None
    fraccion = float(pd.Series(partes).median())
    if fraccion < 0.25:
        return None   # muy temprano: todavía no dice nada
    esperado = float(previos.mean()) * fraccion
    return round(float(hoy.iloc[-1]) / esperado, 2) if esperado > 0 else None


def dividendos(ticker):
    """Dividendos por acción (US$) de los últimos 2 años {fecha de corte: monto} y la próxima fecha de corte."""
    try:
        tk = yf.Ticker(ticker)
        d = tk.dividends
        hist = {}
        if len(d):
            desde = pd.Timestamp.now(tz=d.index.tz) - pd.Timedelta(days=730)
            hist = {i.strftime("%Y-%m-%d"): round(float(v), 4) for i, v in d.items() if i >= desde}
        cal = tk.calendar
        ex = cal.get("Ex-Dividend Date") if isinstance(cal, dict) else None
        prox = ex.isoformat() if ex and ex >= ahora_arg().date() else None
        return hist, prox
    except Exception as e:
        print(f"  {ticker}: sin dividendos ({e})")
        return {}, None


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
    # Las cripto van aparte: precio de CoinGecko, nunca de IOL ni de Yahoo con el nombre corto (ETH ≠ Ethan Allen)
    criptos = [t for t in tickers if es_cripto(t)]
    tickers = [t for t in tickers if not es_cripto(t)]

    simbolos = set()
    for t in tickers:
        simbolos |= {t, simbolo_cedear(t)}
    for ars, usd, _ in PARES_CCL:
        simbolos |= {ars, usd}
    simbolos |= {CRIPTO[t][0] for t in criptos}   # BTC-USD, para el historial en US$
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

    # Historial de precios en pesos (se va armando con cada corrida)
    try:
        with open(ARCHIVO, encoding="utf-8") as f:
            viejo = json.load(f).get("tickers", {})
    except Exception:
        viejo = {}
    hoy_txt = ahora_arg().strftime("%Y-%m-%d")

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
        # Dividendos: cambian poco, se bajan una vez por día
        vj = viejo.get(t, {})
        if vj.get("div_dia") == hoy_txt:
            divs, div_prox = vj.get("dividendos") or {}, vj.get("div_prox")
        else:
            divs, div_prox = dividendos(t)
        salida[t] = {
            "simbolo": ced,
            "fuente": fuente,
            "ars": round(ars, 2) if ars else None,
            "ars_prev": round(ars_prev, 2) if ars_prev else None,
            "usd": round(usd, 4) if usd else None,
            "usd_prev": round(usd_prev, 4) if usd_prev else None,
            "usd_hist": {d.strftime("%Y-%m-%d"): round(float(v), 4) for d, v in usd_d.items()},
            "ars_hist": guardar_hist(viejo.get(t, {}).get("ars_hist", {}), hoy_txt, ars),
            "balance": proximo_balance(t),
            "noticias": noticias_recientes(t),
            "vol_ratio": volumen_relativo(diario, intradia, t),
            "dividendos": divs,
            "div_prox": div_prox,
            "div_dia": hoy_txt,
        }

    # ---- Cripto: precio en vivo (CoinGecko) en pesos con el dólar cripto (CriptoYa) ----
    vivo = precios_vivo(criptos, dolar_respaldo=ccl)
    for t in criptos:
        yahoo = CRIPTO[t][0]
        usd_d = cierres(diario, yahoo)
        v = vivo.get(t, {})
        ars, ars_prev, usd, usd_prev = v.get("ars"), v.get("ars_prev"), v.get("usd"), v.get("usd_prev")
        if usd is None:
            print(f"  {t}: sin precio de CoinGecko")
        salida[t] = {
            "simbolo": yahoo,
            "fuente": "CoinGecko",
            "ars": round(ars, 2) if ars else None,
            "ars_prev": round(ars_prev, 2) if ars_prev else None,
            "usd": round(usd, 6) if usd else None,
            "usd_prev": round(usd_prev, 6) if usd_prev else None,
            "usd_hist": {d.strftime("%Y-%m-%d"): round(float(x), 6) for d, x in usd_d.items()},
            "ars_hist": guardar_hist(viejo.get(t, {}).get("ars_hist", {}), hoy_txt, ars),
            "balance": None,
            "noticias": noticias_recientes(yahoo),
            "vol_ratio": volumen_cripto(t),
        }

    # IA: buena/mala, impacto y explicación (reusa lo ya analizado)
    try:
        import noticias_ia
        cache = noticias_ia.cache_de([n for v in viejo.values() for n in (v.get("noticias") or [])])
        noticias_ia.analizar([(t, n) for t, v in salida.items() for n in v["noticias"]], cache)
    except Exception as e:
        print(f"IA: no se pudo analizar ({e})")

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
