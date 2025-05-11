import logging
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from .db import get_pubkey, get_transaction_history
from .wallet_manager import load_wallet
from .config import BOT_FEE_PERCENTAGE

# Configurar logger
log = logging.getLogger(__name__)

# Función para asegurar que el usuario existe en la base de datos
def ensure(uid: int):
    """Asegura que el usuario existe en la base de datos"""
    from .bot import ensure
    return ensure(uid)

# Función para obtener header de wallet
async def wallet_header(pub: str) -> str:
    """Obtiene el header con información de la wallet"""
    from .bot import wallet_header
    return await wallet_header(pub)

# Función para obtener el keyboard principal
def main_kb(is_refreshing: bool = False):
    """Obtiene el keyboard principal"""
    from .bot import main_kb
    return main_kb(is_refreshing)

# Función para exportar las claves de la wallet
def exports(kp):
    """Exporta las claves privadas y semilla de una wallet"""
    from .bot import exports
    return exports(kp)

async def wallet_cmd(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Muestra la información de la wallet del usuario"""
    uid = u.effective_user.id
    ensure(uid)
    
    pub = get_pubkey(uid)
    if not pub:
        await u.message.reply_text(
            "❌ No tienes una wallet asociada. Usa /start para crear una."
        )
        return
        
    # Mostrar información básica de la wallet
    header = await wallet_header(pub)
    
    await u.message.reply_text(
        header,
        reply_markup=main_kb(),
        parse_mode=ParseMode.HTML
    )

async def backup_cmd(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Permite al usuario hacer un backup de su wallet"""
    uid = u.effective_user.id
    ensure(uid)
    
    kp = load_wallet(uid)
    if not kp:
        await u.message.reply_text(
            "❌ No tienes una wallet asociada. Usa /start para crear una."
        )
        return
    
    # Generar las claves para backup
    backup_data = exports(kp)
    
    # Enviar mensaje privado con instrucciones
    await u.message.reply_text(
        f"🔐 <b>BACKUP DE TU WALLET</b> 🔐\n\n"
        f"<b>⚠️ INFORMACIÓN PRIVADA ⚠️</b>\n"
        f"<i>Guarda esta información en un lugar seguro. Nunca la compartas con nadie.</i>\n\n"
        f"<b>Dirección pública:</b>\n<code>{backup_data['pubkey']}</code>\n\n"
        f"<b>Clave privada:</b>\n<code>{backup_data['private']}</code>\n\n"
        f"<b>Seed (semilla):</b>\n<code>{backup_data['seed']}</code>\n\n"
        f"<b>Instrucciones:</b>\n"
        f"Si alguna vez necesitas recuperar tu wallet, puedes usar cualquiera de estos métodos:\n"
        f"1. Importar la clave privada en Phantom/Solflare\n"
        f"2. Usar la seed phrase en cualquier wallet\n\n"
        f"<b>⚠️ ADVERTENCIA:</b> Esta información da acceso completo a tus fondos. Guárdala de forma segura.",
        parse_mode=ParseMode.HTML
    )

async def help_cmd(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Muestra la ayuda del bot"""
    await u.message.reply_text(
        f"🤖 <b>BOT DE TRADING EN SOLANA</b> 🤖\n\n"
        f"<b>Comandos disponibles:</b>\n"
        f"/start - Inicia el bot y crea una wallet\n"
        f"/wallet - Ver información de tu wallet\n"
        f"/backup - Obtén un backup de tu wallet\n"
        f"/positions - Ver tus posiciones actuales\n"
        f"/mcap [token] - Obtener marketcap de un token\n"
        f"/p - Alias para /positions\n"
        f"/tx - Ver tus últimas transacciones\n"
        f"/fees - Información sobre comisiones\n\n"
        f"<b>Funcionalidades:</b>\n"
        f"• Comprar/vender tokens en Solana\n"
        f"• Ver datos en tiempo real de marketcap\n"
        f"• Detectar automáticamente tokens de Pump.fun\n"
        f"• Monitorear tus posiciones\n\n"
        f"<b>Uso:</b>\n"
        f"Simplemente envía una dirección de token mint o un enlace de Pump.fun para analizar y comprar un token.",
        parse_mode=ParseMode.HTML
    )

async def tx_cmd(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Muestra las últimas transacciones del usuario"""
    uid = u.effective_user.id
    ensure(uid)
    
    pub = get_pubkey(uid)
    if not pub:
        await u.message.reply_text(
            "❌ No tienes una wallet asociada. Usa /start para crear una."
        )
        return
    
    # Obtener historial desde la base de datos
    tx_history = get_transaction_history(uid)
    
    if not tx_history:
        await u.message.reply_text(
            "📝 No tienes transacciones registradas aún.\n\n"
            "Cuando realices operaciones de compra o venta, aparecerán aquí."
        )
        return
    
    # Construir mensaje con las últimas transacciones
    message = f"📜 <b>HISTORIAL DE TRANSACCIONES</b> 📜\n\n"
    
    for tx in tx_history[:10]:  # Mostrar las últimas 10 transacciones
        tx_type = "🟢 COMPRA" if tx.get("type") == "buy" else "🔴 VENTA"
        date = tx.get("timestamp", "").split(" ")[0]
        time = tx.get("timestamp", "").split(" ")[1].split(".")[0]
        symbol = tx.get("symbol", "???")
        amount = tx.get("token_amount", 0)
        price_usd = tx.get("price_usd", 0)
        tx_hash = tx.get("tx_hash", "")
        
        message += f"{tx_type} - {date} {time}\n"
        message += f"<b>{amount:.4f} {symbol}</b> @ ${price_usd:.6f}\n"
        message += f"<a href='https://solscan.io/tx/{tx_hash}'>Ver en Solscan</a>\n\n"
    
    await u.message.reply_text(
        message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def fees_cmd(u: Update, _: ContextTypes.DEFAULT_TYPE):
    """Muestra información sobre las comisiones del bot"""
    await u.message.reply_text(
        f"💰 <b>COMISIONES DEL BOT</b> 💰\n\n"
        f"El bot cobra una comisión del <b>{BOT_FEE_PERCENTAGE}%</b> en cada operación para mantener el servicio y mejorar sus funcionalidades.\n\n"
        f"<b>¿Cómo funciona?</b>\n"
        f"Cuando realizas una compra o venta, se deduce automáticamente la comisión del monto de la transacción.\n\n"
        f"<b>Ejemplo:</b>\n"
        f"Si compras tokens por valor de 1 SOL, se deducirá {BOT_FEE_PERCENTAGE/100:.4f} SOL como comisión, y recibirás tokens por valor de {1-BOT_FEE_PERCENTAGE/100:.4f} SOL.\n\n"
        f"<b>Nota:</b> La comisión se suma a las comisiones normales de red y de los protocolos (slippage).",
        parse_mode=ParseMode.HTML
    ) 