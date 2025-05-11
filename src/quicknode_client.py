import asyncio
import aiohttp
import json
import time
import logging
from typing import Dict, Any, Optional, List, Tuple
import atexit
import random
import sys
import importlib
from asyncio import create_task

from .config import (
    QUICKNODE_RPC_URL, 
    QUICKNODE_WS_URL, 
    RPC_TIMEOUT_SECONDS, 
    TOKEN_CACHE_TTL_SECONDS,
    PRICE_CACHE_TTL_SECONDS,
    PUMPFUN_CACHE_TTL_SECONDS,
    JUPITER_CACHE_TTL_SECONDS,
    VIRAL_TOKEN_CACHE_TTL_SECONDS,
    QUICKNODE_RPC_ENDPOINT
)

# Configurar logging
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    level=logging.INFO)
log = logging.getLogger("quicknode_client")

# Optimización de tiempos para respuestas más rápidas
# Reducir timeouts para agilizar las respuestas
RPC_TIMEOUT_SECONDS = min(RPC_TIMEOUT_SECONDS, 0.7)  # Reducido a 0.7s máximo
WS_TIMEOUT_SECONDS = 1.5  # Reducido
ULTRA_FAST_TIMEOUT = aiohttp.ClientTimeout(total=0.7, connect=0.3, sock_read=0.5)
FAST_TIMEOUT = aiohttp.ClientTimeout(total=1.0, connect=0.4, sock_read=0.7)

# Reducir tiempos de caché para datos más frescos pero manteniendo eficiencia
TOKEN_CACHE_TTL_SECONDS = min(TOKEN_CACHE_TTL_SECONDS, 5)  # Máximo 5 segundos
PRICE_CACHE_TTL_SECONDS = min(PRICE_CACHE_TTL_SECONDS, 3)  # Máximo 3 segundos 
PUMPFUN_CACHE_TTL_SECONDS = min(PUMPFUN_CACHE_TTL_SECONDS, 1)  # Máximo 1 segundo
JUPITER_CACHE_TTL_SECONDS = min(JUPITER_CACHE_TTL_SECONDS, 3)  # Máximo 3 segundos
VIRAL_TOKEN_CACHE_TTL_SECONDS = 0  # Sin caché para tokens virales

# Sistema de caché en memoria
_token_cache: Dict[str, Dict[str, Any]] = {}

# Lista de tokens populares para actualizar en segundo plano
POPULAR_TOKENS = [
    "So11111111111111111111111111111111111111112",  # SOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
]

# Lista de tokens virales/en tendencia que requieren actualizaciones más frecuentes
# Esta lista puede actualizarse dinámicamente basada en tendencias, volumen, etc.
VIRAL_TOKENS = [
    "4pg3DpwQ9LXwgPpNggC9zXiCfQkYpCKKUy4nfxzMcvdS",  # DINGUS
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",  # BONG
    "Hpb4ixbz6Ks1S7FTD6J5izsncatdWpEeKfizqzJHH8HJ",  # PUNK
    "Hpb3DvydpKKQsLXPMzXtduGiKWuEFi7V9bwjdGJcAYSA",  # GOAT
    "GfUXgRdbnChfegaG3GwEjYUaNRY9Ji8nQTA5KGhXMgWr",  # INVENTORS 
    "DNdrnMYwGRSBFABs3jMYwxz5XCBzGsaQ1tUeJsCSn1wD",  # LUMBAGO
    "5KV1SvVjJcpfoEktQnR6kvBsaicQEpDX2Cj2k5BX3HVj",  # NOOT
    "DNhZkUaxHXYvpxZ7LNnHtss8UgXtcYxwH8R6FzGDbQkY",  # JCHAN
]

# Sistema de monitoreo de rendimiento de red y ajuste adaptativo
_network_health = {
    "last_check": time.time(),
    "total_requests": 0,
    "failed_requests": 0,
    "success_rate": 1.0,  # Iniciar asumiendo 100% éxito
    "avg_latency": 0.5,   # Valor inicial conservador
    "measurements": [],   # Lista de mediciones recientes (últimas 50)
    "adaptive_timeout": 1.5,  # Timeout base adaptativo (segundos)
    "min_timeout": 0.8,   # Mínimo timeout permitido
    "max_timeout": 4.0    # Máximo timeout permitido
}

# Registrar función para limpiar al salir
atexit.register(lambda: log.info("Tareas en segundo plano detenidas y caché liberada"))

async def get_sol_balance_qn(wallet_address, rpc_endpoint=None):
    """Obtiene el balance SOL de una wallet usando QuickNode Solana Data API"""
    endpoint = rpc_endpoint or QUICKNODE_RPC_ENDPOINT
    
    if not endpoint or not endpoint.startswith('https://'):
        # Usar API estándar si no es endpoint QuickNode
        return None
        
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "getBalance",
            "params": [wallet_address]
        }
        
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "User-Agent": "Solana/QN-Speed-Client"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'result' in data and 'value' in data['result']:
                        return data['result']['value'] / 1_000_000_000  # Convertir lamports a SOL
    except Exception as e:
        log.debug(f"Error en get_sol_balance_qn: {str(e)}")
    
    return None

async def get_token_balance_qn(wallet_address, token_mint, rpc_endpoint=None):
    """Obtiene el balance de un token específico de una wallet usando QuickNode API"""
    endpoint = rpc_endpoint or QUICKNODE_RPC_ENDPOINT
    
    if not endpoint or not endpoint.startswith('https://'):
        # Usar API estándar si no es endpoint QuickNode
        log.debug("No se proporcionó un endpoint válido para QuickNode")
        return None
        
    try:
        # Primero obtener la cuenta de token asociada
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "qn_getTokenAccounts",
            "params": {
                "wallet": wallet_address,
                "mint": token_mint
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "User-Agent": "Solana/QN-Speed-Client"
        }
        
        log.debug(f"Solicitando balance del token {token_mint} para {wallet_address} vía QuickNode")
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    # Verificar errores en la respuesta
                    if 'error' in data:
                        error_msg = data.get('error', {}).get('message', 'Error desconocido')
                        log.error(f"Error de QuickNode: {error_msg}")
                        return None
                        
                    if 'result' in data and data['result']:
                        # Tomar la primera cuenta de token encontrada
                        token_account = data['result'][0]
                        result = {
                            'amount': int(token_account['amount']),
                            'decimals': token_account['decimals'],
                            'uiAmount': float(token_account['amount']) / (10 ** token_account['decimals'])
                        }
                        log.debug(f"Balance obtenido vía QuickNode: {result['uiAmount']}")
                        return result
                    else:
                        log.debug(f"No se encontraron resultados para el token {token_mint} en QuickNode. Respuesta: {data}")
                        return None
                else:
                    log.error(f"Error en respuesta de QuickNode: HTTP {response.status}")
                    response_text = await response.text()
                    log.error(f"Contenido de respuesta: {response_text[:200]}")
                    return None
    except Exception as e:
        log.debug(f"Error en get_token_balance_qn: {str(e)}")
    
    return None

async def get_token_supply_qn(token_mint, rpc_endpoint=None):
    """Obtiene el supply total de un token usando QuickNode API (más rápido)"""
    endpoint = rpc_endpoint or QUICKNODE_RPC_ENDPOINT
    
    if not endpoint or not endpoint.startswith('https://'):
        # Usar API estándar si no es endpoint QuickNode
        return None
        
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "getTokenSupply",
            "params": [token_mint]
        }
        
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0", 
            "User-Agent": "Solana/QN-Speed-Client"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'result' in data and 'value' in data['result']:
                        value = data['result']['value']
                        return int(value['amount']), value['decimals']
    except Exception as e:
        log.debug(f"Error en get_token_supply_qn: {str(e)}")
    
    return None

async def get_user_tokens_qn(wallet_address, rpc_endpoint=None):
    """Obtiene todos los tokens que posee una wallet usando QuickNode API"""
    endpoint = rpc_endpoint or QUICKNODE_RPC_ENDPOINT
    
    if not endpoint or not endpoint.startswith('https://'):
        # Usar API estándar si no es endpoint QuickNode
        return None
        
    try:
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "qn_getTokenAccountsByOwner",
            "params": {
                "owner": wallet_address
            }
        }
        
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "User-Agent": "Solana/QN-Speed-Client"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=payload, headers=headers, ssl=False) as response:
                if response.status == 200:
                    data = await response.json()
                    if 'result' in data:
                        return data['result']
    except Exception as e:
        log.debug(f"Error en get_user_tokens_qn: {str(e)}")
    
    return None

async def get_sol_price_usd_qn() -> float:
    """
    Obtiene el precio de SOL en USD usando múltiples fuentes en paralelo.
    Implementación ultra-precisa con sistema de consenso y sin redondeo.
    """
    # Lista de fuentes para el precio de SOL en orden de preferencia (ahora con 8 fuentes)
    sources = [
        # Jupiter (primera opción, generalmente más rápida)
        {
            "url": "https://price.jup.ag/v4/price?ids=So11111111111111111111111111111111111111112",
            "parser": lambda data: float(data.get("data", {}).get("So11111111111111111111111111111111111111112", {}).get("price", 0)),
            "priority": 10
        },
        # Alternativa de Jupiter
        {
            "url": "https://quote-api.jup.ag/v4/price?ids=So11111111111111111111111111111111111111112",
            "parser": lambda data: float(data.get("data", {}).get("So11111111111111111111111111111111111111112", {}).get("price", 0)),
            "priority": 9
        },
        # Otra alternativa de Jupiter - más estable
        {
            "url": "https://jupiter-price-api.solana.fm/v4/price?ids=So11111111111111111111111111111111111111112",
            "parser": lambda data: float(data.get("data", {}).get("So11111111111111111111111111111111111111112", {}).get("price", 0)),
            "priority": 9
        },
        # Birdeye
        {
            "url": "https://public-api.birdeye.so/public/price?address=So11111111111111111111111111111111111111112",
            "parser": lambda data: float(data.get("data", {}).get("value", 0)),
            "headers": {"X-API-KEY": "5f88b633a8a142aa9b4b96fe"},  # Clave pública genérica
            "priority": 8
        },
        # CoinGecko
        {
            "url": "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            "parser": lambda data: float(data.get("solana", {}).get("usd", 0)),
            "priority": 7
        },
        # Binance
        {
            "url": "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
            "parser": lambda data: float(data.get("price", 0)),
            "priority": 6
        },
        # Binance alternativo
        {
            "url": "https://api2.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
            "parser": lambda data: float(data.get("price", 0)),
            "priority": 5
        },
        # CryptoCompare
        {
            "url": "https://min-api.cryptocompare.com/data/price?fsym=SOL&tsyms=USD",
            "parser": lambda data: float(data.get("USD", 0)),
            "priority": 4
        }
    ]
    
    # Timeout ultra-agresivo para máxima velocidad - cada fuente tiene solo 1.2 segundos
    timeout = aiohttp.ClientTimeout(total=1.2, connect=0.6, sock_read=0.6)
    
    # Headers comunes con caché desactivada
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache"
    }
    
    # Crear tareas para todas las fuentes en paralelo
    tasks = []
    for i, source in enumerate(sources):
        url = source["url"]
        source_headers = {**headers, **(source.get("headers", {}))}
        
        # Crear tarea con timeout propio y cache buster
        cache_buster = int(time.time() * 1000)
        cache_busted_url = f"{url}{'&' if '?' in url else '?'}_={cache_buster}"
        
        task = asyncio.create_task(
            _fetch_sol_price(cache_busted_url, source_headers, source["parser"], timeout)
        )
        task.source_index = i
        task.priority = source.get("priority", 0)
        tasks.append(task)
    
    # Esperar a que todas las tareas terminen o timeout global
    try:
        # Dar 1.5 segundos máximo para que respondan todas las fuentes posibles
        done, pending = await asyncio.wait(
            tasks,
            timeout=1.5,
            return_when=asyncio.ALL_COMPLETED
        )
        
        # Cancelar tareas pendientes
        for task in pending:
            task.cancel()
        
        # Recolectar todos los resultados válidos
        valid_prices = []
        source_names = []
        
        for task in done:
            try:
                price = task.result()
                if price > 0:
                    source_index = getattr(task, "source_index", -1)
                    if source_index >= 0:
                        # Obtener datos de la fuente
                        source_name = sources[source_index]["url"].split('/')[2]
                        priority = getattr(task, "priority", 0)
                        
                        # Añadir a la lista de precios válidos con sus metadatos
                        valid_prices.append({
                            "price": price,
                            "source": source_name,
                            "priority": priority
                        })
                        source_names.append(source_name)
            except Exception as e:
                log.debug(f"Error procesando resultado: {e}")
        
        # Si tenemos al menos 2 precios válidos, usamos consenso
        if len(valid_prices) >= 2:
            # Ordenar por prioridad (mayor primero)
            valid_prices.sort(key=lambda x: x["priority"], reverse=True)
            
            # Si tenemos más de 3 fuentes, podemos eliminar valores extremos
            if len(valid_prices) >= 4:
                # Extraer solo los precios
                prices_only = [p["price"] for p in valid_prices]
                
                # Eliminar el valor más alto y más bajo (outliers)
                prices_only.sort()
                filtered_prices = prices_only[1:-1]  # Eliminar primero y último
                
                # Calcular el promedio de los valores restantes
                avg_price = sum(filtered_prices) / len(filtered_prices)
                
                # Fuentes usadas para el promedio (sin las extremas)
                sources_used = ", ".join(source_names[1:-1])
                log.info(f"Precio de SOL por consenso: ${avg_price:.4f} (fuentes: {sources_used})")
                
                # Guardar en caché con metadata enriquecida
                _token_cache["sol_price_cache"] = {
                    'price': avg_price,
                    'timestamp': time.time(),
                    'source': f"Consenso ({sources_used})",
                    'sources_count': len(filtered_prices),
                    'all_prices': prices_only
                }
                
                return avg_price
            else:
                # Si tenemos pocas fuentes, usar la de mayor prioridad
                top_price = valid_prices[0]["price"]
                top_source = valid_prices[0]["source"]
                
                log.info(f"Precio de SOL obtenido: ${top_price:.4f} (fuente principal: {top_source})")
                
                # Guardar en caché
                _token_cache["sol_price_cache"] = {
                    'price': top_price,
                    'timestamp': time.time(),
                    'source': top_source
                }
                
                return top_price
        
        # Si solo tenemos un precio válido, usarlo directamente
        elif len(valid_prices) == 1:
            price = valid_prices[0]["price"]
            source = valid_prices[0]["source"]
            
            log.info(f"Precio de SOL obtenido: ${price:.4f} (fuente única: {source})")
            
            # Guardar en caché
            _token_cache["sol_price_cache"] = {
                'price': price,
                'timestamp': time.time(),
                'source': source
            }
            
            return price
            
    except Exception as e:
        log.error(f"Error esperando tareas de precio SOL: {e}")
    
    # Si ninguna fuente primaria responde, intentar usar caché
    cache_key = "sol_price_cache"
    if cache_key in _token_cache:
        cache_data = _token_cache[cache_key]
        # Usar caché si es reciente (menos de 30 minutos)
        if time.time() - cache_data['timestamp'] < 1800:
            log.info(f"Usando precio SOL en caché: ${cache_data['price']}")
            return cache_data['price']
    
    # Valores predeterminados si todas las fuentes fallan
    default_price = 175.85  # Valor actualizado abril 2024
    log.warning(f"Usando precio SOL predeterminado: ${default_price}")
    
    return default_price

async def _fetch_sol_price(url, headers, parser, timeout):
    """Función auxiliar para obtener precio SOL de una fuente específica"""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = parser(data)
                    if price > 0:
                        # Guardar en caché
                        _token_cache["sol_price_cache"] = {
                            'price': price,
                            'timestamp': time.time(),
                            'source': url
                        }
                        return price
    except Exception as e:
        log.debug(f"Error obteniendo precio SOL de {url}: {e}")
    return 0

async def get_dexscreener_token_data_qn(token_or_pair: str, is_pair: bool = False) -> Dict[str, Any]:
    """
    Obtiene información detallada de un token o par desde DexScreener usando QuickNode
    
    Args:
        token_or_pair: La dirección del token o del par
        is_pair: True si la dirección es de un par, False si es de un token
        
    Returns:
        Diccionario con la información del token o None si hay error
    """
    try:
        # Determinar el endpoint correcto basado en si es token o pair
        if is_pair:
            url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{token_or_pair}"
        else:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{token_or_pair}"
            
        log.info(f"Consultando DexScreener via QuickNode: {url}")
        
        # Configurar timeout y headers
        timeout = aiohttp.ClientTimeout(total=RPC_TIMEOUT_SECONDS)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache"
        }
        
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers) as r:
                if r.status == 200:
                    data = await r.json()
                    
                    # Verificar que hay datos
                    if not data.get("pairs") or len(data["pairs"]) == 0:
                        log.warning(f"No se encontraron pares para {token_or_pair}")
                        return None
                    
                    # Tomar el par con mayor liquidez
                    pairs = data["pairs"]
                    pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
                    
                    # Extraer información del token
                    pair = pairs[0]
                    
                    # Obtener variables necesarias
                    base_token = pair.get("baseToken", {})
                    token_address = base_token.get("address")
                    token_name = base_token.get("name", "Unknown")
                    token_symbol = base_token.get("symbol", "???")
                    
                    price_usd = float(pair.get("priceUsd", 0))
                    price_sol = float(pair.get("priceNative", 0))
                    mc = float(pair.get("fdv", 0))
                    lp = float(pair.get("liquidity", {}).get("usd", 0))
                    volume_24h = float(pair.get("volume", {}).get("h24", 0))
                    
                    # Determinar si hay cambio de precio
                    price_change = pair.get("priceChange", {}).get("h24", 0)
                    price_diff_pct = float(price_change) if price_change else 0
                    
                    # Obtener timestamp
                    pair_created_at = pair.get("pairCreatedAt", 0)
                    
                    # Verificar si es renounced (aproximado)
                    renounced = False  # Por defecto asumimos que no
                    
                    # Obtener precio de SOL
                    sol_price = await get_sol_price_usd_qn()
                    
                    # Calcular precio en SOL si no está disponible
                    if price_sol == 0 and price_usd > 0 and sol_price > 0:
                        price_sol = price_usd / sol_price
                    
                    # Datos consolidados para devolver
                    return {
                        "mint": token_address,
                        "name": token_name,
                        "sym": token_symbol,
                        "price": price_usd,
                        "price_sol": price_sol,
                        "last_trade_price": price_usd,
                        "chart_price": price_usd,
                        "mc": mc,
                        "real_time_mc": mc,
                        "lp": lp,
                        "vol": volume_24h,
                        "renounced": renounced,
                        "price_diff_pct": price_diff_pct,
                        "pair_address": pair.get("pairAddress"),
                        "source": "DexScreener via QuickNode",
                        "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                        "pairs": len(pairs)
                    }
                else:
                    log.error(f"Error al consultar DexScreener API: {r.status}")
    except Exception as e:
        log.error(f"Error obteniendo datos de DexScreener: {str(e)}")
    
    return None

async def fetch_pumpfun(mint: str, force_fresh: bool = False) -> Dict[str, Any]:
    """
    Obtiene datos optimizados de un token desde DexScreener y/o Pump.fun con manejo inteligente de caché.
    
    Args:
        mint: La dirección del token en Solana
        force_fresh: Si True, fuerza obtener datos 100% frescos ignorando la caché
        
    Returns:
        Diccionario con datos del token o diccionario vacío si hay error
    """
    # Medir tiempo para analizar rendimiento
    start_time = time.time()
    token_data = None
    
    # Añadir indicador de proceso de carga para UI
    loading_info = {
        "loading": True,
        "started_at": start_time,
        "mint": mint,
        "request_type": "force_fresh" if force_fresh else "normal",
        "status": "iniciando"
    }
    _token_cache[f"loading_status_{mint}"] = loading_info
    
    # Usar caché si está disponible y no se fuerza actualización
    cache_key = f"pumpfun_{mint}"
    if not force_fresh and cache_key in _token_cache:
        cached_data = _token_cache[cache_key]
        cache_age = time.time() - cached_data.get("timestamp", 0)
        
        # Si la caché es lo suficientemente reciente, usarla
        if cache_age < PUMPFUN_CACHE_TTL_SECONDS:
            # Actualizar status de carga
            loading_info["status"] = "usando_cache"
            loading_info["cache_age"] = cache_age
            _token_cache[f"loading_status_{mint}"] = loading_info
            
            log.info(f"✅ Usando caché reciente ({cache_age:.2f}s) para {mint[:8]} (actualizando en segundo plano)")
            
            # Programar actualización en segundo plano sin bloquear
            asyncio.create_task(_background_refresh_token_data(mint))
            
            # Devolver datos de caché inmediatamente
            return cached_data
    else:
        # Si force_fresh=True, eliminar explícitamente cualquier caché existente
        for key_prefix in ["pumpfun_", "token_stats_", "jup_price_", "dex_"]:
            cache_key = f"{key_prefix}{mint}"
            if cache_key in _token_cache:
                del _token_cache[cache_key]
                log.info(f"🔄 Eliminando caché {cache_key} - forzando datos 100% frescos")
    
    # OBTENCIÓN DE DATOS EN TIEMPO REAL
    # Headers optimizados para evitar caché en servidores
    # Valores aleatorios para garantizar que evitemos cachés de CDN o intermediarios
    random_ua_suffix = random.randint(10000, 99999)
    timestamp_now = int(time.time() * 1000)
    
    anti_cache_headers = {
        "User-Agent": f"Telegram/1.0.{random_ua_suffix} ({timestamp_now})",
        "Accept": "application/json, */*",
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "X-Requested-With": f"XMLHttpRequest-{timestamp_now}",
        "X-Cache-Bust": f"{timestamp_now}",
        "X-Timestamp": f"{timestamp_now}",
        "X-Requested-By": f"bot-{timestamp_now}"
    }
    
    # Generar cache busters únicos más agresivos
    timestamp_ms = int(time.time() * 1000)
    random_suffix = random.randint(10000, 99999)
    random_extra = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=5))
    cache_buster_param = f"_t={timestamp_ms}&_r={random_suffix}&_x={random_extra}"
    
    # Actualizar estado de carga
    loading_info["status"] = "obteniendo_datos"
    _token_cache[f"loading_status_{mint}"] = loading_info
    
    # MÉTODO 1: DexScreener API (más rápida y confiable)
    try:
        endpoint = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        
        # Timeout ajustado para mayor agresividad - reducir tiempos de espera
        timeout = aiohttp.ClientTimeout(
            total=1.5 if force_fresh else 1.2,  # Reducir timeout total
            connect=0.6 if force_fresh else 0.5,  # Reducir connect timeout
            sock_read=1.0 if force_fresh else 0.8  # Reducir lectura socket
        )
        
        loading_info["status"] = "consultando_dexscreener"
        _token_cache[f"loading_status_{mint}"] = loading_info
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(endpoint, headers=anti_cache_headers, ssl=False) as response:
                if response.status == 200:
                    result = await response.json()
                    if result and "pairs" in result and len(result["pairs"]) > 0:
                        # Tomar el primer par (generalmente el más relevante)
                        pair = result["pairs"][0]
                        
                        # Obtener datos básicos
                        price_usd = float(pair.get("priceUsd", 0))
                        price_native = float(pair.get("priceNative", 0))
                        
                        # Calcular market cap
                        fdv = pair.get("fdv", 0)
                        if fdv:
                            mc = float(fdv)
                        else:
                            # Intentar calcular basado en supply y precio
                            supply = 0
                            if "totalSupply" in pair:
                                supply = int(float(pair["totalSupply"]))
                            elif "liquidity" in pair and "base" in pair["liquidity"]:
                                supply = int(float(pair["liquidity"]["base"])) * 100  # Estimación
                            
                            mc = price_usd * supply if supply > 0 else 0
                        
                        token_data = {
                            "name": pair.get("baseToken", {}).get("name", "Unknown"),
                            "sym": pair.get("baseToken", {}).get("symbol", "???"),
                            "price": price_usd,
                            "mc": mc,
                            "real_time_mc": mc,
                            "lp": float(pair.get("liquidity", {}).get("usd", 0)),
                            "price_sol": price_native,
                            "last_trade_price": price_usd,
                            "vol": float(pair.get("volume", {}).get("h24", 0)),
                            "pairs_count": len(result["pairs"]),
                            "source": "DexScreener (api.dexscreener.com)",
                            "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                            "timestamp": time.time(),
                            "fresh": True,
                            "latency": time.time() - start_time
                        }
                        
                        log.info(f"✅ Datos obtenidos para {mint[:8]} en {token_data['latency']:.2f}s desde {token_data['source']}")
    except Exception as e:
        log.warning(f"Error consultando DexScreener para {mint[:8]}: {str(e)}")
    
    # MÉTODO 2: Pump.fun API directa (paralelizado)
    if not token_data:
        try:
            # Probar diferentes endpoints para mayor probabilidad de éxito
            pumpfun_endpoints = [
                f"https://pump.fun/api/v1/tokens/{mint}?{cache_buster_param}",
                f"https://api.pump.fun/v1/tokens/{mint}?{cache_buster_param}",
                f"https://pump.fun/api/v2/tokens/{mint}?{cache_buster_param}" # API v2 es más detallada
            ]
            
            # En lugar de intentar secuencialmente, hacer peticiones en paralelo
            # para máxima velocidad y fiabilidad
            loading_info["status"] = "consultando_pumpfun"
            _token_cache[f"loading_status_{mint}"] = loading_info
            
            # Timeout más agresivo para todas las consultas
            timeout = aiohttp.ClientTimeout(
                total=1.5,  # Reducido de 2.5/2.0
                connect=0.7,  # Reducido de 1.0/0.8
                sock_read=1.0  # Reducido de 1.8/1.5
            )
            
            # Ejecutar consultas en paralelo
            tasks = []
            for url in pumpfun_endpoints:
                tasks.append(asyncio.create_task(_fetch_pumpfun_endpoint(url, anti_cache_headers, timeout)))
            
            # Esperar la primera respuesta exitosa o que todas fallen
            for coro in asyncio.as_completed(tasks, timeout=1.8):
                try:
                    result = await coro
                    if result and isinstance(result, dict) and "token" in result:
                        token = result["token"]
                        supply = int(token.get("supply", 0))
                        decimals = int(token.get("decimals", 9))
                        
                        price = float(token.get("price", 0))
                        mc = price * supply / (10 ** decimals)
                        
                        # Compilar datos con información de frescura
                        token_data = {
                            "name": token.get("name", "Unknown"),
                            "sym": token.get("symbol", "???"),
                            "price": price,
                            "mc": mc,
                            "real_time_mc": mc,
                            "lp": float(token.get("liquidity", 0)),
                            "price_sol": float(token.get("priceNative", 0)),
                            "last_trade_price": price,
                            "vol": float(token.get("volume24h", 0)),
                            "source": "Pump.fun API",
                            "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                            "timestamp": time.time(),
                            "fresh": force_fresh,
                            "latency": time.time() - start_time
                        }
                        break
                except Exception as e:
                    continue
        except Exception as e:
            log.warning(f"Error obteniendo datos de pump.fun: {str(e)}")
    
    # MÉTODO 3: Usar Jupiter API como respaldo (simplificado y más rápido)
    if not token_data:
        loading_info["status"] = "consultando_jupiter"
        _token_cache[f"loading_status_{mint}"] = loading_info
        
        try:
            url = f"https://price.jup.ag/v4/price?ids={mint}&vsToken=USDC&{cache_buster_param}"
            timeout = aiohttp.ClientTimeout(total=1.2, connect=0.5, sock_read=0.8)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=anti_cache_headers, ssl=False) as response:
                    if response.status == 200:
                        data = await response.json()
                        if "data" in data and mint in data["data"]:
                            price = float(data["data"][mint].get("price", 0))
                            
                            # Obtener SOL price para conversión (usar caché si disponible)
                            sol_price = await get_sol_price_usd_qn(use_cache=True)
                            price_sol = price / sol_price if sol_price > 0 else 0
                            
                            token_data = {
                                "name": "Unknown Token",
                                "sym": "???",
                                "price": price,
                                "mc": 0,  # No podemos calcular MC sin supply
                                "real_time_mc": 0,
                                "lp": 0,
                                "price_sol": price_sol,
                                "last_trade_price": price,
                                "vol": 0,
                                "source": "Jupiter API",
                                "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                                "timestamp": time.time(),
                                "fresh": force_fresh,
                                "latency": time.time() - start_time
                            }
        except Exception as e:
            log.warning(f"Error consultando Jupiter API: {str(e)}")
    
    # Guardar en caché si tenemos datos
    if token_data:
        _token_cache[cache_key] = token_data
        # Eliminar status de carga
        if f"loading_status_{mint}" in _token_cache:
            del _token_cache[f"loading_status_{mint}"]
        return token_data
    
    # Si no pudimos obtener datos, registrar error y devolver objeto vacío
    loading_info["status"] = "error"
    loading_info["error"] = "No se pudieron obtener datos"
    _token_cache[f"loading_status_{mint}"] = loading_info
    
    return {
        "name": "Unknown Token",
        "sym": "???",
        "price": 0,
        "mc": 0,
        "lp": 0,
        "vol": 0,
        "source": "Error recuperando datos",
        "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
        "timestamp": time.time(),
        "error": True,
        "latency": time.time() - start_time
    }

async def _fetch_pumpfun_endpoint(url: str, headers: dict, timeout: aiohttp.ClientTimeout) -> dict:
    """Función auxiliar para consultar un endpoint de Pump.fun en paralelo"""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, ssl=False) as response:
                if response.status == 200:
                    return await response.json()
    except Exception:
        pass
    return {}

async def _fetch_quicknode_data(mint: str, timeout: aiohttp.ClientTimeout) -> Dict[str, Any]:
    """
    Función optimizada para obtener datos básicos desde QuickNode.
    Se enfoca en velocidad máxima.
    """
    try:
        # Obtener precio del token desde endpoints de Jupiter
        price_url = f"https://price.jup.ag/v4/price?ids={mint}&_={int(time.time() * 1000)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
        }
        
        # Obtener precio primero (lo más crítico)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(price_url, headers=headers) as resp:
                if resp.status == 200:
                    price_data = await resp.json()
                    if "data" in price_data and mint in price_data["data"]:
                        price = float(price_data["data"][mint].get("price", 0))
                        
                        # Si tenemos precio válido, podemos construir el resto de la información
                        if price > 0:
                            # Intentar obtener supply para calcular MC
                            supply = 0
                            try:
                                supply_data = await asyncio.wait_for(
                                    get_token_supply_qn(mint), 
                                    timeout=0.3
                                )
                                raw, dec = supply_data
                                supply = raw / (10 ** dec) if dec > 0 else raw
                            except:
                                pass
                            
                            # Intentar obtener precio de SOL para conversión
                            sol_price = 175.0  # Valor por defecto
                            try:
                                if "sol_price_cache" in _token_cache:
                                    sol_cache = _token_cache["sol_price_cache"]
                                    if time.time() - sol_cache.get('timestamp', 0) < 30:
                                        sol_price = sol_cache.get('price', sol_price)
                            except:
                                pass
                            
                            # Calcular valores derivados
                            price_sol = price / sol_price if sol_price > 0 else 0
                            mc = price * supply if supply > 0 else 0
                            
                            # Construir resultado
                            return {
                                "name": "Unknown",  # No disponible desde este método
                                "sym": "???",      # No disponible desde este método
                                "price": price,
                                "price_sol": price_sol,
                                "mc": mc,
                                "real_time_mc": mc,
                                "lp": 0,           # No disponible desde este método
                                "vol": 0,          # No disponible desde este método
                                "renounced": False,
                                "supply": supply,
                                "last_trade_price": price,
                                "chart_price": price
                            }
    except Exception as e:
        log.debug(f"Error en QuickNode: {e}")
    
    return None

async def _background_refresh_token_data(mint: str):
    """Tarea en segundo plano para actualizar datos de tokens sin bloquear"""
    try:
        # Verificar si ya hay una actualización en progreso para este mint
        lock_key = f"refresh_lock_{mint}"
        if lock_key in _token_cache:
            # Si ya hay un proceso de actualización reciente (menos de 1 segundo), salir
            if time.time() - _token_cache[lock_key].get("timestamp", 0) < 1:
                return
        
        # Registrar lock con timestamp actual
        _token_cache[lock_key] = {"timestamp": time.time()}
        
        log.debug(f"Iniciando actualización en segundo plano para {mint[:8]}")
        
        # Usar timeout reducido (1.5 segundos en lugar de 2.0)
        try:
            from .token_info import get_token_data_multi_source
            
            # Este resultado reemplazará la caché existente
            result = await asyncio.wait_for(
                get_token_data_multi_source(mint),
                timeout=1.5
            )
            
            if result:
                # Actualizar caché con nuevos datos
                cache_key = f"pumpfun_{mint}"
                _token_cache[cache_key] = result
                log.debug(f"✅ Actualización en segundo plano completada para {mint[:8]}")
        except asyncio.TimeoutError:
            log.debug(f"⏱️ Timeout en actualización en segundo plano para {mint[:8]}")
        except Exception as e:
            log.debug(f"❌ Error en actualización en segundo plano para {mint[:8]}: {e}")
        
        # Eliminar lock
        if lock_key in _token_cache:
            del _token_cache[lock_key]
            
    except Exception as e:
        log.error(f"Error general en actualización en segundo plano: {e}")

def _combine_background_data(existing_data: Dict[str, Any], new_results: Dict[str, Any], mint: str) -> Dict[str, Any]:
    """
    Combina datos existentes con nuevos resultados de la actualización en segundo plano.
    Preserva el timestamp original y añade información adicional.
    """
    # Comenzar con los datos existentes
    combined = existing_data.copy()
    
    # PumpFun API v2 tiene prioridad para la mayoría de los campos
    if "api_v2" in new_results and "token" in new_results["api_v2"] and "pair" in new_results["api_v2"]:
        api_data = new_results["api_v2"]
        token_data = api_data["token"]
        pair_data = api_data["pair"]
        
        # Actualizar campos principales
        if "name" in token_data:
            combined["name"] = token_data["name"]
        if "symbol" in token_data:
            combined["sym"] = token_data["symbol"]
        if "fdv" in pair_data:
            combined["mc"] = float(pair_data["fdv"])
            combined["real_time_mc"] = float(pair_data["fdv"])
        if "liquidity" in pair_data and "usd" in pair_data["liquidity"]:
            combined["lp"] = float(pair_data["liquidity"]["usd"])
        if "volume" in pair_data and "h24" in pair_data["volume"]:
            combined["vol"] = float(pair_data["volume"]["h24"] or 0)
        if "priceChange" in pair_data and "h24" in pair_data["priceChange"]:
            combined["price_diff_pct"] = float(pair_data["priceChange"]["h24"] or 0)
        if "priceUsd" in pair_data:
            combined["price"] = float(pair_data["priceUsd"])
            combined["last_trade_price"] = float(pair_data["priceUsd"])
            combined["chart_price"] = float(pair_data["priceUsd"])
        if "priceNative" in pair_data:
            combined["price_sol"] = float(pair_data["priceNative"])
    
    # Integrar datos de DexScreener si están disponibles
    if "dexscreener" in new_results and "pairs" in new_results["dexscreener"] and new_results["dexscreener"]["pairs"]:
        pairs = new_results["dexscreener"]["pairs"]
        # Ordenar por liquidez para obtener el par principal
        pairs.sort(key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        main_pair = pairs[0]
        
        # Solo actualizar si hay datos y no tenemos datos previos
        if "name" not in combined or combined["name"] == "Unknown":
            combined["name"] = main_pair.get("baseToken", {}).get("name", combined["name"])
        if "sym" not in combined or combined["sym"] == "???":
            combined["sym"] = main_pair.get("baseToken", {}).get("symbol", combined["sym"])
        
        # Actualizar liquidez si no está disponible
        if "lp" not in combined or combined["lp"] == 0:
            combined["lp"] = float(main_pair.get("liquidity", {}).get("usd", 0) or 0)
        
        # Actualizar volumen si no está disponible
        if "vol" not in combined or combined["vol"] == 0:
            combined["vol"] = float(main_pair.get("volume", {}).get("h24", 0) or 0)
    
    # Añadir metadatos sobre la actualización
    combined["background_updated"] = True
    combined["update_time"] = time.strftime("%H:%M:%S", time.localtime())
    combined["source"] = f"{combined.get('source', 'desconocida')} (datos completos)"
    
    return combined

async def _full_background_refresh(mint: str, cache_key: str, existing_data: Dict[str, Any]) -> None:
    """
    Realiza una actualización completa en segundo plano cuando la caché es antigua.
    Usa el método completo original para obtener datos actualizados.
    """
    try:
        # Esperar un momento para no competir con la solicitud principal
        await asyncio.sleep(1.0)
        
        log.info(f"Iniciando actualización completa en segundo plano para {mint[:8]}")
        
        # Importar correctamente - usar forma absoluta
        from src.token_info import get_token_stats
        
        # Obtener datos completos
        fresh_data = await asyncio.wait_for(
            get_token_stats(mint),
            timeout=5.0  # Timeout generoso
        )
        
        if fresh_data and fresh_data.get("price", 0) > 0:
            # Actualizar caché con datos frescos
            _token_cache[cache_key] = {
                'data': fresh_data,
                'timestamp': time.time(),
                'source': 'full_background_update'
            }
            
            log.info(f"✅ Actualización completa en segundo plano finalizada para {mint[:8]}")
    except Exception as e:
        log.warning(f"Error en actualización completa: {e}")

async def fetch_jupiter(mint: str) -> Dict[str, Any]:
    """Función optimizada para consultar directamente a Jupiter con múltiples endpoints"""
    current_time = time.time()
    
    # Lista de endpoints alternativos
    jupiter_endpoints = [
        f"https://price.jup.ag/v4/price?ids={mint}&t={int(current_time*1000)}",
        f"https://quote-api.jup.ag/v4/price?ids={mint}&t={int(current_time*1000)}",
        f"https://jupiter-price-api.solana.fm/v4/price?ids={mint}&t={int(current_time*1000)}"
    ]
    
    # Headers optimizados
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache"
    }
    
    # Timeout más generoso para problemas de red
    timeout = aiohttp.ClientTimeout(total=3.0, connect=1.5, sock_read=1.5)
    
    # Probar cada endpoint
    for endpoint_url in jupiter_endpoints:
        try:
            log.info(f"Intentando obtener datos de {endpoint_url}")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint_url, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if data and "data" in data and mint in data["data"]:
                                token_data = data["data"][mint]
                                
                                # Obtener datos básicos
                                price_usd = float(token_data.get("price", 0))
                                
                                # Si no tenemos precio, continuar con el siguiente endpoint
                                if price_usd <= 0:
                                    continue
                                
                                # Intentar obtener supply para calcular el MC
                                try:
                                    supply_data = await asyncio.wait_for(get_token_supply_qn(mint), timeout=1.0)
                                    raw, dec = supply_data
                                    supply = raw / (10 ** dec) if dec > 0 else raw
                                except Exception as e:
                                    log.warning(f"Error obteniendo supply para {mint}: {e}")
                                    supply = 1_000_000_000  # Valor por defecto
                                
                                # Calcular MC basado en el supply y precio
                                mc = price_usd * supply
                                
                                # Obtener precio de SOL actualizado sin valores predeterminados o fijos
                                try:
                                    # Obtener precio de SOL preciso con mayor prioridad
                                    sol_price_future = asyncio.create_task(get_sol_price_usd_qn())
                                    sol_price = await asyncio.wait_for(sol_price_future, timeout=0.5)
                                    
                                    # Si no se obtiene un precio válido, usar caché o respaldo
                                    if sol_price <= 0:
                                        # Verificar si hay precio en caché
                                        if "sol_price_cache" in _token_cache:
                                            sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                        else:
                                            sol_price = 175.85  # Último recurso, actualizado abril 2024
                                except Exception as e:
                                    log.debug(f"Error obteniendo precio SOL: {e}")
                                    # Usar valor de caché o respaldo
                                    if "sol_price_cache" in _token_cache:
                                        sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                    else:
                                        sol_price = 175.85  # Último recurso, actualizado abril 2024
                                
                                price_sol = price_usd / sol_price if sol_price > 0 else 0
                                
                                # Intentar obtener nombre y símbolo del token
                                # Jupiter no proporciona esta información, así que usaremos valores por defecto
                                name = "Unknown"
                                sym = "???"
                                
                                # Intentar obtener nombre/símbolo de un método alternativo
                                try:
                                    # Intentar obtener metadatos del token
                                    if "price_24h" in token_data:
                                        # Si hay cambio de precio 24h, calcular porcentaje
                                        price_24h = token_data.get("price_24h", 0)
                                        if price_24h > 0:
                                            price_diff_pct = ((price_usd / price_24h) - 1) * 100
                                        else:
                                            price_diff_pct = 0
                                    else:
                                        price_diff_pct = 0
                                except:
                                    price_diff_pct = 0
                                
                                log.info(f"✅ Datos obtenidos de Jupiter para {mint[:8]}")
                                
                                return {
                                    "name": name,
                                    "sym": sym,
                                    "price": price_usd,
                                    "price_sol": price_sol,
                                    "mc": mc,
                                    "real_time_mc": mc,
                                    "lp": 0,  # Jupiter no proporciona esta información
                                    "vol": 0,  # Jupiter no proporciona esta información
                                    "renounced": False,
                                    "supply": supply,
                                    "last_trade_price": price_usd,
                                    "chart_price": price_usd,
                                    "price_diff_pct": price_diff_pct,
                                    "source": "Jupiter (Directo)",
                                    "fetchTime": current_time
                                }
                        except Exception as e:
                            log.warning(f"Error procesando datos de {endpoint_url}: {e}")
        except Exception as e:
            log.warning(f"Error conectando a {endpoint_url}: {e}")
    
    # Si no pudimos obtener datos de Jupiter, intentar una alternativa
    try:
        # Intentar con Birdeye como último recurso
        birdeye_url = f"https://public-api.birdeye.so/public/price?address={mint}"
        headers["X-API-KEY"] = "5f88b633a8a142aa9b4b96fe"  # Clave pública genérica
        
        log.info(f"Intentando obtener datos de Birdeye para {mint[:8]}")
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(birdeye_url, headers=headers) as resp:
                if resp.status == 200:
                    try:
                        data = await resp.json()
                        if data and "data" in data and "value" in data["data"]:
                            price_usd = float(data["data"]["value"])
                            
                            if price_usd <= 0:
                                return None
                                
                            # Intentar obtener supply
                            try:
                                supply_data = await asyncio.wait_for(get_token_supply_qn(mint), timeout=1.0)
                                raw, dec = supply_data
                                supply = raw / (10 ** dec) if dec > 0 else raw
                            except:
                                supply = 1_000_000_000
                                
                            # Calcular MC
                            mc = price_usd * supply
                            
                            # Obtener precio de SOL actualizado sin valores predeterminados o fijos
                            try:
                                # Obtener precio de SOL preciso con mayor prioridad
                                sol_price_future = asyncio.create_task(get_sol_price_usd_qn())
                                sol_price = await asyncio.wait_for(sol_price_future, timeout=0.5)
                                
                                # Si no se obtiene un precio válido, usar caché o respaldo
                                if sol_price <= 0:
                                    # Verificar si hay precio en caché
                                    if "sol_price_cache" in _token_cache:
                                        sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                    else:
                                        sol_price = 175.85  # Último recurso, actualizado abril 2024
                            except Exception as e:
                                log.debug(f"Error obteniendo precio SOL: {e}")
                                # Usar valor de caché o respaldo
                                if "sol_price_cache" in _token_cache:
                                    sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                else:
                                    sol_price = 175.85  # Último recurso, actualizado abril 2024
                            
                            price_sol = price_usd / sol_price if sol_price > 0 else 0
                            
                            log.info(f"✅ Datos obtenidos de Birdeye para {mint[:8]}")
                            
                            return {
                                "name": "Unknown",
                                "sym": "???",
                                "price": price_usd,
                                "price_sol": price_sol,
                                "mc": mc,
                                "real_time_mc": mc,
                                "lp": 0,
                                "vol": 0,
                                "renounced": False,
                                "supply": supply,
                                "last_trade_price": price_usd,
                                "chart_price": price_usd,
                                "price_diff_pct": 0,
                                "source": "Birdeye (Fallback)",
                                "fetchTime": current_time
                            }
                    except Exception as e:
                        log.warning(f"Error procesando datos de Birdeye: {e}")
    except Exception as e:
        log.warning(f"Error conectando a Birdeye: {e}")
    
    log.warning(f"No se pudo obtener datos de Jupiter ni alternativas para {mint}")
    return None

async def fetch_dexscreener(mint: str) -> Dict[str, Any]:
    """Función optimizada para consultar directamente a DexScreener con endpoints alternativos"""
    current_time = time.time()
    
    # Lista de endpoints alternativos para mayor redundancia
    dexscreener_endpoints = [
        f"https://api.dexscreener.com/latest/dex/tokens/{mint}?t={int(current_time*1000)}",
        f"https://api.dexscreener.io/latest/dex/tokens/{mint}?t={int(current_time*1000)}",
        f"https://dexscreener.com/api/latest/dex/tokens/{mint}?t={int(current_time*1000)}"
    ]
    
    # Headers optimizados para evitar problemas de caché
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0"
    }
    
    # Timeout más generoso para problemas de red
    timeout = aiohttp.ClientTimeout(total=3.0, connect=1.5, sock_read=2.0)
    
    # Probar cada endpoint hasta que uno funcione
    for endpoint_url in dexscreener_endpoints:
        try:
            log.info(f"Intentando obtener datos de {endpoint_url}")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint_url, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            
                            if data and "pairs" in data and len(data["pairs"]) > 0:
                                # Ordenar pares por liquidez para obtener el par principal
                                pairs = sorted(
                                    data["pairs"], 
                                    key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), 
                                    reverse=True
                                )
                                
                                pair = pairs[0]
                                
                                # Extraer datos básicos
                                price_usd = float(pair.get("priceUsd", 0))
                                mc = float(pair.get("fdv", 0))
                                lp = float(pair.get("liquidity", {}).get("usd", 0))
                                
                                # Obtener precio de SOL actualizado sin valores predeterminados o fijos
                                try:
                                    # Obtener precio de SOL preciso con mayor prioridad
                                    sol_price_future = asyncio.create_task(get_sol_price_usd_qn())
                                    sol_price = await asyncio.wait_for(sol_price_future, timeout=0.5)
                                    
                                    # Si no se obtiene un precio válido, usar caché o respaldo
                                    if sol_price <= 0:
                                        # Verificar si hay precio en caché
                                        if "sol_price_cache" in _token_cache:
                                            sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                        else:
                                            sol_price = 175.85  # Último recurso, actualizado abril 2024
                                except Exception as e:
                                    log.debug(f"Error obteniendo precio SOL: {e}")
                                    # Usar valor de caché o respaldo
                                    if "sol_price_cache" in _token_cache:
                                        sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                    else:
                                        sol_price = 175.85  # Último recurso, actualizado abril 2024
                                
                                price_sol = price_usd / sol_price if sol_price > 0 else 0
                                
                                log.info(f"✅ Datos obtenidos de DexScreener para {mint[:8]}")
                                # Compilar datos
                                return {
                                    "name": pair.get("baseToken", {}).get("name", "Unknown"),
                                    "sym": pair.get("baseToken", {}).get("symbol", "???"),
                                    "price": price_usd,
                                    "price_sol": price_sol,
                                    "mc": mc,
                                    "real_time_mc": mc,
                                    "lp": lp,
                                    "vol": float(pair.get("volume", {}).get("h24", 0)),
                                    "renounced": False,
                                    "supply": float(token_data.get("supply", 0)),
                                    "last_trade_price": price_usd,
                                    "chart_price": price_usd,
                                    "price_diff_pct": float(pair.get("priceChange", {}).get("h24", 0) or 0),
                                    "source": "DexScreener (Directo)",
                                    "fetchTime": current_time
                                }
                        except Exception as e:
                            log.warning(f"Error procesando datos de {endpoint_url}: {e}")
        except Exception as e:
            log.warning(f"Error conectando a {endpoint_url}: {e}")
    
    # Si llegamos aquí, intentar una alternativa: consultar la web directamente
    try:
        web_url = f"https://dexscreener.com/solana/{mint}"
        log.info(f"Intentando obtener datos mediante scraping de DexScreener para {mint[:8]}")
        
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(web_url, headers=headers) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    
                    # Extraer datos usando expresiones regulares
                    import re
                    price_match = re.search(r'data-price="([0-9\.]+)"', html) or re.search(r'>\$([0-9\.]+)<', html)
                    name_match = re.search(r'<title>(.*?)\s*\|', html)
                    symbol_match = re.search(r'\(([\w]+)\)', html) or re.search(r'data-symbol="([\w]+)"', html)
                    
                    if price_match:
                        result = {}
                        
                        # Nombre y símbolo
                        if name_match:
                            result["name"] = name_match.group(1).strip()
                        else:
                            result["name"] = "Unknown"
                            
                        if symbol_match:
                            result["sym"] = symbol_match.group(1)
                        else:
                            result["sym"] = "???"
                        
                        # Precio
                        result["price"] = float(price_match.group(1))
                        
                        # Intentar obtener MC y LP
                        mc_match = re.search(r'FDV.*?\$([0-9\.]+)([KkMmBb]?)', html)
                        lp_match = re.search(r'Liquidity.*?\$([0-9\.]+)([KkMmBb]?)', html)
                        
                        # Market Cap
                        if mc_match:
                            mc_str = mc_match.group(1)
                            mc_unit = mc_match.group(2).upper() if len(mc_match.groups()) > 1 else ""
                            
                            if mc_unit == 'K':
                                result["mc"] = float(mc_str) * 1000
                            elif mc_unit == 'M':
                                result["mc"] = float(mc_str) * 1000000
                            elif mc_unit == 'B':
                                result["mc"] = float(mc_str) * 1000000000
                            else:
                                result["mc"] = float(mc_str)
                        else:
                            result["mc"] = 0
                            
                        # Liquidez
                        if lp_match:
                            lp_str = lp_match.group(1)
                            lp_unit = lp_match.group(2).upper() if len(lp_match.groups()) > 1 else ""
                            
                            if lp_unit == 'K':
                                result["lp"] = float(lp_str) * 1000
                            elif lp_unit == 'M':
                                result["lp"] = float(lp_str) * 1000000
                            elif lp_unit == 'B':
                                result["lp"] = float(lp_str) * 1000000000
                            else:
                                result["lp"] = float(lp_str)
                        else:
                            result["lp"] = 0
                        
                        # Valores por defecto para los campos restantes
                        sol_price = 175.85  # Valor actualizado abril 2024
                        result["price_sol"] = result["price"] / sol_price if sol_price > 0 else 0
                        result["real_time_mc"] = result["mc"]
                        result["vol"] = 0
                        result["renounced"] = False
                        result["supply"] = 0
                        result["last_trade_price"] = result["price"]
                        result["chart_price"] = result["price"]
                        result["price_diff_pct"] = 0
                        result["source"] = "DexScreener (Scraping)"
                        result["fetchTime"] = current_time
                        
                        log.info(f"✅ Datos recuperados mediante scraping de DexScreener para {mint[:8]}")
                        return result
    except Exception as e:
        log.warning(f"Error durante scraping de DexScreener: {e}")
    
    # Si todos los métodos fallan
    log.warning(f"No se pudo obtener datos de DexScreener para {mint}")
    return None

# Limpiar caché periódicamente
async def cache_cleanup_task():
    """
    Tarea en segundo plano para limpiar periódicamente la caché.
    Versión mejorada con protección para entradas en uso activo.
    """
    log.info("Iniciando tarea de limpieza de caché")
    
    while True:
        try:
            current_time = time.time()
            keys_to_remove = []
            
            # Obtener lista de tokens virales para limpieza más agresiva
            viral_tokens = VIRAL_TOKENS
            
            # Revisar cada entrada en la caché
            for key, cache_item in _token_cache.items():
                # Ignorar entradas con prefijo refresh_lock - estas son gestionadas por separado
                if key.startswith("refresh_lock_"):
                    # Pero limpiar locks antiguos (más de 5 segundos)
                    lock_age = current_time - cache_item.get('timestamp', 0)
                    if lock_age > 5.0:
                        keys_to_remove.append(key)
                    continue
                
                # Comprobación para cada tipo de caché
                if key.startswith("sol_price_cache"):
                    # Caché para precio de SOL: 15 segundos máximo
                    cache_age = current_time - cache_item.get('timestamp', 0)
                    if cache_age > 15.0:
                        keys_to_remove.append(key)
                
                elif any(viral in key for viral in viral_tokens):
                    # Tokens virales: 5 segundos máximo
                    cache_age = current_time - cache_item.get('timestamp', 0)
                    if cache_age > 5.0:
                        keys_to_remove.append(key)
                        
                elif key.startswith("pumpfun_") or key.startswith("dex_") or key.startswith("jupiter_"):
                    # Caché para APIs externas: 30 segundos máximo para tokens normales
                    cache_age = current_time - cache_item.get('timestamp', 0)
                    if cache_age > 30.0:
                        keys_to_remove.append(key)
                
                elif key.startswith("token_stats_"):
                    # Caché para datos completos de tokens: 60 segundos máximo
                    cache_age = current_time - cache_item.get('timestamp', 0)
                    if cache_age > 60.0:
                        keys_to_remove.append(key)
                
                # Limpieza de otros tipos de caché con TTL genérico
                elif 'timestamp' in cache_item:
                    cache_age = current_time - cache_item.get('timestamp', 0)
                    # Usar TTL más corto por defecto (120 segundos)
                    if cache_age > 120.0:  
                        keys_to_remove.append(key)
            
            # Eliminar claves marcadas
            for key in keys_to_remove:
                _token_cache.pop(key, None)
                
            # Si se eliminaron entradas, registrar log
            if keys_to_remove:
                log.debug(f"Caché limpiada: {len(keys_to_remove)} entradas eliminadas")
                
            # Reducir el intervalo de limpieza para más rapidez
            await asyncio.sleep(10)  # Verificar cada 10 segundos
            
        except Exception as e:
            log.error(f"Error en tarea de limpieza de caché: {e}")
            await asyncio.sleep(30)  # Esperar un poco más si hay error

def start_cache_cleanup():
    """Inicia la tarea de limpieza de caché en segundo plano"""
    # Esta función debe ser llamada desde un contexto donde ya existe un bucle de eventos
    try:
        # Iniciar solo la tarea de limpieza de caché
        asyncio.create_task(cache_cleanup_task())
        log.info("Tarea de limpieza de caché iniciada correctamente")
        return True
    except RuntimeError as e:
        log.error(f"Error al iniciar limpieza de caché: {e}")
        return False

async def background_refresh_popular_tokens():
    """
    Actualiza periódicamente los datos de tokens populares en segundo plano.
    Esto mejora la velocidad de respuesta cuando los usuarios consultan estos tokens.
    """
    log.info("Iniciando actualización periódica de tokens populares en segundo plano")
    
    while True:
        try:
            # Primero, actualizar tokens virales (más prioritarios y con mayor frecuencia)
            for mint in VIRAL_TOKENS:
                try:
                    # Intentar actualizar datos con un timeout ultra-agresivo
                    log.info(f"Actualizando token viral {mint[:8]}...")
                    
                    # Usar una sesión con timeouts más agresivos para tokens virales
                    timeout = aiohttp.ClientTimeout(total=1.0, connect=0.8, sock_read=0.8)
                    
                    # Crear solicitud directa a PumpFun para máxima velocidad
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        url = f"https://pump.fun/api/v2/tokens/{mint}"
                        headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                            "Accept": "application/json, text/plain, */*"
                        }
                        
                        timestamp_ms = int(time.time() * 1000)
                        url_with_cache_buster = f"{url}?_={timestamp_ms}"
                        
                        async with session.get(url_with_cache_buster, headers=headers) as resp:
                            if resp.status == 200:
                                try:
                                    data_json = await resp.json()
                                    
                                    if data_json.get("token"):
                                        token_data = data_json.get("token", {})
                                        pair_data = data_json.get("pair", {})
                                        
                                        # Convertir datos a formato estándar
                                        price_usd = float(pair_data.get("priceUsd", 0))
                                        mc = float(pair_data.get("fdv", 0))
                                        lp = float(pair_data.get("liquidity", {}).get("usd", 0))
                                        
                                        # Obtener precio de SOL actualizado sin valores predeterminados o fijos
                                        try:
                                            # Obtener precio de SOL preciso con mayor prioridad
                                            sol_price_future = asyncio.create_task(get_sol_price_usd_qn())
                                            sol_price = await asyncio.wait_for(sol_price_future, timeout=0.5)
                                            
                                            # Si no se obtiene un precio válido, usar caché o respaldo
                                            if sol_price <= 0:
                                                # Verificar si hay precio en caché
                                                if "sol_price_cache" in _token_cache:
                                                    sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                                else:
                                                    sol_price = 175.85  # Último recurso, actualizado abril 2024
                                        except Exception as e:
                                            log.debug(f"Error obteniendo precio SOL: {e}")
                                            # Usar valor de caché o respaldo
                                            if "sol_price_cache" in _token_cache:
                                                sol_price = _token_cache["sol_price_cache"].get("price", 175.85)
                                            else:
                                                sol_price = 175.85  # Último recurso, actualizado abril 2024
                                        
                                        price_sol = price_usd / sol_price if sol_price > 0 else 0
                                        
                                        # Compilar datos
                                        processed_data = {
                                            "name": token_data.get("name", "Unknown"),
                                            "sym": token_data.get("symbol", "???"),
                                            "price": price_usd,
                                            "price_sol": price_sol,
                                            "mc": mc,
                                            "real_time_mc": mc,
                                            "lp": lp,
                                            "vol": float(pair_data.get("volume", {}).get("h24", 0)),
                                            "renounced": pair_data.get("renounced", False),
                                            "supply": float(token_data.get("supply", 0)),
                                            "last_trade_price": price_usd,
                                            "chart_price": price_usd,
                                            "price_diff_pct": float(pair_data.get("priceChange", {}).get("h24", 0) or 0),
                                            "source": "PumpFun (Tiempo Real)",
                                            "fetchTime": time.time()
                                        }
                                        
                                        # Guardar en caché
                                        cache_key = f"pumpfun_{mint}"
                                        _token_cache[cache_key] = {
                                            'data': processed_data,
                                            'timestamp': time.time(),
                                            'last_check': time.time()
                                        }
                                        
                                        log.info(f"✅ Datos de token viral actualizados para {mint[:8]} - MC: ${mc:,.2f}")
                                except Exception as e:
                                    log.warning(f"Error procesando respuesta para token viral {mint[:8]}: {e}")
                    
                    # Esperar muy poco entre actualizaciones de tokens virales
                    await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning(f"Error actualizando token viral {mint[:8]}: {e}")
            
            # Esperar un tiempo muy corto antes de actualizar tokens populares regulares
            await asyncio.sleep(0.5)
            
            # Ahora, actualizar tokens populares regulares con menor prioridad
            for mint in POPULAR_TOKENS:
                try:
                    # No actualizar si ya está en la lista de virales (evitar duplicados)
                    if any(viral_token in mint for viral_token in VIRAL_TOKENS):
                        continue
                        
                    # Intentar actualizar datos
                    log.info(f"Actualizando token popular {mint[:8]}...")
                    data = await asyncio.wait_for(
                        fetch_pumpfun(mint),
                        timeout=2.0
                    )
                    
                    # Si se obtuvieron datos, guardarlos en caché
                    if data:
                        cache_key = f"pumpfun_{mint}"
                        _token_cache[cache_key] = {
                            'data': data,
                            'timestamp': time.time(),
                            'last_check': time.time()
                        }
                        log.info(f"✅ Datos actualizados para {mint[:8]}")
                    
                    # Esperar un poco entre actualizaciones para no saturar las APIs
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning(f"Error actualizando token popular {mint[:8]}: {e}")
            
            # Esperar antes de la próxima ronda de actualizaciones
            # Tiempo de espera basado en la frecuencia para tokens virales
            await asyncio.sleep(VIRAL_TOKEN_CACHE_TTL_SECONDS)
        except Exception as e:
            log.error(f"Error en actualización periódica de tokens: {e}")
            await asyncio.sleep(2)  # Esperar menos tiempo antes de reintentar

def start_background_tasks():
    """Inicia todas las tareas en segundo plano"""
    # Esta función debe ser llamada desde un contexto donde ya existe un bucle de eventos
    # Por ejemplo, desde la función main() del bot o desde otro lugar donde asyncio ya está inicializado
    try:
        # Iniciar la tarea de limpieza de caché
        cache_task = asyncio.create_task(cache_cleanup_task())
        cache_task.set_name("cache_cleanup_task")
        
        # DESACTIVADO: Ya no iniciamos la actualización automática para reducir solicitudes
        # tokens_task = asyncio.create_task(background_refresh_popular_tokens())
        # tokens_task.set_name("background_refresh_popular_tokens")
        
        log.info("Tarea de limpieza de caché iniciada correctamente")
        
        # Registrar función para limpiar la caché al salir
        atexit.register(lambda: log.info("Tareas en segundo plano detenidas y caché liberada"))
        
        return True
    except RuntimeError as e:
        log.error(f"Error al iniciar tareas en segundo plano: {e}")
        log.info("Las tareas se iniciarán cuando haya un bucle de eventos en ejecución")
        return False

def record_request_result(success: bool, latency: float):
    """Registra el resultado de una solicitud para ajustar timeouts adaptativos"""
    global _network_health
    
    # Actualizar contadores
    _network_health["total_requests"] += 1
    if not success:
        _network_health["failed_requests"] += 1
    
    # Mantener solo las últimas 50 mediciones
    _network_health["measurements"].append({
        "timestamp": time.time(),
        "success": success,
        "latency": latency
    })
    
    if len(_network_health["measurements"]) > 50:
        _network_health["measurements"].pop(0)
    
    # Calcular tasa de éxito reciente (últimas 50 solicitudes)
    recent_total = len(_network_health["measurements"])
    recent_success = sum(1 for m in _network_health["measurements"] if m["success"])
    _network_health["success_rate"] = recent_success / recent_total if recent_total > 0 else 1.0
    
    # Calcular latencia promedio reciente (solo para solicitudes exitosas)
    successful_latencies = [m["latency"] for m in _network_health["measurements"] if m["success"]]
    _network_health["avg_latency"] = sum(successful_latencies) / len(successful_latencies) if successful_latencies else 0.5
    
    # Ajustar timeout adaptativo basado en rendimiento reciente
    # Si la tasa de éxito es baja o la latencia alta, aumentar timeout
    if _network_health["success_rate"] < 0.7 or _network_health["avg_latency"] > 1.0:
        # Aumentar timeout en 10%
        _network_health["adaptive_timeout"] *= 1.1
    elif _network_health["success_rate"] > 0.9 and _network_health["avg_latency"] < 0.5:
        # Reducir timeout en 5% (más conservador en la reducción)
        _network_health["adaptive_timeout"] *= 0.95
    
    # Asegurar que el timeout esté dentro de límites razonables
    _network_health["adaptive_timeout"] = max(
        min(_network_health["adaptive_timeout"], _network_health["max_timeout"]), 
        _network_health["min_timeout"]
    )
    
    # Log periódico del estado de salud de la red (cada 50 solicitudes)
    if _network_health["total_requests"] % 50 == 0:
        log.info(
            f"Estado de red: {_network_health['success_rate']*100:.1f}% éxito, "
            f"latencia {_network_health['avg_latency']*1000:.0f}ms, "
            f"timeout adaptativo {_network_health['adaptive_timeout']*1000:.0f}ms"
        )

def get_adaptive_timeout() -> aiohttp.ClientTimeout:
    """Obtiene un timeout adaptativo basado en el rendimiento de la red"""
    timeout_value = _network_health["adaptive_timeout"]
    return aiohttp.ClientTimeout(
        total=timeout_value * 2.0,      # Total timeout
        connect=timeout_value * 0.5,     # Connect timeout
        sock_read=timeout_value * 0.8    # Socket read timeout
    )

async def _fetch_source(url: str, headers: Dict, timeout: aiohttp.ClientTimeout = None) -> Dict[str, Any]:
    """
    Función optimizada para recuperar datos de una fuente externa con
    máxima velocidad y manejo de errores mejorado.
    """
    if not timeout:
        timeout = ULTRA_FAST_TIMEOUT  # Usar timeout ultra rápido por defecto
        
    # Añadir parámetros anti-caché
    timestamp = int(time.time() * 1000)
    rand = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=8))
    url_with_cache_buster = f"{url}{'&' if '?' in url else '?'}_={timestamp}&r={rand}"
    
    try:
        start_time = time.time()
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url_with_cache_buster, headers=headers, ssl=False) as response:
                elapsed_ms = int((time.time() - start_time) * 1000)
                
                if response.status == 200:
                    # Intentar decodificar como JSON
                    try:
                        result = await response.json(content_type=None)
                        record_request_result(True, elapsed_ms / 1000)
                        return result
                    except Exception as json_err:
                        # Leer como texto e intentar parsear
                        text = await response.text()
                        try:
                            result = json.loads(text)
                            record_request_result(True, elapsed_ms / 1000)
                            return result
                        except Exception as e:
                            log.error(f"Error parsing JSON from {url}: {str(e)}")
                            record_request_result(False, elapsed_ms / 1000)
                else:
                    log.debug(f"Request to {url} failed with status {response.status}")
                    record_request_result(False, elapsed_ms / 1000)
    except asyncio.TimeoutError:
        log.debug(f"Timeout accessing {url}")
        record_request_result(False, 0)
    except Exception as e:
        log.debug(f"Error accessing {url}: {str(e)}")
        record_request_result(False, 0)
    
    return {}

# Ya no inicializamos las tareas al importar para evitar el error de "no running event loop"
# NO iniciar las tareas aquí, ya que no hay un bucle de eventos en ejecución
# durante la importación del módulo
# La tarea se iniciará desde bot.py cuando el bucle de eventos esté en funcionamiento
# atexit.register(lambda: log.info("Tareas en segundo plano detenidas")) 

async def _get_dexscreener_data(mint: str, cache_buster: str = "") -> Dict[str, Any]:
    """
    Obtiene datos de un token desde DexScreener, que es una fuente muy rápida y confiable.
    
    Args:
        mint: Dirección del token
        cache_buster: String opcional para forzar datos frescos
        
    Returns:
        Dict con información del token o None si hay error
    """
    start_time = time.time()
    request_attempt = 1
    max_attempts = 3 if cache_buster else 1  # Más intentos cuando se requieren datos frescos
    
    while request_attempt <= max_attempts:
        try:
            # Construir URL con parámetros que evitan caché si se pide datos frescos
            base_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
            if cache_buster:
                url = f"{base_url}?_cb={cache_buster}&t={int(time.time() * 1000)}"
            else:
                url = base_url
                
            timeout = aiohttp.ClientTimeout(total=2.0, connect=0.8, sock_read=1.2)
            headers = {
                "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(90, 120)}.0.0.0",
                "Accept": "application/json",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, ssl=False) as r:
                    if r.status == 200:
                        response_time = time.time() - start_time
                        log.debug(f"DexScreener API respondió en {response_time:.2f}s")
                        
                        # Parsear respuesta JSON
                        data = await r.json()
                        
                        # Verificar si hay pairs en la respuesta
                        if not data or not data.get("pairs") or len(data.get("pairs", [])) == 0:
                            request_attempt += 1
                            if request_attempt <= max_attempts:
                                log.warning(f"No se encontraron pares para {mint}, reintentando ({request_attempt}/{max_attempts})")
                                await asyncio.sleep(0.2)
                                continue
                            log.warning(f"No se encontraron pares para {mint} después de {max_attempts} intentos")
                async with session.get(url, headers=headers, ssl=False) as response:
                    response_time = time.time() - start_time
                    
                    if response.status == 200:
                        result = await response.json()
                        
                        if result and "pairs" in result and len(result["pairs"]) > 0:
                            # Si hay múltiples pares, ordenar por liquidez (más alta primero)
                            if len(result["pairs"]) > 1:
                                result["pairs"].sort(
                                    key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0), 
                                    reverse=True
                                )
                            
                            # Tomar el primer par (generalmente el más relevante)
                            pair = result["pairs"][0]
                            
                            # Obtener datos básicos con manejo explícito de errores
                            try:
                                price_usd = float(pair.get("priceUsd", 0))
                            except (ValueError, TypeError):
                                price_usd = 0
                                
                            try:
                                price_native = float(pair.get("priceNative", 0))
                            except (ValueError, TypeError):
                                price_native = 0
                            
                            # Calcular market cap con múltiples métodos de respaldo
                            mc = 0
                            mc_source = "No calculado"
                            
                            # Método 1: Usar FDV directamente (más preciso)
                            try:
                                fdv = pair.get("fdv", None)
                                if fdv is not None and fdv != "":
                                    mc = float(fdv)
                                    mc_source = "FDV directo"
                            except (ValueError, TypeError):
                                pass
                            
                            # Método 2: Usar totalSupply * precio
                            if mc == 0:
                                try:
                                    supply = pair.get("totalSupply", None)
                                    if supply is not None and supply != "" and price_usd > 0:
                                        mc = float(supply) * price_usd
                                        mc_source = "totalSupply * precio"
                                except (ValueError, TypeError):
                                    pass
                            
                            # Método 3: Estimar supply basado en liquidez
                            if mc == 0 and price_usd > 0:
                                try:
                                    liquidity_base = pair.get("liquidity", {}).get("base", 0)
                                    if liquidity_base:
                                        estimated_supply = float(liquidity_base) * 100  # Estimación conservadora
                                        mc = estimated_supply * price_usd
                                        mc_source = "estimación (liquidez * 100)"
                                except (ValueError, TypeError):
                                    pass
                            
                            # Log detallado del market cap
                            if mc > 0:
                                log.info(f"Market Cap calculado: ${mc:,.0f} - método: {mc_source}")
                            
                            log.info(f"✅ Datos obtenidos para {mint[:8]} en {response_time:.2f}s desde DexScreener ({response.url.host})")
                            
                            # Crear respuesta con data enriquecida
                            return {
                                "name": pair.get("baseToken", {}).get("name", "Unknown"),
                                "sym": pair.get("baseToken", {}).get("symbol", "???"),
                                "price": price_usd,
                                "price_sol": price_native,
                                "mc": mc,
                                "real_time_mc": mc,
                                "mc_source": mc_source,
                                "lp": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                                "vol": float(pair.get("volume", {}).get("h24", 0) or 0),
                                "renounced": False,  # DexScreener no proporciona esta info
                                "last_trade_price": price_usd,
                                "price_diff_pct": float(pair.get("priceChange", {}).get("h24", 0) or 0),
                                "source": f"DexScreener ({response.url.host})",
                                "fresh": bool(cache_buster),  # Indicar si son datos frescos forzados
                                "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                                "latency": response_time,
                                "fetchTime": time.time()
                            }
                        else:
                            log.warning(f"No se encontraron pares para {mint[:8]} en DexScreener")
                    else:
                        log.warning(f"Error en DexScreener: status {response.status} para {mint[:8]}")
            
            # Si llegamos aquí, hubo un problema, intentar de nuevo con parámetros más agresivos
            if cache_buster and request_attempt < max_attempts:
                request_attempt += 1
                # Añadir delay antes del segundo intento
                await asyncio.sleep(0.2)
                continue
            else:
                # No más intentos o no se requieren datos frescos
                break
        
        except asyncio.TimeoutError:
            log.warning(f"Timeout al obtener datos de DexScreener para {mint[:8]} (intento {request_attempt}/{max_attempts})")
            if cache_buster and request_attempt < max_attempts:
                request_attempt += 1
                continue
            else:
                break
        except Exception as e:
            log.warning(f"Error obteniendo datos de DexScreener para {mint[:8]}: {e}")
            if cache_buster and request_attempt < max_attempts:
                request_attempt += 1
                continue
            else:
                break
    
    return None