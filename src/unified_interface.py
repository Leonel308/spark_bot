"""
Solución para unificar la interfaz del bot cuando se detecta un token
"""
import asyncio, logging, time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

# Crear un teclado unificado para compra/venta que siempre muestre ambas opciones
def unified_keyboard(symbol, sel=0.5, token_balance=0, is_refreshing=False):
    """
    Crea un teclado unificado que muestra opciones para comprar y vender
    independientemente de si el usuario tiene el token o no.
    
    Args:
        symbol: Símbolo del token
        sel: Cantidad de SOL seleccionada para la compra
        token_balance: Balance del token que tiene el usuario (0 si no tiene)
        is_refreshing: Si está en proceso de actualización
    
    Returns:
        InlineKeyboardMarkup con las opciones apropiadas
    """
    buttons = []
    
    # Primera fila: botones de actualizar, comprar y vender
    refresh_text = "⏳ Actualizando..." if is_refreshing else "🔄 Actualizar"
    
    buy_text = f"🟢 Comprar {symbol}"
    sell_text = f"🔴 Vender {symbol}"
    
    # Si el usuario no tiene tokens, deshabilitar visualmente la opción de venta
    row1 = [
        InlineKeyboardButton(refresh_text, callback_data="BUY_REF"),
    ]
    
    if token_balance > 0:
        row2 = [
            InlineKeyboardButton(buy_text, callback_data="BUY_DETECTED_TOKEN"),
            InlineKeyboardButton(sell_text, callback_data="SELL_DETECTED_TOKEN")
        ]
    else:
        # En lugar de usar 'disabled', cambiamos el texto y callback para indicar que no está disponible
        row2 = [
            InlineKeyboardButton(buy_text, callback_data="BUY_DETECTED_TOKEN"),
            InlineKeyboardButton(f"❌ {sell_text} (No tokens)", callback_data="NO_TOKENS_TO_SELL")
        ]
    
    buttons.append(row1)
    buttons.append(row2)
    
    # Añadir opciones de cantidad de SOL si está en modo compra
    sol_options1 = []
    for sol_amount in [0.1, 0.5, 1.0]:
        text = f"{sol_amount} SOL"
        if sol_amount == sel:
            text = f"✅ {text}"
        sol_options1.append(InlineKeyboardButton(text, callback_data=f"A_{sol_amount}"))
    
    sol_options2 = []
    for sol_amount in [2.0, 5.0, 10.0]:
        text = f"{sol_amount} SOL"
        if sol_amount == sel:
            text = f"✅ {text}"
        sol_options2.append(InlineKeyboardButton(text, callback_data=f"A_{sol_amount}"))
    
    buttons.append(sol_options1)
    buttons.append(sol_options2)
    
    # Botón de confirmar compra y volver
    buttons.append([
        InlineKeyboardButton("💰 Confirmar Compra", callback_data="BUY_EXEC"),
        InlineKeyboardButton("↩️ Volver", callback_data="BACK")
    ])
    
    return InlineKeyboardMarkup(buttons)

# Función para construir un mensaje unificado
def build_unified_message(token_data, user_balance=0, symbol="???", name="Unknown Token",
                         mint="", price=0, marketcap=0, volume=0, liquidity=0, 
                         price_change=0, sol_balance=0, sol_price=100, 
                         selected_sol=0.5, estimated_tokens=0, current_value_usd=0,
                         slippage_pct=0, data_source="", fetch_time=0):
    """
    Construye un mensaje unificado que muestra información sobre el token
    tanto si el usuario lo tiene como si no.
    
    Returns:
        Mensaje formateado con Markdown
    """
    # Mensajes diferentes basados en si el usuario tiene el token o no
    has_token = user_balance > 0
    
    # Encabezado común
    msg = f"🏦 *SOLANA TRADING BOT* 🏦\n\n"
    
    # Información del token
    msg += f"🪙 *{symbol}* - {name}\n"
    msg += f"📍 *Mint:* `{mint}`\n"
    msg += f"_Toca para copiar_\n\n"
    
    # SECCIÓN POSICIÓN - Siempre mostrar esta sección
    msg += f"🧰 *POSICIÓN ACTUAL*\n"
    
    # Si el usuario tiene el token, mostrar información de su posición
    if has_token:
        msg += f"✅ *Tienes:* {user_balance:.4f} {symbol} (${current_value_usd:.2f})\n"
        
        # Mostrar info de PnL si está disponible en token_data
        entry_price = token_data.get('entry_price', 0)
        pnl_pct = token_data.get('pnl_pct', 0)
        pnl_usd = token_data.get('pnl_usd', 0)
        
        if entry_price > 0 and pnl_pct != 0:
            pnl_emoji = "🟢+" if pnl_pct >= 0 else "🔴"
            pnl_sign = "" if pnl_pct < 0 else "+"
            msg += f"📈 *PnL:* {pnl_emoji} {pnl_sign}{pnl_pct:.2f}% (${pnl_usd:.2f})\n"
            msg += f"🔍 *Precio entrada:* ${entry_price:.8f}\n"
    else:
        msg += f"❌ *No tienes tokens {symbol} en tu wallet*\n"
    
    # Separador
    msg += "\n───────────────────\n\n"
    
    # Balance de SOL (siempre mostrar)
    msg += f"💰 *Balance:* {sol_balance:.6f} SOL (${sol_balance * sol_price:.2f})\n"
    msg += f"📈 *SOL Price:* ${sol_price}\n\n"
    
    # Separador
    msg += "───────────────────\n\n"
    
    # Estadísticas del token
    msg += f"📊 *Token Stats*\n"
    msg += f"├USD:  ${price:.8f}"
    
    # Mostrar cambio de precio si está disponible
    if price_change != 0:
        change_emoji = "🟢+" if price_change > 0 else "🔴"
        msg += f" {change_emoji}{abs(price_change):.2f}%\n"
    else:
        msg += "\n"
        
    if marketcap > 0:
        msg += f"├MC:   ${marketcap:,.2f}\n"
    if volume > 0:
        msg += f"├Vol:  ${volume:,.2f}\n"
    if liquidity > 0:
        msg += f"└LP:   ${liquidity:,.2f}\n"
    
    # Información de transacción para compra (siempre mostrar)
    sol_value_usd = selected_sol * sol_price
    
    msg += f"\n💵 *Invirtiendo:* {selected_sol:.4f} SOL (${sol_value_usd:.2f})\n"
    msg += f"🪙 *Recibirás:* {estimated_tokens:.4f} {symbol}\n"
    
    if slippage_pct > 0:
        msg += f"📉 *Slippage:* {slippage_pct:.2f}%\n"
    
    # Información sobre la fuente de datos
    msg += f"\n🕒 *Datos:* {data_source}\n"
    if fetch_time > 0:
        msg += f"⏱️ *Tiempo:* {fetch_time}ms | {time.strftime('%H:%M:%S')}\n"
    
    msg += f"\n_Selecciona cuánto SOL quieres invertir:_"
    
    return msg

# Instrucciones para aplicar esta solución:
"""
Para implementar esta interfaz unificada:

1. Importar este archivo en bot.py:
   from .unified_interface import unified_keyboard, build_unified_message

2. Modificar la función show_buy para usar estas funciones:
   - Usar build_unified_message para construir el mensaje
   - Usar unified_keyboard para crear el teclado

3. Eliminar la bifurcación basada en has_token en la función on_msg
   para que siempre llame a show_buy con la información completa
""" 