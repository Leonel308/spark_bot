import json, os
from pathlib import Path
from cryptography.fernet import Fernet

from .config import ENCRYPTION_KEY, BOT_FEE_PERCENTAGE

DB_PATH = Path(__file__).parent / "bot_db.json"
fernet = Fernet(ENCRYPTION_KEY.encode())

if not DB_PATH.exists():
    with open(DB_PATH, "w") as f:
        json.dump({}, f)

def _read_db():
    with open(DB_PATH) as f:
        return json.load(f)

def _write_db(db):
    with open(DB_PATH, "w") as f:
        json.dump(db, f)

def user_exists(uid: int) -> bool:
    return str(uid) in _read_db()

def add_user(uid: int, pubkey: str) -> None:
    db = _read_db()
    db[str(uid)] = {"pubkey": pubkey, "transactions": [], "positions": {}}
    _write_db(db)

def get_pubkey(uid: int) -> str:
    return _read_db()[str(uid)]["pubkey"]

# --- Nuevas funciones para transacciones y posiciones ---

def record_transaction(uid: int, mint: str, tx_type: str, amount: float, token_amount: float, price_usd: float, tx_hash: str = None):
    """
    Registra una transacción de compra o venta
    
    Args:
        uid: ID del usuario
        mint: Dirección del token
        tx_type: "buy" o "sell"
        amount: Cantidad de SOL usada o recibida
        token_amount: Cantidad de tokens comprados o vendidos
        price_usd: Precio del token en USD al momento de la transacción
        tx_hash: Hash de la transacción en blockchain
    """
    db = _read_db()
    user_data = db[str(uid)]
    
    # Asegurar que exista la estructura de datos
    if "transactions" not in user_data:
        user_data["transactions"] = []
    
    if "positions" not in user_data:
        user_data["positions"] = {}
    
    # Calcular comisión del bot
    fee_percentage = BOT_FEE_PERCENTAGE
    fee_amount = amount * (fee_percentage / 100)
    
    # Cantidad neta (después de comisiones)
    net_amount = amount - fee_amount if tx_type == "sell" else amount
    swap_amount = amount - fee_amount if tx_type == "buy" else amount
    
    # Registrar la transacción
    import time
    transaction = {
        "mint": mint,
        "type": tx_type,
        "sol_amount": amount,
        "token_amount": token_amount,
        "price_usd": price_usd,
        "timestamp": int(time.time()),
        "tx_hash": tx_hash,
        "fee_percentage": fee_percentage,
        "fee_amount": fee_amount,
        "net_amount": net_amount if tx_type == "sell" else None,
        "swap_amount": swap_amount if tx_type == "buy" else None
    }
    user_data["transactions"].append(transaction)
    
    # Actualizar la posición
    if mint not in user_data["positions"]:
        user_data["positions"][mint] = {
            "total_bought": 0,
            "total_sold": 0,
            "avg_buy_price": 0,
            "avg_sell_price": 0,
            "total_cost_sol": 0,
            "total_sold_sol": 0,
            "total_fees_paid": 0
        }
    
    position = user_data["positions"][mint]
    position["total_fees_paid"] = position.get("total_fees_paid", 0) + fee_amount
    
    if tx_type == "buy":
        # Actualizar precio promedio de compra
        total_tokens = position["total_bought"] + token_amount
        if total_tokens > 0:
            position["avg_buy_price"] = ((position["total_bought"] * position["avg_buy_price"]) + 
                                        (token_amount * price_usd)) / total_tokens
        position["total_bought"] += token_amount
        position["total_cost_sol"] += amount
    
    elif tx_type == "sell":
        # Actualizar precio promedio de venta
        total_sold = position["total_sold"] + token_amount
        if total_sold > 0:
            position["avg_sell_price"] = ((position["total_sold"] * position["avg_sell_price"]) + 
                                         (token_amount * price_usd)) / total_sold
        position["total_sold"] += token_amount
        position["total_sold_sol"] += amount
    
    # Guardar cambios
    db[str(uid)] = user_data
    _write_db(db)

def get_position_data(uid: int, mint: str) -> dict:
    """
    Obtiene datos de posición para un token específico
    
    Returns:
        Diccionario con datos de la posición o None si no existe
    """
    db = _read_db()
    user_data = db.get(str(uid), {})
    positions = user_data.get("positions", {})
    
    return positions.get(mint, None)

def get_all_positions_data(uid: int) -> dict:
    """
    Obtiene datos de todas las posiciones del usuario
    
    Returns:
        Diccionario con todas las posiciones
    """
    db = _read_db()
    user_data = db.get(str(uid), {})
    
    return user_data.get("positions", {})

def get_transaction_history(uid: int, mint: str = None, limit: int = 10) -> list:
    """
    Obtiene historial de transacciones, filtrado por token si se especifica
    
    Args:
        uid: ID del usuario
        mint: Dirección del token o None para todos
        limit: Número máximo de transacciones a retornar
    
    Returns:
        Lista de transacciones, ordenadas por más recientes primero
    """
    db = _read_db()
    user_data = db.get(str(uid), {})
    transactions = user_data.get("transactions", [])
    
    # Filtrar por token si se especifica
    if mint:
        transactions = [tx for tx in transactions if tx["mint"] == mint]
    
    # Ordenar por timestamp descendente (más reciente primero)
    transactions.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    
    # Limitar número de resultados
    return transactions[:limit]

def get_total_fees_paid(uid: int) -> float:
    """
    Obtiene el total de comisiones pagadas por un usuario en todas sus transacciones
    
    Args:
        uid: ID del usuario
        
    Returns:
        Cantidad total de SOL pagada en comisiones
    """
    db = _read_db()
    user_data = db.get(str(uid), {})
    transactions = user_data.get("transactions", [])
    
    total_fees = 0.0
    for tx in transactions:
        fee_amount = tx.get("fee_amount", 0)
        total_fees += fee_amount
    
    return total_fees
