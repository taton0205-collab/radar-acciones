# Radar de Acciones — contexto para Claude

## Quién soy y cómo quiero que me hables
- Soy el dueño y admin del proyecto (Argentina). Tengo poco tiempo.
- Respuestas **simples, cortas, paso a paso**, en español rioplatense. Nada de muros de texto.
- No sé programar en profundidad: explicá qué cambia y por qué en 1-2 líneas.
- Antes de cambios grandes, mostrame el plan corto y esperá mi OK.

## Reglas de seguridad (NO romper nunca)
- **Nunca** pedir, mostrar ni escribir claves/tokens en el código, commits o chat. Todo va en GitHub Secrets.
- El código **solo LEE** de IOL (precios y mis operaciones). **Nunca** compra, vende ni mueve plata.
- La importación de operaciones de IOL es **solo para la cuenta del admin** (yo).
- Cada usuario ve **solo sus datos** (RLS en Supabase). No romper eso.
- El repo es **público**: no subir datos personales.
- Probar antes de subir. Si algo puede romper la app para otros usuarios, avisar.

## Qué es
App web (PWA) multi-usuario para seguir **CEDEARs/acciones en pesos**:
ranking del S&P 500 + argentinas + ETFs con nota fundamental y técnica, cartera, seguimiento,
avisos por Telegram, noticias con IA. Usuarios: yo (admin) y hasta ~10 personas aprobadas por mí.

URL: https://taton0205-collab.github.io/radar-acciones/

## Arquitectura (todo en la raíz del repo, sin carpetas salvo .github)
| Archivo | Qué hace |
|---|---|
| `index.html` | Toda la app (vanilla JS, un solo archivo). Pestañas Ranking / Noticias / Cartera. Login magic link de Supabase (supabase-js 2.45.4 UMD). |
| `bot_acciones.py` | Ranking diario → `data.json` (incluye `historial`). Nota = 50% fundamental + 50% técnica, comparación por sector (PESO_SECTOR=0.5). Bonus "cruce reciente" SMA50>SMA200 (hasta +10 si cruzó en ≤30 días). Listas `ARGENTINA` (ADRs) y `ETFS` (solo técnico). Marca `CEDEAR` con la lista pública de BYMA (`SIMBOLO_CEDEAR`: BAC→BA.C, DIS→DISN); si BYMA falla no oculta nada. La app y los resúmenes muestran solo las que tienen CEDEAR. Noticias top 40 con CEDEAR (Yahoo → fallback Google Noticias RSS). |
| (cripto) | `CRIPTO` en `bot_acciones.py`: 15 cripto con nota solo técnica + `_serie` (cierres 1 año). En la app, botón "₿ Cripto": cada 30 s (máx. 1 pedido/20 s) baja precio de CoinGecko + dólar cripto de CriptoYa (mediana USDT) y recalcula la nota con `tecnicoCripto()` (copia de `analisis_tecnico()`: si cambiás una, cambiá la otra). No van a los resúmenes de Telegram. **Parte 2 pendiente**: cartera y avisos de cripto. |
| `precios.py` | Precios en pesos (IOL, fallback Yahoo), CCL (mediana GGAL/YPF/PAM/BMA), `usd_hist`, `ars_hist`, próximo balance, noticias → `precios.json`. `SIMBOLO_LOCAL` mapea ADR→símbolo BYMA (YPF→YPFD, DIS→DISN, etc.). |
| `alertas.py` | Telegram cada 15 min: vincular/desvincular, stop (fijo o dinámico), objetivo, movimientos fuertes (% por usuario), balances, nota que cae, noticias de impacto grande, resumen diario 19:30, resumen semanal domingo 11:00, **chequeo de salud** (solo al admin). Deduplica con tabla `avisos_enviados`. |
| `importar_iol.py` | Importa mis compras/ventas terminadas de IOL (solo CEDEARs y acciones) a la cartera del admin, con `iol_numero` y `broker='IOL'`. |
| `noticias_ia.py` | Gemini (gratis): sentimiento buena/mala/neutra, impacto chico/mediano/grande, explicación. Usa `responseSchema`. Cachea por URL (no re-analiza). |
| `manifest.json`, `icon.png` | PWA. |
| `actualizar.yml` (raíz) | Copia vieja suelta, no se usa. |

### Workflows (`.github/workflows/`)
- `precios.yml` → `precios.py` (cron `7,37 13-20 * * 1-5` UTC) + commit `precios.json`.
- `actualizar.yml` → `bot_acciones.py` (cron `17 21 * * 1-5`) + commit `data.json`.
- `alertas.yml` → `alertas.py` cada 15 min (no commitea).
- `importar_iol.yml` → `importar_iol.py` 21:00 UTC lun-vie.
- Los que commitean usan `concurrency: guardar-cambios` y `git pull --rebase`.
- **GitHub atrasa/saltea los cron**: por eso hay despertadores externos en **cron-job.org** que llaman a la API `workflow_dispatch` con un token fine-grained (solo Actions read/write en este repo). Hechos: **Precios** (cada 15 min, 10-17:45 ART, lun-vie), **Avisos** (cada 15 min, todos los días), **Importar IOL** (18:00 lun-vie) y **Ranking** (18:15 lun-vie).

### Secretos de GitHub (nombres)
`SUPABASE_SECRET`, `TELEGRAM_TOKEN`, `IOL_USUARIO`, `IOL_CLAVE`, `GEMINI_KEY`.
SUPABASE_URL: https://iyejwbchituodjjeailw.supabase.co

### Supabase
Tablas con RLS "lo mio" (`usuario = auth.uid() and esta_aprobado()`):
- `perfiles` (aprobado, es_admin, telegram_chat_id, telegram_codigo, aviso_movimiento)
- `operaciones` (ticker, tipo compra/venta, fecha, cantidad, precio, comision, iol_numero, broker, nota)
- `objetivos` (vender_en, stop, stop_tipo fijo/dinamico, stop_pct, max_precio)
- `seguimiento` (precio_compra, nota)
- `notas_accion` (texto por ticker)
- `avisos_enviados` (solo el bot)
Funciones: `es_admin`, `esta_aprobado`, `cambiar_nombre`, `tickers_en_uso` (solo service_role),
`desvincular_telegram`, `cambiar_aviso_movimiento`. El primer usuario registrado fue admin.
Cambios de esquema: dame un `.sql` idempotente para pegar en SQL Editor (terminar con un `select '... lista ✅'`).

## Problemas conocidos / cosas a tener en cuenta
- IOL panel `/Cotizaciones/cedears/todos/argentina` da 400 → se usa cotización por ticker (`model.plazo=t1`). Normal.
- Yahoo da 404 para acciones locales (BYMA, HARG…) en `.info`: normal.
- Cambios de ratio de CEDEAR (ej. ETHA) hay que corregirlos a mano en `operaciones`.
- Para acciones locales sin ADR, el gráfico de cartera es aproximado hasta juntar `ars_hist`.
- Google Noticias RSS y la cuenta de IOL son para **uso personal**: si se vende la app, cambiar fuentes.

## Pendiente
1. Semana de prueba con un usuario (no sumar features hasta ver que todo anda).
2. Ideas: backtest, modo práctica (plata ficticia), cripto en pesos, modo simple (vista fácil),
   dividendos, comparar 2 acciones, "¿qué pasa si…?", volumen raro.
3. Proyecto **"Radar Premium"** antes de fin de 2026: planes Gratis/Premium, Mercado Pago,
   landing, resolver fuentes de datos comerciales, monotributo, cuidar CNV (herramienta de seguimiento, no recomendaciones).
