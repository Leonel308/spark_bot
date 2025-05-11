import os, logging, aiohttp, traceback
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.async_client import AsyncToken
from .config import RPC_ENDPOINT
import asyncio
import time
import random
import re
import json
import websockets

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

log = logging.getLogger(__name__)

# Añadir referencia a las funciones mejoradas de QuickNode
from .quicknode_client import get_sol_balance_qn, get_token_balance_qn, get_token_supply_qn, get_user_tokens_qn, get_sol_price_usd_qn

# Funciones compatibles para mantener API coherente (redireccionan a QuickNode)
async def get_sol_balance(wallet_address, rpc_endpoint=None):
    """Obtiene el balance SOL de una wallet (función de compatibilidad)"""
    # Intentar primero con QuickNode para velocidad
    result = await get_sol_balance_qn(wallet_address, rpc_endpoint)
    if result is not None:
        return result
        
    # Fallback a método tradicional
    try:
        client = AsyncClient(rpc_endpoint or RPC_ENDPOINT)
        response = await client.get_balance(Pubkey.from_string(wallet_address))
        await client.close()
        return response.value / LAMPORTS_PER_SOL
    except Exception as e:
        log.error(f"Error obteniendo balance SOL: {e}")
        return 0

async def get_sol_price_usd(use_cache=True):
    """Obtiene el precio actual de SOL en USD"""
    # Usar la implementación optimizada de QuickNode
    try:
        return await get_sol_price_usd_qn()
    except Exception as e:
        log.error(f"Error obteniendo precio de SOL: {e}")
        # Valor de respaldo actualizado
        return 175.85  # Valor actualizado abril 2024

async def get_token_balance(wallet_address, token_mint, rpc_endpoint=None):
    """Obtiene el balance de un token (función de compatibilidad)"""
    log.info(f"Consultando balance para token {token_mint} en wallet {wallet_address}")
    
    # Intentar primero con QuickNode para velocidad
    try:
        result = await get_token_balance_qn(wallet_address, token_mint, rpc_endpoint)
        if result is not None:
            balance = result["uiAmount"]
            log.info(f"Balance de token obtenido vía QuickNode: {balance}")
            return balance
        log.info("No se pudo obtener balance vía QuickNode, usando método tradicional")
    except Exception as e:
        log.error(f"Error al obtener balance con QuickNode: {e}")
        
    # Fallback a método tradicional
    try:
        client = AsyncClient(rpc_endpoint or RPC_ENDPOINT)
        resp = await client.get_token_accounts_by_owner(
            Pubkey.from_string(wallet_address),
            TokenAccountOpts(program_id=TOKEN_PROGRAM_ID, mint=Pubkey.from_string(token_mint))
        )
        await client.close()
        
        if len(resp.value) > 0:
            token_amount = int(resp.value[0].account.data.parsed['info']['tokenAmount']['amount'])
            token_decimal = resp.value[0].account.data.parsed['info']['tokenAmount']['decimals']
            balance = token_amount / (10 ** token_decimal)
            log.info(f"Balance de token obtenido vía método tradicional: {balance}")
            return balance
        log.info(f"No se encontraron cuentas de token para {token_mint}")
        return 0
    except Exception as e:
        log.error(f"Error obteniendo balance del token (método tradicional): {e}")
        return 0

async def get_token_supply(token_mint, rpc_endpoint=None):
    """Obtiene el supply de un token (función de compatibilidad)"""
    # Intentar primero con QuickNode para velocidad
    result = await get_token_supply_qn(token_mint, rpc_endpoint)
    if result is not None:
        return result
        
    # Fallback a método tradicional
    try:
        client = AsyncClient(rpc_endpoint or RPC_ENDPOINT)
        info = await client.get_token_supply(Pubkey.from_string(token_mint))
        await client.close()
        
        if info.value:
            amount = int(info.value.amount)
            decimals = info.value.decimals
            return amount, decimals
        return 0, 0
    except Exception as e:
        log.error(f"Error obteniendo supply del token: {e}")
        return 0, 0

async def get_user_tokens(wallet_address, rpc_endpoint=None):
    """Obtiene todos los tokens que posee un usuario (función de compatibilidad)"""
    # Intentar primero con QuickNode para velocidad
    result = await get_user_tokens_qn(wallet_address, rpc_endpoint)
    if result is not None:
        # Extraer solo las direcciones de los tokens
        return [token.get("mint") for token in result if token.get("mint")]
        
    # Fallback a método tradicional
    try:
        client = AsyncClient(rpc_endpoint or RPC_ENDPOINT)
        resp = await client.get_token_accounts_by_owner(
            Pubkey.from_string(wallet_address),
            TokenAccountOpts(program_id=TOKEN_PROGRAM_ID)
        )
        await client.close()
        
        tokens = []
        for account in resp.value:
            mint = account.account.data.parsed['info']['mint']
            amount = int(account.account.data.parsed['info']['tokenAmount']['amount'])
            decimals = account.account.data.parsed['info']['tokenAmount']['decimals']
            
            # Solo incluir tokens con balance positivo
            if amount > 0:
                tokens.append(mint)
                
        return tokens
    except Exception as e:
        log.error(f"Error obteniendo tokens del usuario: {e}")
        return []

# URLs de las APIs
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
PUMP_API_V2 = "https://api.pump.fun/v2/tokens/{mint}"
PUMP_FUN_API_V2 = "https://pump.fun/api/v2/tokens/{mint}"
PUMP_FUN_NEXT_DATA = "https://pump.fun/_next/data/latest/token/{mint}.json"
PUMP_FUN_API_V1 = "https://pump.fun/api/v1/tokens/{mint}"

# WebSocket endpoints
PUMP_FUN_WEBSOCKET = "wss://pumpportal.fun/api/data"
SOLANA_TRACKER_WEBSOCKET = "wss://api.solanatracker.io/datastream"

class PumpfunMarketData:
    """Clase para obtener datos en tiempo real del marketcap de tokens en Pump.fun"""
    
    def __init__(self, rpc_endpoint=None):
        self.rpc_endpoint = rpc_endpoint or RPC_ENDPOINT
        self._token_cache = {}
        self._websocket_connection = None
        self._ws_subscriptions = set()
        self._ws_callback_handlers = {}
        self._last_ws_message = {}
        
    async def start_websocket_connection(self):
        """Inicia una conexión WebSocket para datos en tiempo real"""
        if self._websocket_connection and not self._websocket_connection.closed:
            return
        
        try:
            # Intentar primero el WebSocket de PumpPortal que tiene datos más oficiales
            self._websocket_connection = await websockets.connect(
                PUMP_FUN_WEBSOCKET, 
                ping_interval=20,
                close_timeout=5,
                max_size=10 * 1024 * 1024  # 10MB max message size
            )
            log.info("✅ Conexión WebSocket establecida con PumpPortal")
            
            # Iniciar tarea de escucha en segundo plano
            asyncio.create_task(self._listen_websocket())
        except Exception as e:
            log.error(f"Error al conectar WebSocket: {e}")
            # Intentar con la alternativa
            try:
                self._websocket_connection = await websockets.connect(
                    SOLANA_TRACKER_WEBSOCKET,
                    ping_interval=20,
                    close_timeout=5
                )
                log.info("✅ Conexión WebSocket establecida con SolanaTracker (alternativa)")
                
                # Iniciar tarea de escucha en segundo plano
                asyncio.create_task(self._listen_websocket())
            except Exception as e2:
                log.error(f"Error también con WebSocket alternativo: {e2}")
                self._websocket_connection = None
    
    async def _listen_websocket(self):
        """Escucha mensajes del WebSocket en segundo plano"""
        if not self._websocket_connection:
            return
            
        try:
            # Control de errores más robusto y reconexión
            retry_count = 0
            max_retries = 5
            
            while retry_count < max_retries:
                try:
                    async for message in self._websocket_connection:
                        try:
                            # Resetear contador de reintentos cuando recibimos mensajes exitosamente
                            retry_count = 0
                            
                            data = json.loads(message)
                            # Identificar tipo de mensaje y procesarlo
                            if 'mint' in data and ('price' in data or 'priceUsd' in data or 'marketCap' in data or 'marketCapUsd' in data):
                                mint = data.get('mint')
                                self._last_ws_message[mint] = {
                                    'data': data,
                                    'timestamp': time.time()
                                }
                                
                                # Notificar a los callbacks registrados
                                if mint in self._ws_callback_handlers:
                                    for callback in self._ws_callback_handlers[mint]:
                                        asyncio.create_task(callback(data))
                        except json.JSONDecodeError:
                            log.debug(f"Error decodificando mensaje WebSocket: {message[:100]}...")
                            continue
                        except Exception as e:
                            log.debug(f"Error procesando mensaje WebSocket: {e}")
                            continue
                
                except websockets.exceptions.ConnectionClosed as e:
                    retry_count += 1
                    wait_time = min(2 * retry_count, 10)  # Espera exponencial hasta 10 segundos máximo
                    log.warning(f"Conexión WebSocket cerrada (intento {retry_count}/{max_retries}), reconectando en {wait_time} segundos...")
                    await asyncio.sleep(wait_time)
                    
                    # Intentar reconexión
                    try:
                        if self._websocket_connection and not self._websocket_connection.closed:
                            await self._websocket_connection.close()
                            
                        # Usar ping interval más pequeño para detectar desconexiones más rápido
                        self._websocket_connection = await websockets.connect(
                            PUMP_FUN_WEBSOCKET, 
                            ping_interval=10,
                            close_timeout=5,
                            max_size=10 * 1024 * 1024  # 10MB max message size
                        )
                        log.info("✅ Reconexión WebSocket exitosa")
                    except Exception as reconnect_error:
                        log.error(f"Error al reconectar WebSocket: {reconnect_error}")
                        
            log.error("Máximo número de reintentos de conexión WebSocket alcanzado, deteniendo escucha")
            
        except Exception as e:
            log.error(f"Error en WebSocket: {e}")
            if self._websocket_connection:
                await self._websocket_connection.close()
            self._websocket_connection = None
    
    async def subscribe_token_realtime(self, mint):
        """Suscribe a actualizaciones en tiempo real para un token específico"""
        if not self._websocket_connection:
            await self.start_websocket_connection()
            
        if not self._websocket_connection or self._websocket_connection.closed:
            log.warning("No se pudo establecer conexión WebSocket")
            return False
            
        try:
            # Formato de suscripción para PumpPortal
            if PUMP_FUN_WEBSOCKET in str(self._websocket_connection.uri):
                subscription = {
                    "method": "subscribeTokenTrade",
                    "keys": [mint]
                }
            # Formato alternativo para SolanaTracker
            else:
                subscription = {
                    "op": "subscribe",
                    "channel": "token",
                    "tokens": [mint]
                }
                
            await self._websocket_connection.send(json.dumps(subscription))
            self._ws_subscriptions.add(mint)
            log.info(f"✅ Suscrito a datos en tiempo real para {mint}")
            return True
        except Exception as e:
            log.error(f"Error al suscribirse a token {mint}: {e}")
            return False
    
    def register_token_callback(self, mint, callback):
        """Registra una función callback para ser llamada cuando hay datos nuevos de un token"""
        if mint not in self._ws_callback_handlers:
            self._ws_callback_handlers[mint] = []
        self._ws_callback_handlers[mint].append(callback)
        
    async def get_marketcap_realtime(self, mint: str) -> dict:
        """
        Obtiene datos de marketcap en tiempo real para un token de Pump.fun
        usando múltiples fuentes en paralelo para máxima velocidad y fiabilidad
        
        Args:
            mint: La dirección del token
            
        Returns:
            Diccionario con datos del token incluyendo marketcap, precio, etc.
        """
        start_time = time.time()
        log.info(f"Solicitando marketcap en tiempo real para {mint}")
        
        # Comprobar primero si tenemos datos recientes en caché
        # 1. Verificar caché de WebSocket (más rápida)
        if mint in self._last_ws_message and time.time() - self._last_ws_message[mint]['timestamp'] < 5:
            ws_data = self._last_ws_message[mint]['data']
            log.info(f"Usando datos WebSocket en caché para {mint}")
            
            # Devolver con el formato estandarizado
            return {
                "marketCapUsd": ws_data.get("marketCap", ws_data.get("marketCapUsd", 0)),
                "priceUsd": ws_data.get("price", ws_data.get("priceUsd", 0)),
                "name": ws_data.get("name", ""),
                "symbol": ws_data.get("symbol", ""),
                "liquidity": ws_data.get("liquidity", 0),
                "volume24h": ws_data.get("volume24h", ws_data.get("volume", 0)),
                "source": "WebSocket-Realtime",
                "timestamp": int(time.time()),
                "fetch_time_ms": 0  # Instantáneo desde caché
            }
         
        # Suscribir al token para actualizaciones futuras (en segundo plano)
        if self._websocket_connection and mint not in self._ws_subscriptions:
            asyncio.create_task(self.subscribe_token_realtime(mint))
            
        # Realizar múltiples consultas en paralelo para obtener los datos más rápidos
        tasks = []
        
        # 1. Consulta a Jupiter API (muy rápida)
        tasks.append(self._get_token_data_jupiter(mint))
        
        # 2. Consulta a DexScreener API (datos adicionales)
        tasks.append(self._get_token_data_dexscreener(mint))
        
        # 3. Consulta a Pump.fun API (datos oficiales)
        tasks.append(self._get_token_data_pumpfun(mint))
        
        # 4. Si hay dependencia de QuickNode, usarla también
        if self.has_quicknode_client:
            from .quicknode_client import fetch_pumpfun
            if fetch_pumpfun:
                tasks.append(fetch_pumpfun(mint, force_fresh=True))
                
        # Esperar a que termine la consulta más rápida con timeout agresivo
        try:
            done, pending = await asyncio.wait(
                tasks,
                timeout=2.0,  # Timeout agresivo de 2 segundos
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Procesar resultados de la consulta más rápida
            best_result = None
            
            for task in done:
                try:
                    result = task.result()
                    if result and isinstance(result, dict):
                        # Si hay un resultado con marketcap o precio, usarlo
                        if result.get("marketCapUsd", 0) > 0 or result.get("mc", 0) > 0 or result.get("priceUsd", 0) > 0 or result.get("price", 0) > 0:
                            best_result = result
                            break
                except Exception as e:
                    log.error(f"Error procesando tarea: {str(e)}")
            
            # Cancelar consultas pendientes si ya tenemos un buen resultado
            for task in pending:
                task.cancel()
                
            # Si tenemos un resultado válido, devolverlo en formato estandarizado
            if best_result:
                # Normalizar el formato según la fuente
                normalized = {
                    "marketCapUsd": best_result.get("marketCapUsd", best_result.get("mc", best_result.get("marketCap", 0))),
                    "priceUsd": best_result.get("priceUsd", best_result.get("price", 0)),
                    "name": best_result.get("name", "Unknown"),
                    "symbol": best_result.get("symbol", best_result.get("sym", "")),
                    "liquidity": best_result.get("liquidity", best_result.get("lp", 0)),
                    "volume24h": best_result.get("volume24h", best_result.get("vol", 0)),
                    "source": best_result.get("source", "API"),
                    "timestamp": int(time.time()),
                    "fetch_time_ms": int((time.time() - start_time) * 1000)
                }
                
                # Almacenar en caché local para consultas futuras
                self._last_ws_message[mint] = {
                    'data': normalized,
                    'timestamp': time.time()
                }
                
                return normalized
        except Exception as e:
            log.error(f"Error obteniendo datos en tiempo real: {str(e)}")
                
        # Si todos los métodos rápidos fallan, intentar con un método más confiable pero más lento
        try:
            # Intentar método respaldo (API directa con timeout generoso)
            result = await self._get_token_data_direct(mint)
            if result:
                # Normalizar formato
                normalized = {
                    "marketCapUsd": result.get("marketCapUsd", result.get("mc", 0)),
                    "priceUsd": result.get("priceUsd", result.get("price", 0)),
                    "name": result.get("name", "Unknown"),
                    "symbol": result.get("symbol", ""),
                    "liquidity": result.get("liquidity", 0),
                    "volume24h": result.get("volume24h", 0),
                    "source": result.get("source", "API-Direct"),
                    "timestamp": int(time.time()),
                    "fetch_time_ms": int((time.time() - start_time) * 1000)
                }
                
                # Almacenar en caché local
                self._last_ws_message[mint] = {
                    'data': normalized,
                    'timestamp': time.time()
                }
                
                return normalized
        except Exception as e:
            log.error(f"También falló el método de respaldo: {str(e)}")
            
        # Si todo falla, devolver datos vacíos
        return {
            "marketCapUsd": 0,
            "priceUsd": 0,
            "name": "Unknown",
            "symbol": "",
            "liquidity": 0,
            "volume24h": 0,
            "source": "Error",
            "timestamp": int(time.time()),
            "fetch_time_ms": int((time.time() - start_time) * 1000),
            "error": True
        }
    
    def _generate_anticache_headers(self):
        """Genera headers especiales para evitar caché por completo"""
        # Generar nonce aleatorio para evitar caché
        nonce = ''.join(random.choices('0123456789abcdef', k=16))
        timestamp = int(time.time() * 1000)
        random_ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{random.randint(100, 120)}.0.{random.randint(4000, 6000)}.{random.randint(100, 200)} Safari/537.36"
        
        return {
            "User-Agent": random_ua,
            "Accept": "application/json",
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-Requested-With": "XMLHttpRequest",
            "X-Timestamp": str(timestamp),
            "X-Nonce": nonce,
            "If-Modified-Since": "0",
            "If-None-Match": f"W/\"random-{nonce}\"",
            "X-Cache-Bust": str(timestamp)
        }
        
    async def _get_dexscreener_data_nocache(self, mint: str) -> dict:
        """Obtiene datos desde DexScreener con optimización para evitar caché por completo"""
        try:
            # Crear parámetros anti-caché
            timestamp = int(time.time() * 1000)
            rand_string = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16))
            url = f"{DEXSCREENER_API.format(mint=mint)}?t={timestamp}&r={rand_string}&cb={random.randint(10000, 99999)}"
            
            headers = self._generate_anticache_headers()
            
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.5)) as session:  # Timeout de 500ms
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
                                    # Intentar calcular con supply
                                    supply_data = await self.get_token_supply(mint)
                                    if supply_data:
                                        amount, decimals = supply_data
                                        if amount > 0 and decimals > 0:
                                            real_supply = amount / (10 ** decimals)
                                            mc = real_supply * price
                                except Exception as e:
                                    log.debug(f"Error calculando MC con supply: {e}")
                            
                            return {
                                "marketCapUsd": mc,
                                "priceUsd": price,
                                "symbol": pair.get("baseToken", {}).get("symbol", ""),
                                "name": pair.get("baseToken", {}).get("name", ""),
                                "source": "DexScreener-Instant",
                                "liquidity": float(pair.get("liquidity", {}).get("usd", 0) or 0),
                                "volume24h": float(pair.get("volume", {}).get("h24", 0) or 0),
                                "timestamp": int(time.time())
                            }
        except Exception as e:
            log.debug(f"Error obteniendo datos de DexScreener: {e}")
        
        return {"marketCapUsd": 0, "source": "DexScreener-error"}
    
    async def _get_pumpfun_data_nocache(self, mint: str) -> dict:
        """Obtiene datos de Pump.fun con múltiples consultas en paralelo para evitar caché por completo"""
        try:
            # Generar timestamp y parámetros anti-caché
            timestamp = int(time.time() * 1000)
            rand_string = ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=16))
            
            # Headers para forzar datos frescos
            headers = self._generate_anticache_headers()
            
            # Crear múltiples URLs con parámetros anti-caché para consultas paralelas
            endpoints = [
                f"{PUMP_API_V2.format(mint=mint)}?_={timestamp}&r={rand_string}&cb={random.randint(10000, 99999)}",
                f"{PUMP_FUN_API_V2.format(mint=mint)}?_={timestamp}&r={rand_string}&cb={random.randint(10000, 99999)}",
                f"{PUMP_FUN_NEXT_DATA.format(mint=mint)}?_={timestamp}&r={rand_string}&cb={random.randint(10000, 99999)}",
                f"{PUMP_FUN_API_V1.format(mint=mint)}?_={timestamp}&r={rand_string}&cb={random.randint(10000, 99999)}"
            ]
            
            # Ejecutar todas las consultas en paralelo para máxima velocidad
            tasks = []
            for url in endpoints:
                tasks.append(asyncio.create_task(self._fetch_endpoint_with_timeout(url, headers, 0.4)))  # 400ms timeout
            
            # Esperar a que CUALQUIERA complete - timeout ultra-agresivo
            done, pending = await asyncio.wait(
                tasks, 
                timeout=0.4, # 400ms máximo de espera
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Procesar el primer resultado válido
            for task in done:
                try:
                    result = task.result()
                    if result:
                        # Cancelar las tareas pendientes
                        for p_task in pending:
                            p_task.cancel()
                        
                        price = float(result.get("price", 0))
                        mc = float(result.get("marketCap", 0))
                        
                        # Calcular MC si es necesario
                        if price > 0 and mc <= 0 and "supply" in result and "decimals" in result:
                            supply = int(result.get("supply", 0))
                            decimals = int(result.get("decimals", 9))
                            if supply > 0:
                                mc = price * (supply / (10 ** decimals))
                        
                        return {
                            "marketCapUsd": mc,
                            "priceUsd": price,
                            "symbol": result.get("symbol", ""),
                            "name": result.get("name", ""),
                            "source": "Pump.fun-Instant",
                            "liquidity": float(result.get("liquidity", 0)),
                            "volume24h": float(result.get("volume24h", 0)),
                            "timestamp": int(time.time())
                        }
                except Exception as e:
                    log.debug(f"Error en tarea Pump.fun: {e}")
                    
            # Todos pendientes han sido cancelados o fallaron
            return {"marketCapUsd": 0, "source": "Pump.fun-error"}
            
        except Exception as e:
            log.error(f"Error general al consultar Pump.fun: {e}")
        
        return {"marketCapUsd": 0, "source": "Pump.fun-error"}
    
    async def _fetch_endpoint_with_timeout(self, url, headers, timeout=0.5):
        """Función auxiliar para consultar un endpoint con timeout muy estricto"""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
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
                        return data  # Devolver los datos tal cual si no coincide con formatos conocidos
        except asyncio.TimeoutError:
            # Timeout alcanzado
            log.debug(f"Timeout alcanzado consultando {url}")
        except Exception as e:
            log.debug(f"Error consultando {url}: {str(e)[:100]}")
        return None
    
    async def get_token_supply(self, mint: str) -> tuple[int,int]:
        """Obtiene el supply del token y decimales"""
        try:
            client = AsyncClient(self.rpc_endpoint)
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
    
    async def start_realtime_monitor(self, tokens: list, refresh_interval: float = 5.0):
        """
        Inicia un monitor en tiempo real para varios tokens
        
        Args:
            tokens: Lista de direcciones de tokens para monitorear
            refresh_interval: Intervalo de actualización en segundos
        """
        log.info(f"Iniciando monitor en tiempo real para {len(tokens)} tokens")
        
        # Iniciar conexión WebSocket
        await self.start_websocket_connection()
        
        # Suscribirse a todos los tokens
        for token in tokens:
            await self.subscribe_token_realtime(token)
        
        while True:
            tasks = []
            for token in tokens:
                tasks.append(asyncio.create_task(self.get_marketcap_realtime(token)))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Procesar resultados
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    log.error(f"Error al obtener datos para {tokens[i]}: {result}")
                else:
                    log.info(f"Token: {result.get('name', tokens[i])} ({result.get('symbol', '???')})")
                    log.info(f"Precio: ${result.get('priceUsd', 0):.10f}")
                    log.info(f"Market Cap: ${result.get('marketCapUsd', 0):,.2f}")
                    log.info(f"Fuente: {result.get('source', 'desconocida')}")
                    log.info("-" * 40)
            
            # Esperar antes de la siguiente actualización
            await asyncio.sleep(refresh_interval)

# Función auxiliar para ejecutar el monitor desde línea de comandos
async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Monitor de Market Cap en tiempo real para tokens de Pump.fun')
    parser.add_argument('tokens', nargs='+', help='Direcciones de tokens para monitorear')
    parser.add_argument('--interval', type=float, default=5.0, help='Intervalo de actualización en segundos')
    parser.add_argument('--rpc', type=str, help='URL del endpoint RPC (opcional)')
    
    args = parser.parse_args()
    
    # Crear instancia del monitor
    monitor = PumpfunMarketData(rpc_endpoint=args.rpc)
    
    # Iniciar monitor
    await monitor.start_realtime_monitor(args.tokens, args.interval)

if __name__ == "__main__":
    asyncio.run(main())
