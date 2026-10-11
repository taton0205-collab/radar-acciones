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

# --- Filtros fundamentales ---
PE_MAX = 30                # P/E máximo
ROE_MIN = 0.15             # ROE mínimo (0.15 = 15%)
DEUDA_PAT_MAX = 100        # Deuda/Patrimonio máximo en % (100 = 1 vez)
CREC_INGRESOS_MIN = 0.05   # Crecimiento de ingresos interanual mínimo (5%)
CREC_GANANCIAS_MIN = 0.0   # Crecimiento de ganancias mínimo (> 0%)
# Flujo de caja libre: tiene que ser positivo

# --- Filtros técnicos ---
RSI_MIN, RSI_MAX = 40, 65  # Zona de RSI buscada
VOL_RATIO_MIN = 1.0        # Volumen últimos 20 días / últimos 60 días

# --- Peso de cada análisis en el puntaje final (tienen que sumar 1) ---
PESO_FUNDAMENTAL = 0.5
PESO_TECNICO = 0.5

TOP_N = 15                          # Cuántas mostrar en pantalla
MAX_POR_SECTOR = 3                  # Máximo de acciones del mismo sector en el top (0 = sin límite)
ARCHIVO_SALIDA = "ranking_acciones.csv"
ARCHIVO_WEB = "docs/data.json"      # Datos que lee la página web
NOTICIAS_TOP = 40                   # Se bajan noticias de las primeras N del ranking
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
    datos = {"Precio": precio, "RSI": rsi_actual, "% sobre SMA200": dist_sma200}
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
    return datos, criterios, notas


def obtener_sp500():
    """Baja la lista actual del S&P 500 desde Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
    tabla = pd.read_html(io.StringIO(html))[0]
    # Yahoo usa guion en vez de punto (BRK.B -> BRK-B)
    return [t.replace(".", "-") for t in tabla["Symbol"]]


def obtener_noticias(ticker):
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
        print(f"  {ticker}: sin noticias ({e})")
    return noticias


def guardar_web(ranking):
    """Guarda el ranking en JSON para la página web."""
    hora_arg = datetime.now(timezone.utc) - timedelta(hours=3)
    filas = json.loads(ranking.to_json(orient="records"))  # NaN -> null
    print(f"Bajando noticias de las primeras {NOTICIAS_TOP}...")
    for fila in filas[:NOTICIAS_TOP]:
        fila["Noticias"] = obtener_noticias(fila["Ticker"])
        time.sleep(0.2)
    datos = {
        "actualizado": hora_arg.strftime("%d/%m/%Y %H:%M"),
        "max_por_sector": MAX_POR_SECTOR,
        "acciones": filas,
    }
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

    print(f"Descargando precios de {len(tickers)} acciones...")
    precios = yf.download(
        tickers, period="1y", auto_adjust=True,
        group_by="ticker", progress=False, threads=True,
    )

    filas = []
    for i, ticker in enumerate(tickers, 1):
        print(f"Analizando [{i}/{len(tickers)}] {ticker}      ", end="\r")
        try:
            if isinstance(precios.columns, pd.MultiIndex):
                df = precios[ticker]
            else:
                df = precios

            tecnico = analisis_tecnico(df)
            if tecnico is None:
                continue
            datos_tec, crit_tec, notas_tec = tecnico
            if sum(crit_tec.values()) < TECNICO_MINIMO:
                continue  # gráfico flojo: no vale la pena pedir fundamentales

            info = yf.Ticker(ticker).info
            datos_fund, crit_fund, notas_fund = analisis_fundamental(info)
        except Exception as e:
            print(f"\n  {ticker}: no se pudo analizar ({e})")
            continue

        ok_fund = sum(crit_fund.values())
        ok_tec = sum(crit_tec.values())
        nota_fund = sum(notas_fund.values()) / len(notas_fund) * 100
        nota_tec = sum(notas_tec.values()) / len(notas_tec) * 100
        puntaje = nota_fund * PESO_FUNDAMENTAL + nota_tec * PESO_TECNICO
        falla = [c for c, ok in {**crit_fund, **crit_tec}.items() if not ok]

        filas.append({
            "Ticker": ticker,
            "Empresa": info.get("shortName", ""),
            "Sector": info.get("sector", ""),
            "Puntaje": round(puntaje, 1),
            "Nota fund": round(nota_fund),
            "Nota tec": round(nota_tec),
            "Fund": f"{ok_fund}/{len(crit_fund)}",
            "Tec": f"{ok_tec}/{len(crit_tec)}",
            **datos_tec,
            **datos_fund,
            "No cumple": ", ".join(falla) if falla else "-",
            "_notas": {k: round(v * 100) for k, v in {**notas_fund, **notas_tec}.items()},
        })
        time.sleep(0.2)  # para no saturar a Yahoo

    print()
    if not filas:
        print("No se pudo analizar ninguna acción. Revisá tu conexión.")
        return

    ranking = (
        pd.DataFrame(filas)
        .sort_values("Puntaje", ascending=False)
        .reset_index(drop=True)
    )
    ranking.index += 1
    ranking = ranking.round(1)
    guardar_web(ranking)
    ranking = ranking.drop(columns="_notas")

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
