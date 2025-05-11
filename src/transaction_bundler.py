"""
Módulo para agrupar transacciones en Solana usando técnicas de bundling.
Basado en la implementación del repositorio github.com/henrytirla/Solana-Trading-Bot
"""

import logging
import asyncio
import time
from typing import List, Optional, Dict, Any
from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from .config import RPC_ENDPOINT

# Configurar logging
log = logging.getLogger(__name__)

class TransactionBundler:
    """
    Agrupa múltiples transacciones para enviarlas como un conjunto.
    Esto mejora la eficiencia y reduce la posibilidad de errores parciales.
    """
    
    def __init__(self, rpc_endpoint=None):
        """
        Inicializa el agrupador de transacciones.
        
        Args:
            rpc_endpoint: Endpoint RPC a utilizar
        """
        self.rpc_endpoint = rpc_endpoint or RPC_ENDPOINT
        self.solana_client = Client(self.rpc_endpoint)
        self.async_client = AsyncClient(self.rpc_endpoint)
        self.bundle_queue = []
        self.max_bundle_size = 5  # Máximo número de transacciones por bundle
        
    def add_transaction(self, tx_instructions, keypair, label=None):
        """
        Añade una transacción al bundle actual.
        
        Args:
            tx_instructions: Lista de instrucciones para la transacción
            keypair: Keypair para firmar la transacción
            label: Etiqueta opcional para identificar la transacción
            
        Returns:
            int: Índice de la transacción en el bundle
        """
        tx_data = {
            "instructions": tx_instructions,
            "keypair": keypair,
            "label": label or f"tx_{len(self.bundle_queue)}"
        }
        
        self.bundle_queue.append(tx_data)
        return len(self.bundle_queue) - 1
    
    def clear_bundle(self):
        """Limpia el bundle actual sin enviarlo"""
        self.bundle_queue = []
    
    async def send_bundle(self, wait_for_all_confirmations=True) -> Dict[str, Any]:
        """
        Envía el bundle actual como un conjunto de transacciones.
        
        Args:
            wait_for_all_confirmations: Si es True, espera a que todas las transacciones se confirmen
            
        Returns:
            Dict: Resultados del envío con firmas y estados
        """
        if not self.bundle_queue:
            log.warning("Intento de enviar un bundle vacío")
            return {"success": False, "error": "Bundle vacío", "transactions": []}
        
        log.info(f"Preparando bundle con {len(self.bundle_queue)} transacciones")
        
        # Obtener un blockhash fresco para todas las transacciones
        try:
            blockhash = self.solana_client.get_latest_blockhash().value.blockhash
            log.info(f"Obtenido blockhash: {blockhash}")
        except Exception as e:
            log.error(f"Error obteniendo blockhash: {e}")
            return {"success": False, "error": f"Error de blockhash: {str(e)}", "transactions": []}
        
        results = {"success": True, "transactions": []}
        versioned_txs = []
        
        # Preparar todas las transacciones
        for i, tx_data in enumerate(self.bundle_queue):
            try:
                # Compilar mensaje
                message = MessageV0.try_compile(
                    tx_data["keypair"].pubkey(),
                    tx_data["instructions"],
                    [],
                    blockhash
                )
                
                # Crear transacción versionada
                versioned_tx = VersionedTransaction(message, [tx_data["keypair"]])
                versioned_txs.append({
                    "tx": versioned_tx,
                    "index": i,
                    "label": tx_data["label"]
                })
            except Exception as e:
                log.error(f"Error preparando transacción {i} ({tx_data['label']}): {e}")
                results["transactions"].append({
                    "index": i,
                    "label": tx_data["label"],
                    "success": False,
                    "error": f"Error de preparación: {str(e)}"
                })
        
        # Enviar transacciones
        send_tasks = []
        for tx_info in versioned_txs:
            task = asyncio.create_task(
                self._send_and_confirm_single_tx(
                    tx_info["tx"], 
                    tx_info["index"], 
                    tx_info["label"],
                    wait_for_confirmation=wait_for_all_confirmations
                )
            )
            send_tasks.append(task)
        
        # Esperar a que todas las transacciones se envíen/confirmen
        tx_results = await asyncio.gather(*send_tasks, return_exceptions=True)
        
        # Procesar resultados
        for result in tx_results:
            if isinstance(result, Exception):
                log.error(f"Error general en transacción: {result}")
                results["success"] = False
                results["transactions"].append({
                    "success": False,
                    "error": f"Error general: {str(result)}"
                })
            else:
                results["transactions"].append(result)
                if not result["success"]:
                    results["success"] = False
        
        # Limpiar el bundle
        self.clear_bundle()
        
        return results
    
    async def _send_and_confirm_single_tx(self, tx, index, label, wait_for_confirmation=True):
        """
        Envía y confirma una única transacción dentro del bundle.
        
        Args:
            tx: Transacción versionada
            index: Índice en el bundle
            label: Etiqueta de la transacción
            wait_for_confirmation: Si es True, espera la confirmación
            
        Returns:
            Dict: Resultado del envío con firma y estado
        """
        result = {
            "index": index,
            "label": label,
            "success": False
        }
        
        try:
            # Enviar transacción
            start_time = time.time()
            txn_resp = await self.async_client.send_transaction(
                txn=tx,
                opts=TxOpts(skip_preflight=True)  # Saltar preflight para mayor velocidad
            )
            
            send_time = time.time() - start_time
            signature = txn_resp.value
            
            result["signature"] = signature
            result["send_time_ms"] = int(send_time * 1000)
            result["solscan_url"] = f"https://solscan.io/tx/{signature}"
            
            log.info(f"TX {label} enviada en {send_time*1000:.2f}ms - Sig: {signature}")
            
            # Si no necesitamos esperar confirmación, terminamos aquí
            if not wait_for_confirmation:
                result["success"] = True
                result["confirmed"] = False
                result["message"] = "Transacción enviada, no se esperó confirmación"
                return result
            
            # Esperar confirmación con timeout optimizado
            confirm_start = time.time()
            confirmation = await self.async_client.confirm_transaction(
                signature,
                commitment=Confirmed,
                sleep_seconds=0.1  # Más agresivo que el por defecto
            )
            
            confirm_time = time.time() - confirm_start
            result["confirm_time_ms"] = int(confirm_time * 1000)
            result["total_time_ms"] = int((time.time() - start_time) * 1000)
            
            if confirmation.value and confirmation.value[0].err is None:
                log.info(f"TX {label} confirmada en {confirm_time*1000:.2f}ms")
                result["success"] = True
                result["confirmed"] = True
                result["message"] = "Transacción confirmada exitosamente"
            else:
                err = confirmation.value[0].err if confirmation.value else "Unknown"
                log.error(f"TX {label} error en confirmación: {err}")
                result["success"] = False
                result["confirmed"] = False
                result["error"] = f"Error en confirmación: {err}"
        
        except Exception as e:
            log.error(f"TX {label} error al enviar: {e}")
            result["success"] = False
            result["error"] = f"Error al enviar: {str(e)}"
        
        return result

# Ejemplo de uso
async def test_bundler():
    from solders.keypair import Keypair
    from solders.system_program import transfer, TransferParams
    
    # Crear instancia del agrupador
    bundler = TransactionBundler()
    
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
    
    # Añadir transacciones al bundle
    bundler.add_transaction([instruction], keypair, "tx_test_1")
    bundler.add_transaction([instruction], keypair, "tx_test_2")
    
    # Enviar bundle
    results = await bundler.send_bundle()
    
    print(f"Bundle results: {results}")
    
if __name__ == "__main__":
    # Código para probar este módulo independientemente
    asyncio.run(test_bundler()) 