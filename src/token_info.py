import aiohttp, asyncio, logging, json, os, base64, time, random
from .config      import BIRDEYE_KEY, PUMPFUN_CACHE_TTL_SECONDS, QUICKNODE_RPC_URL
from .market_data import get_sol_price_usd, get_token_supply
from urllib.parse import quote
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

# Importar funciones optimizadas desde quicknode_client
from .quicknode_client import fetch_pumpfun, _token_cache

log = logging.getLogger(__name__)

# Actualizar URLs para usar múltiples endpoints
PUMP_V1   = "https://pump.fun/api/v1/tokens/{mint}"
PUMP_V2   = "https://pump.fun/api/v2/tokens/{mint}"
PUMP_ALT  = "https://pump.fun/_next/data/latest/token/{mint}.json"
DS_TOKEN  = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
BE_PRICE  = "https://public-api.birdeye.so/public/price?address={mint}"
JUP_PRICE = "https://price.jup.ag/v4/price?ids={mint}"
HEAD_BE   = {"X-API-KEY": BIRDEYE_KEY} if BIRDEYE_KEY else {}
TIMEOUT   = aiohttp.ClientTimeout(total=15, connect=5, sock_connect=5)
NO_CACHE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/plain, */*",  # Formatos aceptados
    "Accept-Encoding": "gzip, deflate, br",         # Compresión aceptada
    "Connection": "keep-alive"                      # Mantener conexión
}

# Configuración de APIs y endpoints
RPC_ENDPOINT    = os.environ.get("RPC_URL", "https://api.mainnet-beta.solana.com")
SOL_PRICE_URL   = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"

# Cache local para mejorar rendimiento
_token_cache = {}

# Ajustes para optimización de respuesta rápida
ULTRA_FAST_TIMEOUT = aiohttp.ClientTimeout(total=0.7, connect=0.3, sock_read=0.5)
FAST_TIMEOUT = aiohttp.ClientTimeout(total=1.0, connect=0.4, sock_read=0.7)
MAX_CONCURRENT_REQUESTS = 3  # Limitar cantidad de solicitudes paralelas

async def jget(url: str, head: dict | None = None) -> dict | None:
    """Realiza una petición GET y devuelve el JSON de respuesta."""
    max_retries = 1  # Reducido para mayor velocidad
    
    # Añadir parámetros para evitar caché
    timestamp_ms = int(time.time() * 1000)
    random_suffix = timestamp_ms % 10000
    cache_buster = f"&_={timestamp_ms}&r={random_suffix}" if "?" in url else f"?_={timestamp_ms}&r={random_suffix}"
    url_with_cache_buster = f"{url}{cache_buster}"
    
    # Combinar headers optimizados para velocidad
    headers = {
        **NO_CACHE_HEADERS,
        **(head or {}),
        "Accept-Encoding": "gzip, deflate",  # Excluir Brotli explícitamente
        "Connection": "keep-alive"           # Mantener conexión
    }
    
    # Timeout ultra-agresivo para respuestas rápidas
    timeout = ULTRA_FAST_TIMEOUT
    
    for retry in range(max_retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                log.debug(f"API Request to {url_with_cache_buster}")
                async with s.get(url_with_cache_buster, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        try:
                            # Intentar decodificar el JSON directamente
                            return await r.json(content_type=None)
                        except Exception as json_error:
                            # Si falla, intentar leer el texto y luego convertirlo a JSON
                            log.debug(f"Error al decodificar JSON directamente: {json_error}")
                            text = await r.text()
                            try:
                                import json
                                return json.loads(text)
                            except Exception as text_error:
                                log.error(f"Error al procesar respuesta como texto: {text_error}")
                                raise
                    else:
                        log.debug(f"API Error: Status {r.status} - URL: {url}")
                        
            # Si llegamos aquí y todavía tenemos reintentos, esperar un poco y reintentar
            if retry < max_retries:
                log.debug(f"Reintentando petición ({retry+1}/{max_retries})")
                await asyncio.sleep(0.1)  # Tiempo de espera mínimo
                
        except Exception as e:
            log.debug(f"Error en jget: {e}")
            if retry < max_retries:
                log.debug(f"Reintentando después de error ({retry+1}/{max_retries})")
                await asyncio.sleep(0.1)  # Tiempo de espera mínimo
    
    return None

async def get_dexscreener_data(mint: str) -> dict:
    """Obtener datos de DexScreener para un token"""
    log.info(f"Intentando obtener datos de DexScreener para {mint}")
    data = await jget(DS_TOKEN.format(mint=mint))
    if data and isinstance(data, dict):
        pairs = data.get("pairs", [])
        if pairs:
            # Ordenar por liquidez para obtener el par principal
            pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
            main_pair = pairs[0]
            return {
                "name": main_pair.get("baseToken", {}).get("name", ""),
                "symbol": main_pair.get("baseToken", {}).get("symbol", ""),
                "price": float(main_pair.get("priceUsd", 0)),
                "liquidity": float(main_pair.get("liquidity", {}).get("usd", 0)),
                "volume": float(main_pair.get("volume", {}).get("h24", 0)),
                "fdv": float(main_pair.get("fdv", 0))
            }
    return {}

async def pump_data(mint: str) -> dict:
    """Obtiene datos de un token desde PumpFun con optimización de velocidad."""
    # Generar timestamp y parámetros anti-caché para evitar respuestas cacheadas
    timestamp = int(time.time() * 1000)
    rand_string = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=12))
    random_num = random.randint(10000, 99999)
    
    # User agent aleatorio para evitar restricciones
    user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36"
    
    # Headers optimizados para forzar datos frescos
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Requested-With": f"XMLHttpRequest-{random_num}",
        "If-Modified-Since": "0"
    }
    
    # Consultas paralelas a múltiples endpoints para mayor velocidad
    endpoints = [
        f"https://api.pump.fun/v2/tokens/{mint}?_={timestamp}&r={rand_string}&nocache={random_num}",
        f"https://pump.fun/api/v2/tokens/{mint}?_={timestamp}&r={rand_string}&nocache={random_num}",
        f"https://pump.fun/_next/data/latest/token/{mint}.json?_={timestamp}&r={rand_string}&nocache={random_num}",
        f"https://pump.fun/api/v1/tokens/{mint}?_={timestamp}&r={rand_string}&nocache={random_num}"
    ]
    
    # Ejecutar todas las consultas en paralelo para máxima velocidad
    tasks = []
    for url in endpoints:
        tasks.append(asyncio.create_task(_fetch_endpoint(url, headers)))
    
    # Esperar a que cualquier tarea complete - timeout agresivo
    try:
        done, pending = await asyncio.wait(
            tasks, 
            timeout=2.0,
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Procesar el primer resultado válido
        for task in done:
            try:
                result = task.result()
                if result:
                    log.info(f"✅ Datos obtenidos rápidamente de Pump.fun")
                    # Cancelar el resto de tareas
                    for p_task in pending:
                        p_task.cancel()
                    return result
            except Exception as e:
                log.debug(f"Error en tarea Pump.fun: {e}")
                
        # Si el primer intento falla, esperar un poco más por otros resultados
        if pending:
            done2, still_pending = await asyncio.wait(
                pending, 
                timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancelar tareas restantes
            for task in still_pending:
                task.cancel()
                
            # Procesar resultados adicionales
            for task in done2:
                try:
                    result = task.result()
                    if result:
                        log.info(f"✅ Datos obtenidos de Pump.fun (segundo intento)")
                        return result
                except Exception as e:
                    log.debug(f"Error en tarea adicional Pump.fun: {e}")
    except Exception as e:
        log.error(f"Error general al consultar Pump.fun: {e}")
    
    # Fallback: intentar scraping directo
    try:
        url = f"https://pump.fun/token/{mint}"
        log.info(f"Intentando scraping directo: {url}")
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
            async with session.get(url, headers={**headers, "Accept": "text/html"}, ssl=False) as response:
                if response.status == 200:
                    html = await response.text()
                    
                    # Buscar datos con expresiones regulares
                    scraped_data = {}
                    
                    # Obtener market cap
                    mc_match = re.search(r'Market\s*Cap:?\s*\$?([0-9,\.]+)([KkMmBb]?)', html, re.IGNORECASE)
                    if mc_match:
                        mc_str = mc_match.group(1).replace(',', '')
                        mc_unit = mc_match.group(2).upper() if len(mc_match.groups()) > 1 and mc_match.group(2) else ""
                        
                        mc = float(mc_str)
                        if mc_unit == 'K':
                            mc *= 1000
                        elif mc_unit == 'M':
                            mc *= 1000000
                        elif mc_unit == 'B':
                            mc *= 1000000000
                        
                        scraped_data["marketCap"] = mc
                    
                    # Obtener precio
                    price_match = re.search(r'Price:?\s*\$?([0-9\.]+)', html, re.IGNORECASE)
                    if price_match:
                        scraped_data["price"] = float(price_match.group(1))
                        
                    # Obtener nombre y símbolo
                    name_match = re.search(r'<title>(.*?)\s*\|', html)
                    if name_match:
                        scraped_data["name"] = name_match.group(1).strip()
                        
                    symbol_match = re.search(r'\(([\w]+)\)', html)
                    if symbol_match:
                        scraped_data["symbol"] = symbol_match.group(1)
                    
                    if "price" in scraped_data or "marketCap" in scraped_data:
                        log.info(f"Datos obtenidos por scraping: precio=${scraped_data.get('price', 'N/A')}, MC=${scraped_data.get('marketCap', 'N/A')}")
                        return scraped_data
    except Exception as e:
        log.error(f"Error al hacer scraping de Pump.fun: {e}")
    
    # Si todo falla, devolver objeto vacío
    return {}

async def _fetch_endpoint(url, headers):
    """Función auxiliar para consultar un endpoint de Pump.fun"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(url, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Extraer datos según el formato
                    if "token" in data:
                        if isinstance(data["token"], dict):
                            return data["token"]
                        elif "pageProps" in data and "token" in data["pageProps"]:
                            return data["pageProps"]["token"]
                    elif "pageProps" in data and "token" in data["pageProps"]:
                        return data["pageProps"]["token"]
    except Exception as e:
        log.debug(f"Error consultando {url}: {e}")
    return None

async def price_be(mint: str) -> float:
    if not BIRDEYE_KEY:
        log.warning("No hay BIRDEYE_KEY configurada")
        return 0.0
    js = await jget(BE_PRICE.format(mint=mint), HEAD_BE)
    try:
        if js and isinstance(js, dict):
            value = js.get("data", {}).get("value", 0)
            log.info(f"Precio de Birdeye: {value}")
            return float(value)
    except Exception as e:
        log.error(f"Error procesando respuesta de Birdeye: {e}")
    return 0.0

async def price_jup(mint: str) -> float:
    js = await jget(JUP_PRICE.format(mint=mint))
    try:
        if js and isinstance(js, dict):
            price = js.get(mint, {}).get("price", 0)
            log.info(f"Precio de Jupiter: {price}")
            return float(price)
    except Exception as e:
        log.error(f"Error procesando respuesta de Jupiter: {e}")
    return 0.0

async def get_sol_price_usd() -> float:
    """Obtiene el precio de SOL en USD de múltiples fuentes."""
    # Lista de APIs alternativas para obtener el precio de SOL
    apis = [
        # CoinGecko (puede tener rate limits)
        "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
        # Binance API - precio del par SOL/USDT
        "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
        # Fallback price source
        "https://api2.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
    ]

    # Intentar cada API en orden
    for api_url in apis:
        try:
            js = await jget(api_url)
            if js and isinstance(js, dict):
                # Formato CoinGecko
                if "solana" in js:
                    return float(js.get("solana", {}).get("usd", 0))
                # Formato Binance
                elif "symbol" in js and js.get("symbol") == "SOLUSDT":
                    return float(js.get("price", 0))
        except Exception as e:
            log.error(f"Error obteniendo precio de SOL desde {api_url}: {e}")
    
    # Si todas las APIs fallan, usar un valor razonable
    return 20.0  # Valor predeterminado si todas las fuentes fallan

async def get_token_supply(mint: str) -> tuple[int,int]:
    """Obtiene el supply del token y decimales"""
    try:
        client = AsyncClient(RPC_ENDPOINT)
        info = await client.get_token_supply(Pubkey.from_string(mint))
        await client.close()
        
        if info.value:
            raw_supply = int(info.value.amount)
            decimals = info.value.decimals
            return raw_supply, decimals
    except Exception as e:
        log.error(f"Error al obtener supply de {mint}: {e}")
    
    # Valor por defecto si falla
    return 0, 0

async def get_pumpfun_realtime_mc(mint: str) -> dict:
    """
    Obtiene el market cap en tiempo real desde múltiples fuentes para asegurar datos frescos

    Returns:
        Dict con marketCapUsd, priceUsd, y otros datos disponibles
    """
    start_time = time.time()
    log.info(f"Obteniendo market cap en tiempo real para {mint}")
    
    # Lista de resultados de todas las fuentes que consultamos
    all_results = []
    
    # Ejecutar todas las consultas en paralelo para máxima velocidad
    tasks = []
    
    # 1. Tarea DexScreener - generalmente la más rápida
    tasks.append(asyncio.create_task(_get_dexscreener_data(mint)))
    
    # 2. Tarea PumpFun - datos más precisos pero más lenta
    tasks.append(asyncio.create_task(_get_pumpfun_data(mint)))
    
    # 3. Tarea Scraping - último recurso
    tasks.append(asyncio.create_task(_get_scraping_data(mint)))
    
    # Esperar a que cualquier tarea complete con un timeout agresivo
    try:
        # Primera ronda: esperar por la respuesta más rápida (generalmente DexScreener)
        done, pending = await asyncio.wait(
            tasks, 
            timeout=1.2,  # Timeout ultra-agresivo
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Procesar resultados inmediatos
        for task in done:
            try:
                result = task.result()
                if result and result.get("marketCapUsd", 0) > 0:
                    all_results.append(result)
                    log.info(f"✅ Datos rápidos obtenidos de {result.get('source', 'desconocida')} en {time.time() - start_time:.2f}s")
            except Exception as e:
                log.warning(f"Error en tarea de market cap: {e}")
        
        # Segunda ronda: dar un poco más de tiempo a las otras fuentes
        if pending and (not all_results or all_results[0].get("marketCapUsd", 0) == 0):
            log.info("⏱️ Esperando datos adicionales...")
            extra_done, still_pending = await asyncio.wait(
                pending, 
                timeout=0.8,  # Un poco más de tiempo para fuentes alternativas
                return_when=asyncio.ALL_COMPLETED
            )
            
            # Cancelar cualquier tarea restante
            for task in still_pending:
                task.cancel()
            
            # Procesar resultados adicionales
            for task in extra_done:
                try:
                    result = task.result()
                    if result and result.get("marketCapUsd", 0) > 0:
                        all_results.append(result)
                        log.info(f"✅ Datos adicionales obtenidos de {result.get('source', 'desconocida')} en {time.time() - start_time:.2f}s")
                except Exception as e:
                    log.warning(f"Error en tarea adicional: {e}")
        else:
            # Cancelar todas las tareas pendientes si ya tenemos un buen resultado
            for task in pending:
                task.cancel()
                
    except Exception as e:
        log.error(f"Error general en consultas paralelas: {e}")
    
    # Procesar resultados
    if all_results:
        # Filtrar resultados con MC > 0 
        valid_results = [r for r in all_results if r.get("marketCapUsd", 0) > 0]
        
        if valid_results:
            # Si tenemos resultados válidos, tomar el de MC más alto para mayor seguridad
            best_result = max(valid_results, key=lambda x: x.get("marketCapUsd", 0))
            
            # Agregar información de diagnóstico
            best_result["fetch_time_ms"] = int((time.time() - start_time) * 1000)
            best_result["sources_count"] = len(valid_results)
            best_result["all_sources"] = [r.get("source", "desconocida") for r in valid_results]
            
            log.info(f"Market cap final: ${best_result['marketCapUsd']:,.2f} (de {best_result['source']}) en {best_result['fetch_time_ms']}ms")
            return best_result
    
    # Si todo falla, devolver objeto vacío
    log.warning(f"No se pudo obtener market cap para {mint} después de {time.time() - start_time:.2f}s")
    return {
        "marketCapUsd": 0,
        "priceUsd": 0,
        "symbol": "",
        "name": "",
        "source": "Ninguna fuente disponible",
        "fetch_time_ms": int((time.time() - start_time) * 1000)
    }

# Funciones auxiliares para el método optimizado
async def _get_dexscreener_data(mint: str) -> dict:
    """Obtiene datos desde DexScreener con optimización de velocidad"""
    try:
        # Crear parámetros anti-caché
        timestamp = int(time.time() * 1000)
        rand_string = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}?{timestamp}&r={rand_string}"
        
        # User agent aleatorio
        user_agent = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36"
        
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.0)) as session:
            async with session.get(url, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "pairs" in data and data["pairs"]:
                        # Ordenar por liquidez
                        pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                        pair = pairs[0]
                        
                        # Obtener datos
                        price = float(pair.get("priceUsd", 0))
                        mc = float(pair.get("fdv", 0))  # Fully Diluted Valuation
                        
                        # Cálculo con supply si es necesario
                        if price > 0 and mc <= 0:
                            try:
                                # Intentar calcular con supply en caché
                                from .quicknode_client import _token_cache
                                cache_key = f"supply_{mint}"
                                if cache_key in _token_cache:
                                    supply_data = _token_cache[cache_key]
                                    amount = supply_data.get("amount", 0)
                                    decimals = supply_data.get("decimals", 0)
                                    if amount > 0 and decimals > 0:
                                        real_supply = amount / (10 ** decimals)
                                        mc = real_supply * price
                                else:
                                    # Intento rápido sin caché
                                    supply_data = await get_token_supply(mint)
                                    if supply_data:
                                        amount, decimals = supply_data
                                        if amount > 0 and decimals > 0:
                                            real_supply = amount / (10 ** decimals)
                                            mc = real_supply * price
                                            # Guardar en caché para futuras consultas
                                            _token_cache[cache_key] = {
                                                "amount": amount,
                                                "decimals": decimals,
                                                "timestamp": time.time()
                                            }
                            except Exception as e:
                                log.debug(f"Error calculando MC con supply: {e}")
                        
                        return {
                            "marketCapUsd": mc,
                            "priceUsd": price,
                            "symbol": pair.get("baseToken", {}).get("symbol", ""),
                            "name": pair.get("baseToken", {}).get("name", ""),
                            "source": "DexScreener",
                            "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                            "volume24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                            "timestamp": int(time.time())
                        }
    except Exception as e:
        log.debug(f"Error obteniendo datos de DexScreener: {e}")
    
    return {"marketCapUsd": 0, "source": "DexScreener-error"}

async def _get_pumpfun_data(mint: str) -> dict:
    """Obtiene datos desde Pump.fun con optimización de velocidad"""
    try:
        # Consultar Pump.fun directamente con método optimizado
        token_data = await pump_data(mint)
        
        if token_data:
            price = float(token_data.get("price", 0))
            mc = float(token_data.get("marketCap", 0))
            
            # Calcular MC si es necesario
            if price > 0 and mc <= 0 and "supply" in token_data and "decimals" in token_data:
                supply = int(token_data.get("supply", 0))
                decimals = int(token_data.get("decimals", 9))
                if supply > 0:
                    mc = price * (supply / (10 ** decimals))
            
            return {
                "marketCapUsd": mc,
                "priceUsd": price,
                "symbol": token_data.get("symbol", ""),
                "name": token_data.get("name", ""),
                "source": "Pump.fun",
                "liquidity": float(token_data.get("liquidity", 0)),
                "volume24h": float(token_data.get("volume24h", 0)),
                "timestamp": int(time.time())
            }
    except Exception as e:
        log.debug(f"Error obteniendo datos de Pump.fun: {e}")
    
    return {"marketCapUsd": 0, "source": "PumpFun-error"}

async def _get_scraping_data(mint: str) -> dict:
    """Obtiene datos mediante scraping con optimización de velocidad"""
    try:
        url = f"https://pump.fun/token/{mint}"
        
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
            async with session.get(url, headers=headers, ssl=False) as response:
                if response.status == 200:
                    html = await response.text()
                    import re
                    
                    # Scraping directo con expresiones regulares optimizadas
                    mc_match = re.search(r'Market\s*Cap:?\s*\$?([0-9,\.]+)([KkMmBb]?)', html, re.IGNORECASE)
                    price_match = re.search(r'Price:?\s*\$?([0-9\.]+)', html, re.IGNORECASE)
                    name_match = re.search(r'<title>(.*?)\s*\|', html)
                    symbol_match = re.search(r'\(([\w]+)\)', html)
                    
                    mc = 0
                    price = 0
                    name = ""
                    symbol = ""
                    
                    if mc_match:
                        mc_str = mc_match.group(1).replace(',', '')
                        mc_unit = mc_match.group(2).upper() if len(mc_match.groups()) > 1 and mc_match.group(2) else ""
                        
                        mc = float(mc_str)
                        if mc_unit == 'K':
                            mc *= 1000
                        elif mc_unit == 'M':
                            mc *= 1000000
                        elif mc_unit == 'B':
                            mc *= 1000000000
                    
                    if price_match:
                        price = float(price_match.group(1))
                    
                    if name_match:
                        name = name_match.group(1).strip()
                    
                    if symbol_match:
                        symbol = symbol_match.group(1)
                    
                    if mc > 0 or price > 0:
                        return {
                            "marketCapUsd": mc,
                            "priceUsd": price,
                            "symbol": symbol,
                            "name": name,
                            "source": "Scraping",
                            "timestamp": int(time.time())
                        }
    except Exception as e:
        log.debug(f"Error en scraping: {e}")
    
    return {"marketCapUsd": 0, "source": "Scraping-error"}

async def get_token_stats(mint: str, *, pool: str | None = None, force_fresh: bool = False) -> dict:
    """
    Obtiene estadísticas completas de un token desde múltiples fuentes.
    Optimizado para respuestas rápidas y datos en tiempo real.
    """
    start_time = time.time()  # Medir tiempo de respuesta
    
    # Configurar estado de carga para UI (visible en la interfaz)
    loading_key = f"loading_status_{mint}"
    _token_cache[loading_key] = {
        "loading": True,
        "started_at": start_time,
        "status": "iniciando"
    }
    
    # Verificar caché primero y devolver datos si son recientes
    cache_key = f"token_stats_{mint}"
    if not force_fresh and cache_key in _token_cache:
        cache_data = _token_cache[cache_key]
        cache_age = time.time() - cache_data.get('timestamp', 0)
        
        # Si los datos son muy recientes (menos de 3 segundos), usarlos inmediatamente
        if cache_age < 3:
            log.info(f"Usando datos en caché ultra-recientes ({cache_age:.1f}s) para {mint[:8]}")
            # Marcar como no cargando y devolver inmediatamente
            _token_cache[loading_key] = {"loading": False}
            return cache_data
    
    # Iniciar obtención de datos en tiempo real
    token_data = {}
    
    try:
        # Obtener datos multi-fuente en paralelo con timeout agresivo
        _token_cache[loading_key]["status"] = "consultando_dexscreener"
        
        # Ejecutar consulta con cancelación temprana para máxima velocidad
        multi_source_task = asyncio.create_task(get_token_data_multi_source(mint))
        
        # Esperar solo hasta 1.2 segundos máximo (optimizado para respuesta UI)
        try:
            token_data = await asyncio.wait_for(multi_source_task, timeout=1.2)
        except asyncio.TimeoutError:
            log.warning(f"Timeout en obtención multi-fuente para {mint[:8]}, usando datos parciales")
            # Verificar si tenemos datos parciales ya disponibles
            if hasattr(multi_source_task, "_result"):
                token_data = multi_source_task._result
            # Cancelar la tarea para liberar recursos
            multi_source_task.cancel()
        
        # Continuar con procesos paralelos para el resto de datos
        _token_cache[loading_key]["status"] = "procesando_datos"
        
        # Datos básicos mínimos para respuesta rápida
        if not token_data:
            token_data = {
                "name": "Loading...",
                "sym": "...",
                "price": 0,
                "price_sol": 0,
                "vol": 0,
                "lp": 0,
                "mc": 0
            }
        
        # Asegurar que tenemos un precio en SOL
        if "price_sol" not in token_data or token_data["price_sol"] == 0:
            sol_price = await get_sol_price_usd()
            if sol_price > 0 and token_data.get("price", 0) > 0:
                token_data["price_sol"] = token_data["price"] / sol_price
        
        # Calcular market cap en tiempo real con máxima prioridad
        _token_cache[loading_key]["status"] = "calculando_mc"
        try:
            mc_result = await asyncio.wait_for(
                get_pumpfun_realtime_mc(mint), 
                timeout=0.8
            )
            if mc_result and isinstance(mc_result, dict):
                if "supply_amount" in mc_result:
                    token_data["supply"] = mc_result["supply_amount"]
                if "real_time_mc" in mc_result:
                    token_data["real_time_mc"] = mc_result["real_time_mc"]
                    token_data["mc_formatted"] = mc_result.get("mc_formatted", "")
                    token_data["mc_source"] = mc_result.get("source", "")
                    token_data["mc_sources_count"] = mc_result.get("sources_count", 0)
                    token_data["data_freshness"] = mc_result.get("data_freshness", "")
        except asyncio.TimeoutError:
            log.warning(f"Timeout al obtener market cap en tiempo real para {mint[:8]}")
        
        # Datos extra en segundo plano: ATH y cambio de 1h
        ath_task = asyncio.create_task(_get_token_ath(mint))
        price_change_1h_task = asyncio.create_task(_get_token_price_change_1h(mint))
        
        # Continuar operaciones rápidas mientras esperamos los datos extra
        
        # Esperar datos de ATH con timeout muy breve
        try:
            _token_cache[loading_key]["status"] = "obteniendo_ath"
            ath_data = await asyncio.wait_for(ath_task, timeout=0.5)
            if ath_data:
                token_data.update(ath_data)
        except asyncio.TimeoutError:
            # No bloqueamos la respuesta por datos de ATH
            log.debug(f"Timeout al obtener ATH para {mint[:8]}")
        
        # Esperar datos de cambio de precio 1h con timeout breve
        try:
            _token_cache[loading_key]["status"] = "obteniendo_1h"
            price_change_1h = await asyncio.wait_for(price_change_1h_task, timeout=0.5)
            if price_change_1h is not None:
                token_data["price_change_1h"] = price_change_1h
        except asyncio.TimeoutError:
            # No bloqueamos la respuesta por datos de cambio de 1h
            log.debug(f"Timeout al obtener cambio de precio 1h para {mint[:8]}")
        
        # Añadir métricas de rendimiento
        token_data["fetch_time_ms"] = int((time.time() - start_time) * 1000)
        token_data["response_time"] = f"{token_data['fetch_time_ms']}ms"
        token_data["timestamp"] = int(time.time())
        
        # Actualizar caché con datos frescos
        _token_cache[cache_key] = token_data
        
        # Finalizar estado de carga
        _token_cache[loading_key] = {
            "loading": False,
            "completed_at": time.time()
        }
        
        return token_data
    
    except Exception as e:
        log.error(f"Error al obtener estadísticas para {mint[:8]}: {str(e)}")
        
        # Marcar como no cargando
        _token_cache[loading_key] = {
            "loading": False,
            "error": True,
            "completed_at": time.time()
        }
        
        # En caso de error, devolver datos mínimos
        return {
            "name": "Error",
            "sym": "ERR",
            "price": 0,
            "price_sol": 0,
            "error": str(e),
            "timestamp": int(time.time())
        }

async def get_token_extra_data(mint: str) -> dict:
    """
    Obtiene datos adicionales del token como holders, ATH y fecha de creación
    
    Args:
        mint: Dirección del token
        
    Returns:
        Dict con datos adicionales del token
    """
    result = {}
    
    try:
        # Consultar primera para obtener información adicional
        # 1. Intentar obtener datos de holders desde Solscan o DefiLlama
        holders_task = asyncio.create_task(_get_token_holders(mint))
        
        # 2. Intentar obtener datos de ATH desde Birdeye o Dexscreener
        ath_task = asyncio.create_task(_get_token_ath(mint))
        
        # 3. Intentar obtener fecha de creación
        creation_task = asyncio.create_task(_get_token_creation_time(mint))
        
        # 4. Intentar obtener cambio de precio en 1h
        price_1h_task = asyncio.create_task(_get_token_price_change_1h(mint))
        
        # Esperar a que todas las tareas completen con un timeout agresivo
        results = await asyncio.gather(
            holders_task, ath_task, creation_task, price_1h_task,
            return_exceptions=True
        )
        
        # Procesar resultados
        if not isinstance(results[0], Exception) and results[0]:
            result.update({"holders": results[0]})
            
        if not isinstance(results[1], Exception) and results[1]:
            result.update(results[1])
            
        if not isinstance(results[2], Exception) and results[2]:
            result.update({"creation_time": results[2]})
            
        if not isinstance(results[3], Exception) and results[3]:
            result.update({"price_change_1h": results[3]})
            
    except Exception as e:
        log.error(f"Error obteniendo datos extra del token: {e}")
    
    return result

async def _get_token_holders(mint: str) -> int:
    """Obtiene el número de holders del token"""
    try:
        # Intentar obtener datos de Solscan
        url = f"https://api.solscan.io/token/meta?token={mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "data" in data and "holder" in data["data"]:
                        return int(data["data"]["holder"])
    except Exception as e:
        log.debug(f"Error obteniendo holders desde Solscan: {e}")

async def _get_token_ath(mint: str) -> dict:
    """Obtiene el ATH (All-Time High) del token y el cambio porcentual utilizando fuentes reales"""
    result = {}
    log.info(f"Iniciando búsqueda de ATH para {mint[:8]}...")
    
    # Crear lista de fuentes de datos a consultar en paralelo
    ath_tasks = []
    
    # 1. Consultar Birdeye (mejor fuente para ATH)
    if BIRDEYE_KEY:
        log.debug(f"Añadiendo tarea Birdeye para {mint[:8]}")
        ath_tasks.append(asyncio.create_task(_get_ath_birdeye(mint)))
    else:
        log.warning("No hay BIRDEYE_KEY configurada, omitiendo fuente Birdeye")
    
    # 2. Consultar Dexscreener (segunda mejor fuente)
    log.debug(f"Añadiendo tarea Dexscreener para {mint[:8]}")
    ath_tasks.append(asyncio.create_task(_get_ath_dexscreener(mint)))
    
    # Ejecutar todas las consultas en paralelo con timeout más agresivo
    log.info(f"Ejecutando {len(ath_tasks)} tareas en paralelo para ATH de {mint[:8]}")
    
    try:
        # Usar timeout agresivo de 1.5 segundos para las tareas paralelas
        done, pending = await asyncio.wait(
            ath_tasks,
            timeout=1.5,
            return_when=asyncio.ALL_COMPLETED
        )
        
        # Cancelar cualquier tarea pendiente
        for task in pending:
            task.cancel()
        
        # Procesar resultados
        valid_results = []
        for task in done:
            try:
                res = task.result()
                if isinstance(res, dict) and "ath" in res and res["ath"] > 0:
                    log.info(f"Resultado válido de ATH: {res.get('source', 'Unknown')}: {res['ath']}")
                    valid_results.append(res)
                elif isinstance(res, dict):
                    log.warning(f"Resultado incompleto de ATH: {res}")
            except Exception as e:
                log.error(f"Error en tarea ATH: {str(e)}")
    
        if valid_results:
            log.info(f"Encontrados {len(valid_results)} resultados válidos de ATH para {mint[:8]}")
            
            # Si tenemos múltiples resultados, elegimos el más conservador (menor ATH)
            # Esto nos dará un cambio porcentual más realista
            min_ath = min(valid_results, key=lambda x: x["ath"])
            max_ath = max(valid_results, key=lambda x: x["ath"])
            
            log.info(f"ATH mínimo: {min_ath['ath']} ({min_ath.get('source', 'Desconocido')})")
            log.info(f"ATH máximo: {max_ath['ath']} ({max_ath.get('source', 'Desconocido')})")
            
            # Verificamos si la diferencia es muy grande (+ de 50%), en ese caso tomamos el promedio
            if max_ath["ath"] / min_ath["ath"] > 1.5:
                # Calculamos el promedio excluyendo valores extremos
                valid_values = sorted([r["ath"] for r in valid_results])
                if len(valid_values) > 2:
                    valid_values = valid_values[1:-1]  # Excluir extremos
                avg_ath = sum(valid_values) / len(valid_values)
                result["ath"] = avg_ath
                result["ath_source"] = "Promedio"
                log.info(f"Usando ATH promedio: {avg_ath}")
            else:
                # Si no hay gran diferencia, usamos el valor de menor ATH (más conservador)
                result["ath"] = min_ath["ath"]
                result["ath_source"] = min_ath.get("source", "Desconocido")
                log.info(f"Usando ATH más conservador: {result['ath']} ({result['ath_source']})")
                
            # Obtener precio actual para calcular cambio porcentual - con timeout reducido
            try:
                current_price_task = asyncio.create_task(get_current_price_for_comparison(mint))
                current_price = await asyncio.wait_for(current_price_task, timeout=0.8)
                
                log.info(f"Precio actual para {mint[:8]}: {current_price}")
                
                if current_price > 0 and result["ath"] > 0:
                    # Cálculo correcto: cuánto ha bajado desde el ATH
                    ath_change = ((current_price / result["ath"]) - 1) * 100
                    result["ath_change_pct"] = ath_change
                    log.info(f"Cambio desde ATH: {ath_change:.2f}%")
                    
                    # Añadir datos adicionales útiles
                    if ath_change < -50:  # Si ha bajado más del 50% desde ATH
                        result["potential_from_ath"] = f"{abs(ath_change):.1f}% potencial desde ATH"
                    
                    # Calcular multiplicador hasta ATH
                    result["multiplier_to_ath"] = result["ath"] / current_price
                    log.info(f"Multiplicador hasta ATH: {result['multiplier_to_ath']:.2f}x")
            except asyncio.TimeoutError:
                log.warning(f"Timeout al obtener precio actual para {mint[:8]}")
        else:
            log.warning(f"No se encontraron resultados válidos de ATH para {mint[:8]}")
    except Exception as e:
        log.error(f"Error general en consulta de ATH: {e}")
    
    return result

async def _get_ath_birdeye(mint: str) -> dict:
    """Obtiene ATH desde Birdeye API"""
    try:
        url = f"https://public-api.birdeye.so/public/tokenomics?address={mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        if BIRDEYE_KEY:
            headers["X-API-KEY"] = BIRDEYE_KEY
        
        # Usar un solo intento con timeout agresivo
        timeout = aiohttp.ClientTimeout(total=1.5, connect=0.7, sock_read=1.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "data" in data and "ath" in data["data"]:
                        ath = float(data["data"]["ath"])
                        log.info(f"ATH obtenido desde Birdeye: {ath}")
                        return {
                            "ath": ath,
                            "source": "Birdeye"
                        }
    except asyncio.TimeoutError:
        log.debug(f"Timeout en solicitud Birdeye ATH")
    except Exception as e:
        log.debug(f"Error obteniendo ATH desde Birdeye: {e}")
    
    return {}

async def _get_ath_dexscreener(mint: str) -> dict:
    """Obtiene ATH desde Dexscreener"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        # Usar un solo intento con timeout agresivo
        timeout = aiohttp.ClientTimeout(total=1.5, connect=0.7, sock_read=1.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "pairs" in data and data["pairs"]:
                        # Ordenar pares por liquidez para obtener el principal
                        pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
                        if not pairs:
                            return {}
                            
                        pair = pairs[0]
                        
                        if "priceUsd" in pair and "priceChange" in pair:
                            current_price = float(pair["priceUsd"])
                            price_change = pair["priceChange"]
                            
                            # Intentar extraer ATH directamente si está disponible
                            if "athUSD" in pair:
                                ath = float(pair["athUSD"])
                                log.info(f"ATH obtenido directamente desde Dexscreener: {ath}")
                                return {
                                    "ath": ath,
                                    "source": "Dexscreener-ATH"
                                }
                            
                            # Si no tenemos ATH directo pero tenemos cambio desde ATH, calcular ATH
                            if "ath" in price_change and current_price > 0:
                                ath_change = float(price_change.get("ath", -80))  # Default a -80% si no está
                                
                                if ath_change < 0:  # Solo tiene sentido si es negativo
                                    # ATH = current_price / (1 + ath_change/100)
                                    ath = current_price / (1 + ath_change/100)
                                    log.info(f"ATH calculado desde Dexscreener: {ath} (basado en cambio {ath_change}%)")
                                    return {
                                        "ath": ath,
                                        "source": "Dexscreener-Calculado"
                                    }
    except asyncio.TimeoutError:
        log.debug(f"Timeout en solicitud Dexscreener ATH")
    except Exception as e:
        log.debug(f"Error obteniendo ATH desde Dexscreener: {e}")
    
    return {}

async def _get_token_creation_time(mint: str) -> int:
    """Obtiene la fecha de creación del token"""
    try:
        # Intentar obtener datos de Solscan
        url = f"https://api.solscan.io/token/meta?token={mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "data" in data and "mintAddress" in data["data"]:
                        if "createdTime" in data["data"]:
                            return int(data["data"]["createdTime"] / 1000)  # Convertir de ms a s
    except Exception as e:
        log.debug(f"Error obteniendo fecha de creación desde Solscan: {e}")
    
    # Fallback - intentar obtener desde Solana Explorer API
    try:
        # Usar la API de Solana Explorer para obtener la transacción inicial
        url = f"https://explorer-api.devnet.solana.com/tokens/{mint}/txs?limit=1&offset=0"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "data" in data and len(data["data"]) > 0:
                        tx = data["data"][0]
                        if "blockTime" in tx:
                            return int(tx["blockTime"])
    except Exception as e:
        log.debug(f"Error obteniendo fecha de creación desde Solana Explorer: {e}")
    
    return 0

async def _get_token_price_change_1h(mint: str) -> float:
    """Obtiene el cambio de precio en 1H del token consultando fuentes reales"""
    
    # Lista de fuentes a consultar en paralelo
    change_1h_tasks = []
    
    # 1. Dexscreener (generalmente la mejor fuente para cambios de precio)
    change_1h_tasks.append(asyncio.create_task(_get_1h_change_dexscreener(mint)))
    
    # 2. Birdeye (buena fuente alternativa)
    if BIRDEYE_KEY:
        change_1h_tasks.append(asyncio.create_task(_get_1h_change_birdeye(mint)))
    
    # 3. Precios históricos de Birdeye (datos reales)
    if BIRDEYE_KEY:
        change_1h_tasks.append(asyncio.create_task(_get_1h_change_historical_birdeye(mint)))
    
    # Ejecutar todas las consultas en paralelo con timeout agresivo
    try:
        # Usar timeout agresivo de 1.5 segundos para la obtención de datos
        done, pending = await asyncio.wait(
            change_1h_tasks,
            timeout=1.2,
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Si tenemos al menos un resultado, podemos usar ese para respuesta rápida
        valid_results = []
        for task in done:
            try:
                res = task.result()
                if isinstance(res, dict) and "change_1h" in res and isinstance(res["change_1h"], (int, float)):
                    valid_results.append(res)
                    log.info(f"Cambio 1H obtenido desde {res.get('source', 'desconocida')}: {res['change_1h']}%")
            except Exception as e:
                log.debug(f"Error en tarea de cambio 1H: {e}")
        
        # Si ya tenemos al menos un resultado, podemos usarlo directamente
        if valid_results:
            # Para tokens críticos, usar el primer resultado válido para máxima velocidad
            result = valid_results[0]
            log.info(f"Usando cambio 1H de {result.get('source', 'desconocida')}: {result['change_1h']}%")
            return result["change_1h"]
        
        # Si no tenemos resultados rápidos, esperar un poco más por los pendientes
        if pending:
            extra_done, still_pending = await asyncio.wait(
                pending,
                timeout=0.8,
                return_when=asyncio.ALL_COMPLETED
            )
            
            # Cancelar cualquier tarea pendiente
            for task in still_pending:
                task.cancel()
            
            # Procesar resultados adicionales
            for task in extra_done:
                try:
                    res = task.result()
                    if isinstance(res, dict) and "change_1h" in res and isinstance(res["change_1h"], (int, float)):
                        valid_results.append(res)
                        log.info(f"Cambio 1H obtenido desde {res.get('source', 'desconocida')}: {res['change_1h']}%")
                except Exception as e:
                    log.debug(f"Error en tarea adicional de cambio 1H: {e}")
        
        if valid_results:
            # Para cambios de precio, promediamos las fuentes pero damos más peso a Dexscreener
            weighted_sum = 0
            total_weight = 0
            
            for result in valid_results:
                source = result.get("source", "")
                weight = 2.0 if "Dexscreener" in source else 1.0
                weighted_sum += result["change_1h"] * weight
                total_weight += weight
            
            if total_weight > 0:
                avg_change = weighted_sum / total_weight
                log.info(f"Cambio 1H calculado para {mint[:8]}: {avg_change:.2f}% (promedio ponderado de {len(valid_results)} fuentes)")
                return avg_change
    except Exception as e:
        log.error(f"Error general en obtención de cambio 1H: {e}")
    
    # Si no tenemos datos válidos, intentar calcular cambio desde el caché
    try:
        price_cache_key = f"price_cache_{mint}"
        if price_cache_key in _token_cache:
            cache_data = _token_cache[price_cache_key]
            if "price_1h_ago" in cache_data and "current_price" in cache_data:
                price_1h_ago = cache_data["price_1h_ago"]
                current_price = cache_data["current_price"]
                if price_1h_ago > 0 and current_price > 0:
                    change_1h = ((current_price / price_1h_ago) - 1) * 100
                    log.info(f"Cambio 1H calculado desde caché para {mint[:8]}: {change_1h:.2f}%")
                    return change_1h
    except Exception as e:
        log.debug(f"Error calculando cambio 1H desde caché: {e}")
    
    return 0

async def _get_1h_change_dexscreener(mint: str) -> dict:
    """Obtiene cambio 1H desde Dexscreener"""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        # Usar un solo intento con timeout agresivo
        timeout = aiohttp.ClientTimeout(total=1.2, connect=0.5, sock_read=0.8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "pairs" in data and data["pairs"]:
                        # Ordenar pares por liquidez para obtener el principal
                        pairs = sorted(data["pairs"], key=lambda x: float(x.get("liquidity", {}).get("usd", 0)), reverse=True)
                        if not pairs:
                            return {}
                            
                        pair = pairs[0]
                        
                        if "priceChange" in pair and "h1" in pair["priceChange"]:
                            change_1h = float(pair["priceChange"]["h1"])
                            log.info(f"Cambio 1H obtenido desde Dexscreener: {change_1h}%")
                            return {
                                "change_1h": change_1h,
                                "source": "Dexscreener"
                            }
    except asyncio.TimeoutError:
        log.debug(f"Timeout en solicitud Dexscreener 1H")
    except Exception as e:
        log.debug(f"Error obteniendo cambio 1H desde Dexscreener: {e}")
    
    return {}

async def _get_1h_change_birdeye(mint: str) -> dict:
    """Obtiene cambio 1H desde Birdeye"""
    try:
        url = f"https://public-api.birdeye.so/public/defi/token_overview?address={mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        if BIRDEYE_KEY:
            headers["X-API-KEY"] = BIRDEYE_KEY
        
        # Usar un solo intento con timeout agresivo
        timeout = aiohttp.ClientTimeout(total=1.2, connect=0.5, sock_read=0.8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "data" in data and "priceChange" in data["data"] and "h1" in data["data"]["priceChange"]:
                        change_1h = float(data["data"]["priceChange"]["h1"])
                        log.info(f"Cambio 1H obtenido desde Birdeye: {change_1h}%")
                        return {
                            "change_1h": change_1h,
                            "source": "Birdeye"
                        }
    except asyncio.TimeoutError:
        log.debug(f"Timeout en solicitud Birdeye 1H")
    except Exception as e:
        log.debug(f"Error obteniendo cambio 1H desde Birdeye: {e}")
    
    return {}

async def _get_1h_change_historical_birdeye(mint: str) -> dict:
    """Calcula cambio 1H usando los precios históricos reales de Birdeye"""
    try:
        # Obtener precio actual con timeout reducido
        current_price_task = asyncio.create_task(get_current_price_for_comparison(mint))
        current_price = await asyncio.wait_for(current_price_task, timeout=0.8)
        
        if current_price <= 0:
            return {}
        
        # Calcular timestamp de hace 1 hora exacta
        one_hour_ago = int(time.time()) - 3600
        
        if BIRDEYE_KEY:
            url = f"https://public-api.birdeye.so/public/defi/historical_price_unix?address={mint}&timestamp={one_hour_ago}"
            headers = {
                "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
                "Accept": "application/json",
                "X-API-KEY": BIRDEYE_KEY
            }
            
            # Usar un solo intento con timeout agresivo
            timeout = aiohttp.ClientTimeout(total=1.2, connect=0.5, sock_read=0.8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data and "data" in data and "value" in data["data"]:
                            price_1h_ago = float(data["data"]["value"])
                            if price_1h_ago > 0:
                                # Guardar en caché para futuras consultas
                                price_cache_key = f"price_cache_{mint}"
                                _token_cache[price_cache_key] = {
                                    "price_1h_ago": price_1h_ago,
                                    "current_price": current_price,
                                    "timestamp": time.time()
                                }
                                
                                # Calcular cambio
                                change_1h = ((current_price / price_1h_ago) - 1) * 100
                                log.info(f"Cambio 1H calculado desde precios históricos Birdeye: {change_1h}% (Precio actual: {current_price}, Precio 1h atrás: {price_1h_ago})")
                                return {
                                    "change_1h": change_1h,
                                    "source": "Birdeye-Historical"
                                }
    except asyncio.TimeoutError:
        log.debug(f"Timeout en solicitud Birdeye Historical")
    except Exception as e:
        log.debug(f"Error calculando cambio 1H desde precios históricos de Birdeye: {e}")
    
    return {}

async def get_current_price_for_comparison(mint: str) -> float:
    """Obtiene el precio actual del token para comparaciones"""
    try:
        # Intentar obtener datos de Dexscreener (generalmente el más exacto)
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        headers = {
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.{random.randint(1000, 9999)} Safari/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        # Usar timeout mucho más agresivo
        timeout = aiohttp.ClientTimeout(total=0.8, connect=0.4, sock_read=0.6)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and "pairs" in data and data["pairs"]:
                        pair = data["pairs"][0]
                        if "priceUsd" in pair:
                            return float(pair["priceUsd"])
    except Exception as e:
        log.debug(f"Error obteniendo precio actual desde Dexscreener: {e}")
    
    # Fallback a Jupiter con timeout reducido
    try:
        jup_task = asyncio.create_task(price_jup(mint))
        price = await asyncio.wait_for(jup_task, timeout=0.8)
        if price > 0:
            return price
    except Exception as e:
        log.debug(f"Error obteniendo precio actual desde Jupiter: {e}")
    
    return 0

async def get_token_data_multi_source(mint: str) -> dict:
    """
    Versión optimizada para obtener datos desde múltiples fuentes en paralelo,
    priorizando respuesta rápida para UI.
    """
    start_time = time.time()
    results_data = []
    log.info(f"Obteniendo datos multi-fuente para {mint[:8]}...")
    
    # Limpia la caché de DexScreener para este token específico
    dex_cache_key = f"dex_{mint}"
    if dex_cache_key in _token_cache:
        log.info(f"🔄 Eliminando caché {dex_cache_key} - forzando datos 100% frescos")
        _token_cache.pop(dex_cache_key, None)
    
    # Priorizar consultas más rápidas primero
    tasks = []
    
    # 1. Consulta a DexScreener (suele ser la más rápida)
    tasks.append(asyncio.create_task(_get_dexscreener_data(mint)))
    
    # 2. Consulta a QuickNode/Jupiter (segundo más rápido)
    if 'fetch_pumpfun' in globals() or 'fetch_pumpfun' in locals():
        tasks.append(asyncio.create_task(fetch_pumpfun(mint, force_fresh=True)))
    
    # 3. Pump.fun solo si es necesario
    tasks.append(asyncio.create_task(_get_pumpfun_data(mint)))
    
    # Esperar a que la primera fuente responda
    first_response_timeout = 0.4  # Timeout ultra-agresivo para primera respuesta
    try:
        # Esperar a que cualquier tarea complete primero
        done, pending = await asyncio.wait(
            tasks, 
            timeout=first_response_timeout, 
            return_when=asyncio.FIRST_COMPLETED
        )
        
        # Procesar el primer resultado
        for task in done:
            try:
                result = task.result()
                if result and isinstance(result, dict):
                    # Verificar que tengamos datos útiles
                    has_useful_data = (
                        result.get("price", 0) > 0 or 
                        result.get("mc", 0) > 0 or
                        result.get("marketCapUsd", 0) > 0 or
                        result.get("fdv", 0) > 0
                    )
                    
                    if has_useful_data:
                        log.info(f"✅ Datos rápidos obtenidos de {result.get('source', 'desconocida')} para {mint[:8]} en {time.time() - start_time:.3f}s")
                        results_data.append(result)
            except Exception as e:
                log.warning(f"Error al obtener primer resultado: {str(e)}")
        
        # Si tenemos datos útiles, esperar un poco más por otras fuentes
        if results_data:
            # La espera adicional depende de cuánto tiempo hemos tardado ya
            elapsed = time.time() - start_time
            additional_wait = max(0.3, 0.8 - elapsed)  # Máximo 0.8s total
            
            # Esperar por más resultados brevemente
            more_done, still_pending = await asyncio.wait(
                pending, 
                timeout=additional_wait,
                return_when=asyncio.ALL_COMPLETED
            )
            
            # Cancelar tareas pendientes para no bloquear
            for task in still_pending:
                task.cancel()
                
            # Añadir resultados adicionales
            for task in more_done:
                try:
                    result = task.result()
                    if result and isinstance(result, dict):
                        # Verificar que tengamos datos útiles
                        has_useful_data = (
                            result.get("price", 0) > 0 or 
                            result.get("mc", 0) > 0 or
                            result.get("marketCapUsd", 0) > 0 or
                            result.get("fdv", 0) > 0
                        )
                        
                        if has_useful_data:
                            source = result.get('source', 'desconocida')
                            log.info(f"✅ Datos adicionales obtenidos de {source} para {mint[:8]} en {time.time() - start_time:.3f}s")
                            results_data.append(result)
                except Exception as e:
                    log.warning(f"Error al obtener resultado adicional: {str(e)}")
        else:
            # Si no obtuvimos datos útiles, esperar un poco más
            log.info(f"⏱️ Esperando datos adicionales...")
            
            elapsed = time.time() - start_time
            additional_wait = max(0.5, 1.0 - elapsed)  # Máximo 1.0s total
            
            more_done, still_pending = await asyncio.wait(
                pending, 
                timeout=additional_wait,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Cancelar tareas pendientes para no bloquear
            for task in still_pending:
                task.cancel()
                
            # Añadir resultados adicionales
            for task in more_done:
                try:
                    result = task.result()
                    if result and isinstance(result, dict):
                        # Verificar que tengamos datos útiles
                        has_useful_data = (
                            result.get("price", 0) > 0 or 
                            result.get("mc", 0) > 0 or
                            result.get("marketCapUsd", 0) > 0 or
                            result.get("fdv", 0) > 0
                        )
                        
                        if has_useful_data:
                            source = result.get('source', 'desconocida')
                            log.info(f"✅ Datos obtenidos de {source} en {time.time() - start_time:.3f}s")
                            results_data.append(result)
                except Exception as e:
                    log.warning(f"Error al obtener resultado: {str(e)}")
    except Exception as e:
        log.error(f"Error en multi-source: {str(e)}")
    
    # Combinar resultados de todas las fuentes
    combined_data = _combine_token_data(results_data, mint, start_time)
    
    # Dar prioridad a la respuesta rápida
    log.info(f"Datos multi-fuente completados para {mint[:8]} en {time.time() - start_time:.2f}s")
    return combined_data

def _combine_token_data(results_data, mint, start_time):
    """
    Función auxiliar para combinar datos de múltiples fuentes.
    Versión optimizada para rendimiento.
    """
    # Verificar si results_data es una lista o un diccionario
    if isinstance(results_data, list):
        # Convertir la lista a un diccionario por fuente
        results_dict = {}
        for item in results_data:
            if isinstance(item, dict) and "source" in item:
                source = item["source"]
                results_dict[source] = item
            elif isinstance(item, dict):
                # Si no tiene source, usar un nombre genérico
                source = f"Source_{len(results_dict)}"
                item["source"] = source
                results_dict[source] = item
        results_data = results_dict

    # Si después de la conversión no tenemos datos, devolver un objeto básico
    if not results_data or not isinstance(results_data, dict) or len(results_data) == 0:
        log.warning(f"No hay datos válidos para combinar para {mint[:8]}")
        return {
            "name": "Unknown Token",
            "sym": "???",
            "price": 0,
            "price_sol": 0,
            "mc": 0,
            "lp": 0,
            "vol": 0,
            "supply": 0,
            "error": "No hay datos válidos disponibles",
            "source": "Sin fuentes disponibles",
            "timestamp": int(time.time()),
            "response_time": f"{time.time() - start_time:.2f}s",
            "latency": time.time() - start_time
        }
    
    # Prioridades para cada campo (de mayor a menor)
    # Reducido para hacer la función más rápida
    source_priorities = {
        "PumpFun": 3,
        "QuickNode": 2,
        "DexScreener": 1
    }
    
    # Ordenar fuentes por prioridad una sola vez
    sources_by_priority = sorted(
        results_data.keys(),
        key=lambda s: source_priorities.get(s, 0),
        reverse=True
    )
    
    # Mapeo de campos entre diferentes fuentes
    # Simplificado para mejorar rendimiento
    field_mapping = {
        "name": ["name", "token_name"],
        "sym": ["sym", "symbol", "ticker"],
        "price": ["price", "priceUsd"],
        "price_sol": ["price_sol", "priceSol"],
        "mc": ["mc", "real_time_mc", "marketCap", "fdv", "marketCapUsd"],
        "lp": ["lp", "liquidity", "liquidityUsd"],
        "vol": ["vol", "volume", "volumeUsd"],
        "supply": ["supply"]
    }
    
    # Primera fuente de prioridad
    primary_source = sources_by_priority[0] if sources_by_priority else None
    
    # Si tenemos un resultado de PumpFun y es completo, usarlo directamente
    if "PumpFun" in results_data and all(
        results_data["PumpFun"].get(field, 0) > 0 
        for field in ["price", "mc"]
    ):
        base_data = results_data["PumpFun"].copy()
        log.info(f"✅ Usando datos completos de PumpFun directamente")
        
        # Asegurar que tenemos todos los campos necesarios
        for field in ["name", "sym", "price", "mc", "lp", "vol", "supply", "price_sol"]:
            if field not in base_data or base_data[field] is None:
                # Intentar obtener de otras fuentes
                for source in sources_by_priority:
                    if source != "PumpFun" and source in results_data:
                        for possible_name in field_mapping.get(field, [field]):
                            if possible_name in results_data[source] and results_data[source][possible_name]:
                                base_data[field] = results_data[source][possible_name]
                                break
                        if field in base_data and base_data[field]:
                            break
                
                # Si todavía no tenemos el campo, usar valor predeterminado
                if field not in base_data or base_data[field] is None:
                    base_data[field] = 0 if field not in ["name", "sym"] else ("Unknown" if field == "name" else "???")
        
        # Agregar campos adicionales
        base_data["response_time"] = f"{time.time() - start_time:.2f}s"
        base_data["source"] = "PumpFun (Optimizado)"
        base_data["sources_used"] = list(results_data.keys())
        base_data["fetchTime"] = time.time()
        base_data["real_time_mc"] = base_data.get("mc", 0)
        base_data["last_trade_price"] = base_data.get("price", 0)
        base_data["chart_price"] = base_data.get("price", 0)
        base_data["renounced"] = base_data.get("renounced", False)
        base_data["latency"] = time.time() - start_time
        
        return base_data
    
    # Si no podemos usar directamente PumpFun, combinar datos normalmente
    # Combinar datos de todas las fuentes
    combined_data = {}
    
    # Iterar por cada campo requerido
    for field, possible_names in field_mapping.items():
        # Verificar cada fuente por prioridad
        for source in sources_by_priority:
            source_data = results_data[source]
            
            # Buscar el campo en la fuente actual
            for possible_name in possible_names:
                if possible_name in source_data and source_data[possible_name]:
                    combined_data[field] = source_data[possible_name]
                    break
            
            # Si encontramos el campo, pasar a la siguiente campo
            if field in combined_data:
                break
    
    # Asegurarse de que tenemos todos los campos necesarios
    required_fields = {
        "name": "Unknown Token",
        "sym": "???",
        "price": 0,
        "mc": 0,
        "lp": 0,
        "vol": 0,
        "supply": 0,
        "price_sol": 0
    }
    
    for field, default_value in required_fields.items():
        if field not in combined_data or combined_data[field] is None:
            combined_data[field] = default_value
    
    # Si tenemos precio pero no mc, intentar calcular mc
    if combined_data["price"] > 0 and combined_data["mc"] <= 0 and combined_data["supply"] > 0:
        combined_data["mc"] = combined_data["price"] * combined_data["supply"]
    
    # Si no tenemos precio_sol pero tenemos precio, usar precio de SOL actualizado
    if combined_data["price"] > 0 and combined_data["price_sol"] <= 0:
        # Obtener precio SOL actualizado
        try:
            from .quicknode_client import _token_cache
            # Intentar usar valor en caché
            if "sol_price_cache" in _token_cache:
                sol_price = _token_cache["sol_price_cache"].get("price")
                if sol_price and sol_price > 0:
                    combined_data["price_sol"] = combined_data["price"] / sol_price
                    log.info(f"Usando precio SOL en caché (${sol_price}) para calcular price_sol")
                else:
                    # Valor respaldo actualizado
                    combined_data["price_sol"] = combined_data["price"] / 175.85
            else:
                # Valor respaldo actualizado
                combined_data["price_sol"] = combined_data["price"] / 175.85
        except ImportError:
            # Valor respaldo actualizado
            combined_data["price_sol"] = combined_data["price"] / 175.85
    
    # Agregar información adicional
    combined_data["response_time"] = f"{time.time() - start_time:.2f}s"
    combined_data["source"] = f"Multi-Source ({primary_source})" if primary_source else "Multi-Source"
    combined_data["sources_used"] = list(results_data.keys())
    combined_data["fetchTime"] = time.time()
    combined_data["real_time_mc"] = combined_data["mc"]
    combined_data["last_trade_price"] = combined_data["price"]
    combined_data["chart_price"] = combined_data["price"]
    combined_data["renounced"] = False
    combined_data["latency"] = time.time() - start_time
    
    log.info(f"Datos multi-fuente completados para {mint[:8]} en {time.time() - start_time:.2f}s")
    
    return combined_data
