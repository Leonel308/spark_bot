"""
Módulo de integración con Jito para enviar transacciones con alta prioridad.
Basado en la implementación del repositorio github.com/henrytirla/Solana-Trading-Bot
"""

import logging
import time
import asyncio
from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_price, set_compute_unit_limit
from solders.system_program import transfer, TransferParams
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from .config import RPC_ENDPOINT

# Configurar logging
log = logging.getLogger(__name__)

# Constantes
LAMPORTS_PER_SOL = 1_000_000_000

class PriorityFeesManager:
    """
    Gestiona la configuración de comisiones prioritarias para transacciones.
    Permite optimizar el tiempo de confirmación de transacciones en la red Solana.
    """
    
    def __init__(self, rpc_endpoint=None):
        """
        Inicializa el gestor de comisiones prioritarias.
        
        Args:
            rpc_endpoint: Endpoint RPC a utilizar, por defecto usa el configurado
        """
        self.rpc_endpoint = rpc_endpoint or RPC_ENDPOINT
        self.solana_client = Client(self.rpc_endpoint)
        self.async_client = AsyncClient(self.rpc_endpoint)
        
        # Configuración por defecto para unidades de cómputo
        self.default_compute_limit = 200_000
        self.default_compute_price = 1_000  # micro-lamports
        
    async def get_optimal_compute_settings(self):
        """
        Determina la configuración óptima de compute units y precio
        basada en la congestión actual de la red.
        
        Returns:
            tuple: (compute_unit_limit, compute_unit_price)
        """
        try:
            # En una implementación avanzada, aquí consultaríamos la congestión de la red
            # Por ahora, usamos valores conservadores pero efectivos
            compute_limit = self.default_compute_limit
            
            # Intentar obtener el precio prioritario actual basado en transacciones recientes
            # Esto podría implementarse consultando las últimas transacciones confirmadas
            # y viendo qué precio de unidad computacional pagaron
            compute_price = self.default_compute_price
            
            # Si tenemos la API de Jito disponible, podríamos consultar el precio óptimo
            # pero eso requiere credenciales adicionales
            
            return compute_limit, compute_price
            
        except Exception as e:
            log.error(f"Error al determinar configuración óptima: {e}")
            # Devolver valores predeterminados en caso de error
            return self.default_compute_limit, self.default_compute_price
    
    def create_priority_fee_instructions(self, compute_limit=None, compute_price=None):
        """
        Crea las instrucciones para establecer comisiones prioritarias
        
        Args:
            compute_limit: Límite de unidades de cómputo
            compute_price: Precio por unidad de cómputo en micro-lamports
            
        Returns:
            list: Lista de instrucciones para priorizar la transacción
        """
        # Si no se proporcionan valores, usar los predeterminados
        compute_limit = compute_limit or self.default_compute_limit
        compute_price = compute_price or self.default_compute_price
        
        instructions = [
            set_compute_unit_limit(compute_limit),
            set_compute_unit_price(compute_price)
        ]
        
        return instructions
        
    async def send_with_priority(self, keypair, instructions, blockhash=None, compute_limit=None, compute_price=None):
        """
        Envía una transacción con comisiones prioritarias
        
        Args:
            keypair: Keypair del firmante
            instructions: Lista de instrucciones de la transacción
            blockhash: Blockhash a usar, si no se proporciona se obtiene uno nuevo
            compute_limit: Límite de unidades de cómputo personalizado
            compute_price: Precio por unidad de cómputo personalizado
            
        Returns:
            str: Firma de la transacción si tuvo éxito
        """
        try:
            # Si no se proporcionaron parámetros de compute, obtener los óptimos
            if not compute_limit or not compute_price:
                compute_limit, compute_price = await self.get_optimal_compute_settings()
            
            # Crear instrucciones para establecer comisiones prioritarias
            priority_instructions = self.create_priority_fee_instructions(compute_limit, compute_price)
            
            # Añadir instrucciones de prioridad al inicio de las instrucciones existentes
            final_instructions = priority_instructions + instructions
            
            # Obtener blockhash reciente si no se proporcionó
            if not blockhash:
                blockhash = self.solana_client.get_latest_blockhash().value.blockhash
            
            # Compilar mensaje de transacción
            message = MessageV0.try_compile(
                keypair.pubkey(),
                final_instructions,
                [],
                blockhash
            )
            
            # Crear transacción versionada (Versioned Transaction es más rápida)
            transaction = VersionedTransaction(message, [keypair])
            
            # Enviar transacción
            start_time = time.time()
            txn_sig = await self.async_client.send_transaction(
                txn=transaction,
                opts=TxOpts(skip_preflight=True)  # Skip preflight para mayor velocidad
            )
            
            log.info(f"Transacción enviada en {(time.time() - start_time)*1000:.2f}ms: {txn_sig.value}")
            
            # Esperar confirmación con timeout optimizado
            confirmation = await self.async_client.confirm_transaction(
                txn_sig.value,
                commitment=Confirmed,
                sleep_seconds=0.1  # Más agresivo que el valor por defecto
            )
            
            if confirmation.value and confirmation.value[0].err is None:
                log.info(f"Transacción confirmada en {(time.time() - start_time)*1000:.2f}ms")
                return txn_sig.value
            else:
                log.error(f"Error en confirmación: {confirmation.value[0].err if confirmation.value else 'Unknown'}")
                return None
            
        except Exception as e:
            log.error(f"Error enviando transacción prioritaria: {e}")
            return None

# Ejemplo de uso
async def test_priority_fee():
    from solana.keypair import Keypair
    
    # Crear instancia del gestor
    priority_manager = PriorityFeesManager()
    
    # Supongamos que tenemos una keypair
    keypair = Keypair.generate()
    
    # Crear una instrucción dummy para transferir 0 SOL a sí mismo
    instruction = transfer(
        TransferParams(
            from_pubkey=keypair.pubkey(),
            to_pubkey=keypair.pubkey(),
            lamports=0
        )
    )
    
    # Enviar con prioridad
    tx_sig = await priority_manager.send_with_priority(
        keypair=keypair,
        instructions=[instruction],
        compute_limit=300_000,  # Personalizado para este ejemplo
        compute_price=25_000    # Personalizado para este ejemplo
    )
    
    print(f"Transaction signature: {tx_sig}")
    
if __name__ == "__main__":
    # Código para probar este módulo independientemente
    asyncio.run(test_priority_fee()) 