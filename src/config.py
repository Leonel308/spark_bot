from pathlib import Path
from dotenv import load_dotenv
import os
import logging
from typing import Dict, Any, List, Optional, Tuple
from configparser import ConfigParser

# Configurar logging
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)

# Cargar configuración desde archivo o usar valores predeterminados
config = ConfigParser()
config_path = Path('config.ini')

if config_path.exists():
    config.read(config_path)
    log.info(f"Configuración cargada desde {config_path}")
else:
    log.warning(f"No se encontró archivo de configuración en {config_path}. Se usarán valores predeterminados.")

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR.parent / ".env", override=False)

# RPC Endpoints
RPC_ENDPOINT = os.environ.get('RPC_ENDPOINT') or config.get('rpc', 'endpoint', fallback='https://api.mainnet-beta.solana.com')
QUICKNODE_RPC_ENDPOINT = os.environ.get('QUICKNODE_RPC_ENDPOINT') or config.get('rpc', 'quicknode_endpoint', fallback='')

# BOT Token
BOT_TOKEN = os.getenv("BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
BIRDEYE_KEY = os.getenv("BIRDEYE_API_KEY", "").strip()

# BOT Fee settings
BOT_FEE_PERCENTAGE = float(os.environ.get('BOT_FEE_PERCENTAGE') or config.get('bot', 'fee_percentage', fallback='1.0'))
BOT_FEE_RECIPIENT = os.environ.get('BOT_FEE_RECIPIENT') or config.get('bot', 'fee_recipient', fallback='')

# WebSocket y API config
ENABLE_WEBSOCKET = bool(int(os.environ.get('ENABLE_WEBSOCKET') or config.get('api', 'enable_websocket', fallback='1')))
MARKETDATA_CACHE_TTL_SECONDS = int(os.environ.get('MARKETDATA_CACHE_TTL') or config.get('api', 'cache_ttl', fallback='30'))
WEBSOCKET_RECONNECT_INTERVAL = int(os.environ.get('WEBSOCKET_RECONNECT_INTERVAL') or config.get('api', 'ws_reconnect', fallback='60'))

# Endpoints para datos en tiempo real
PUMPFUN_WEBSOCKET_ENDPOINT = "wss://api.pump.fun/socket"
PUMPFUN_API_ENDPOINT = "https://api.pump.fun"
JUPITER_API_ENDPOINT = "https://price.jup.ag/v4"

# Secret key para almacenamiento seguro
SECRET_KEY = os.environ.get('SECRET_KEY') or config.get('security', 'secret_key', fallback='default_key_change_this!')

# QuickNode endpoints
QUICKNODE_RPC_URL = "https://icy-wandering-glitter.solana-mainnet.quiknode.pro/8b0d6498732085e4fc46994f84b6917771b0e232/"
QUICKNODE_WS_URL = "wss://icy-wandering-glitter.solana-mainnet.quiknode.pro/8b0d6498732085e4fc46994f84b6917771b0e232/"

# Timeouts optimizados para QuickNode y APIs externas
RPC_TIMEOUT_SECONDS = float(os.environ.get('RPC_TIMEOUT_SECONDS') or config.get('rpc', 'timeout_seconds', fallback='5.0'))
WS_TIMEOUT_SECONDS = 1.5

# Configuración de caché (todos los valores reducidos para mayor frescura)
TOKEN_CACHE_TTL_SECONDS = 8  # Caché estándar reducido
PRICE_CACHE_TTL_SECONDS = 3   # Precios más actualizados
PUMPFUN_CACHE_TTL_SECONDS = 1 # Ultra-rápido para PumpFun
JUPITER_CACHE_TTL_SECONDS = 5

# Configuración para tokens virales/populares (actualización en tiempo real)
VIRAL_TOKEN_CACHE_TTL_SECONDS = 0  # Actualización constante para tokens virales

# API Keys de respaldo (utilizadas cuando las primarias fallan)
BACKUP_BIRDEYE_KEYS = [
    "5f88b633a8a142aa9b4b96fe",  # Clave pública genérica
    "f5a5bec8b9be4eb2ab9471cff572153e",  # Clave alternativa
]

# Configuración de comisiones del bot
BOT_FEE_PERCENTAGE = 1.0  # Comisión del 1% para compras y ventas
BOT_FEE_RECIPIENT = "4xEntsVwcSHNoEfCyYnR6CtBTeefC9cmC4DkJvd1Cotc"  # Wallet que recibe las comisiones

# Definir el endpoint de QuickNode (más rápido para consultas específicas)
QUICKNODE_RPC_ENDPOINT = os.environ.get('QUICKNODE_RPC_ENDPOINT') or config.get('rpc', 'quicknode_endpoint', fallback=RPC_ENDPOINT)

# Endpoint para WebSocket (asegurar baja latencia)
WS_RPC_ENDPOINT = os.environ.get('WS_RPC_ENDPOINT') or config.get('rpc', 'ws_endpoint', fallback=RPC_ENDPOINT.replace('https://', 'wss://'))

# Configuración de caché para optimizar velocidad y reducir llamadas API
CACHE_ENABLED = os.environ.get('CACHE_ENABLED', 'true').lower() in ('true', 'yes', '1')
DEFAULT_CACHE_TTL_SECONDS = int(os.environ.get('DEFAULT_CACHE_TTL_SECONDS') or config.get('cache', 'default_ttl_seconds', fallback='60'))
MARKETDATA_CACHE_TTL_SECONDS = int(os.environ.get('MARKETDATA_CACHE_TTL_SECONDS') or config.get('cache', 'marketdata_ttl_seconds', fallback='3'))
TOKEN_INFO_CACHE_TTL_SECONDS = int(os.environ.get('TOKEN_INFO_CACHE_TTL_SECONDS') or config.get('cache', 'token_info_ttl_seconds', fallback='300'))
PUMPFUN_CACHE_TTL_SECONDS = int(os.environ.get('PUMPFUN_CACHE_TTL_SECONDS') or config.get('cache', 'pumpfun_ttl_seconds', fallback='5'))
JUPITER_CACHE_TTL_SECONDS = int(os.environ.get('JUPITER_CACHE_TTL_SECONDS') or config.get('cache', 'jupiter_ttl_seconds', fallback='10'))
VIRAL_TOKEN_CACHE_TTL_SECONDS = int(os.environ.get('VIRAL_TOKEN_CACHE_TTL_SECONDS') or config.get('cache', 'viral_token_ttl_seconds', fallback='600'))

# Configuración para conexiones rápidas
ENABLE_WEBSOCKET = os.environ.get('ENABLE_WEBSOCKET', 'true').lower() in ('true', 'yes', '1')
MAX_WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get('MAX_WEBSOCKET_RECONNECT_ATTEMPTS') or config.get('websocket', 'max_reconnect_attempts', fallback='5'))
WEBSOCKET_RECONNECT_DELAY_SECONDS = float(os.environ.get('WEBSOCKET_RECONNECT_DELAY_SECONDS') or config.get('websocket', 'reconnect_delay_seconds', fallback='1.0'))

# Configuración para optimización de velocidad
PARALLEL_REQUESTS_MAX = int(os.environ.get('PARALLEL_REQUESTS_MAX') or config.get('performance', 'parallel_requests_max', fallback='10'))
HTTP_REQUEST_TIMEOUT_SECONDS = float(os.environ.get('HTTP_REQUEST_TIMEOUT_SECONDS') or config.get('performance', 'http_request_timeout_seconds', fallback='2.0'))
ENABLE_PREFETCH = os.environ.get('ENABLE_PREFETCH', 'true').lower() in ('true', 'yes', '1')

# Imprime información de configuración al iniciar
log.info(f"RPC Endpoint: {RPC_ENDPOINT}")
log.info(f"QuickNode RPC Endpoint: {QUICKNODE_RPC_ENDPOINT}")
log.info(f"WebSocket RPC Endpoint: {WS_RPC_ENDPOINT}")
log.info(f"Cache habilitado: {CACHE_ENABLED}")
log.info(f"WebSocket habilitado: {ENABLE_WEBSOCKET}")

# Lista de URLs de RPC públicos para usar como respaldo (fallback)
PUBLIC_RPC_FALLBACKS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-api.projectserum.com",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.rpcpool.com"
]

# Configuración para priorización de transacciones
PRIORITY_FEES_ENABLED = os.environ.get('PRIORITY_FEES_ENABLED', 'true').lower() in ('true', 'yes', '1')
DEFAULT_COMPUTE_LIMIT = int(os.environ.get('DEFAULT_COMPUTE_LIMIT') or config.get('priority', 'compute_limit', fallback='200000'))
DEFAULT_COMPUTE_PRICE = int(os.environ.get('DEFAULT_COMPUTE_PRICE') or config.get('priority', 'compute_price', fallback='1000'))
PRIORITY_AUTO_ADJUST = os.environ.get('PRIORITY_AUTO_ADJUST', 'true').lower() in ('true', 'yes', '1')

# Configuración para bundling de transacciones
TRANSACTION_BUNDLING_ENABLED = os.environ.get('TRANSACTION_BUNDLING_ENABLED', 'true').lower() in ('true', 'yes', '1')
MAX_BUNDLE_SIZE = int(os.environ.get('MAX_BUNDLE_SIZE') or config.get('bundling', 'max_bundle_size', fallback='5'))
BUNDLE_WAIT_FOR_CONFIRMATIONS = os.environ.get('BUNDLE_WAIT_FOR_CONFIRMATIONS', 'true').lower() in ('true', 'yes', '1')

def get_config_dict() -> Dict[str, Any]:
    """Devuelve un diccionario con toda la configuración actual"""
    return {
        'RPC_ENDPOINT': RPC_ENDPOINT,
        'QUICKNODE_RPC_ENDPOINT': QUICKNODE_RPC_ENDPOINT,
        'WS_RPC_ENDPOINT': WS_RPC_ENDPOINT,
        'RPC_TIMEOUT_SECONDS': RPC_TIMEOUT_SECONDS,
        'CACHE_ENABLED': CACHE_ENABLED,
        'DEFAULT_CACHE_TTL_SECONDS': DEFAULT_CACHE_TTL_SECONDS,
        'MARKETDATA_CACHE_TTL_SECONDS': MARKETDATA_CACHE_TTL_SECONDS,
        'TOKEN_INFO_CACHE_TTL_SECONDS': TOKEN_INFO_CACHE_TTL_SECONDS,
        'PUMPFUN_CACHE_TTL_SECONDS': PUMPFUN_CACHE_TTL_SECONDS,
        'JUPITER_CACHE_TTL_SECONDS': JUPITER_CACHE_TTL_SECONDS,
        'VIRAL_TOKEN_CACHE_TTL_SECONDS': VIRAL_TOKEN_CACHE_TTL_SECONDS,
        'ENABLE_WEBSOCKET': ENABLE_WEBSOCKET,
        'MAX_WEBSOCKET_RECONNECT_ATTEMPTS': MAX_WEBSOCKET_RECONNECT_ATTEMPTS,
        'WEBSOCKET_RECONNECT_DELAY_SECONDS': WEBSOCKET_RECONNECT_DELAY_SECONDS,
        'PARALLEL_REQUESTS_MAX': PARALLEL_REQUESTS_MAX,
        'HTTP_REQUEST_TIMEOUT_SECONDS': HTTP_REQUEST_TIMEOUT_SECONDS,
        'ENABLE_PREFETCH': ENABLE_PREFETCH,
        'PRIORITY_FEES_ENABLED': PRIORITY_FEES_ENABLED,
        'DEFAULT_COMPUTE_LIMIT': DEFAULT_COMPUTE_LIMIT,
        'DEFAULT_COMPUTE_PRICE': DEFAULT_COMPUTE_PRICE,
        'PRIORITY_AUTO_ADJUST': PRIORITY_AUTO_ADJUST,
        'TRANSACTION_BUNDLING_ENABLED': TRANSACTION_BUNDLING_ENABLED,
        'MAX_BUNDLE_SIZE': MAX_BUNDLE_SIZE,
        'BUNDLE_WAIT_FOR_CONFIRMATIONS': BUNDLE_WAIT_FOR_CONFIRMATIONS
    }

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN no está definido")
if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY no está definido")
