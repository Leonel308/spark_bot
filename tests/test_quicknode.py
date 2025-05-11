import asyncio
import sys
import logging
import os
import time

# Configurar el path para importar los módulos desde src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.quicknode_client import get_pumpfun_data_qn, get_sol_price_usd_qn, start_cache_cleanup

# Configurar logging para mostrar en consola
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("test_quicknode")

# Token de prueba: POG
TEST_TOKEN = "5WTXGHAyxKuQP7JVpBfzVSHQNEyqCsP2FxKRxCQ5PUN2"

async def test_token_data():
    """Prueba la obtención de datos de token usando QuickNode"""
    print(f"Probando obtención de datos para token: {TEST_TOKEN}")
    
    # Medir tiempo
    start_time = time.time()
    
    # Primera llamada (sin caché)
    token_data = await get_pumpfun_data_qn(TEST_TOKEN, force_refresh=True)
    
    elapsed = time.time() - start_time
    
    if token_data:
        print(f"✅ Datos obtenidos en {elapsed:.2f} segundos:")
        print(f"  • Nombre: {token_data.get('name')}")
        print(f"  • Símbolo: {token_data.get('sym')}")
        print(f"  • Precio: ${token_data.get('price', 0):.8f}")
        print(f"  • Market Cap: ${token_data.get('mc', 0):,.2f}")
        print(f"  • Liquidez: ${token_data.get('lp', 0):,.2f}")
        print(f"  • Fuente de datos: {token_data.get('source', 'desconocida')}")
        
        # Probar caché
        print("\nProbando caché (debería ser más rápido)...")
        cache_start = time.time()
        cached_data = await get_pumpfun_data_qn(TEST_TOKEN)
        cache_elapsed = time.time() - cache_start
        
        print(f"✅ Datos de caché obtenidos en {cache_elapsed:.4f} segundos")
        print(f"  • Mejora de velocidad: {elapsed/cache_elapsed:.1f}x más rápido")
        
        # Obtener precio de SOL
        sol_price = await get_sol_price_usd_qn()
        print(f"\nPrecio de SOL: ${sol_price:.2f}")
        
        return True
    else:
        print("❌ No se pudieron obtener datos del token")
        return False

async def main():
    """Función principal de prueba"""
    print("="*60)
    print("Prueba de integración con QuickNode")
    print("="*60)
    
    # Iniciar servicio de limpieza de caché
    start_cache_cleanup()
    
    # Ejecutar prueba
    success = await test_token_data()
    
    print("\n"+"="*60)
    print(f"Resultado: {'✅ Todo en orden' if success else '❌ Error'}")
    
    return 0 if success else 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code) 