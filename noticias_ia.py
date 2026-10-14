"""
NOTICIAS CON IA (Google Gemini, plan gratis)

Para cada noticia decide si es buena, mala o neutra para la empresa, cuánto impacto
puede tener y escribe una explicación simple en español.
- Necesita el secreto GEMINI_KEY. Si no está, no hace nada (las noticias siguen apareciendo igual).
- Nunca analiza dos veces la misma noticia: reusa lo que ya estaba guardado.
"""

import json
import os
import time

import requests

GEMINI_KEY = os.environ.get("GEMINI_KEY", "").strip()
MODELOS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-2.0-flash"]
POR_LOTE = 20          # noticias por consulta
MAX_POR_CORRIDA = 160  # tope para no pasarse del plan gratis
PAUSA = 5              # segundos entre consultas

CAMPOS = ("sentimiento", "impacto", "explicacion")

PROMPT = """Sos un analista financiero que le explica noticias a un inversor argentino que recién empieza.
Para cada noticia de la lista decidí:
- "sentimiento": "buena", "mala" o "neutra" PARA LA EMPRESA del ticker (no para el mercado en general).
- "impacto": "chico", "mediano" o "grande" sobre el precio de la acción. "grande" solo si es algo importante
  de verdad (resultados muy distintos a lo esperado, juicios o multas grandes, fusiones, cambios de CEO,
  productos clave, guías de ganancias, regulaciones fuertes). La mayoría de las noticias son "chico".
- "explicacion": 2 o 3 oraciones cortas, en español rioplatense simple, sin tecnicismos (si usás uno, explicalo).
  Decí qué pasó y por qué puede importarle a alguien que tiene la acción. No des recomendaciones de compra o venta.
Si el título no alcanza para saber, usá "neutra" y "chico" y explicá lo que se puede entender.

Respondé SOLO un JSON: una lista de objetos {"id": <número>, "sentimiento": ..., "impacto": ..., "explicacion": ...}.

Noticias:
"""


def _consultar(texto):
    for modelo in MODELOS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": texto}]}],
                      "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}},
                timeout=90)
        except Exception as e:
            print(f"  IA: error de conexión ({e})")
            continue
        if r.status_code == 404:
            continue   # ese modelo no existe más, pruebo el siguiente
        if r.status_code == 429:
            print("  IA: límite del plan gratis alcanzado, sigo en la próxima corrida")
            return None
        if r.status_code != 200:
            print(f"  IA: respuesta {r.status_code}: {r.text[:200]}")
            return None
        try:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)
        except Exception as e:
            print(f"  IA: no pude leer la respuesta ({e})")
            return None
    return None


def _valido(a):
    return (isinstance(a, dict) and a.get("sentimiento") in ("buena", "mala", "neutra")
            and a.get("impacto") in ("chico", "mediano", "grande") and a.get("explicacion"))


def analizar(items, cache):
    """
    items: lista de (ticker, noticia_dict). Agrega a cada noticia los campos de la IA.
    cache: {url: {sentimiento, impacto, explicacion}} con lo ya analizado antes.
    """
    pendientes = []
    for t, n in items:
        previo = cache.get(n.get("url"))
        if previo and _valido(previo):
            n.update({k: previo[k] for k in CAMPOS})
        elif not _valido(n):
            pendientes.append((t, n))
    if not pendientes:
        return
    if not GEMINI_KEY:
        print(f"IA: sin GEMINI_KEY, {len(pendientes)} noticias quedan sin analizar")
        return
    pendientes = pendientes[:MAX_POR_CORRIDA]
    hechas = 0
    for i in range(0, len(pendientes), POR_LOTE):
        lote = pendientes[i:i + POR_LOTE]
        lista = "\n".join(f'{j}. [{t}] {n["titulo"]} ({n.get("fuente", "")})' for j, (t, n) in enumerate(lote))
        res = _consultar(PROMPT + lista)
        if res is None:
            break
        if isinstance(res, dict):
            res = next((v for v in res.values() if isinstance(v, list)), [])
        for a in res if isinstance(res, list) else []:
            try:
                j = int(a.get("id"))
            except Exception:
                continue
            if 0 <= j < len(lote) and _valido(a):
                lote[j][1].update({k: str(a[k]).strip() for k in CAMPOS})
                hechas += 1
        if i + POR_LOTE < len(pendientes):
            time.sleep(PAUSA)
    print(f"IA: {hechas} noticias analizadas")


def cache_de(noticias):
    """Arma el cache {url: análisis} a partir de noticias ya guardadas."""
    return {n["url"]: {k: n[k] for k in CAMPOS} for n in noticias if n.get("url") and _valido(n)}
