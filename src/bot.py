import asyncio, json, logging, re, textwrap, base58, aiohttp, time, random, traceback
from enum import Enum, auto
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, Defaults, ContextTypes
)
from telegram.constants import ChatAction
from telegram.error import BadRequest

from .config         import BOT_TOKEN, BOT_FEE_PERCENTAGE, BOT_FEE_RECIPIENT, ENABLE_WEBSOCKET
from .wallet_manager import create_wallet, load_wallet
from .db             import add_user, user_exists, get_pubkey, record_transaction, get_position_data
from .db             import get_transaction_history, get_total_fees_paid
from .market_data    import get_sol_balance, get_token_supply, get_token_balance, get_user_tokens, PumpfunMarketData
from .token_info     import get_token_stats, get_pumpfun_realtime_mc, NO_CACHE_HEADERS
from .dex_client     import swap_sol_for_tokens, swap_tokens_for_sol
from .quicknode_client import (
    get_sol_balance_qn, get_token_balance_qn, get_user_tokens_qn,
    get_sol_price_usd_qn, get_token_supply_qn, fetch_pumpfun, 
    get_dexscreener_token_data_qn, start_cache_cleanup, cache_cleanup_task, start_background_tasks,
    _get_dexscreener_data
)
from .unified_interface import unified_keyboard, build_unified_message
# Importar manejadores de comandos
from .cmd_handlers import wallet_cmd, backup_cmd, help_cmd, tx_cmd, fees_cmd

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    level=logging.INFO)
log = logging.getLogger(__name__)

# Inicializar el objeto para obtener datos de marketcap en tiempo real
market_monitor = PumpfunMarketData()

# Lista de tokens monitoreados activamente (para optimizar el websocket)
_monitored_tokens = set()

# Caché de datos de marketcap para tokens (mint address -> datos)
tokens_market_data = {}

# Inicializar el sistema WebSocket si está habilitado
async def initialize_websocket():
    """Inicializa la conexión WebSocket para datos en tiempo real"""
    if ENABLE_WEBSOCKET:
        log.info("Iniciando conexión WebSocket para datos de tokens en tiempo real...")
        try:
            await market_monitor.start_websocket_connection()
            log.info("✅ Conexión WebSocket iniciada correctamente")
            
            # Registrar callback global para datos de tokens monitoreados
            async def token_update_callback(data):
                """Callback que se ejecuta cuando hay datos nuevos de un token"""
                if not data or not isinstance(data, dict) or "mint" not in data:
                    return
                    
                mint = data.get("mint")
                # Actualizar caché con datos recibidos
                tokens_market_data[mint] = {
                    "marketCapUsd": data.get("marketCap", data.get("marketCapUsd", 0)),
                    "priceUsd": data.get("price", data.get("priceUsd", 0)),
                    "symbol": data.get("symbol", ""),
                    "name": data.get("name", ""),
                    "liquidity": data.get("liquidity", 0),
                    "volume24h": data.get("volume24h", data.get("volume", 0)),
                    "source": "WebSocket-Stream",
                    "timestamp": int(time.time())
                }
                log.debug(f"Datos de {mint} actualizados desde WebSocket")
            
            # Monitorear tokens populares de forma predeterminada
            from .quicknode_client import POPULAR_TOKENS
            for token in POPULAR_TOKENS:
                _monitored_tokens.add(token)
                # Registrar el callback para este token
                market_monitor.register_token_callback(token, token_update_callback)
                # Suscribir al token para actualizaciones
                await market_monitor.subscribe_token_realtime(token)
                
            log.info(f"Monitoreo WebSocket iniciado para {len(_monitored_tokens)} tokens populares")
            
        except Exception as e:
            log.error(f"Error al inicializar WebSocket: {e}")
    else:
        log.info("WebSocket deshabilitado en la configuración")

# ───── Detectar mint / pump.fun / dexscreener ─────
# Expresión mejorada para detectar mint addresses directamente
MINT_RE = re.compile(r"(?:^|(?<![a-zA-Z0-9/]))[1-9A-HJ-NP-Za-km-z]{32,44}(?:$|(?![a-zA-Z0-9]))")
PUMP_RE = re.compile(r"pump\.fun/(?:coin|[A-Za-z0-9_-]+)/([1-9A-HJ-NP-Za-km-z]{32,44})")
# Mejorada para detectar URLs de DexScreener en cualquier formato
POOL_RE = re.compile(r"dexscreener\.com/(?:solana|[a-z0-9-]+)/([a-zA-Z0-9]{32,44})", re.IGNORECASE)

async def pool_to_mint(pool: str) -> str | None:
    """
    Convierte un pair address de DexScreener en un token mint address.
    
    Args:
        pool: El pool/pair address de DexScreener
        
    Returns:
        El token mint address (baseToken) o None si no se puede obtener
    """
    log.info(f"Obteniendo mint address para el par de DexScreener: {pool}")
    
    # Intentar primero con la función optimizada de QuickNode
    try:
        token_data = await get_dexscreener_token_data_qn(pool, is_pair=True)
        if token_data and "mint" in token_data:
            mint = token_data["mint"]
            log.info(f"Mint address recuperado con QuickNode: {mint}")
            return mint
    except Exception as e:
        log.warning(f"Error usando QuickNode para resolver pool: {str(e)}")
    
    # Fallback al método original si QuickNode falla
    url = f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool}"
    
    try:
        # Usar un timeout más generoso para evitar fallos en redes lentas
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            # Añadir headers para evitar bloqueos por rate limit
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache"
            }
            
            # Hacer log de la URL para depuración
            log.info(f"Consultando API de DexScreener: {url}")
            
            async with s.get(url, headers=headers) as r:
                if r.status == 200:
                    js = await r.json()
                    
                    # Log del resultado para depuración
                    log.info(f"Respuesta de DexScreener obtenida con status 200")
                    
                    # Verificar que la respuesta contenga información del par
                    if "pairs" in js and js["pairs"] and len(js["pairs"]) > 0:
                        pair = js["pairs"][0]
                        
                        # Log para depuración
                        if "baseToken" in pair:
                            log.info(f"baseToken encontrado: {pair['baseToken']}")
                        else:
                            log.error("baseToken no encontrado en la respuesta")
                        
                        # Verificar que el par tenga un baseToken (el token que queremos)
                        if "baseToken" in pair and "address" in pair["baseToken"]:
                            mint = pair["baseToken"]["address"]
                            log.info(f"Mint address recuperado correctamente: {mint}")
                            return mint
                        else:
                            # Intentar con quoteToken si baseToken no está disponible
                            if "quoteToken" in pair and "address" in pair["quoteToken"] and pair["quoteToken"]["address"] != "So11111111111111111111111111111111111111112":
                                mint = pair["quoteToken"]["address"]
                                log.info(f"No se encontró baseToken, usando quoteToken: {mint}")
                                return mint
                            else:
                                log.error(f"No se encontraron tokens válidos en la respuesta de DexScreener")
                    else:
                        log.error(f"Respuesta de DexScreener no contiene información de pares: {js}")
                else:
                    log.error(f"Error al consultar DexScreener API: {r.status} - {await r.text()}")
    except Exception as e:
        log.error(f"Excepción al procesar pair de DexScreener: {str(e)}")
    
    return None

async def extract(text: str) -> tuple[str|None,str|None]:
    """
    Extrae el mint address o pool address de un texto
    """
    text = text.strip()
    
    log.info(f"Procesando texto para extracción: {text}")
    
    # Caso especial: si el texto completo parece ser un mint address, procesarlo directamente
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", text):
        log.info(f"Detectado mint address directo (texto completo): {text}")
        return text, None
    
    # Manejar URL de DexScreener (prioridad sobre mint directo)
    if "dexscreener.com" in text:
        # Usar regex primero (caso más común)
        if p := POOL_RE.search(text):
            pool = p.group(1)
            log.info(f"Detectado enlace de DexScreener con pool ID: {pool}")
            
            # Obtener el mint address a partir del pool con QuickNode (más rápido)
            mint = await pool_to_mint(pool)
            
            # Si se pudo resolver, devolver mint y pool
            if mint:
                log.info(f"Pool {pool} resuelto a mint {mint}")
                return mint, pool
            else:
                log.error(f"No se pudo resolver el mint address para el pool {pool}")
        
        # Si el regex falló, intentar extraer manualmente
        parts = text.split("/")
        for part in parts:
            # Buscar una parte que parezca un address de Solana
            if re.match(r"^[a-zA-Z0-9]{32,44}$", part):
                pool = part
                log.info(f"Detectado posible pool ID desde URL: {pool}")
                
                # Obtener el mint address a partir del pool con QuickNode
                mint = await pool_to_mint(pool)
                
                # Si se pudo resolver, devolver mint y pool
                if mint:
                    log.info(f"Pool {pool} resuelto a mint {mint}")
                    return mint, pool
    
    # Si es un mint address directo
    if m := MINT_RE.search(text):
        mint = m.group(0)
        log.info(f"Detectado mint address directo con regex: {mint}")
        return mint, None
        
    # Si es un enlace de pump.fun
    if p := PUMP_RE.search(text):
        mint = p.group(1)
        log.info(f"Detectado enlace de pump.fun: {mint}")
        return mint, None
    
    log.warning(f"No se pudo extraer ningún mint o pool address del texto: {text}")
    return None, None

# ───────── FSM ─────────
class Step(Enum):
    IDLE   = auto()
    TOKEN  = auto()
    CUSTOM = auto()
    SELL   = auto()
    SELL_CUSTOM = auto()

STATE: dict[int,dict] = {}
def ensure(uid: int):
    STATE.setdefault(uid, {"step": Step.IDLE})

# ───── Keyboards ─────
def buy_kb(sel: float|None, is_refreshing: bool = False) -> InlineKeyboardMarkup:
    def btn(v):
        mark = "✅ " if sel==v else ""
        return InlineKeyboardButton(f"{mark}{v} SOL", callback_data=f"A_{v}")

    # Determinar si es una cantidad personalizada
    is_custom = sel and sel not in {0.5, 1, 3, 5, 10}
    custom_btn_text = f"✅ {sel} SOL" if is_custom else "X SOL 🖊"

    # Texto del botón de actualización (cambia si está en proceso de recarga)
    refresh_btn_text = "⏳ Actualizando..." if is_refreshing else "🔄 Actualizar"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Volver", callback_data="BACK"),
         InlineKeyboardButton(refresh_btn_text, callback_data="REF")],
        [btn(0.5), btn(1), btn(3)],
        [btn(5), btn(10),
         InlineKeyboardButton(custom_btn_text, callback_data="A_X")],
        [InlineKeyboardButton("🚀 COMPRAR 🚀", callback_data="BUY_EXEC")]
    ])

# Keyboard con estado de refresh para pantalla principal
def main_kb(is_refreshing: bool = False) -> InlineKeyboardMarkup:
    # Texto del botón de actualización (cambia si está en proceso de recarga)
    refresh_btn_text = "⏳ Actualizando..." if is_refreshing else "🔄 Refrescar"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 Comprar", callback_data="BUY"),
         InlineKeyboardButton("🔴 Vender", callback_data="SELL")],
        [InlineKeyboardButton("📊 Posiciones", callback_data="POSITIONS"),
         InlineKeyboardButton(refresh_btn_text, callback_data="REFRESH_MAIN")],
        [InlineKeyboardButton("📋 Copiar Wallet", callback_data="COPY_WALLET")],
        [InlineKeyboardButton("🔑 Exportar", callback_data="EXPORT"),
         InlineKeyboardButton("❓ Ayuda", callback_data="HELP")]
    ])

# Keyboard estático para usar cuando no necesitamos actualizar estado
MAIN_KB = main_kb(False)

# Keyboard para posiciones con estado de refresh
def positions_kb(is_refreshing: bool = False) -> InlineKeyboardMarkup:
    # Texto del botón de actualización (cambia si está en proceso de recarga)
    refresh_btn_text = "⏳ Actualizando..." if is_refreshing else "🔄 Actualizar"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(refresh_btn_text, callback_data="POS_REFRESH")],
        [InlineKeyboardButton("🟢 Comprar", callback_data="BUY"),
         InlineKeyboardButton("🔴 Vender", callback_data="SELL")],
        [InlineKeyboardButton("↩️ Volver", callback_data="BACK")]
    ])

# Keyboard estático para usar cuando no necesitamos actualizar estado
POSITIONS_KB = positions_kb(False)

BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Volver",callback_data="BACK")]])

# Crear teclado de venta para un token específico
def sell_kb(token_data: dict, sel: float|None = None, is_refreshing: bool = False) -> InlineKeyboardMarkup:
    """Crea teclado para vender tokens"""
    
    # Obtener balance y símbolo del token
    balance = token_data.get('balance', 0)
    symbol = token_data.get('symbol', '???')
    
    def btn(pct):
        mark = "✅ " if sel==pct else ""
        sell_amount = balance * (pct / 100)
        return InlineKeyboardButton(f"{mark}{pct}% ({sell_amount:.4f})", callback_data=f"S_{pct}")

    # Texto del botón de actualización (cambia si está en proceso de recarga)
    refresh_btn_text = "⏳ Actualizando..." if is_refreshing else "🔄 Actualizar"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Back", callback_data="POSITIONS"),
         InlineKeyboardButton(refresh_btn_text, callback_data="SELL_REF")],
        [btn(25), btn(50), btn(75)],
        [btn(100),
         InlineKeyboardButton(("✅ Custom 🖊" if sel and sel not in {25,50,75,100} else "Custom 🖊"),
                              callback_data="S_X")],
        [InlineKeyboardButton(f"VENDER {symbol} 🔴", callback_data="SELL_EXEC")]
    ])

# ───────── Helpers ─────────
def format_solana_address(address: str) -> str:
    """Formatea una dirección de Solana para que sea visualmente atractiva y copiable"""
    # En Telegram, el formato correcto para texto copiable es usar comillas invertidas
    return f"`{address}`"

async def wallet_header(pub: str) -> str:
    sol, px = await asyncio.gather(get_sol_balance(pub),
                                   get_sol_price_usd_qn())
    
    # Obtener información detallada sobre la fuente del precio de SOL
    sol_source = "desconocida"
    fuentes_usadas = 0
    timestamp = 0
    last_update_ms = 0
    try:
        from .quicknode_client import _token_cache
        if "sol_price_cache" in _token_cache:
            cache_data = _token_cache["sol_price_cache"]
            timestamp = cache_data.get('timestamp', 0)
            
            # Extraer información sobre la fuente
            if "source" in cache_data:
                sol_source = cache_data["source"]
                # Si es consenso, mostrar número de fuentes
                if "Consenso" in sol_source:
                    # Intentar obtener el número de fuentes
                    fuentes_usadas = cache_data.get("sources_count", 0)
                # Si es una URL, simplificar para mostrar solo el dominio
                elif "/" in sol_source:
                    sol_source = sol_source.split('/')[2]  # Extraer solo el dominio
            
            # Intentar obtener lista completa de precios
            all_prices = cache_data.get("all_prices", [])
            if all_prices and len(all_prices) > 1:
                fuentes_usadas = len(all_prices)
                
            # Obtener información sobre latencia en la obtención de datos
            last_update_ms = cache_data.get("fetch_time_ms", 0)
    except Exception as e:
        pass
        
    # Calcular tiempo desde la actualización
    update_age = time.time() - timestamp if timestamp > 0 else 0
    
    # Formato mejorado y estructurado 
    header = f"🏦 *SOLANA TRADING BOT* 🏦\n\n"
    
    # Sección Wallet con estilo mejorado
    header += f"*👛 Wallet*\n"
    header += f"{format_solana_address(pub)}\n"
    header += f"_Toca para copiar_\n\n"
    
    # Separador visual más distintivo
    header += f"───────────────────\n\n"
    
    # Sección Balance con mejor formato
    header += f"*💰 Balance:* {sol:.6f} SOL (${sol*px:,.2f})\n"
    
    # Mostrar precio de SOL sin ningún redondeo para precisión total
    # Agregar información sobre fuente y frescura de datos
    if fuentes_usadas > 1:
        header += f"*📈 SOL Price:* ${px} _(consenso de {fuentes_usadas} fuentes)_\n"
    else:
        header += f"*📈 SOL Price:* ${px} _(vía {sol_source})_\n"
    
    # Agregar información sobre edad de los datos
    if update_age > 0:
        if update_age < 5:
            header += f"_Datos en tiempo real ({update_age:.1f}s)_ ✅\n\n"
        elif update_age < 30:
            header += f"_Datos recientes ({update_age:.1f}s)_ ⚡\n\n"
        else:
            header += f"_Datos de hace {update_age:.1f}s_ ⚠️\n\n"
    else:
        header += f"_Datos en tiempo real_ ✅\n\n"
    
    # Separador visual
    header += f"───────────────────\n"
    return header

def exports(kp):
    raw = bytes(kp)
    return base58.b58encode(raw).decode(), json.dumps(list(raw))

def format_token_name(name: str, symbol: str) -> str:
    """Formatea el nombre y símbolo del token de manera atractiva"""
    return f"{symbol} - {name}"

def format_token_line(position: dict) -> str:
    """Formatea una línea de información de token para posiciones en estilo Trojan"""
    symbol = position.get('symbol', '???')
    name = position.get('name', 'Unknown Token')
    balance = position.get('balance', 0)
    price = position.get('price_usd', 0)
    value = position.get('value_usd', 0)
    pnl_pct = position.get('pnl_pct', 0)
    response_time = position.get('response_time', '')
    source = position.get('source', '')
    
    # Emoji basado en PnL
    emoji = "🔴" if pnl_pct < 0 else "🟢"
    pnl_sign = "" if pnl_pct < 0 else "+"
    
    # Estilo Trojan más condensado
    line = f"{emoji} *{symbol}* - {name}\n"
    line += f"Balance: *{balance:.4f}* (${value:.2f})\n"
    line += f"Precio: *${price:.8f}*"
    
    # Añadir PnL si disponible
    if 'pnl_pct' in position and 'entry_price' in position:
        entry_price = position.get('entry_price', 0)
        line += f" | PnL: *{pnl_sign}{pnl_pct:.2f}%*\n"
    else:
        line += f"\n"
    
    # Añadir información de la fuente y tiempo de respuesta si está disponible
    if source or response_time:
        info_line = "_"
        if source:
            info_line += f"via {source}"
        if response_time:
            if source:
                info_line += f" en {response_time}"
            else:
                info_line += f"respuesta en {response_time}"
        info_line += "_\n"
        line += info_line
    
    return line

# ───────── Positions Handler ─────────
async def get_token_position(pubkey: str, mint: str) -> dict:
    """Obtiene la posición actual de un token para un wallet"""
    try:
        # Obtener balance del token
        log.info(f"Obteniendo posición del token {mint} para wallet {pubkey}")
        
        # Forzar reintento hasta 3 veces para obtener balance en caso de problemas
        balance = None
        retry_count = 0
        max_retries = 3
        
        while balance is None and retry_count < max_retries:
            try:
                balance = await get_token_balance(pubkey, mint)
                log.info(f"Balance obtenido para {mint}: {balance}")
            except Exception as e:
                retry_count += 1
                log.warning(f"Error al obtener balance (intento {retry_count}/{max_retries}): {e}")
                await asyncio.sleep(0.5)  # Pequeña pausa entre reintentos
        
        # Si después de los reintentos no tenemos balance, verificar en la base de datos
        if not balance or balance <= 0:
            # Intentar verificar en la base de datos si tenemos registros de este token
            from .db import get_position_data
            
            # Encontrar el uid correspondiente a este pubkey
            uid = None
            for u_id, u_data in STATE.items():
                if get_pubkey(u_id) == pubkey:
                    uid = u_id
                    break
            
            if uid:
                position_data = get_position_data(uid, mint)
                if position_data and position_data.get('total_bought', 0) > position_data.get('total_sold', 0):
                    # Si hay más tokens comprados que vendidos según DB, forzar un balance mínimo
                    remaining = position_data['total_bought'] - position_data.get('total_sold', 0)
                    if remaining > 0:
                        log.warning(f"No se detectó balance pero hay {remaining} tokens en la base de datos. Usando este valor.")
                        balance = remaining
            
        if not balance or balance <= 0:
            log.info(f"No balance detectado para {mint}, retornando None")
            return None
            
        # Obtener datos del token
        log.info(f"Obteniendo datos del token {mint}")
        token_data = await get_token_stats(mint)
        
        # Obtener datos históricos de la posición
        from .db import get_position_data
        uid = None
        for u_id, u_data in STATE.items():
            if get_pubkey(u_id) == pubkey:
                uid = u_id
                break
        
        position_data = get_position_data(uid, mint) if uid else None
        log.info(f"Datos de posición para {mint}: {position_data}")
        
        # Calcular valores
        current_price = token_data.get('price', 0)
        usd_value = balance * current_price
        
        # Información de PnL
        avg_buy_price = position_data.get('avg_buy_price', 0) if position_data else 0
        total_bought = position_data.get('total_bought', 0) if position_data else 0
        total_sold = position_data.get('total_sold', 0) if position_data else 0
        
        # Calcular precio promedio de entrada
        entry_price = avg_buy_price if avg_buy_price > 0 else current_price
        
        # Calcular PnL
        pnl_pct = ((current_price / entry_price) - 1) * 100 if entry_price > 0 else 0
        pnl_usd = balance * (current_price - entry_price) if entry_price > 0 else 0
        
        # Retornar información de posición
        position_info = {
            'mint': mint,
            'symbol': token_data.get('sym', '???'),
            'name': token_data.get('name', 'Unknown Token'),
            'balance': balance,
            'price_usd': current_price,
            'value_usd': usd_value,
            'lp': token_data.get('lp', 0),
            'mc': token_data.get('real_time_mc', 0),
            'entry_price': entry_price,
            'pnl_pct': pnl_pct,
            'pnl_usd': pnl_usd,
            'total_bought': total_bought,
            'total_sold': total_sold,
            'avg_buy_price': avg_buy_price
        }
        
        # Añadir información de tiempo de respuesta y fuente si está disponible
        if 'response_time' in token_data:
            position_info['response_time'] = token_data['response_time']
        if 'source' in token_data:
            position_info['source'] = token_data['source']
        
        log.info(f"Posición completa para {mint}: Balance={balance}, Precio=${current_price}, PnL={pnl_pct}%")
        return position_info
    except Exception as e:
        log.error(f"Error al obtener posición para {mint}: {e}")
        log.error(traceback.format_exc())
        return None

async def get_all_positions(pubkey: str) -> list:
    """Obtiene todas las posiciones (tokens) que tiene un usuario"""
    # Lista de tokens conocidos (mint addresses)
    tokens = await get_user_tokens(pubkey)
    log.info(f"Tokens encontrados para {pubkey}: {len(tokens)}")
    
    # Para cada token, obtener detalles
    positions = []
    
    # Procesar en paralelo para mayor velocidad
    async def get_position(token):
        return await get_token_position(pubkey, token)
    
    tasks = [get_position(token) for token in tokens]
    results = await asyncio.gather(*tasks)
    
    # Filtrar y añadir solo posiciones válidas
    positions = [pos for pos in results if pos and pos.get("value_usd", 0) > 0.01]
    
    # Actualizar datos de marketcap en tiempo real para tokens importantes
    if positions:
        # Seleccionar los tokens más valiosos para obtener datos de marketcap
        top_tokens = sorted(positions, key=lambda x: x.get("value_usd", 0), reverse=True)[:5]
        mint_addresses = [pos["mint"] for pos in top_tokens]
        
        # Obtener datos de marketcap en paralelo
        async def get_mcap(mint):
            try:
                # Verificar caché primero
                if mint in tokens_market_data and time.time() - tokens_market_data[mint].get("timestamp", 0) < 60:
                    return mint, tokens_market_data[mint]
                    
                # Si no hay datos en caché o están obsoletos, obtener nuevos
                data = await market_monitor.get_marketcap_realtime(mint)
                tokens_market_data[mint] = data
                return mint, data
            except Exception as e:
                log.error(f"Error al obtener marketcap para {mint}: {e}")
                return mint, {}
        
        # Ejecutar consultas en paralelo
        mcap_tasks = [get_mcap(mint) for mint in mint_addresses]
        mcap_results = await asyncio.gather(*mcap_tasks)
        
        # Actualizar posiciones con datos de marketcap
        mcap_data = dict(mcap_results)
        for pos in positions:
            if pos["mint"] in mcap_data:
                data = mcap_data[pos["mint"]]
                if data:
                    # Actualizar datos importantes
                    if data.get("priceUsd", 0) > 0:
                        pos["price"] = data["priceUsd"]
                    if data.get("marketCapUsd", 0) > 0:
                        pos["marketcap"] = data["marketCapUsd"]
                    if data.get("name") and not pos.get("name"):
                        pos["name"] = data["name"]
                    if data.get("symbol") and not pos.get("symbol"):
                        pos["symbol"] = data["symbol"]
                    # Recalcular valor total con precio actualizado
                    if data.get("priceUsd", 0) > 0:
                        pos["value_usd"] = pos["amount"] * data["priceUsd"]
    
    # Ordenar por valor USD (de mayor a menor)
    return sorted(positions, key=lambda x: x.get("value_usd", 0), reverse=True)

async def show_positions(update, context=None, is_refreshing=False):
    """Muestra las posiciones actuales del usuario con estilo Trojan"""
    # Determinar si estamos respondiendo a un comando o callback
    if isinstance(update, Update):
        uid = update.effective_user.id
        msg = update.message
    else:
        # Es un callback query
        uid = update.from_user.id
        msg = update

    ensure(uid)
    pubkey = get_pubkey(uid)
    
    # Mostrar mensaje de carga
    loading_message = "⏳ *Cargando posiciones...*\n\n_Recuperando datos de tus tokens en tiempo real..._\n\nEste proceso puede tomar algunos segundos dependiendo de la cantidad de tokens."
    if hasattr(msg, 'edit_text'):
        resp = await msg.edit_text(loading_message, parse_mode="Markdown")
    elif hasattr(msg, 'reply_text'):
        resp = await msg.reply_text(loading_message, parse_mode="Markdown")
    else:
        # Para objetos CallbackQuery sin attribute reply_text
        resp = await msg.message.edit_text(loading_message, parse_mode="Markdown")
    
    # Mostrar "escribiendo..." mientras procesamos
    if hasattr(update, 'effective_chat'):
        await update._bot.send_chat_action(update.effective_chat.id, action="typing")
    
    # Obtener balance de SOL y precio
    sol_balance, sol_price = await asyncio.gather(
        get_sol_balance(pubkey),
        get_sol_price_usd_qn()
    )
    sol_value = sol_balance * sol_price
    
    # Obtener posiciones de tokens
    start_time = time.time()
    positions = await get_all_positions(pubkey)
    fetch_time = time.time() - start_time
    
    # Calcular valor total del portfolio
    total_value = sol_value
    for pos in positions:
        total_value += pos.get('value_usd', 0)
    
    # Construir mensaje con estilo Trojan
    message = f"📊 *POSICIONES* 📊\n\n"
    
    # Sección Wallet (formato Trojan)
    message += f"👛 *Wallet*\n"
    message += f"`{pubkey}`\n"
    message += f"_Toca para copiar_\n\n"
    
    # Separador visual
    message += f"───────────────────\n\n"
    
    # Sección Balance (formato Trojan)
    message += f"💰 *Balance:* {sol_balance:.4f} SOL (${sol_value:.2f})\n"
    if positions:
        message += f"🪙 *Tokens:* {len(positions)}\n"
        message += f"💵 *Total:* ${total_value:.2f}\n\n"
    else:
        message += f"🪙 *No tienes tokens*\n"
        message += f"💵 *Total:* ${total_value:.2f}\n\n"
    
    # Separador visual
    message += f"───────────────────\n\n"
    
    # Añadir posiciones (estilo Trojan)
    if positions:
        message += f"🔸 *TUS TOKENS* 🔸\n\n"
        
        for i, pos in enumerate(positions):
            if i >= 5:  # Limitar a 5 posiciones para evitar mensajes muy largos
                message += f"\n_...y {len(positions) - 5} más_"
                break
                
            message += format_token_line(pos) + "\n"
        
        # Separador visual
        message += f"───────────────────\n"
    else:
        message += "_No tienes tokens en tu wallet._\n"
        message += "_Usa el botón *Comprar* para adquirir tokens._\n\n"
        
        # Separador visual
        message += f"───────────────────\n"
    
    # Añadir nota sobre actualización de datos
    message += f"\n_Datos obtenidos en {fetch_time:.1f}s. Haz click en Actualizar para refrescar._"
    
    # Mostrar mensaje
    if hasattr(update, 'edit_message_text'):
        await update.edit_message_text(message, reply_markup=positions_kb(is_refreshing), parse_mode="Markdown")
    else:
        await update.message.reply_text(message, reply_markup=positions_kb(is_refreshing), parse_mode="Markdown")

# ───────── Commands ─────────
async def start(u: Update, _):
    uid = u.effective_user.id
    ensure(uid)
    
    # Verificar si el usuario ya existe, si no, crear una wallet
    if not user_exists(uid):
        add_user(uid, create_wallet())
    
    # Obtener la dirección de wallet del usuario
    pubkey = get_pubkey(uid)
    
    # Mostrar el mensaje de bienvenida con el menú principal
    header = await wallet_header(pubkey)
    await u.message.reply_text(
        header,
        reply_markup=main_kb(),
        parse_mode="Markdown"
    )

async def positions_cmd(u: Update, _):
    """Comando para mostrar posiciones"""
    await show_positions(u)

async def marketcap_cmd(u: Update, ctx):
    """Comando para obtener marketcap en tiempo real de un token"""
    text = u.message.text.strip()
    parts = text.split(' ', 1)
    
    # Verificar si se proporcionó un token
    if len(parts) > 1:
        text = parts[1].strip()
        mint, pool = await extract(text)
        
        if not mint:
            await u.message.reply_text("❌ No se pudo identificar el token. Envía una dirección de token válida o un enlace de DexScreener/Pump.fun.")
            return
    else:
        await u.message.reply_text("❌ Envía la dirección del token o un enlace después del comando, ejemplo:\n/mcap GRLAEHVGfQMGDFuEk5JLGzZDsJckJUNrfh2y8pTsebPL")
        return
    
    # Mensaje de espera
    msg = await u.message.reply_text("⏳ Obteniendo datos de marketcap en tiempo real...")
    
    try:
        # Verificar caché
        if mint in tokens_market_data and time.time() - tokens_market_data[mint].get("timestamp", 0) < 30:
            # Usar datos en caché si son recientes (menos de 30 segundos)
            data = tokens_market_data[mint]
            source = f"{data.get('source', 'desconocida')} (caché)"
        else:
            # Obtener datos frescos
            data = await market_monitor.get_marketcap_realtime(mint)
            # Actualizar caché
            tokens_market_data[mint] = data
            source = data.get('source', 'desconocida')
        
        # Crear mensaje con los datos
        name = data.get('name', '???')
        symbol = data.get('symbol', '???')
        price = data.get('priceUsd', 0)
        mcap = data.get('marketCapUsd', 0)
        liquidity = data.get('liquidity', 0)
        volume = data.get('volume24h', 0)
        fetch_time = data.get('fetch_time_ms', 0)
        
        # Crear mensaje formateado
        text = f"📊 <b>{name} ({symbol})</b>\n\n"
        text += f"🏷️ <code>{mint}</code>\n\n"
        text += f"💰 <b>Precio:</b> ${price:.10f}\n"
        text += f"📈 <b>Market Cap:</b> ${mcap:,.2f}\n"
        
        if liquidity > 0:
            text += f"💧 <b>Liquidez:</b> ${liquidity:,.2f}\n"
        
        if volume > 0:
            text += f"📊 <b>Volumen 24h:</b> ${volume:,.2f}\n"
        
        text += f"\n<i>Fuente: {source} | Tiempo: {fetch_time}ms</i>"
        
        await msg.edit_text(text, parse_mode=ParseMode.HTML)
    
    except Exception as e:
        log.error(f"Error en comando marketcap: {e}")
        await msg.edit_text(f"❌ Error al obtener datos de marketcap: {str(e)}")

async def on_msg(u: Update, _):
    uid = u.effective_user.id; ensure(uid)
    st  = STATE[uid]; txt = (u.message.text or "").strip()

    if st["step"] == Step.CUSTOM:
        try:
            # Validar que el número sea válido y convertirlo a float
            amount = float(txt.replace(",","."))
            
            # Validar que la cantidad sea positiva
            if amount <= 0:
                await u.message.reply_text("❌ La cantidad debe ser mayor que cero.")
                return
                
            # Guardar la cantidad en el estado
            st["amount"] = amount
            st["step"] = Step.TOKEN
            
            # Mostrar la interfaz de compra actualizada con la cantidad personalizada
            await show_buy(u, edit=False)
            
            # Enviar un mensaje de confirmación
            await u.message.reply_text(f"✅ Cantidad establecida: {amount} SOL\nSelecciona 'COMPRAR' para finalizar la transacción.")
        except ValueError:
            await u.message.reply_text("❌ Número inválido. Ingresa una cantidad válida en SOL (por ejemplo: 0.5, 1.2, etc).")
        return
        
    if st["step"] == Step.SELL_CUSTOM:
        try:
            # Validar que sea un porcentaje válido
            sell_pct = float(txt.replace(",","."))
            if sell_pct <= 0 or sell_pct > 100:
                await u.message.reply_text("❌ El porcentaje debe estar entre 1 y 100.")
                return
                
            # Actualizar estado
            st["sell_pct"] = sell_pct
            st["step"] = Step.SELL
            
            # Actualizar datos del token
            token_data = st["token_data"]
            token_data["sell_pct"] = sell_pct
            
            # Mostrar interfaz de venta actualizada
            await show_sell_token(u, token_data)
        except:
            await u.message.reply_text("❌ Número inválido.")
        return

    # Caso especial: verificar si el texto completo es un mint address
    if re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", txt):
        log.info(f"Detectado mensaje que es exactamente un mint address: {txt}")
        mint, pool = txt, None
    # Iniciar análisis y enviar mensaje de "analizando" solo si no es un comando o respuesta corta
    elif len(txt) > 4 and not txt.startswith("/"):
        # Iniciar la extracción de tokens/address en paralelo para no bloquear la UI
        extract_task = asyncio.create_task(extract(txt))
        
        # Enviar mensaje de análisis (opcional, desactivar si se prefiere sin mensajes intermedios)
        """
        analyzing_message = await u.message.reply_text(
            "⏳ *Analizando enlace...*", 
            parse_mode="Markdown",
            disable_notification=True
        )
        """
        
        # Obtener resultado de la extracción
        mint, pool = await extract_task
    else:
        mint, pool = await extract(txt)
        
    if not mint:
        return

    # Verificar si el usuario tiene este token para vender
    pubkey = get_pubkey(uid)
    
    # Iniciar mensaje de carga rápidamente
    loading_msg = await u.message.reply_text(
        "⏳ *Cargando datos del token...*", 
        parse_mode="Markdown"
    )
    
    try:
        # Consultar balance en paralelo mientras preparamos la UI
        log.info(f"Verificando si el usuario {uid} tiene el token {mint}")
        
        # Utilizar la función get_position que es más robusta
        token_position = await get_token_position(pubkey, mint)
        
        # Determinar si el usuario tiene el token basado en la información de posición
        has_token = False
        balance = 0
        if token_position:
            has_token = True
            balance = token_position.get('balance', 0)
            log.info(f"Usuario tiene balance de {balance} tokens {mint}")
        else:
            log.info(f"Usuario no tiene tokens {mint} o no se pudo detectar")
        
        # Guardar info en el estado
        st.update(
            step=Step.TOKEN,
            mint=mint, 
            pool=pool,
            amount=None
        )
                
        # Obtener estadísticas en segundo plano
        stats_task = asyncio.create_task(get_token_stats(mint, pool=pool))
        st["stats"] = await stats_task
        
        # Si el usuario tiene tokens, guardar también esa información
        if has_token and balance > 0:
            log.info(f"Guardando información del token {mint} que posee el usuario")
            st["token_data"] = token_position
        
        # Borrar mensaje de carga
        await loading_msg.delete()
        
        # Mostrar interfaz unificada de compra/venta
        await show_buy(u, edit=False)
    except Exception as e:
        log.error(f"Error al procesar token: {e}")
        await loading_msg.edit_text(f"❌ Error al cargar el token: {str(e)}")

# Función para mostrar tokens disponibles para vender
async def show_sell_tokens(update):
    """Muestra una lista de tokens que el usuario puede vender"""
    uid = update.from_user.id
    ensure(uid)
    
    pubkey = get_pubkey(uid)
    
    # Mostrar mensaje de carga
    loading_message = "⏳ *Cargando tus tokens...*\n\n_Recuperando información de tus tokens en tiempo real..._"
    if hasattr(update, 'edit_message_text'):
        await update.edit_message_text(loading_message, parse_mode="Markdown")
    else:
        await update.message.reply_text(loading_message, parse_mode="Markdown")
    
    # Obtener posiciones del usuario
    positions = await get_all_positions(pubkey)
    
    if not positions:
        # No tiene tokens para vender
        message = "❌ *No tienes tokens para vender*\n\nUsa el botón 'Comprar' para adquirir tokens primero."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Comprar", callback_data="BUY")],
            [InlineKeyboardButton("↩️ Volver", callback_data="BACK")]
        ])
        
        if hasattr(update, 'edit_message_text'):
            await update.edit_message_text(message, reply_markup=kb, parse_mode="Markdown")
        else:
            await update.message.reply_text(message, reply_markup=kb, parse_mode="Markdown")
        return
    
    # Ordenar por valor (mayor a menor)
    positions.sort(key=lambda x: x.get("value_usd", 0), reverse=True)
    
    # Construir mensaje
    message = "*🔴 Selecciona un token para vender:*\n\n"
    
    # Crear teclado con los tokens
    buttons = []
    for pos in positions:
        symbol = pos.get("symbol", "???")
        name = pos.get("name", "Unknown")
        balance = pos.get("balance", 0)
        value = pos.get("value_usd", 0)
        mint = pos.get("mint", "")
        
        # Añadir solo tokens con balance positivo
        if balance > 0 and mint:
            # Añadir línea en el mensaje
            message += f"• {symbol}: {balance:.4f} (${value:.2f})\n"
            
            # Crear botón para este token
            btn_text = f"{symbol} - {balance:.4f}"
            buttons.append([InlineKeyboardButton(btn_text, callback_data=f"SELL_TOKEN_{mint}")])
    
    # Añadir botón de volver
    buttons.append([InlineKeyboardButton("↩️ Volver", callback_data="BACK")])
    kb = InlineKeyboardMarkup(buttons)
    
    # Mostrar mensaje con teclado
    if hasattr(update, 'edit_message_text'):
        await update.edit_message_text(message, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(message, reply_markup=kb, parse_mode="Markdown")

# ───────── Show BUY ─────────
async def show_buy(target, *, edit=True, is_refreshing=False):
    try:
        # Corregir la obtención del user_id para manejar diferentes tipos de objetos
        if hasattr(target, 'from_user'):
            uid = target.from_user.id
        elif hasattr(target, 'effective_user'):
            uid = target.effective_user.id
        else:
            log.error(f"No se pudo determinar el user_id en show_buy: {type(target)}")
            return
        
        ensure(uid)
        
        msg_id = target.message.message_id if hasattr(target, "message") else target.message_id
        
        # Obtener datos del state
        st = STATE[uid]
        mint = st.get("mint")
        sel = st.get("amount")
        
        # Si no hay mint seleccionado, o se usó /buy command, reiniciar flujo
        if not mint:
            await target.message.reply_text("Por favor, envía el mint address del token que quieres comprar.",
                                          reply_markup=BACK_KB)
            return
        
        # Si estamos editando un mensaje existente, primero mostrar un mensaje de carga
        # para mejorar la experiencia de usuario y evitar impresión de timeout
        if edit and not is_refreshing:
            try:
                await target.edit_message_text(
                    "⏳ *Cargando datos...*",
                    parse_mode="Markdown"
                )
            except Exception as e:
                # Ignorar errores aquí, solo intentamos mejorar la UX
                pass
            
        # Formateo de valores numéricos
        def fmt_usd(val: float, prefix="$") -> str:
            """Formato para valores USD, con prefijo personalizable"""
            if val >= 1_000_000_000:
                return f"{prefix}{val/1_000_000_000:.2f}B"
            elif val >= 1_000_000:
                return f"{prefix}{val/1_000_000:.2f}M"
            elif val >= 1_000:
                return f"{prefix}{val/1_000:.2f}K"
            else:
                return f"{prefix}{val:.2f}"

        def fmt_sol(val: float) -> str:
            """Formato para valores SOL"""
            return f"{val:.4f} SOL"
        
        # Iniciar todas las consultas en paralelo para máxima velocidad
        # 1. Obtener datos actualizados de marketcap en tiempo real
        realtime_data = None
        fresh_data_task = None
        
        # Comprobar caché primero para respuesta instantánea
        if mint in tokens_market_data and time.time() - tokens_market_data[mint].get("timestamp", 0) < 60:
            realtime_data = tokens_market_data[mint]
            log.info(f"Usando datos de marketcap en caché para {mint}")
        else:
            # Si no hay caché o es antigua, iniciar consulta en segundo plano
            fresh_data_task = asyncio.create_task(market_monitor.get_marketcap_realtime(mint))
            log.info(f"Iniciando consulta de datos de marketcap en tiempo real para {mint}")
        
        # 2. Recuperar datos básicos del estado
        data = st.get("stats", {})
        
        # Registrar para depuración qué datos tenemos
        log.info(f"Datos de token disponibles en show_buy: {bool(data)}, mint: {mint}")
        
        # 3. Completar solicitudes de datos en paralelo con otros datos importantes
        tasks = []
        
        # 3.1 Datos de Dex Screener si no tenemos datos suficientes
        if not data or data.get("name", "") == "Unknown Token":
            tasks.append(asyncio.create_task(_get_dexscreener_data(mint)))
            log.info(f"Solicitando datos directos de DexScreener para {mint}")
            
        # 3.2 Saldo de SOL del usuario
        pubkey = get_pubkey(uid)
        tasks.append(asyncio.create_task(get_sol_balance(pubkey)))
        
        # 3.3 Balance actual del token si ya lo tiene
        tasks.append(asyncio.create_task(get_token_balance(pubkey, mint)))
        
        # 3.4 Precio actual de SOL
        tasks.append(asyncio.create_task(get_sol_price_usd_qn()))
        
        # 3.5 Datos de Pump.fun si la consulta de tiempo real está en curso
        if fresh_data_task:
            tasks.append(fresh_data_task)
        
        # Esperar a que todas las consultas complementarias terminen
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Procesar resultados
        result_index = 0
        
        # Datos de DexScreener si fueron solicitados
        if not data or data.get("name", "") == "Unknown Token":
            dex_result = results[result_index]
            result_index += 1
            if isinstance(dex_result, dict) and dex_result.get("price", 0) > 0:
                data = dex_result
                log.info(f"Recuperación exitosa directa desde DexScreener: {data.get('name', 'Unknown')}")
        
        # Saldo de SOL
        sol_balance = results[result_index] if not isinstance(results[result_index], Exception) else 0
        result_index += 1
        
        # Balance de token
        token_balance = results[result_index] if not isinstance(results[result_index], Exception) else 0
        result_index += 1
        
        # Verificar si tenemos balance inicial de token
        have_token = token_balance > 0
        log.info(f"Balance inicial detectado para {mint}: {token_balance}")
        
        # Precio de SOL
        sol_price = results[result_index] if not isinstance(results[result_index], Exception) else 100.0
        result_index += 1
        
        # Datos en tiempo real si estaban pendientes
        if fresh_data_task:
            real_result = results[result_index]
            if not isinstance(real_result, Exception) and real_result.get("marketCapUsd", 0) > 0:
                realtime_data = real_result
                # Actualizar caché
                tokens_market_data[mint] = realtime_data
                log.info(f"Datos de marketcap actualizados en tiempo real para {mint}")
        
        # Actualizar datos con información en tiempo real si está disponible
        if realtime_data and realtime_data.get("marketCapUsd", 0) > 0:
            if not data:
                data = {}
            
            # Actualizar campos importantes
            if realtime_data.get("priceUsd", 0) > 0:
                data["price"] = realtime_data["priceUsd"]
            
            data["marketcap"] = realtime_data["marketCapUsd"]
            
            if realtime_data.get("name"):
                data["name"] = realtime_data["name"]
                
            if realtime_data.get("symbol"):
                data["symbol"] = realtime_data["symbol"]
                
            if realtime_data.get("liquidity", 0) > 0:
                data["liquidity"] = realtime_data["liquidity"]
                
            if realtime_data.get("volume24h", 0) > 0:
                data["volume24h"] = realtime_data["volume24h"]
                
            # Añadir fuente para mostrarla en la UI
            data["source"] = realtime_data.get("source", "tiempo real")
            data["fetch_time_ms"] = realtime_data.get("fetch_time_ms", 0)
        
        # Si aún no tenemos datos suficientes, intentar método alternativo
        if not data or not data.get("price", 0) > 0:
            log.warning(f"Datos insuficientes, intentando recuperación final con get_token_stats")
            try:
                data = await get_token_stats(mint, force_fresh=True)
            except Exception as e:
                log.error(f"Error en recuperación final: {e}")
        
        # Verificar datos mínimos para mostrar
        if not data or not data.get("price", 0) > 0:
            # No pudimos obtener datos, mostrar error
            if edit:
                await target.edit_message_text(
                    "❌ *No se pudieron obtener datos para este token*\n\n"
                    "El token podría no estar listado o no tener suficiente liquidez.\n\n"
                    f"Mint address: `{mint}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Volver", callback_data="BACK")]]),
                    parse_mode="Markdown"
                )
            else:
                await target.message.reply_text(
                    "❌ *No se pudieron obtener datos para este token*\n\n"
                    "El token podría no estar listado o no tener suficiente liquidez.\n\n"
                    f"Mint address: `{mint}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Volver", callback_data="BACK")]]),
                    parse_mode="Markdown"
                )
            return
        
        # Usar datos disponibles
        name = data.get("name", "Unknown Token")
        symbol = data.get("symbol", data.get("sym", "???"))
        price = data.get("price", 0)
        volume = data.get("volume24h", data.get("vol", 0))
        liquidity = data.get("liquidity", data.get("lp", 0))
        marketcap = data.get("marketcap", data.get("mc", data.get("real_time_mc", 0)))
        data_source = data.get("source", "desconocido")
        fetch_time = data.get("fetch_time_ms", 0)
        price_change = data.get("price_diff_pct", 0)
        
        # Elegir cantidad de SOL a invertir si no está seleccionada
        if sel is None:
            sel = 0.5  # Valor por defecto
        
        # Calcular valor en USD basado en precio SOL
        sol_value_usd = sel * sol_price
        
        # Obtener estimación de tokens a recibir
        amount_out = await calculate_output_amount(mint, sel)
        
        # Calcular valor en USD de los tokens
        token_value_usd = amount_out * price
        
        # Calcular slippage efectivo
        slippage_pct = ((sol_value_usd - token_value_usd) / sol_value_usd) * 100 if sol_value_usd > 0 else 0
        
        # Calcular valor actual de la posición si ya tiene el token
        current_value_usd = token_balance * price
        
        # Obtener datos adicionales de posición si el usuario ya tiene este token
        position_data = None
        token_data = {}
        if token_balance > 0:
            log.info(f"Usuario tiene {token_balance} tokens de {mint}, obteniendo datos de posición")
            from .db import get_position_data
            position_data = get_position_data(uid, mint)
            log.info(f"Datos de posición obtenidos: {position_data}")
            
            # Si tenemos token_data en el estado, usarlo
            if "token_data" in st:
                token_data = st["token_data"]
            # Si no, crear un diccionario con los datos básicos
            else:
                token_data = {
                    'mint': mint,
                    'symbol': symbol,
                    'name': name,
                    'balance': token_balance,
                    'price_usd': price,
                    'value_usd': current_value_usd
                }
                
                # Agregar información de PnL si tenemos datos de posición
                if position_data and position_data.get("avg_buy_price", 0) > 0:
                    entry_price = position_data.get("avg_buy_price", 0)
                    pnl_pct = ((price / entry_price) - 1) * 100 if entry_price > 0 else 0
                    pnl_usd = current_value_usd - (token_balance * entry_price) if entry_price > 0 else 0
                    
                    token_data.update({
                        'entry_price': entry_price,
                        'pnl_pct': pnl_pct,
                        'pnl_usd': pnl_usd
                    })
        else:
            log.info(f"Usuario no tiene tokens de {mint}, saltando datos de posición")

        # Construir mensaje unificado con toda la información relevante
        from .unified_interface import build_unified_message, unified_keyboard
        
        msg = build_unified_message(
            token_data=token_data,
            user_balance=token_balance,
            symbol=symbol,
            name=name,
            mint=mint,
            price=price,
            marketcap=marketcap,
            volume=volume,
            liquidity=liquidity,
            price_change=price_change,
            sol_balance=sol_balance,
            sol_price=sol_price,
            selected_sol=sel,
            estimated_tokens=amount_out,
            current_value_usd=current_value_usd,
            slippage_pct=slippage_pct,
            data_source=data_source,
            fetch_time=fetch_time
        )

        # Crear teclado unificado que muestra tanto opciones de compra como de venta
        keyboard = unified_keyboard(
            symbol=symbol,
            sel=sel,
            token_balance=token_balance,
            is_refreshing=is_refreshing
        )
        
        # Mostrar mensaje con teclado de opciones
        if edit:
            await target.edit_message_text(
                msg,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
        else:
            await target.message.reply_text(
                msg,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            
    except Exception as e:
        log.error(f"Error en show_buy: {e}")
        log.error(traceback.format_exc())
        
        # Intentar enviar mensaje de error
        try:
            if edit:
                await target.edit_message_text(
                    f"❌ *Error al cargar datos del token*\n\n"
                    f"Por favor intenta de nuevo: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Volver", callback_data="BACK")]]),
                    parse_mode="Markdown"
                )
            else:
                await target.message.reply_text(
                    f"❌ *Error al cargar datos del token*\n\n"
                    f"Por favor intenta de nuevo: {str(e)}",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Volver", callback_data="BACK")]]),
                    parse_mode="Markdown"
                )
        except Exception:
            # Si incluso enviar el mensaje de error falla, no hacer nada más
            pass

# ───────── Callbacks ─────────
async def get_dexscreener_token_data(token_or_pair: str, is_pair: bool = False) -> dict | None:
    """
    Obtiene información detallada de un token o par desde DexScreener
    
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
            
        log.info(f"Consultando DexScreener: {url}")
        
        # Configurar timeout y headers
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Cache-Control": "no-cache"
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
                        "source": "DexScreener",
                        "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                        "pairs": len(pairs)
                    }
                else:
                    log.error(f"Error al consultar DexScreener API: {r.status}")
    except Exception as e:
        log.error(f"Error obteniendo datos de DexScreener: {str(e)}")
    
    return None

async def force_pump_data(mint: str) -> dict:
    """
    Obtiene datos de token específicamente desde fuentes en tiempo real con mínima latencia.
    """
    # Iniciar el temporizador para medir latencia
    start_time = time.time()
    log.info(f"Forzando datos en tiempo real para {mint}")
    
    # Asegurar que las cachés están limpias
    from .quicknode_client import _token_cache
    for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_", "token_stats_"]:
        cache_key = f"{key_prefix}{mint}"
        if cache_key in _token_cache:
            del _token_cache[cache_key]
            log.info(f"Caché forzosamente eliminada: {cache_key}")
    
    # Ejecutar múltiples consultas en paralelo para minimizar tiempo de espera
    tasks = []
    
    # 1. Consulta DexScreener (generalmente la más rápida)
    from .token_info import _get_dexscreener_data
    tasks.append(asyncio.create_task(_get_dexscreener_data(mint)))
    
    # 2. Consulta PumpFun (datos más precisos)
    from .token_info import _get_pumpfun_data
    tasks.append(asyncio.create_task(_get_pumpfun_data(mint)))
    
    # 3. Consulta QuickNode (respaldo)
    from .quicknode_client import fetch_pumpfun
    if 'fetch_pumpfun' in globals():
        tasks.append(asyncio.create_task(fetch_pumpfun(mint, force_fresh=True)))
    
    # Lista para almacenar resultados
    all_results = []
    
    try:
        # Esperar resultados con timeout agresivo
        done, pending = await asyncio.wait(
            tasks, 
            timeout=2.0,  # Timeout extendido para garantizar datos
            return_when=asyncio.ALL_COMPLETED  # Esperar todos los resultados para máxima calidad
        )
        
        # Cancelar tareas pendientes después del timeout
        for task in pending:
            task.cancel()
        
        # Procesar todos los resultados completados
        for task in done:
            try:
                result = task.result()
                if result and (result.get("marketCapUsd", 0) > 0 or result.get("mc", 0) > 0 or result.get("price", 0) > 0):
                    all_results.append(result)
                    log.info(f"✅ Datos obtenidos de {result.get('source', 'desconocida')} en {time.time() - start_time:.2f}s")
            except Exception as e:
                log.debug(f"Error en tarea de datos: {e}")
    except Exception as e:
        log.error(f"Error general en consulta paralela: {e}")
    
    # Si tenemos resultados, seleccionar el mejor
    if all_results:
        # Normalizar resultados para comparación uniforme
        normalized_results = []
        for result in all_results:
            # Crear diccionario normalizado con todos los valores posibles
            normalized = {
                "price": result.get("price", result.get("priceUsd", 0)),
                "mc": result.get("mc", result.get("marketCapUsd", result.get("real_time_mc", 0))),
                "symbol": result.get("symbol", result.get("sym", "")),
                "name": result.get("name", ""),
                "source": result.get("source", "desconocida"),
                "liquidity": result.get("liquidity", result.get("lp", 0)),
                "volume": result.get("volume24h", result.get("vol", 0)),
                "price_change": result.get("price_diff_pct", 0),
                "original_data": result
            }
            normalized_results.append(normalized)
        
        # Filtrar por resultados con MC o precio
        valid_results = [r for r in normalized_results if r["mc"] > 0 or r["price"] > 0]
        
        if valid_results:
            # Preferir resultado con mejor MC o con precio no nulo
            if any(r["mc"] > 0 for r in valid_results):
                best_result = max([r for r in valid_results if r["mc"] > 0], key=lambda x: x["mc"])
            else:
                best_result = max(valid_results, key=lambda x: x["price"])
            
            # Obtener los datos originales
            token_data = best_result["original_data"]
            
            # Asegurar que tenemos todas las claves necesarias
            final_data = {
                "name": token_data.get("name", best_result["name"]),
                "sym": token_data.get("sym", best_result["symbol"]),
                "symbol": best_result["symbol"],
                "price": best_result["price"],
                "mc": best_result["mc"],
                "real_time_mc": best_result["mc"],  # Asegurando que usamos el MC en tiempo real
                "lp": best_result["liquidity"],
                "vol": best_result["volume"],
                "price_sol": token_data.get("price_sol", 0),
                "source": best_result["source"],
                "price_diff_pct": best_result["price_change"],
                "refresh_time": time.strftime("%H:%M:%S", time.localtime()),
                "latency": time.time() - start_time,
                "timestamp": int(time.time()),
                "forced": True,
                "fresh": True
            }
            
            log.info(f"Datos de token obtenidos en {final_data['latency']:.2f}s - MC: ${final_data['mc']:,.2f} de {final_data['source']}")
            return final_data
    
    # Si todas las consultas paralelas fallan, intentar con el método tradicional
    try:
        from .token_info import get_token_stats
        token_data = await get_token_stats(mint, force_fresh=True)
        
        if token_data:
            token_data["latency"] = time.time() - start_time
            token_data["refresh_time"] = time.strftime("%H:%M:%S", time.localtime())
            token_data["forced"] = True
            token_data["fresh"] = True
            log.info(f"Datos obtenidos con método tradicional en {token_data['latency']:.2f}s")
            return token_data
    except Exception as e:
        log.error(f"Error también en método tradicional: {e}")
    
    # Devolver datos mínimos si todo falla
    return {
        "name": "Unknown Token",
        "sym": "???",
        "symbol": "???",
        "price": 0,
        "mc": 0,
        "real_time_mc": 0,
        "lp": 0,
        "vol": 0,
        "source": "No se pudieron obtener datos",
        "latency": time.time() - start_time,
        "forced": True,
        "error": True
    }

async def cb_router(u: Update, _):
    q    = u.callback_query; await q.answer()
    uid  = q.from_user.id; ensure(uid)
    st   = STATE[uid]; data = q.data

    if data == "BACK":
        STATE[uid] = {"step": Step.IDLE}
        await q.edit_message_text(await wallet_header(get_pubkey(uid)),
                                  reply_markup=MAIN_KB, parse_mode="Markdown")
        return

    if data == "COPY_WALLET":
        # Responder con el texto copiable de la wallet
        pubkey = get_pubkey(uid)
        await q.answer(f"Wallet copiada: {pubkey}", show_alert=True)
        # Volver a mostrar el menú principal con un mensaje de confirmación
        header = await wallet_header(pubkey)
        copy_confirmation = f"{header}\n✅ *¡Wallet copiada al portapapeles!*"
        
        await q.edit_message_text(copy_confirmation, reply_markup=MAIN_KB, parse_mode="Markdown")
        return
        
    if data == "EXPORT":
        b58, arr = exports(load_wallet(uid))
        # Mensaje mejorado para exportar claves
        export_message = "🔐 *EXPORTAR CLAVES PRIVADAS* 🔐\n\n"
        export_message += "⚠️ *ADVERTENCIA*: Nunca compartas estas claves con nadie.\n\n"
        export_message += "📌 *Base58*\n`" + b58 + "`\n\n"
        export_message += "📌 *JSON*\n`" + arr + "`"
        
        await q.edit_message_text(export_message,
                                reply_markup=BACK_KB, parse_mode="Markdown")
        return
        
    if data == "POSITIONS":
        await show_positions(q)
        return
        
    if data == "HELP":
        # Añadimos un mensaje de ayuda más detallado
        help_message = "📚 *GUÍA DE USO* 📚\n\n"
        help_message += "• *Comprar* - Compra tokens en Solana\n"
        help_message += "• *Vender* - Vende tokens que ya tienes\n"
        help_message += "• *Posiciones* - Ver tus tokens y balances\n"
        help_message += "• *Refrescar* - Actualiza el precio de SOL y balance\n"
        help_message += "• *Copiar Wallet* - Copia tu dirección\n"
        help_message += "• *Exportar* - Exportar claves privadas\n\n"
        help_message += "✨ *Para comprar un token*, envía su mint address"
        
        await q.edit_message_text(help_message,
                                reply_markup=BACK_KB, parse_mode="Markdown")
        return
        
    if data == "REFRESH_MAIN":
        # Mostrar mensaje de carga con teclado actualizado
        await q.edit_message_text(
            "⏳ *Actualizando datos en tiempo real...*\n\n"
            "_Forzando refresco del balance y precio de SOL..._",
            reply_markup=main_kb(True),  # Mostrar teclado con botón de actualización en estado de carga
            parse_mode="Markdown"
        )
        
        # Mostrar "escribiendo..." mientras procesamos
        if hasattr(q, 'message') and hasattr(q.message, 'chat'):
            await u._bot.send_chat_action(q.message.chat.id, action="typing")
            
        try:
            # Limpiar TODAS las cachés relevantes
            from .quicknode_client import _token_cache
            
            # 1. Forzar actualización del precio SOL eliminando su caché
            cache_key = "sol_price_cache"
            if cache_key in _token_cache:
                del _token_cache[cache_key]
            
            # 2. Limpiar cachés de cualquier token que se esté visualizando
            if uid in STATE and STATE[uid].get("stats") and STATE[uid].get("stats").get("mint"):
                mint = STATE[uid]["stats"]["mint"]
                # Limpiar todas las posibles cachés de este token
                for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_"]:
                    cache_key = f"{key_prefix}{mint}"
                    if cache_key in _token_cache:
                        del _token_cache[cache_key]
                        log.info(f"Caché eliminada: {cache_key}")
            
            # 3. También limpiar cachés de tokens populares para forzar refresco
            from .quicknode_client import POPULAR_TOKENS, VIRAL_TOKENS
            for mint in [*POPULAR_TOKENS, *VIRAL_TOKENS]:
                cache_key = f"pumpfun_{mint}"
                if cache_key in _token_cache:
                    del _token_cache[cache_key]
            
            # Obtener datos completamente frescos
            pubkey = get_pubkey(uid)
            
            # Obtener el balance y precio con timeout reducido para respuesta más rápida
            sol_balance = asyncio.create_task(get_sol_balance(pubkey))
            sol_price = asyncio.create_task(get_sol_price_usd_qn())
            
            # Esperar a que ambas tareas terminen con un timeout agresivo
            sol_data = await asyncio.gather(sol_balance, sol_price, return_exceptions=True)
            
            # Verificar si hay excepciones y usar valores respaldo si es necesario
            if isinstance(sol_data[0], Exception):
                log.error(f"Error al obtener balance: {sol_data[0]}")
                sol_balance_value = 0
            else:
                sol_balance_value = sol_data[0]
                
            if isinstance(sol_data[1], Exception):
                log.error(f"Error al obtener precio SOL: {sol_data[1]}")
                # Intentar obtener precio desde otro método
                try:
                    sol_price_value = await get_sol_price_usd()
                except:
                    sol_price_value = 175.85  # Valor predeterminado actualizado
            else:
                sol_price_value = sol_data[1]
            
            # Construir header con datos frescos
            header = f"🏦 *SOLANA TRADING BOT* 🏦\n\n"
            header += f"*👛 Wallet*\n"
            header += f"{format_solana_address(pubkey)}\n"
            header += f"_Toca para copiar_\n\n"
            header += f"───────────────────\n\n"
            header += f"*💰 Balance:* {sol_balance_value:.6f} SOL (${sol_balance_value*sol_price_value:,.2f})\n"
            header += f"*📈 SOL Price:* ${sol_price_value}\n\n"
            header += f"───────────────────\n"
            
            # Mostrar datos actualizados
            await q.edit_message_text(
                f"{header}\n✅ *Datos actualizados en tiempo real*\n_Caché limpiada completamente_",
                reply_markup=MAIN_KB,
                parse_mode="Markdown"
            )
            
            # Notificar al usuario
            await q.answer("✅ Todos los datos actualizados al instante", show_alert=False)
            
        except Exception as e:
            log.error(f"Error al actualizar datos: {e}")
            
            # En caso de error, mostrar mensaje y volver a pantalla principal
            await q.edit_message_text(
                await wallet_header(get_pubkey(uid)),
                reply_markup=MAIN_KB,
                parse_mode="Markdown"
            )
            
            await q.answer(f"❌ Error al actualizar: {str(e)[:50]}", show_alert=True)
        return
            
    if data == "BUY" or data == "BUY_DETECTED_TOKEN":
        if data == "BUY_DETECTED_TOKEN":
            # Ya tenemos el mint en st["mint"], solo necesitamos mostrar la pantalla de compra
            if "mint" not in st:
                await q.answer("❌ Error: No se encontró el token")
                return
            await show_buy(q)
        else:
            # Comando normal de compra
            STATE[uid] = {"step": Step.TOKEN}
            await q.edit_message_text("💬 Envía el mint address del token que quieres comprar.",
                                    reply_markup=BACK_KB)
        return
        
    if data == "SELL_DETECTED_TOKEN":
        # El usuario quiere vender un token detectado
        if "mint" not in st or "token_data" not in st:
            await q.answer("❌ Error: No se encontró información del token")
            return
            
        # Usar los datos que ya tenemos en el estado
        mint = st["mint"]
        token_data = st["token_data"]
        
        # Actualizar estado para vender
        STATE[uid] = {
            "step": Step.SELL,
            "mint": mint,
            "token_data": token_data
        }
        
        # Mostrar interfaz de venta
        await show_sell_token(q, token_data)
        return
        
    if data == "NO_TOKENS_TO_SELL":
        # El usuario intentó vender un token que no tiene
        await q.answer("❌ No tienes tokens para vender. Compra primero antes de intentar vender.", show_alert=True)
        return
    
    if data == "BUY_REF":
        try:
            # Obtener datos del estado actual
            st = STATE[uid]
            if "mint" not in st:
                await q.answer("⚠️ No hay token seleccionado")
                return
                
            # Guardar texto original por si hay error
            original_text = q.message.text
            
            # Verificar si hay una actualización en curso para este token y este usuario
            mint = st["mint"]
            refresh_lock_key = f"refresh_lock_{uid}_{mint}"
            
            # Comprobar si hay un bloqueo de actualización reciente (dentro de 3 segundos)
            from .quicknode_client import _token_cache
            if refresh_lock_key in _token_cache:
                last_refresh = _token_cache[refresh_lock_key].get('timestamp', 0)
                time_since_last_refresh = time.time() - last_refresh
                
                # Si la última actualización fue muy reciente, prevenir spam de refresh
                if time_since_last_refresh < 3.0:  # 3 segundos de cooldown entre actualizaciones
                    await q.answer(f"⏱️ Espera un momento... ({3.0 - time_since_last_refresh:.1f}s)", show_alert=True)
                    return
                    
            # Establecer bloqueo de actualización
            _token_cache[refresh_lock_key] = {
                'timestamp': time.time(),
                'uid': uid
            }
            
            # Mostrar mensaje de carga para mejorar UX
            await q.edit_message_text(
                "⏳ *Actualizando datos del token en tiempo real...*\n\n"
                "_Espera un momento mientras obtenemos los datos más recientes..._\n\n"
                "🔄 Consultando múltiples fuentes de datos...",
                parse_mode="Markdown"
            )
            
            # Mostrar "escribiendo..." mientras procesamos
            if hasattr(q, 'message') and hasattr(q.message, 'chat'):
                await u._bot.send_chat_action(q.message.chat.id, action="typing")
            
            # Limpiar todas las cachés relacionadas con este token para forzar datos frescos
            for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_", "token_stats_"]:
                cache_key = f"{key_prefix}{mint}"
                if cache_key in _token_cache:
                    del _token_cache[cache_key]
                    log.info(f"Caché eliminada: {cache_key}")
                    
            # Limpiar caché de SOL para asegurar precio actualizado
            if "sol_price_cache" in _token_cache:
                del _token_cache["sol_price_cache"]
                
            # Crear función asíncrona para obtener datos frescos con timeout
            async def get_fresh_data():
                # Forzar actualización y esperar máximo 3.5 segundos (ampliado para mayor chance de éxito)
                try:
                    return await asyncio.wait_for(
                        get_token_stats(mint, pool=st.get("pool")),
                        timeout=3.5
                    )
                except asyncio.TimeoutError:
                    log.warning(f"Timeout al obtener datos frescos para {mint}")
                    return None
                    
            # Registrar tiempo inicial para medir latencia
            start_time = time.time()
            
            # Intentar obtener datos nuevos de múltiples fuentes en paralelo
            try:
                fresh_data = await get_fresh_data()
                elapsed = time.time() - start_time
                
                if fresh_data:
                    # Añadir información de latencia para depuración
                    fresh_data["refresh_time"] = time.strftime("%H:%M:%S", time.localtime())
                    fresh_data["latency"] = elapsed
                    
                    # Actualizar estado con datos frescos
                    st["stats"] = fresh_data
                    
                    # Mostrar interfaz actualizada
                    await show_buy(q, edit=True)
                    
                    # Notificar al usuario
                    source = fresh_data.get("source", "desconocida")
                    await q.answer(f"✅ Datos actualizados en {elapsed:.2f}s vía {source}", show_alert=False)
                    return
            except Exception as e:
                log.error(f"Error en actualización rápida: {e}")
            
            # Si falla, intentar con el método tradicional con un timeout más generoso
            try:
                log.info(f"Intentando método tradicional con timeout extendido")
                from .token_info import get_token_stats
                st["stats"] = await asyncio.wait_for(
                    get_token_stats(mint, pool=st.get("pool")),
                    timeout=4.0  # Timeout más largo para método de respaldo
                )
                await show_buy(q, edit=True)
                await q.answer("✅ Datos actualizados", show_alert=False)
            except Exception as e2:
                log.error(f"Error también en método tradicional: {e2}")
                # Si ambos métodos fallan, mostrar error y restaurar texto original
                await q.answer("❌ No se pudieron actualizar los datos", show_alert=True)
                await q.message.edit_text(original_text, reply_markup=q.message.reply_markup, parse_mode="Markdown")
            
        except Exception as e:
            log.error(f"Error al actualizar datos: {e}")
            await q.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
            await q.message.edit_text(original_text, reply_markup=q.message.reply_markup, parse_mode="Markdown")
        return
        
    if data == "SELL":
        # Mostrar lista de tokens para vender
        await show_sell_tokens(q)
        return
        
    if data.startswith("SELL_TOKEN_"):
        # Usuario seleccionó un token para vender
        mint = data.split("_")[2]
        pubkey = get_pubkey(uid)
        
        # Cargar datos del token
        token_data = await get_token_position(pubkey, mint)
        if token_data:
            # Guardar información en el estado
            STATE[uid] = {
                "step": Step.SELL,
                "mint": mint,
                "token_data": token_data
            }
            
            # Mostrar interfaz de venta
            await show_sell_token(q, token_data)
        else:
            await q.edit_message_text("❌ No se pudo cargar la información del token. Inténtalo de nuevo.",
                                     reply_markup=BACK_KB)
        return
        
    if data == "REF":
        # Botón de refresh - Actualizar datos en tiempo real
        # Verificar si ya hay un refresh en progreso para este usuario
        refresh_lock_key = f"refresh_lock_{uid}"
        if refresh_lock_key in STATE:
            # Si el último refresh fue hace menos de 2 segundos, ignorar
            last_refresh_time = STATE[refresh_lock_key]
            if time.time() - last_refresh_time < 2.0:
                await q.answer("⏳ Espera un momento entre actualizaciones", show_alert=True)
                return
        
        # Guardar mensaje y teclado originales
        original_text = q.message.text
        original_markup = q.message.reply_markup
        
        # Establecer bloqueo para evitar múltiples solicitudes
        STATE[refresh_lock_key] = time.time()
        
        # Actualizar UI para mostrar que estamos recargando (cambia el botón)
        # Obtener datos para construir el mismo teclado pero con el botón de actualización modificado
        selection = None
        if "amount" in st:
            selection = st["amount"]
        
        # Actualizar mensaje con el botón de actualización en estado de carga
        await q.edit_message_text(
            original_text,
            reply_markup=buy_kb(selection, True), # Teclado con botón de actualización en proceso
            parse_mode="Markdown"
        )
        
        # Iniciar actualización
        await q.answer("🔄 Actualizando datos en tiempo real...", show_alert=False)
        
        # Enviar acción "typing" mientras actualizamos
        if hasattr(q, 'message') and hasattr(q.message, 'chat'):
            await u._bot.send_chat_action(q.message.chat.id, action="typing")
        
        try:
            mint = st["mint"]
            
            # Limpiar todas las cachés relacionadas con este token
            from .quicknode_client import _token_cache
            for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_", "token_stats_"]:
                cache_key = f"{key_prefix}{mint}"
                if cache_key in _token_cache:
                    del _token_cache[cache_key]
                    log.info(f"Caché eliminada: {cache_key}")
                    
            # Limpiar caché de SOL para asegurar precio actualizado
            if "sol_price_cache" in _token_cache:
                del _token_cache["sol_price_cache"]
            
            # Usar forzado de datos específico para obtener datos en tiempo real
            fresh_data = await force_pump_data(mint)
            
            if fresh_data:
                # Actualizar datos en el estado
                st["stats"] = fresh_data
                
                # Asegurar que se actualizaron todos los campos importantes
                log.info(f"Datos actualizados: precio=${fresh_data.get('price', 0)} MC=${fresh_data.get('mc', 0):,.2f}")
                
                # Pequeña pausa para asegurar que los datos se actualicen completamente
                await asyncio.sleep(0.1)
                
                # Mostrar datos actualizados
                await show_buy(q)
                
                # Confirmar actualización
                source = fresh_data.get("source", "desconocida")
                fetch_time = fresh_data.get("latency", 0)
                await q.answer(f"✅ Datos actualizados en {fetch_time:.2f}s vía {source}", show_alert=False)
            else:
                # Si falló la obtención de datos frescos, intenta con método tradicional
                log.warning("No se pudieron obtener datos frescos, usando método tradicional")
                stats = await get_token_stats(mint, force_fresh=True)
                if stats:
                    st["stats"] = stats
                    await show_buy(q)
                    await q.answer("✅ Datos actualizados", show_alert=False)
                else:
                    # Si ambos métodos fallan, restaurar mensaje original
                    await q.edit_message_text(
                        original_text, 
                        reply_markup=buy_kb(selection, False),
                        parse_mode="Markdown"
                    )
                    await q.answer("❌ No se pudieron actualizar los datos", show_alert=True)
        except Exception as e:
            log.error(f"Error al actualizar datos: {e}")
            # Restaurar UI original pero con el botón de actualización normal
            await q.edit_message_text(
                original_text,
                reply_markup=buy_kb(selection, False), # Volver al estado normal
                parse_mode="Markdown"
            )
            await q.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
        return
        
    if data == "POS_REFRESH":
        # Mostrar mensaje de carga con teclado actualizado
        await q.edit_message_text(
            "⏳ *Actualizando posiciones en tiempo real...*\n\n"
            "_Forzando refresco de todos los tokens..._\n\n"
            "Este proceso puede tardar unos segundos. Estamos obteniendo datos\n"
            "frescos de múltiples fuentes para darte la información más precisa.",
            reply_markup=positions_kb(True),  # Teclado con botón de actualización en proceso
            parse_mode="Markdown"
        )
        
        # Mostrar "escribiendo..." mientras procesamos
        if hasattr(q, 'message') and hasattr(q.message, 'chat'):
            await u._bot.send_chat_action(q.message.chat.id, action="typing")
        
        try:
            # Limpiar cachés de tokens
            from .quicknode_client import _token_cache
            pubkey = get_pubkey(uid)
            
            # 1. Obtener lista de tokens que posee el usuario
            tokens = await get_user_tokens(pubkey)
            
            # 2. Limpiar caché para todos estos tokens
            for mint in tokens:
                for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_"]:
                    cache_key = f"{key_prefix}{mint}"
                    if cache_key in _token_cache:
                        del _token_cache[cache_key]
                        log.info(f"Caché de posición eliminada: {cache_key}")
            
            # 3. Forzar actualización del precio SOL
            cache_key = "sol_price_cache"
            if cache_key in _token_cache:
                del _token_cache[cache_key]
            
            # Establecer inicio del tiempo para medir rendimiento
            start_time = time.time()
            
            # Obtener posiciones actualizadas con datos frescos
            try:
                await show_positions(q)
                
                # Mostrar mensaje de éxito con tiempo de actualización
                elapsed = time.time() - start_time
                await q.answer(f"✅ Posiciones actualizadas en {elapsed:.1f}s", show_alert=False)
            except Exception as e:
                log.error(f"Error al actualizar posiciones: {e}")
                # Restaurar mensaje con teclado normal
                await q.edit_message_text(
                    "❌ *Error al actualizar posiciones*\n\n_Por favor, intenta de nuevo más tarde._",
                    reply_markup=positions_kb(False), # Volver al estado normal
                    parse_mode="Markdown"
                )
                await q.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
            return
        except Exception as e:
            log.error(f"Error al actualizar cachés: {e}")
            # Restaurar mensaje con teclado normal
            await q.edit_message_text(
                "❌ *Error al actualizar posiciones*\n\n_Por favor, intenta de nuevo más tarde._",
                reply_markup=positions_kb(False), # Volver al estado normal
                parse_mode="Markdown"
            )
            await q.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
            return
            
    if data == "SELL_REF":
        try:
            # Obtener datos del token actual
            st = STATE[uid]
            if not st.get("token_data"):
                await q.answer("⚠️ No hay token seleccionado para actualizar")
                return
                
            # Guardar mensaje y teclado originales
            orig_message = q.message.text
            
            # Obtener datos del token y selección actual para construir el teclado
            token_data = st["token_data"]
            sell_pct = st.get("sell_pct", None)
            
            # Mostrar mensaje de carga con teclado actualizado
            await q.message.edit_text(
                "⏳ *Actualizando token en tiempo real...*\n\n"
                "_Forzando refresco de los datos del token..._",
                reply_markup=sell_kb(token_data, sell_pct, True),  # Teclado con botón de actualización en proceso
                parse_mode="Markdown"
            )
            
            # Mostrar "escribiendo..." mientras procesamos
            if hasattr(q, 'message') and hasattr(q.message, 'chat'):
                await u._bot.send_chat_action(q.message.chat.id, action="typing")
                
            # Limpiar cachés del token actual
            mint = st["token_data"]["mint"]
            
            # Limpiar todas las cachés relacionadas con este token
            from .quicknode_client import _token_cache
            for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_"]:
                cache_key = f"{key_prefix}{mint}"
                if cache_key in _token_cache:
                    del _token_cache[cache_key]
                    log.info(f"Caché del token eliminada: {cache_key}")
            
            # Obtener datos frescos del token
            pubkey = get_pubkey(uid)
            fresh_token_data = await get_token_position(pubkey, mint)
            
            if fresh_token_data:
                # Actualizar datos en el estado
                st["token_data"] = fresh_token_data
                # Mostrar el token actualizado
                await show_sell_token(q, fresh_token_data)
                await q.answer("✅ Datos del token actualizados", show_alert=False)
            else:
                # Si no se pudieron obtener datos frescos, restaurar mensaje original pero con teclado normal
                await q.message.edit_text(
                    orig_message, 
                    reply_markup=sell_kb(token_data, sell_pct, False),  # Volver al estado normal
                    parse_mode="Markdown"
                )
                await q.answer("❌ No se pudieron actualizar los datos", show_alert=True)
        except Exception as e:
            log.error(f"Error al actualizar token: {e}")
            await q.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
        return

    if data.startswith("S_"):
        # Usuario seleccionó un porcentaje para vender
        if st["step"] == Step.SELL:
            if data == "S_X":
                # Usuario quiere ingresar porcentaje personalizado
                STATE[uid]["step"] = Step.SELL_CUSTOM
                await q.message.reply_text("💬 Envía el porcentaje que deseas vender (1-100):")
                return
                
            # Convertir valor a porcentaje
            sell_pct = float(data.split("_")[1])
            
            # Actualizar datos del token con el porcentaje seleccionado
            token_data = st["token_data"]
            token_data["sell_pct"] = sell_pct
            STATE[uid]["sell_pct"] = sell_pct
            
            # Mostrar interfaz actualizada
            await show_sell_token(q, token_data)
        return
        
    if data == "SELL_EXEC":
        # Ejecutar venta de token
        if st["step"] == Step.SELL and "sell_pct" in st:
            mint = st["mint"]
            token_data = st["token_data"]
            sell_pct = st["sell_pct"]
            
            if sell_pct <= 0 or sell_pct > 100:
                await q.answer("❌ Porcentaje inválido. Debe estar entre 1 y 100.", show_alert=True)
                return
                
            # Calcular cantidad a vender
            balance = token_data.get("balance", 0)
            sell_amount = balance * (sell_pct / 100)
            
            if sell_amount <= 0:
                await q.answer("❌ Cantidad a vender debe ser mayor a 0.", show_alert=True)
                return
                
            # Mostrar mensaje de carga con mejor formato
            await q.edit_message_text(
                "⏳ *Enviando transacción de venta...*\n\n"
                "_La transacción está siendo procesada._\n"
                "_Por favor, espera un momento..._",
                reply_markup=None,
                parse_mode="Markdown"
            )
            
            # Mostrar acción de typing mientras se procesa
            if hasattr(q, 'message') and hasattr(q.message, 'chat'):
                await u._bot.send_chat_action(q.message.chat.id, action="typing")
                     
            try:
                # Ejecutar venta
                sig = await swap_tokens_for_sol(
                    load_wallet(uid),
                    mint,
                    sell_amount
                )
                
                # Registrar la transacción
                price_usd = token_data.get("price_usd", 0)
                amount_sol = sell_amount * token_data.get("price_sol", 0)
                
                record_transaction(
                    uid=uid,
                    mint=mint,
                    tx_type="sell",
                    amount=amount_sol,
                    token_amount=sell_amount,
                    price_usd=price_usd,
                    tx_hash=sig
                )
                
                # Calcular comisión del bot (1% del SOL recibido)
                fee_amount = amount_sol * (BOT_FEE_PERCENTAGE / 100)
                net_amount = amount_sol - fee_amount
                
                # Mostrar confirmación con mejor formato
                symbol = token_data.get("symbol", "???")
                confirmation = (
                    f"✅ *Venta enviada exitosamente* ✅\n\n"
                    f"*📊 Detalles de la transacción:*\n"
                    f"───────────────────\n\n"
                    f"*🪙 Token:* {symbol}\n"
                    f"*💰 Cantidad:* {sell_amount:.4f}\n"
                    f"*💵 Precio:* ${price_usd:.8f}\n"
                    f"*💲 Valor total:* ${sell_amount * price_usd:.2f}\n\n"
                    f"*💸 Comisión ({BOT_FEE_PERCENTAGE}%):* {fee_amount:.6f} SOL\n"
                    f"*💰 Recibirás (neto):* {net_amount:.6f} SOL\n\n"
                    f"*🔗 Tx Hash:*\n`{sig}`\n\n"
                    f"_Toca el hash para copiarlo_\n"
                    f"───────────────────\n"
                )
                
                await q.edit_message_text(
                    confirmation,
                    reply_markup=BACK_KB,
                    parse_mode="Markdown"
                )
                
                # Volver a estado IDLE
                STATE[uid] = {"step": Step.IDLE}
                
            except Exception as e:
                error_message = (
                    f"❌ *Error al procesar la venta* ❌\n\n"
                    f"*Detalles del error:*\n`{str(e)}`\n\n"
                    f"_Por favor, intenta de nuevo más tarde._"
                )
                await q.edit_message_text(
                    error_message,
                    reply_markup=BACK_KB,
                    parse_mode="Markdown"
                )
        else:
            await q.answer("❌ Primero selecciona cuánto deseas vender.", show_alert=True)
        return

    if data == "POS_SORT":
        # Mostrar opciones para ordenar posiciones
        sort_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Por valor", callback_data="SORT_VALUE"),
             InlineKeyboardButton("📊 Por % portfolio", callback_data="SORT_PCT")],
            [InlineKeyboardButton("🔤 Por nombre", callback_data="SORT_NAME"),
             InlineKeyboardButton("🔄 Por cambio 24h", callback_data="SORT_CHANGE")],
            [InlineKeyboardButton("↩️ Volver", callback_data="POSITIONS")]
        ])
        await q.edit_message_text("Selecciona cómo ordenar tus posiciones:",
                                  reply_markup=sort_kb)
        return
        
    if data.startswith("SORT_"):
        # Actualizar preferencia de ordenación y mostrar posiciones
        sort_option = data.split("_")[1]
        # Guardar preferencia (implementar en futuras versiones)
        await show_positions(q)
        return

    if st["step"] not in (Step.TOKEN, Step.CUSTOM):
        return

    if data.startswith("A_"):
        if data == "A_X":
            st["step"] = Step.CUSTOM
            await q.message.reply_text("💬 Envía cantidad en SOL:")
            return
        st["amount"] = float(data.split("_")[1])
        await show_buy(q)
        return

    if data == "BUY_EXEC":
        if not st.get("amount"):
            await q.answer("❌ Selecciona cantidad primero", show_alert=True)
            return
        
        # Validación adicional para la cantidad    
        amount_sol = st.get("amount")
        if amount_sol <= 0:
            await q.answer("❌ La cantidad debe ser mayor que cero", show_alert=True)
            return
            
        # Mostrar mensaje de carga con mejor formato
        await q.edit_message_text(
            "⏳ *Enviando transacción de compra...*\n\n"
            "_La transacción está siendo procesada._\n"
            "_Por favor, espera un momento..._",
            reply_markup=None,
            parse_mode="Markdown"
        )
        
        # Mostrar acción de typing mientras se procesa
        if hasattr(q, 'message') and hasattr(q.message, 'chat'):
            await u._bot.send_chat_action(q.message.chat.id, action="typing")
            
        try:
            # Obtener datos actuales del token para registrar la transacción
            mint = st["mint"]
            price_usd = st["stats"]["price"]
            symbol = st["stats"]["sym"]
            
            # Verificar balance de SOL antes de ejecutar la transacción
            try:
                from solana.rpc.async_api import AsyncClient
                from .config import RPC_ENDPOINT
                
                client = AsyncClient(RPC_ENDPOINT)
                sol_balance = await client.get_balance(load_wallet(uid).pubkey())
                sol_balance_value = sol_balance.value / 1e9  # Convertir lamports a SOL
                
                # Verificar si tiene suficiente SOL para la transacción + gas (0.00005 SOL para mayor seguridad)
                if sol_balance_value < amount_sol + 0.00005:
                    await client.close()
                    raise Exception(f"Saldo insuficiente. Tienes {sol_balance_value:.6f} SOL, necesitas al menos {amount_sol + 0.00005:.6f} SOL (incluyendo gas).")
                
                await client.close()
            except Exception as balance_error:
                # Si el error ya indica fondos insuficientes, propagar directamente
                if "Saldo insuficiente" in str(balance_error):
                    raise balance_error
                # De lo contrario, solo registrar y continuar
                log.warning(f"No se pudo verificar el balance antes de la compra: {str(balance_error)}")
            
            # Calcular comisión del bot (1% del SOL invertido)
            fee_amount = amount_sol * (BOT_FEE_PERCENTAGE / 100)
            swap_amount = amount_sol - fee_amount
            
            # Recalcular la cantidad de tokens que se recibirán (considerando la comisión)
            token_amount = swap_amount / st["stats"]["price_sol"] if st["stats"]["price_sol"] > 0 else 0
            
            # Ejecutar la transacción
            sig = await swap_sol_for_tokens(load_wallet(uid),
                                           mint, amount_sol, st.get("pool"))
            
            # Obtener el precio actual de SOL en USD para cálculos
            try:
                sol_price_usd = await get_sol_price_usd_qn()
            except Exception as e:
                log.warning(f"No se pudo obtener el precio de SOL en USD: {str(e)}")
                sol_price_usd = 0  # Valor por defecto si falla
            
            # Registrar la transacción en la base de datos
            record_transaction(
                uid=uid,
                mint=mint,
                tx_type="buy",
                amount=amount_sol,
                token_amount=token_amount,
                price_usd=price_usd,
                tx_hash=sig
            )
            
            # Mostrar confirmación con mejor formato
            confirmation = (
                f"✅ *Compra enviada exitosamente* ✅\n\n"
                f"*📊 Detalles de la transacción:*\n"
                f"───────────────────\n\n"
                f"*🪙 Token:* {symbol}\n"
                f"*💰 Invertiste:* {amount_sol} SOL (${amount_sol * sol_price_usd:.2f})\n"
                f"*💸 Comisión ({BOT_FEE_PERCENTAGE}%):* {fee_amount:.6f} SOL\n"
                f"*💱 Cantidad para swap:* {swap_amount:.6f} SOL\n"
                f"*💵 Recibes:* {token_amount:.4f} tokens\n"
                f"*💲 Precio:* ${price_usd:.8f}\n\n"
                f"*🔗 Tx Hash:*\n`{sig}`\n\n"
                f"_Toca el hash para copiarlo_\n"
                f"───────────────────\n"
            )
            
            await q.edit_message_text(
                confirmation,
                reply_markup=BACK_KB,
                parse_mode="Markdown"
            )
            
            STATE[uid] = {"step": Step.IDLE}
        except Exception as e:
            error_msg = str(e).lower()
            error_text = str(e)
            log.error(f"Error al procesar compra: {error_text}")
            
            # Manejar diferentes tipos de error
            if "insufficient funds" in error_msg or "saldo insuficiente" in error_msg or "lamports" in error_msg:
                # Error de fondos insuficientes
                error_message = (
                    f"❌ *Error: Fondos insuficientes* ❌\n\n"
                    f"No tienes suficiente SOL para completar esta transacción.\n\n"
                    f"Necesitas al menos {amount_sol + 0.00005:.6f} SOL (incluyendo gas)\n\n"
                    f"Por favor, recarga tu wallet o selecciona una cantidad menor."
                )
            elif "not enough signers" in error_msg:
                # Error de firma
                error_message = (
                    f"❌ *Error técnico en la transacción* ❌\n\n"
                    f"Se produjo un error con la firma de la transacción.\n"
                    f"Este es un problema técnico temporal.\n\n"
                    f"Por favor, intenta nuevamente en unos minutos."
                )
            elif "no se pudo encontrar ruta" in error_msg or "liquidez" in error_msg:
                # Error de liquidez
                error_message = (
                    f"❌ *No se pudo comprar {symbol}* ❌\n\n"
                    f"Este token no tiene suficiente liquidez para ser comprado automáticamente.\n\n"
                    f"Puedes intentar comprarlo directamente en:\n"
                    f"• [Raydium](https://raydium.io/swap/)\n"
                    f"• [Pump.fun](https://pump.fun/)\n"
                    f"• [Jupiter](https://jup.ag/swap/)"
                )
                
                # Mostrar mensaje sin cargar previsualizaciones de enlaces
                await q.edit_message_text(
                    error_message,
                    reply_markup=BACK_KB,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                return
            else:
                # Error genérico
                error_message = (
                    f"❌ *Error al procesar la compra* ❌\n\n"
                    f"*Detalles del error:*\n`{error_text}`\n\n"
                    f"_Por favor, intenta de nuevo más tarde._"
                )
            
            await q.edit_message_text(
                error_message,
                reply_markup=BACK_KB,
                parse_mode="Markdown"
            )

# Función para mostrar interfaz de venta de un token específico
async def show_sell_token(update, token_data, is_refreshing=False):
    """Muestra la interfaz para vender un token específico"""
    uid = update.from_user.id
    ensure(uid)
    
    # Extraer datos del token
    symbol = token_data.get('symbol', '???')
    name = token_data.get('name', 'Unknown Token')
    balance = token_data.get('balance', 0)
    price = token_data.get('price_usd', 0)
    value = token_data.get('value_usd', 0)
    pnl_pct = token_data.get('pnl_pct', 0)
    pnl_usd = token_data.get('pnl_usd', 0)
    entry_price = token_data.get('entry_price', 0)
    mint = token_data.get('mint', '')
    
    # Emoji basado en PnL
    emoji = "🔴" if pnl_pct < 0 else "🟢"
    pnl_sign = "" if pnl_pct < 0 else "+"
    
    # Porcentaje seleccionado para vender
    sell_pct = token_data.get('sell_pct', 0)
    sell_amount = balance * (sell_pct / 100) if sell_pct else 0
    sell_value = sell_amount * price
    
    # Mostrar "escribiendo..." mientras procesamos
    if hasattr(update, 'effective_chat') and getattr(update, '_bot', None):
        await update._bot.send_chat_action(update.effective_chat.id, action="typing")
    
    # Construir mensaje con estilo mejorado y separadores
    message = f"🔴 *VENDER {symbol}* 🔴\n\n"
    
    # Sección Token
    message += f"*🪙 Token:* {symbol} - {name}\n"
    message += f"*📍 Mint:* `{mint}`\n"
    message += f"_Toca para copiar_\n\n"
    
    # Separador visual
    message += f"───────────────────\n\n"
    
    # Sección Balance
    message += f"*💰 Balance:* {balance:.4f} {symbol} (${value:.2f})\n"
    message += f"*💵 Precio:* ${price:.8f}\n"
    
    # Mostrar PnL si está disponible
    if pnl_pct != 0:
        message += f"*📈 PnL:* {emoji} {pnl_sign}{pnl_pct:.2f}% (${pnl_usd:.2f})\n"
        message += f"*🔍 Entrada:* ${entry_price:.8f}\n\n"
    else:
        message += "\n"
    
    # Separador visual
    message += f"───────────────────\n\n"
    
    # Sección Venta
    if sell_pct:
        message += f"*🛒 VENTA:* {sell_pct}% = {sell_amount:.4f} {symbol}\n"
        message += f"*💵 Valor aproximado:* ${sell_value:.2f}\n\n"
    else:
        message += "*🛒 Selecciona el porcentaje a vender*\n\n"
    
    # Separador visual
    message += f"───────────────────\n"
    
    # Al final, usar el keyboard con estado de refresh
    if hasattr(update, 'edit_message_text'):
        await update.edit_message_text(
            message, 
            reply_markup=sell_kb(token_data, token_data.get("sell_pct"), is_refreshing),
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            message, 
            reply_markup=sell_kb(token_data, token_data.get("sell_pct"), is_refreshing),
            parse_mode="Markdown"
        )

# ───────── Main ─────────
def main():
    """Función principal para iniciar el bot"""
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(
        Defaults(parse_mode=ParseMode.HTML)
    ).build()

    # Definir manejadores de comandos
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("p", positions_cmd))
    app.add_handler(CommandHandler("mcap", marketcap_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tx", tx_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    
    # Manejador para mensajes normales (enlaces, mint, etc.)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_msg))
    
    # Manejador para callbacks de botones
    app.add_handler(CallbackQueryHandler(cb_router))
    
    # Iniciar tareas en segundo plano para mantenimiento de caché
    async def init_background_tasks():
        # Esperar un momento para asegurar que el bucle de eventos está funcionando
        await asyncio.sleep(1)
        log.info("Iniciando tareas en segundo plano...")
        
        # Iniciar limpieza de caché de QuickNode
        try:
            # Iniciar limpieza de caché
            cache_cleanup = asyncio.create_task(cache_cleanup_task())
            cache_cleanup.set_name("cache_cleanup_task")
            log.info("Tarea de limpieza de caché iniciada correctamente")
        except Exception as e:
            log.error(f"Error al iniciar limpieza de caché: {e}")
        
        # Iniciar conexión WebSocket para datos en tiempo real
        try:
            ws_task = asyncio.create_task(initialize_websocket())
            ws_task.set_name("websocket_connection")
            log.info("Conexión WebSocket iniciada")
        except Exception as e:
            log.error(f"Error al iniciar WebSocket: {e}")
        
        # Registrar función para limpiar la caché al salir
        import atexit
        atexit.register(lambda: log.info("Tareas en segundo plano detenidas y caché liberada"))
        
        log.info("Todas las tareas en segundo plano iniciadas correctamente")

    # Añadir tarea de inicialización
    app.job_queue.run_once(lambda _: asyncio.create_task(init_background_tasks()), 0)
    
    # Iniciar el bot
    app.run_polling()

# Función para calcular la cantidad de tokens que se recibirían en un swap
async def calculate_output_amount(token_mint: str, sol_amount: float) -> float:
    """
    Calcula la cantidad aproximada de tokens que se recibirían al swapear SOL.
    
    Args:
        token_mint: Dirección del token
        sol_amount: Cantidad de SOL a swapear
        
    Returns:
        Cantidad estimada de tokens a recibir
    """
    try:
        # Obtener datos del token
        token_data = await get_token_stats(token_mint)
        
        if not token_data or not token_data.get("price_sol", 0) > 0:
            log.warning(f"No se pudo obtener el precio en SOL para {token_mint}")
            # Devolver una estimación simple basada en el precio USD si está disponible
            if token_data and token_data.get("price", 0) > 0 and "sol_price" in globals():
                token_price_usd = token_data["price"]
                sol_price_usd = globals().get("sol_price", 0)
                if sol_price_usd > 0:
                    return (sol_amount * sol_price_usd) / token_price_usd
            return sol_amount * 10000  # Valor aproximado por defecto
        
        # Calcular basado en el precio del token en SOL
        price_sol = token_data["price_sol"]
        
        # Aplicar descuento por slippage (aproximadamente 1%)
        effective_price = price_sol * 0.99
        
        # Descontar la comisión del bot
        net_sol_amount = sol_amount * (1 - BOT_FEE_PERCENTAGE / 100)
        
        # Calcular cantidad de tokens
        token_amount = net_sol_amount / effective_price if effective_price > 0 else 0
        
        return token_amount
    except Exception as e:
        log.error(f"Error calculando output: {e}")
        # En caso de error, devolver una aproximación simple
        return sol_amount * 10000  # Valor aproximado por defecto

if __name__ == "__main__":
    main()

