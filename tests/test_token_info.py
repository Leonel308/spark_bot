import asyncio
import logging
import sys

# Configurar logging para ver la salida
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)

# Importar funciones a probar
from src.token_info import _get_token_ath, _get_token_price_change_1h

async def main():
    # Token de prueba (JUP - Jupiter Governance Token)
    token_mint = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
    
    print("\n---------- TEST ATH ----------")
    print(f"Obteniendo ATH para token: {token_mint}")
    ath_data = await _get_token_ath(token_mint)
    print(f"Resultado ATH: {ath_data}")
    
    print("\n---------- TEST CAMBIO 1H ----------")
    print(f"Obteniendo cambio de precio 1H para token: {token_mint}")
    change_1h = await _get_token_price_change_1h(token_mint)
    print(f"Resultado cambio 1H: {change_1h}")
    
    # Probar con otro token (SOL)
    sol_mint = "So11111111111111111111111111111111111111112"
    print("\n---------- TEST ATH (SOL) ----------")
    print(f"Obteniendo ATH para SOL: {sol_mint}")
    sol_ath_data = await _get_token_ath(sol_mint)
    print(f"Resultado ATH SOL: {sol_ath_data}")
    
    print("\n---------- TEST CAMBIO 1H (SOL) ----------")
    print(f"Obteniendo cambio de precio 1H para SOL: {sol_mint}")
    sol_change_1h = await _get_token_price_change_1h(sol_mint)
    print(f"Resultado cambio 1H SOL: {sol_change_1h}")

if __name__ == "__main__":
    asyncio.run(main()) 