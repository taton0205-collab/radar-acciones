"""
BOT DE SELECCIÓN DE ACCIONES (EE.UU.)
Combina análisis fundamental + técnico y arma un ranking de candidatas.

Instalación (una sola vez):
    pip install yfinance pandas requests lxml

Uso:
    python bot_acciones.py

Es una herramienta para FILTRAR candidatas, no una recomendación de compra.
"""

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import requests
import yfinance as yf

# ======================= CONFIGURACIÓN =======================
USAR_SP500 = True   # True = analiza las ~500 del S&P 500 | False = usa la lista de abajo

# Para ahorrar tiempo, solo se piden los fundamentales de las acciones que
# cumplen al menos esta cantidad de criterios técnicos (de 4). Poné 0 para analizar todas.
TECNICO_MINIMO = 2

# Lista propia (se usa si USAR_SP500 = False o si falla la descarga del S&P 500)
TICKERS = [
    # Tecnología
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AVGO", "ORCL", "ADBE",
    "CRM", "AMD", "INTC", "QCOM", "TXN", "CSCO", "IBM", "MU", "WDC",
    # Finanzas
    "JPM", "BAC", "WFC", "GS", "MS", "V", "MA", "AXP", "BRK-B",
    # Salud
    "JNJ", "UNH", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT",
    # Consumo
    "KO", "PEP", "PG", "WMT", "COST", "MCD", "NKE", "SBUX", "HD", "DIS",
    "NFLX", "TSLA",
    # Energía e industria
    "XOM", "CVX", "CAT", "DE", "GE", "HON", "BA", "UPS", "LMT", "RTX",
    # Telecom
    "T", "VZ",
]

# Argentinas: se analizan sus ADR en dólares (Nueva York), que tienen datos completos
ARGENTINA = ["GGAL", "YPF", "PAM", "BMA", "BBAR", "SUPV", "CEPU", "TGS", "EDN",
             "LOMA", "TEO", "CRESY", "IRS", "VIST", "MELI", "GLOB",
             "TX", "CAAP", "ARCO", "BIOX"]
# ETFs (fondos que se compran como una acción). No tienen balances: se califican solo por el gráfico
ETFS = ["SPY", "QQQ", "DIA", "IWM", "EEM", "EWZ", "XLK", "XLF", "XLE", "XLV",
        "SMH", "GLD", "SLV", "ARKK", "IBIT", "XLY", "XLI", "XLU", "EWJ", "FXI", "URA"]
# Cripto (se compran en exchanges, no en IOL). Solo nota técnica, como los ETFs.
# Ticker en la app: (símbolo en Yahoo, id en CoinGecko para el precio en vivo, nombre)
CRIPTO = {
    "BTC": ("BTC-USD", "bitcoin", "Bitcoin"),
    "ETH": ("ETH-USD", "ethereum", "Ethereum"),
    "BNB": ("BNB-USD", "binancecoin", "BNB"),
    "XRP": ("XRP-USD", "ripple", "XRP"),
    "SOL": ("SOL-USD", "solana", "Solana"),
    "TRX": ("TRX-USD", "tron", "TRON"),
    "DOGE": ("DOGE-USD", "dogecoin", "Dogecoin"),
    "ADA": ("ADA-USD", "cardano", "Cardano"),
    "LINK": ("LINK-USD", "chainlink", "Chainlink"),
    "XLM": ("XLM-USD", "stellar", "Stellar"),
    "AVAX": ("AVAX-USD", "avalanche-2", "Avalanche"),
    "LTC": ("LTC-USD", "litecoin", "Litecoin"),
    "DOT": ("DOT-USD", "polkadot", "Polkadot"),
    "BCH": ("BCH-USD", "bitcoin-cash", "Bitcoin Cash"),
    "HBAR": ("HBAR-USD", "hedera-hashgraph", "Hedera"),
}

# --- Filtros fundamentales ---
PE_MAX = 30                # P/E máximo
ROE_MIN = 0.15             # ROE mínimo (0.15 = 15%)
DEUDA_PAT_MAX = 100        # Deuda/Patrimonio máximo en % (100 = 1 vez)
CREC_INGRESOS_MIN = 0.05   # Crecimiento de ingresos interanual mínimo (5%)
CREC_GANANCIAS_MIN = 0.0   # Crecimiento de ganancias mínimo (> 0%)
# Flujo de caja libre: tiene que ser positivo

# --- Filtros técnicos ---
RSI_MIN, RSI_MAX = 40, 65  # Zona de RSI buscada
# Cruce reciente: puntos extra si la SMA50 cruzó hacia arriba a la SMA200 hace poco
CRUCE_DIAS_MAX = 30        # Hasta cuántos días atrás cuenta como "reciente"
CRUCE_BONUS = 10           # Puntos extra máximos en la nota técnica (cruce de hoy = 10, de hace 30 días = 0)
VOL_RATIO_MIN = 1.0        # Volumen últimos 20 días / últimos 60 días

# --- Comparación contra el sector ---
PESO_SECTOR = 0.5          # Cuánto pesa la comparación con su sector (0 = nada, 1 = solo sector)
MIN_EMPRESAS_SECTOR = 4    # Mínimo de empresas en el sector para poder comparar

# --- Historial ---
DIAS_HISTORIAL = 60        # Cuántas actualizaciones se guardan

# --- Peso de cada análisis en el puntaje final (tienen que sumar 1) ---
PESO_FUNDAMENTAL = 0.5
PESO_TECNICO = 0.5

TOP_N = 15                          # Cuántas mostrar en pantalla
MAX_POR_SECTOR = 3                  # Máximo de acciones del mismo sector en el top (0 = sin límite)
ARCHIVO_SALIDA = "ranking_acciones.csv"
ARCHIVO_WEB = "data.json"      # Datos que lee la página web
NOTICIAS_TOP = 40                   # Se bajan noticias de las primeras N del ranking

# CEDEARs que se pueden comprar en Argentina (lista pública de BYMA, sin claves)
BYMA_CEDEARS = "https://open.bymadata.com.ar/vanoms-be-core/rest/api/bymadata/free/cedears"
# Ticker de EE.UU. -> símbolo del CEDEAR en BYMA, cuando no es el mismo
SIMBOLO_CEDEAR = {"BAC": "BA.C", "DIS": "DISN", "BRK-B": "BRKB"}
NOTICIAS_POR_EMPRESA = 5
# =============================================================


def escala(valor, malo, bueno):
    """Nota de 0 a 1: 0 si el valor es 'malo' o peor, 1 si es 'bueno' o mejor,
    y proporcional en el medio. Sirve tanto si más es mejor como si menos es mejor."""
    if valor is None or pd.isna(valor):
        return 0.0
    nota = (valor - malo) / (bueno - malo)
    return float(min(max(nota, 0.0), 1.0))


def nota_rango(valor, lim_bajo, ideal_bajo, ideal_alto, lim_alto):
    """Nota 1 dentro del rango ideal, baja hasta 0 en los límites."""
    if valor is None or pd.isna(valor):
        return 0.0
    if valor < ideal_bajo:
        return escala(valor, lim_bajo, ideal_bajo)
    if valor > ideal_alto:
        return escala(valor, lim_alto, ideal_alto)
    return 1.0


def rsi(cierres, periodo=14):
    """RSI clásico (suavizado de Wilder)."""
    delta = cierres.diff()
    suba = delta.clip(lower=0)
    baja = -delta.clip(upper=0)
    media_suba = suba.ewm(alpha=1 / periodo, adjust=False).mean()
    media_baja = baja.ewm(alpha=1 / periodo, adjust=False).mean()
    rs = media_suba / media_baja
    return 100 - 100 / (1 + rs)


def analisis_tecnico(df):
    """Devuelve (datos, criterios) o None si no hay historia suficiente."""
    cierre = df["Close"].dropna()
    volumen = df["Volume"].dropna()
    if len(cierre) < 200:
        return None

    precio = cierre.iloc[-1]
    sma50 = cierre.rolling(50).mean().iloc[-1]
    sma200 = cierre.rolling(200).mean().iloc[-1]
    rsi_actual = rsi(cierre).iloc[-1]
    vol_ratio = volumen.tail(20).mean() / volumen.tail(60).mean()

    dist_sma200 = (precio / sma200 - 1) * 100   # % arriba/abajo de la SMA200
    dist_cruce = (sma50 / sma200 - 1) * 100     # % de la SMA50 sobre la SMA200

    criterios = {
        "Precio>SMA200": precio > sma200,
        "SMA50>SMA200": sma50 > sma200,
        "RSI en zona": RSI_MIN <= rsi_actual <= RSI_MAX,
        "Volumen en alza": vol_ratio >= VOL_RATIO_MIN,
    }
    # Notas graduadas (0 a 1)
    notas = {
        # Ideal: entre 3% y 25% arriba de la SMA200 (muy arriba = muy estirada)
        "Precio>SMA200": nota_rango(dist_sma200, -5, 3, 25, 50),
        "SMA50>SMA200": escala(dist_cruce, -2, 5),
        "RSI en zona": nota_rango(rsi_actual, 30, 45, 60, 75),
        "Volumen en alza": escala(vol_ratio, 0.8, 1.3),
    }
    # Cruce reciente: ¿hace cuántos días la SMA50 pasó arriba de la SMA200?
    arriba = (cierre.rolling(50).mean() > cierre.rolling(200).mean()).iloc[199:]
    dias_cruce = None
    if bool(arriba.iloc[-1]):
        cambios = arriba.ne(arriba.shift())
        cambios.iloc[0] = False
        idx = cambios[cambios].index
        if len(idx):
            dias_cruce = int(len(arriba.loc[idx[-1]:]) - 1)   # días hábiles desde el cruce
    # Nota del cruce (bonus): 1 si cruzó hoy, baja hasta 0 a los CRUCE_DIAS_MAX días
    notas["Cruce reciente"] = escala(dias_cruce, CRUCE_DIAS_MAX, 0) if dias_cruce is not None else 0.0

    datos = {"Precio": precio, "RSI": rsi_actual, "% sobre SMA200": dist_sma200, "Días desde cruce": dias_cruce}
    return datos, criterios, notas


def analisis_fundamental(info):
    """Devuelve (datos, criterios). Si falta un dato, ese criterio no suma."""
    pe = info.get("trailingPE") or info.get("forwardPE")
    roe = info.get("returnOnEquity")
    deuda = info.get("debtToEquity")
    crec_ing = info.get("revenueGrowth")
    crec_gan = info.get("earningsGrowth")
    fcf = info.get("freeCashflow")
    ingresos = info.get("totalRevenue")
    margen_fcf = fcf / ingresos if (fcf is not None and ingresos) else None

    criterios = {
        "P/E": pe is not None and 0 < pe <= PE_MAX,
        "ROE": roe is not None and roe >= ROE_MIN,
        "Deuda/Pat": deuda is not None and deuda <= DEUDA_PAT_MAX,
        "Crec. ingresos": crec_ing is not None and crec_ing >= CREC_INGRESOS_MIN,
        "Crec. ganancias": crec_gan is not None and crec_gan > CREC_GANANCIAS_MIN,
        "FCF positivo": fcf is not None and fcf > 0,
    }
    datos = {
        "P/E": pe,
        "ROE %": roe * 100 if roe is not None else None,
        "Deuda/Pat %": deuda,
        "Crec. ingresos %": crec_ing * 100 if crec_ing is not None else None,
        "Crec. ganancias %": crec_gan * 100 if crec_gan is not None else None,
        "Margen FCF %": margen_fcf * 100 if margen_fcf is not None else None,
    }
    # Notas graduadas (0 a 1)
    if pe is None or pe <= 0:
        nota_pe = 0.0
    elif pe < 6:
        nota_pe = 0.5   # P/E sospechosamente bajo: puede ser ganancia extraordinaria
    else:
        nota_pe = escala(pe, PE_MAX + 10, 15)
    notas = {
        "P/E": nota_pe,
        "ROE": escala(roe, 0.05, 0.30),
        "Deuda/Pat": escala(deuda, 200, 30),
        "Crec. ingresos": escala(crec_ing, 0.0, 0.20),
        "Crec. ganancias": escala(crec_gan, 0.0, 0.25),
        "FCF positivo": escala(margen_fcf, 0.0, 0.20),
    }
    crudos = {"pe": pe, "roe": roe, "deuda": deuda, "ci": crec_ing, "cg": crec_gan, "fcf": margen_fcf}
    return datos, criterios, notas, crudos


def notas_vs_sector(c, med):
    """Notas (0 a 1) comparando la empresa contra la mediana de su sector."""
    def dif(clave, rango):
        if c[clave] is None or med.get(clave) is None:
            return None
        return escala(c[clave] - med[clave], -rango, rango)

    def ratio(clave, malo, bueno):
        if c[clave] is None or not med.get(clave):
            return None
        if clave == "pe" and c[clave] <= 0:
            return 0.0
        return escala(c[clave] / med[clave], malo, bueno)

    return {
        "P/E": ratio("pe", 1.6, 0.7),            # más barata que su sector = mejor
        "ROE": dif("roe", 0.10),
        "Deuda/Pat": ratio("deuda", 2.0, 0.5),   # menos deuda que su sector = mejor
        "Crec. ingresos": dif("ci", 0.08),
        "Crec. ganancias": dif("cg", 0.15),
        "FCF positivo": dif("fcf", 0.08),
    }


def medianas_por_sector(filas):
    """Mediana de cada dato fundamental por sector."""
    sectores = {}
    for f in filas:
        if f["_crudos"] is None:
            continue  # ETFs: no tienen datos de empresa
        sectores.setdefault(f["Sector"], []).append(f["_crudos"])
    medianas = {}
    for sector, lista in sectores.items():
        if len(lista) < MIN_EMPRESAS_SECTOR or not sector:
            continue
        med = {}
        for clave in ["pe", "roe", "deuda", "ci", "cg", "fcf"]:
            valores = [c[clave] for c in lista if c[clave] is not None and not (clave == "pe" and c[clave] <= 0)]
            med[clave] = float(pd.Series(valores).median()) if len(valores) >= 3 else None
        medianas[sector] = med
    return medianas


def obtener_sp500():
    """Baja la lista actual del S&P 500 desde Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
    tabla = pd.read_html(io.StringIO(html))[0]
    # Yahoo usa guion en vez de punto (BRK.B -> BRK-B)
    return [t.replace(".", "-") for t in tabla["Symbol"]]


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


def obtener_noticias(ticker, empresa=""):
    """Últimas noticias de la empresa (Yahoo Finance)."""
    noticias = []
    try:
        for n in yf.Ticker(ticker).news or []:
            c = n.get("content", n)  # yfinance nuevo usa "content", el viejo no
            titulo = c.get("title")
            url = ((c.get("canonicalUrl") or {}).get("url")
                   or (c.get("clickThroughUrl") or {}).get("url") or c.get("link"))
            if not titulo or not url:
                continue
            fuente = (c.get("provider") or {}).get("displayName") or c.get("publisher", "")
            fecha = c.get("pubDate") or ""
            if not fecha and c.get("providerPublishTime"):
                fecha = datetime.fromtimestamp(c["providerPublishTime"], timezone.utc).isoformat()
            noticias.append({"titulo": titulo, "url": url, "fuente": fuente, "fecha": fecha})
            if len(noticias) >= NOTICIAS_POR_EMPRESA:
                break
    except Exception as e:
        print(f"  {ticker}: sin noticias de Yahoo ({e})")
    if not noticias:
        noticias = noticias_google(ticker, empresa, NOTICIAS_POR_EMPRESA)
    return noticias


def leer_historial():
    """Lee el historial guardado en la corrida anterior (dentro del data.json viejo)."""
    try:
        with open(ARCHIVO_WEB, encoding="utf-8") as f:
            return json.load(f).get("historial", {})
    except Exception:
        return {}


def obtener_cedears():
    """Símbolos de los CEDEARs en pesos de BYMA. None si no se pudo bajar la lista."""
    try:
        r = requests.post(BYMA_CEDEARS, timeout=30,
                          json={"excludeZeroPxAndQty": False, "T2": True, "T1": True, "T0": False})
        r.raise_for_status()
        simbolos = {x["symbol"] for x in r.json() if x.get("denominationCcy") == "ARS"}
    except Exception as e:
        print(f"BYMA: no se pudo bajar la lista de CEDEARs ({e}). Muestro todas.")
        return None
    if len(simbolos) < 200:   # lista rara/incompleta: mejor no ocultar nada
        print(f"BYMA: la lista vino con solo {len(simbolos)} CEDEARs. Muestro todas.")
        return None
    return simbolos


def marcar_cedears(filas):
    """Agrega a cada fila "CEDEAR" (True/False, o None si no se sabe) y el "Símbolo BYMA"."""
    cedears = obtener_cedears()
    for fila in filas:
        t = fila["Ticker"]
        simbolo = SIMBOLO_CEDEAR.get(t, t).replace("-", "")
        if fila.get("Mercado") == "Cripto":
            fila["CEDEAR"] = None      # se compran en exchanges
            continue
        if fila.get("Mercado") == "Argentina":
            fila["CEDEAR"] = True      # se compran como acción local
        else:
            fila["CEDEAR"] = None if cedears is None else simbolo in cedears
            if simbolo != t:
                fila["Símbolo BYMA"] = simbolo
    if cedears is not None:
        print(f"CEDEARs: {sum(1 for f in filas if f['CEDEAR'])} de {len(filas)} se pueden comprar en Argentina")


def guardar_web(ranking):
    """Guarda el ranking en JSON para la página web."""
    hora_arg = datetime.now(timezone.utc) - timedelta(hours=3)
    filas = json.loads(ranking.to_json(orient="records"))  # NaN -> null

    # Historial: {"2026-10-11": {"AAPL": 81.2, ...}, ...}
    historial = leer_historial()
    hoy = hora_arg.strftime("%Y-%m-%d")
    fechas_previas = sorted(f for f in historial if f != hoy)
    anterior = historial[fechas_previas[-1]] if fechas_previas else {}
    puestos_ant = {t: i + 1 for i, (t, _) in enumerate(sorted(anterior.items(), key=lambda x: -x[1]))}
    historial[hoy] = {f["Ticker"]: f["Puntaje"] for f in filas}
    fechas = sorted(historial)[-DIAS_HISTORIAL:]
    historial = {f: historial[f] for f in fechas}

    for puesto, fila in enumerate(filas, 1):
        t = fila["Ticker"]
        fila["Historial"] = [[f, historial[f][t]] for f in fechas if t in historial[f]][-30:]
        fila["Cambio"] = round(fila["Puntaje"] - anterior[t], 1) if t in anterior else None
        fila["Puesto anterior"] = puestos_ant.get(t)
        fila["Nueva"] = bool(anterior) and t not in anterior

    marcar_cedears(filas)
    # Noticias solo de las primeras que se pueden comprar (+ todas las cripto, aparte)
    con_noticias = ([f for f in filas if f["CEDEAR"] is not False and f["Mercado"] != "Cripto"][:NOTICIAS_TOP]
                    + [f for f in filas if f["Mercado"] == "Cripto"])
    print(f"Bajando noticias de las primeras {NOTICIAS_TOP}...")
    for fila in con_noticias:
        fila["Noticias"] = obtener_noticias(fila.get("Yahoo") or fila["Ticker"], fila.get("Empresa", ""))
        time.sleep(0.2)

    # IA: buena/mala, impacto y explicación (reusa lo que ya estaba analizado)
    try:
        import noticias_ia
        viejo = json.load(open(ARCHIVO_WEB, encoding="utf-8")).get("acciones", []) if os.path.exists(ARCHIVO_WEB) else []
        cache = noticias_ia.cache_de([n for f in viejo for n in (f.get("Noticias") or [])])
        noticias_ia.analizar([(f["Ticker"], n) for f in con_noticias for n in f.get("Noticias") or []], cache)
    except Exception as e:
        print(f"IA: no se pudo analizar ({e})")
    datos = {
        "actualizado": hora_arg.strftime("%d/%m/%Y %H:%M"),
        "max_por_sector": MAX_POR_SECTOR,
        # Para que la app recalcule la nota técnica de las cripto igual que el bot
        "params_tecnico": {"rsi_min": RSI_MIN, "rsi_max": RSI_MAX, "vol_ratio_min": VOL_RATIO_MIN,
                           "cruce_dias_max": CRUCE_DIAS_MAX, "cruce_bonus": CRUCE_BONUS},
        "acciones": filas,
        "historial": historial,
    }
    if os.path.dirname(ARCHIVO_WEB):
        os.makedirs(os.path.dirname(ARCHIVO_WEB), exist_ok=True)
    with open(ARCHIVO_WEB, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False)
    print(f"Datos para la web guardados en: {ARCHIVO_WEB}")


def main():
    tickers = TICKERS
    if USAR_SP500:
        try:
            tickers = obtener_sp500()
            print(f"Lista del S&P 500 descargada: {len(tickers)} acciones")
        except Exception as e:
            print(f"No se pudo bajar el S&P 500 ({e}). Uso la lista propia.")

    extra = [t for t in ARGENTINA + ETFS if t not in tickers]
    tickers = list(tickers) + extra + [y for y, _, _ in CRIPTO.values()]
    yahoo_a_cripto = {y: t for t, (y, _, _) in CRIPTO.items()}
    print(f"Descargando precios de {len(tickers)} acciones...")
    precios = yf.download(
        tickers, period="1y", auto_adjust=True,
        group_by="ticker", progress=False, threads=True,
    )

    filas = []
    for i, ticker in enumerate(tickers, 1):
        print(f"Analizando [{i}/{len(tickers)}] {ticker}      ", end="\r")
        yahoo = ticker
        ticker = yahoo_a_cripto.get(yahoo, yahoo)   # BTC-USD -> BTC
        try:
            if isinstance(precios.columns, pd.MultiIndex):
                df = precios[yahoo]
            else:
                df = precios

            tecnico = analisis_tecnico(df)
            if tecnico is None:
                continue
            datos_tec, crit_tec, notas_tec = tecnico
            mercado = ("Cripto" if yahoo in yahoo_a_cripto else "ETF" if ticker in ETFS
                       else "Argentina" if ticker in ARGENTINA else "EE.UU.")
            if mercado == "EE.UU." and sum(crit_tec.values()) < TECNICO_MINIMO:
                continue  # gráfico flojo: no vale la pena pedir fundamentales

            extra_cripto = {}
            if mercado == "Cripto":
                _, gecko, nombre = CRIPTO[ticker]
                info = {"shortName": nombre, "sector": "Cripto"}
                datos_fund, crit_fund, notas_fund, crudos = {}, {}, {}, None
                # Historial para que la app recalcule la nota con el precio en vivo
                cierres = df["Close"].dropna()
                volumen = df["Volume"].dropna()
                extra_cripto = {
                    "Yahoo": yahoo, "CoinGecko": gecko,
                    "_serie": {"fin": cierres.index[-1].strftime("%Y-%m-%d"),
                               "cierres": [float(f"{v:.6g}") for v in cierres],
                               "vol_ratio": float(volumen.tail(20).mean() / volumen.tail(60).mean())},
                }
            elif mercado == "ETF":
                try:
                    info = yf.Ticker(ticker).info
                except Exception:
                    info = {}
                info = {"shortName": info.get("shortName") or info.get("longName") or ticker, "sector": "ETF"}
                datos_fund, crit_fund, notas_fund, crudos = {}, {}, {}, None
            else:
                info = yf.Ticker(ticker).info
                datos_fund, crit_fund, notas_fund, crudos = analisis_fundamental(info)
        except Exception as e:
            print(f"\n  {ticker}: no se pudo analizar ({e})")
            continue

        falla = [c for c, ok in {**crit_fund, **crit_tec}.items() if not ok]
        filas.append({
            "Ticker": ticker,
            "Empresa": info.get("shortName", ""),
            "Sector": info.get("sector", ""),
            "Mercado": mercado,
            "Fund": f"{sum(crit_fund.values())}/{len(crit_fund)}",
            "Tec": f"{sum(crit_tec.values())}/{len(crit_tec)}",
            **datos_tec,
            **datos_fund,
            "No cumple": ", ".join(falla) if falla else "-",
            "_notas_abs": notas_fund,
            "_notas_tec": notas_tec,
            "_crudos": crudos,
            **extra_cripto,
        })
        time.sleep(0.2)  # para no saturar a Yahoo

    print()
    if not filas:
        print("No se pudo analizar ninguna acción. Revisá tu conexión.")
        return

    # Notas finales: mezcla de valor absoluto + comparación con su sector
    medianas = medianas_por_sector(filas)
    for f in filas:
        med = medianas.get(f["Sector"]) if f["_crudos"] is not None else None
        notas_fund = dict(f["_notas_abs"])
        if med:
            for k, rel in notas_vs_sector(f["_crudos"], med).items():
                if rel is not None:
                    notas_fund[k] = notas_fund[k] * (1 - PESO_SECTOR) + rel * PESO_SECTOR
        notas_tec = f["_notas_tec"]
        nota_fund = sum(notas_fund.values()) / len(notas_fund) * 100 if notas_fund else None
        base = {k: v for k, v in notas_tec.items() if k != "Cruce reciente"}
        nota_tec = sum(base.values()) / len(base) * 100
        nota_tec = min(100.0, nota_tec + notas_tec.get("Cruce reciente", 0) * CRUCE_BONUS)
        if nota_fund is None:   # ETF y cripto: solo cuenta el gráfico
            f["Puntaje"] = round(nota_tec, 1)
            f["Nota fund"] = None
        else:
            f["Puntaje"] = round(nota_fund * PESO_FUNDAMENTAL + nota_tec * PESO_TECNICO, 1)
            f["Nota fund"] = round(nota_fund)
        f["Nota tec"] = round(nota_tec)
        f["_notas"] = {k: round(v * 100) for k, v in {**notas_fund, **notas_tec}.items()}
        pct = lambda v: round(v * 100, 1) if v is not None else None
        f["_sector"] = None if not med else {
            "P/E": round(med["pe"], 1) if med["pe"] else None, "ROE": pct(med["roe"]),
            "Deuda/Pat": round(med["deuda"]) if med["deuda"] else None,
            "Crec. ingresos": pct(med["ci"]), "Crec. ganancias": pct(med["cg"]),
            "FCF positivo": pct(med["fcf"]),
        }
        for k in ("_notas_abs", "_notas_tec", "_crudos"):
            del f[k]

    ranking = (
        pd.DataFrame(filas)
        .sort_values("Puntaje", ascending=False)
        .reset_index(drop=True)
    )
    ranking.index += 1
    ranking = ranking.round(1)
    guardar_web(ranking)
    ranking = ranking.drop(columns=["_notas", "_sector", "_serie"], errors="ignore")

    # Top con límite por sector, para diversificar
    if MAX_POR_SECTOR > 0:
        top = ranking.groupby("Sector", sort=False).head(MAX_POR_SECTOR).head(TOP_N)
    else:
        top = ranking.head(TOP_N)

    print(f"\nTOP {TOP_N} CANDIDATAS (máx. {MAX_POR_SECTOR} por sector)\n")
    columnas = ["Ticker", "Empresa", "Sector", "Puntaje", "Nota fund", "Nota tec", "P/E", "RSI"]
    pd.set_option("display.max_colwidth", 25)
    print(top[columnas].to_string())

    # sep=";" y decimal="," para que abra bien en Excel en español
    ranking.to_csv(ARCHIVO_SALIDA, sep=";", decimal=",",
                   encoding="utf-8-sig", index_label="Puesto")
    print(f"\nRanking completo guardado en: {ARCHIVO_SALIDA}")


if __name__ == "__main__":
    main()
