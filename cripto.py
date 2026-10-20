"""
CRIPTO: lista de cripto que sigue el Radar y su precio en vivo.

Lo usan bot_acciones.py (ranking), precios.py (cartera) y alertas.py (avisos, también fines de semana).
Fuentes (gratis para uso personal): CoinGecko para el precio en US$ y CriptoYa para el dólar cripto.
Solo necesita `requests`.
"""

import requests

# Ticker en la app: (símbolo en Yahoo, id en CoinGecko, nombre)
# En la app estos tickers SIEMPRE son cripto (ETH es Ethereum, no la acción Ethan Allen).
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

COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
CRIPTOYA_USDT = "https://criptoya.com/api/usdt/ars/1"


def es_cripto(ticker):
    return (ticker or "").upper() in CRIPTO


def dolar_cripto():
    """Pesos por 1 USDT: mediana de lo que cobran los exchanges argentinos (CriptoYa). None si falla."""
    try:
        r = requests.get(CRIPTOYA_USDT, timeout=20)
        r.raise_for_status()
        valores = sorted(x["totalAsk"] for x in r.json().values()
                         if isinstance(x, dict) and (x.get("totalAsk") or 0) > 0)
        return valores[len(valores) // 2] if valores else None
    except Exception as e:
        print(f"CriptoYa: no se pudo bajar el dólar cripto ({e})")
        return None


def precios_vivo(tickers, dolar_respaldo=None):
    """{ticker: {"usd", "usd_prev", "ars", "ars_prev", "dolar"}} con el precio de ahora y el de hace 24 h.
    Solo para los tickers que son cripto. Si CoinGecko falla, devuelve {} (no inventa precios)."""
    ids = {CRIPTO[t.upper()][1]: t.upper() for t in tickers if es_cripto(t)}
    if not ids:
        return {}
    try:
        r = requests.get(COINGECKO, timeout=20, params={
            "ids": ",".join(ids), "vs_currencies": "usd", "include_24hr_change": "true"})
        r.raise_for_status()
        datos = r.json()
    except Exception as e:
        print(f"CoinGecko: no se pudo bajar el precio de las cripto ({e})")
        return {}
    dolar = dolar_cripto() or dolar_respaldo
    salida = {}
    for gid, t in ids.items():
        p = datos.get(gid) or {}
        usd = p.get("usd")
        if not usd:
            continue
        cambio = p.get("usd_24h_change")
        prev = usd / (1 + cambio / 100) if cambio is not None else None
        salida[t] = {
            "usd": usd,
            "usd_prev": prev,
            "ars": usd * dolar if dolar else None,
            "ars_prev": prev * dolar if dolar and prev else None,
            "dolar": dolar,
        }
    return salida
