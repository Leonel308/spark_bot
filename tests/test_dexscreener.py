import asyncio
import sys
import logging
import os

# Configurar el path para importar los módulos desde src
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.bot import pool_to_mint, extract

# Configurar logging para mostrar en consola
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("test_dexscreener")

async def test_dexscreener_link():
    # URL específica que hay que probar
    test_url = "https://dexscreener.com/solana/5elrsn6qdqtqsbf8kdw4b8mvpeeazhccwadptzmyszxh"
    print(f"Probando URL: {test_url}")
    
    # Extraer mint address usando la función de la aplicación
    mint, pool = await extract(test_url)
    
    if mint:
        print(f"✅ Éxito! Se encontró mint address: {mint}")
        print(f"Pool address original: {pool}")
        return True
    else:
        print(f"❌ Error: No se pudo extraer el mint address de {test_url}")
        return False

async def test_pool_to_mint_direct():
    # Pool address a probar directamente
    pool = "5elrsn6qdqtqsbf8kdw4b8mvpeeazhccwadptzmyszxh"
    print(f"Probando pool_to_mint directamente con pool: {pool}")
    
    # Intentar recuperar el mint address
    mint = await pool_to_mint(pool)
    
    if mint:
        print(f"✅ Éxito! pool_to_mint encontró mint address: {mint}")
        return True
    else:
        print(f"❌ Error: pool_to_mint no pudo encontrar el mint address para {pool}")
        return False

async def main():
    print("="*50)
    print("Iniciando pruebas de DexScreener...")
    print("="*50)
    
    # Probar la extracción desde URL completa
    url_result = await test_dexscreener_link()
    
    print("\n" + "="*50)
    
    # Probar la función pool_to_mint directamente
    pool_result = await test_pool_to_mint_direct()
    
    print("\n" + "="*50)
    
    if url_result and pool_result:
        print("✅✅ Todas las pruebas pasaron correctamente!")
        return 0
    else:
        print("❌❌ Al menos una prueba falló")
        return 1

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code) 