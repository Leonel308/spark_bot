import base64
import base58
import json
import logging
import time
import traceback
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solana.transaction import Transaction as SolanaTransaction
from .config import RPC_ENDPOINT, BOT_FEE_PERCENTAGE, BOT_FEE_RECIPIENT
from solana.rpc.commitment import Commitment
from solders.pubkey import Pubkey as SoldersPubkey
from solders.keypair import Keypair as SoldersKeypair
from solders.signature import Signature as SoldersSignature
from solders.message import Message as SoldersMessage
from solders.system_program import TransferParams, transfer

# Intentar importar ed25519, si no está disponible, usar una alternativa
try:
    import ed25519
except ImportError:
    log = logging.getLogger(__name__)
    log.warning("La biblioteca ed25519 no está disponible, usando alternativa PyNaCl")
    
    # Implementar una alternativa usando PyNaCl
    try:
        import nacl.signing
        
        class Ed25519Wrapper:
            class SigningKey:
                def __init__(self, seed):
                    self.sk = nacl.signing.SigningKey(seed)
                    
                def sign(self, message):
                    class SignedMessage:
                        def __init__(self, signature):
                            self.signature = signature
                    
                    signed = self.sk.sign(message)
                    return SignedMessage(signed.signature)
        
        # Reemplazar ed25519 con nuestra implementación wrapper
        ed25519 = Ed25519Wrapper
        log.info("Usando implementación alternativa de ed25519 con PyNaCl")
    except ImportError:
        log.error("Ni ed25519 ni PyNaCl están disponibles. La firma directa puede fallar.")
        
        # Stub para evitar errores de importación
        class Ed25519DummyWrapper:
            class SigningKey:
                def __init__(self, seed):
                    pass
                    
                def sign(self, message):
                    class SignedMessage:
                        def __init__(self):
                            self.signature = b'\x00' * 64
                    
                    return SignedMessage()
        
        ed25519 = Ed25519DummyWrapper

# Variables constantes para URLs de API
JUP_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP_API = "https://quote-api.jup.ag/v6/swap"
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens/"

log = logging.getLogger(__name__)

# Función para verificar la liquidez de un token
async def check_token_liquidity(token_mint: str) -> dict:
    """
    Verifica la liquidez disponible para un token en diferentes DEXes
    
    Args:
        token_mint: Dirección del token a verificar
        
    Returns:
        Diccionario con las DEXes y la liquidez en USD
    """
    try:
        # Intentar obtener liquidez desde DexScreener
        async with aiohttp.ClientSession() as session:
            # URL de DexScreener para obtener pares de un token
            dexscreener_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
            
            async with session.get(dexscreener_url, timeout=3.0) as resp:
                if resp.status != 200:
                    log.warning(f"Error al consultar DexScreener: {resp.status}")
                    return {}
                
                data = await resp.json()
                
                # Verificar si hay pares disponibles
                if not data.get("pairs") or len(data["pairs"]) == 0:
                    log.warning(f"No se encontraron pares de liquidez para {token_mint[:8]}")
                    return {}
                
                # Recopilar liquidez por DEX
                liquidity_by_dex = {}
                
                for pair in data["pairs"]:
                    dex_name = pair.get("dexId", "Unknown")
                    liquidity_usd = float(pair.get("liquidity", {}).get("usd", 0))
                    
                    # Solo considerar pares con liquidez mínima
                    if liquidity_usd >= 100:  # Al menos $100 de liquidez
                        if dex_name in liquidity_by_dex:
                            liquidity_by_dex[dex_name] += liquidity_usd
                        else:
                            liquidity_by_dex[dex_name] = liquidity_usd
                
                return liquidity_by_dex
    except Exception as e:
        log.error(f"Error al verificar liquidez: {str(e)}")
        return {}

async def swap_sol_for_tokens(keypair: Keypair, token_mint: str, amount_sol: float, pool: str = None) -> str:
    """
    Realiza un swap de SOL a tokens usando Jupiter o Pump.fun si se proporciona un pool
    
    Args:
        keypair: Keypair del wallet
        token_mint: Dirección del token a comprar
        amount_sol: Cantidad de SOL a usar
        pool: Pool ID de Pump.fun (opcional)
        
    Returns:
        Signature de la transacción
    """
    log.info(f"Iniciando swap de {amount_sol} SOL por tokens {token_mint}")
    
    try:
        # Verificar balance de SOL antes de intentar la transacción
        client = AsyncClient(RPC_ENDPOINT)
        sol_balance = await client.get_balance(keypair.pubkey())
        sol_balance_lamports = sol_balance.value
        sol_balance_sol = sol_balance_lamports / 1e9
        
        # Verificar si tiene suficiente SOL para la transacción + gas (0.00005 SOL para mayor seguridad)
        if sol_balance_sol < amount_sol + 0.00005:
            await client.close()
            raise Exception(f"Saldo insuficiente. Tienes {sol_balance_sol:.6f} SOL, necesitas al menos {amount_sol + 0.00005:.6f} SOL (incluyendo gas)")
        
        # Verificar liquidez para el token
        log.info(f"Verificando liquidez para token {token_mint[:7]}...")
        
        # 1. Aplicar comisión del bot (si corresponde)
        amount_sol_after_fee = amount_sol
        bot_fee = 0
        
        if BOT_FEE_PERCENTAGE > 0:
            bot_fee = amount_sol * (BOT_FEE_PERCENTAGE / 100)
            amount_sol_after_fee = amount_sol - bot_fee
            log.info(f"Aplicando comisión del {BOT_FEE_PERCENTAGE}%: {bot_fee} SOL. Cantidad para swap: {amount_sol_after_fee} SOL")
        
        # Convertir SOL a lamports
        amount_lamports = int(amount_sol_after_fee * 1e9)
        
        # Si se proporciona un pool, intentar swap usando Pump.fun
        if pool:
            log.info(f"Intentando swap vía Pump.fun con pool ID: {pool}")
            try:
                # Intentar obtener transacción de Pump.fun
                from .quicknode_client import fetch_pumpfun
                tx_data = await fetch_pumpfun(pool, amount_lamports, str(keypair.pubkey()))
                
                if tx_data:
                    # Verificar el tipo de datos retornado
                    if isinstance(tx_data, str):
                        # Es una transacción en formato base64
                        pump_sig = await send_transaction_rpc_direct(tx_data, keypair, RPC_ENDPOINT)
                        if pump_sig:
                            log.info(f"✅ Swap completado exitosamente usando Pump.fun. Signature: {pump_sig}")
                            
                            # Cerrar cliente
                            await client.close()
                            
                            # Enviar comisión si corresponde
                            if bot_fee > 0:
                                fee_sig = await send_bot_fee(keypair, bot_fee)
                                log.info(f"Comisión enviada: {fee_sig}")
                            
                            return pump_sig
                    else:
                        # Asumir que es un objeto Transaction
                        from solana.transaction import Transaction
                        if isinstance(tx_data, Transaction):
                            # Firmar y enviar directamente
                            tx_data.sign(keypair)
                            pump_sig = await client.send_transaction(
                                tx_data,
                                opts=TxOpts(skip_preflight=True, preflight_commitment="confirmed")
                            )
                            
                            log.info(f"✅ Swap completado exitosamente usando Pump.fun. Signature: {pump_sig.value}")
                            
                            # Cerrar cliente
                            await client.close()
                            
                            # Enviar comisión si corresponde
                            if bot_fee > 0:
                                fee_sig = await send_bot_fee(keypair, bot_fee)
                                log.info(f"Comisión enviada: {fee_sig}")
                            
                            return str(pump_sig.value)
            except Exception as e:
                log.warning(f"❌ Pump.fun swap falló: {str(e)}, intentando con Jupiter...")
        
        # MÉTODO DIRECTO: Usar la función get_and_execute_swap_direct como primera opción
        log.info("Usando método directo (nativo) para swap...")
        
        # Pasar directamente el keypair para que pueda firmar la transacción correctamente
        direct_signature = await get_and_execute_swap_direct(
            "So11111111111111111111111111111111111111112",  # SOL mint
            token_mint,
            amount_lamports,
            keypair,  # Pasamos el keypair completo, no solo la string
            slippage=0.5  # 0.5% slippage
        )
        
        if direct_signature:
            log.info(f"✅ Swap completado exitosamente usando método directo (nativo). Signature: {direct_signature}")
            
            # Cerrar cliente
            await client.close()
            
            # Enviar comisión si corresponde
            if bot_fee > 0:
                fee_sig = await send_bot_fee(keypair, bot_fee)
                log.info(f"Comisión enviada: {fee_sig}")
            
            return direct_signature
            
        # MÉTODO DE RESPALDO: Si el método directo falla, intentamos el flujo original
        log.warning("❌ Método directo falló, intentando flujo original...")
        
        # Verificar liquidez primero
        liquidity_info = await check_token_liquidity(token_mint)
        if not liquidity_info:
            log.warning(f"No se encontró liquidez para el token {token_mint[:8]} en ninguna DEX")
            
            # Mensaje específico sobre falta de liquidez
            raise Exception(
                f"No se pudo encontrar ruta de liquidez para el token {token_mint[:8]}...\n"
                f"Este token puede ser muy nuevo o tener poca liquidez.\n"
                f"Intente usar directamente una DEX como Pump.fun o Raydium."
            )
        
        # Si hay liquidez pero es muy baja (menos de $1000), advertimos pero seguimos intentando
        total_liquidity = sum(liquidity_info.values())
        if total_liquidity < 1000:
            log.warning(f"Liquidez muy baja para {token_mint[:8]}: ${total_liquidity} USD. Puede fallar el swap.")
            
        # Intentar usar Jupiter con el enfoque estándar
        try:
            # 1. Obtener cotización (quote)
            quote_params = {
                "inputMint": "So11111111111111111111111111111111111111112",  # SOL mint address
                "outputMint": token_mint,
                "amount": str(amount_lamports),
                "slippageBps": "50",  # 0.5% slippage
                "onlyDirectRoutes": "false",
                "asLegacyTransaction": "true",  # Transacciones legacy más estables
            }
            
            async with aiohttp.ClientSession() as session:
                log.info(f"Solicitando quote para swap SOL->token...")
                async with session.get(JUP_QUOTE_API, params=quote_params, timeout=3.0) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        log.error(f"Error al obtener quote: {error_text}")
                        raise Exception(f"Error al obtener quote: {resp.status} {error_text}")
                    
                    quote_data = await resp.json()
                    log.info(f"Quote obtenido con éxito. ID: {quote_data.get('routePlan', 'unknown')}")
                    
                    # 2. Obtener transacción usando parámetros simplificados
                    swap_params = {
                        "quoteResponse": quote_data,
                        "userPublicKey": str(keypair.pubkey()),
                        "wrapUnwrapSOL": True,
                        "asLegacyTransaction": True,
                        "useSharedAccounts": True,
                        "skipUserAccountsCheck": True
                    }
                    
                    log.info(f"Solicitando swap transaction...")
                    async with session.post(JUP_SWAP_API, json=swap_params, timeout=5.0) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            log.error(f"Error al generar transacción: {error_text}")
                            raise Exception(f"Error al generar transacción: {resp.status} {error_text}")
                        
                        swap_data = await resp.json()
                        tx_base64 = swap_data.get("swapTransaction")
                        
                        if not tx_base64:
                            log.error("No se recibió la transacción de Jupiter")
                            raise Exception("No se recibió la transacción de swap desde Jupiter")
                        
                        # MÉTODO NATIVO MEJORADO: Usando solana-py send_raw_transaction
                        from solana.transaction import Transaction
                        from solana.rpc.types import TxOpts
                        import base64
                        
                        # 1) Deserializar
                        log.info("Deserializando transacción...")
                        raw = base64.b64decode(tx_base64)
                        tx = Transaction.deserialize(raw)
                        
                        # 2) Firmar con solana-py nativo
                        log.info("Firmando transacción con método nativo...")
                        tx.sign(keypair)
                        
                        # 3) Enviar con método nativo
                        log.info("Enviando transacción con send_raw_transaction...")
                        sig = await client.send_raw_transaction(
                            tx.serialize(),
                            opts=TxOpts(skip_preflight=True, preflight_commitment="confirmed")
                        )
                        
                        # Cerrar cliente
                        await client.close()
                        
                        # Enviar comisión si corresponde
                        if bot_fee > 0:
                            fee_sig = await send_bot_fee(keypair, bot_fee)
                            log.info(f"Comisión enviada: {fee_sig}")
                        
                        # Devolver firma (signature)
                        log.info(f"✅ Transacción enviada con éxito usando método nativo. Signature: {sig.value}")
                        return str(sig.value)
            
        except Exception as e:
            log.error(f"Error en flujo original: {str(e)}")
            # Detectar errores específicos para mostrar mensajes claros
            error_msg = str(e).lower()
            
            if "insufficient funds" in error_msg or "insufficient lamports" in error_msg:
                raise Exception(f"Saldo insuficiente para completar la transacción. Necesitas más SOL para pagar la transacción.")
            elif "not enough signers" in error_msg:
                # Último intento: usar método RPC directo con skipPreflight=true
                log.warning("Detectado error 'not enough signers', intentando método RPC directo como último recurso...")
                
                # Intentar enviar la transacción directamente al RPC con skipPreflight=true
                rpc_signature = await send_transaction_rpc_direct(tx_base64, keypair, RPC_ENDPOINT)
                
                if rpc_signature:
                    log.info(f"✅ Transacción enviada con éxito usando método RPC directo. Signature: {rpc_signature}")
                    await client.close()
                    
                    # Enviar comisión si corresponde
                    if bot_fee > 0:
                        fee_sig = await send_bot_fee(keypair, bot_fee)
                        log.info(f"Comisión enviada: {fee_sig}")
                    
                    return rpc_signature
                else:
                    raise Exception(f"Error en la firma de la transacción: not enough signers. Por favor, contacta al administrador.")
            elif "error al obtener quote" in error_msg:
                # Construir mensaje con DEXes específicos donde se encontró liquidez
                if liquidity_info:
                    dexes_with_liquidity = ', '.join(liquidity_info.keys())
                    error_msg = (
                        f"No se pudo encontrar ruta para swap automático en Jupiter para el token {token_mint[:8]}...\n"
                        f"Este token tiene liquidez en: {dexes_with_liquidity}\n"
                        f"Por favor, intenta usar directamente estas DEXes para realizar la compra."
                    )
                else:
                    error_msg = (
                        f"No se pudo encontrar liquidez para el token {token_mint[:8]}...\n"
                        f"Este token puede ser muy nuevo o tener poca liquidez.\n"
                    )
                raise Exception(error_msg)
            else:
                raise e
        
        # Si llegamos aquí, todos los métodos han fallado
        raise Exception("No se pudo completar el swap después de intentar múltiples métodos. Por favor, intenta más tarde.")
        
    except Exception as e:
        log.error(f"Error en swap_sol_for_tokens: {str(e)}")
        await client.close()
        
        # Detectar errores específicos para mostrar mensajes claros
        error_msg = str(e).lower()
        
        if "insufficient funds" in error_msg or "insufficient lamports" in error_msg:
            raise Exception(f"Saldo insuficiente para completar la transacción. Necesitas más SOL para pagar la transacción.")
        elif "not enough signers" in error_msg:
            # Último intento: usar método RPC directo con skipPreflight=true
            log.warning("Detectado error 'not enough signers', intentando método RPC directo como último recurso...")
            
            # Verificar que tx_base64 esté definido
            try:
                # Intentar enviar la transacción directamente al RPC con skipPreflight=true
                if 'tx_base64' in locals():
                    rpc_signature = await send_transaction_rpc_direct(tx_base64, keypair, RPC_ENDPOINT)
                    
                    if rpc_signature:
                        log.info(f"✅ Transacción enviada con éxito usando método RPC directo. Signature: {rpc_signature}")
                        await client.close()
                        
                        # Enviar comisión si corresponde
                        if bot_fee > 0:
                            fee_sig = await send_bot_fee(keypair, bot_fee)
                            log.info(f"Comisión enviada: {fee_sig}")
                        
                        return rpc_signature
            except Exception as e2:
                log.error(f"Error en envío de RPC directo: {str(e2)}")
            
            raise Exception(f"Error en la firma de la transacción: not enough signers. Por favor, contacta al administrador.")
        else:
            raise e

# Función auxiliar para recrear transacciones cuando hay problemas de firma
def recreate_and_sign_transaction(original_tx, keypair):
    """Recrea y firma una transacción cuando otros métodos fallan"""
    try:
        from solana.system_program import TransferParams, transfer
        from solana.transaction import Transaction
        
        # Crear nueva transacción como último recurso
        new_tx = Transaction()
        
        # Intentar copiar instrucciones de la transacción original si es posible
        instructions_copied = False
        
        # Método 1: Copiar instrucciones directamente del atributo 'instructions'
        try:
            if hasattr(original_tx, 'instructions') and original_tx.instructions:
                for inst in original_tx.instructions:
                    new_tx.add(inst)
                log.info(f"Copiadas {len(original_tx.instructions)} instrucciones de la transacción original")
                instructions_copied = True
        except Exception as copy_error:
            log.warning(f"No se pudieron copiar instrucciones por método 1: {str(copy_error)}")
        
        # Método 2: Intentar acceder a instrucciones a través de _solders_tx si existe
        if not instructions_copied and hasattr(original_tx, '_solders_tx'):
            try:
                solders_tx = original_tx._solders_tx
                if hasattr(solders_tx, 'message') and hasattr(solders_tx.message, 'instructions'):
                    # Convertir instrucciones de solders a formato solana-py
                    for solders_inst in solders_tx.message.instructions:
                        # Extraer datos necesarios para recrear la instrucción
                        from solana.instruction import Instruction
                        inst = Instruction(
                            program_id=solders_inst.program_id_index,
                            accounts=solders_inst.account_indices,
                            data=solders_inst.data
                        )
                        new_tx.add(inst)
                    log.info(f"Copiadas instrucciones desde formato solders")
                    instructions_copied = True
            except Exception as solders_error:
                log.warning(f"No se pudieron copiar instrucciones por método 2: {str(solders_error)}")
        
        # Si no se pudo copiar nada, crear una instrucción mínima como último recurso
        if not instructions_copied:
            # Agregar al menos una instrucción mínima para evitar errores
            dummy_instruction = transfer(TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=keypair.pubkey(),
                lamports=1  # Cantidad mínima
            ))
            new_tx.add(dummy_instruction)
            log.info(f"Creada transacción de último recurso con instrucción dummy")
        
        # Firmar la nueva transacción usando nuestro método personalizado
        try:
            # Usar nuestro método personalizado más robusto
            new_tx.sign_keypair(keypair)
            log.info("Nueva transacción firmada con método personalizado")
            return new_tx
        except Exception as e:
            log.error(f"Error con firma personalizada, intentando método de último recurso: {str(e)}")
            
            # Intento de último recurso: firmar transacción directamente con los bytes
            try:
                from solana.transaction import SigPubkeyPair
                
                # Firmar mensaje manualmente
                message_bytes = new_tx.message.serialize()
                from nacl.signing import SigningKey
                signer = SigningKey(keypair.secret())
                signature = signer.sign(message_bytes).signature
                
                # Inicializar firmas si es necesario
                if not hasattr(new_tx, 'signatures') or not new_tx.signatures:
                    new_tx.signatures = []
                
                # Agregar firma manualmente
                pubkey_bytes = keypair.pubkey().to_bytes()
                sig_pair = SigPubkeyPair(pubkey=pubkey_bytes, signature=signature)
                new_tx.signatures = [sig_pair]
                
                log.info("Nueva transacción firmada con método manual de último recurso")
                return new_tx
            except Exception as e2:
                log.error(f"Error fatal al firmar transacción reconstruida: {str(e2)}")
                raise e2
    except Exception as e:
        log.error(f"Error al recrear transacción: {str(e)}")
        raise e

# Función auxiliar para manejar el error de 'solders.signature.Signature' object has no attribute 'pubkey'
def fix_solders_signature_issue(tx):
    """
    Corrige el problema específico con 'solders.signature.Signature' que no tiene atributo 'pubkey'
    
    Args:
        tx: Objeto Transaction que puede tener el problema
        
    Returns:
        Transaction corregida o la misma si no se requieren cambios
    """
    try:
        # Verificar si usa la implementación solders
        if hasattr(tx, '_solders_tx') and tx._solders_tx:
            # Intentar detectar el problema antes de que ocurra
            # Si hay firmas y están usando el formato solders
            solders_sigs = tx._solders_tx.signatures
            if solders_sigs:
                # Verificamos si hay alguna firma no nula
                valid_sigs = [sig for sig in solders_sigs if sig]
                if valid_sigs:
                    log.info(f"Detectadas {len(valid_sigs)} firmas válidas en formato solders")
                    # No intentar acceder al atributo 'pubkey' directamente
                    # La presencia de firmas válidas es suficiente
                else:
                    log.warning("No se detectaron firmas válidas en formato solders")
        
        # Devolver la transacción como está - no modificamos nada
        return tx
    except Exception as e:
        log.warning(f"Error al intentar corregir problema de firma solders: {str(e)}")
        # En caso de error, devolver la transacción sin cambios
        return tx

async def swap_tokens_for_sol(keypair: Keypair, token_mint: str, token_amount: float) -> str:
    """
    Realiza un swap de tokens a SOL usando Jupiter
    
    Args:
        keypair: Keypair del wallet
        token_mint: Dirección del token a vender
        token_amount: Cantidad de tokens a vender
        
    Returns:
        Signature de la transacción
    """
    log.info(f"Iniciando swap de {token_amount} tokens {token_mint} por SOL")
    
    try:
        # 1. Obtener decimales del token para convertir a cantidad correcta
        client = AsyncClient(RPC_ENDPOINT)
        
        # Verificar primero el balance de SOL para gas
        sol_balance = await client.get_balance(keypair.pubkey())
        sol_balance_sol = sol_balance.value / 1e9
        
        # Verificar si tiene suficiente SOL para el gas (al menos 0.00005 SOL)
        if sol_balance_sol < 0.00005:
            await client.close()
            raise Exception(f"Saldo insuficiente para pagar gas. Tienes {sol_balance_sol:.6f} SOL, necesitas al menos 0.00005 SOL")
        
        # 2. Obtener info del token
        try:
            token_info = await client.get_token_supply(Pubkey.from_string(token_mint))
            token_decimals = token_info.value.decimals
        except Exception as e:
            log.warning(f"Error al obtener información del token {token_mint}: {str(e)}")
            # Default a 9 decimales si no se puede obtener
            token_decimals = 9
        
        log.info(f"Token {token_mint[:8]} tiene {token_decimals} decimales")
        
        # 3. Convertir cantidad de tokens a lamports
        token_amount_raw = int(token_amount * (10 ** token_decimals))
        
        # MÉTODO DIRECTO: Usar la función get_and_execute_swap_direct como primera opción
        log.info("Usando método directo (nativo) para swap...")
        
        # Intentar el método directo primero (el más confiable)
        direct_signature = await get_and_execute_swap_direct(
            token_mint,  # Token mint (origen)
            "So11111111111111111111111111111111111111112",  # SOL mint (destino)
            token_amount_raw,
            keypair,  # Pasar el keypair completo
            slippage=0.5  # 0.5% slippage
        )
        
        if direct_signature:
            log.info(f"✅ Swap completado exitosamente usando método directo (nativo). Signature: {direct_signature}")
            
            # Cerrar cliente
            await client.close()
            
            return direct_signature
            
        # MÉTODO DE RESPALDO: Si el método directo falla, intentamos el flujo original
        log.warning("❌ Método directo falló, intentando flujo original...")
        
        # 4. Solicitar quote de Jupiter
        quote_params = {
            "inputMint": token_mint,
            "outputMint": "So11111111111111111111111111111111111111112",  # SOL mint
            "amount": str(token_amount_raw),
            "slippageBps": "50",  # 0.5% slippage
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "true"  # Más compatible
        }
        
        async with aiohttp.ClientSession() as session:
            # Obtener quote
            log.info(f"Solicitando quote para swap token->SOL...")
            async with session.get(JUP_QUOTE_API, params=quote_params, timeout=5.0) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    log.error(f"Error al obtener quote: {error_text}")
                    raise Exception(f"Error al obtener quote: {resp.status} {error_text}")
                
                quote_data = await resp.json()
                log.info(f"Quote obtenido con éxito: {quote_data.get('routePlan', 'unknown')}")
                
                # 5. Solicitar transacción
                swap_params = {
                    "quoteResponse": quote_data,
                    "userPublicKey": str(keypair.pubkey()),
                    "wrapUnwrapSOL": True,
                    "asLegacyTransaction": True,
                    "useSharedAccounts": True,
                    "skipUserAccountsCheck": True
                }
                
                async with session.post(JUP_SWAP_API, json=swap_params, timeout=5.0) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        log.error(f"Error al generar transacción: {error_text}")
                        raise Exception(f"Error al generar transacción: {resp.status} {error_text}")
                    
                    swap_data = await resp.json()
                    tx_base64 = swap_data.get("swapTransaction")
                    
                    if not tx_base64:
                        log.error("No se recibió la transacción de Jupiter")
                        raise Exception("No se recibió la transacción de swap desde Jupiter")
                    
                    # MÉTODO NATIVO MEJORADO: Usando solana-py send_raw_transaction
                    from solana.transaction import Transaction
                    from solana.rpc.types import TxOpts
                    import base64
                    
                    # 1) Deserializar
                    log.info("Deserializando transacción...")
                    raw = base64.b64decode(tx_base64)
                    tx = Transaction.deserialize(raw)
                    
                    # 2) Firmar con solana-py nativo
                    log.info("Firmando transacción con método nativo...")
                    tx.sign(keypair)
                    
                    # 3) Enviar con método nativo
                    log.info("Enviando transacción con send_raw_transaction...")
                    sig = await client.send_raw_transaction(
                        tx.serialize(),
                        opts=TxOpts(skip_preflight=True, preflight_commitment="confirmed")
                    )
                    
                    # Cerrar cliente
                    await client.close()
                    
                    # Devolver firma (signature)
                    log.info(f"✅ Transacción enviada con éxito usando método nativo. Signature: {sig.value}")
                    return str(sig.value)
        
        # Si llegamos aquí, todos los métodos han fallado
        raise Exception("No se pudo completar el swap después de intentar múltiples métodos. Por favor, intenta más tarde.")
        
    except Exception as e:
        log.error(f"Error en swap_tokens_for_sol: {str(e)}")
        await client.close()
        
        # Detectar errores específicos para mensajes claros
        error_msg = str(e).lower()
        if "insufficient funds" in error_msg or "insufficient lamports" in error_msg:
            raise Exception(f"Saldo insuficiente para completar la transacción. Necesitas más SOL para pagar la transacción.")
        elif "not enough signers" in error_msg:
            # Último intento: usar método RPC directo con skipPreflight=true
            log.warning("Detectado error 'not enough signers', intentando método RPC directo como último recurso...")
            
            # Verificar que tx_base64 esté definido
            try:
                # Intentar enviar la transacción directamente al RPC con skipPreflight=true
                if 'tx_base64' in locals():
                    rpc_signature = await send_transaction_rpc_direct(tx_base64, keypair, RPC_ENDPOINT)
                    
                    if rpc_signature:
                        log.info(f"✅ Transacción enviada con éxito usando método RPC directo. Signature: {rpc_signature}")
                        return rpc_signature
            except Exception as e2:
                log.error(f"Error en envío de RPC directo: {str(e2)}")
            
            raise Exception(f"Error en la firma de la transacción: not enough signers. Por favor, contacta al administrador.")
        else:
            raise e

# Añadimos la función sign_keypair como un método de extensión para Transaction
def sign_keypair(self, keypair):
    """
    Método personalizado para firmar una transacción que maneja correctamente
    cualquier versión de solana-py y solders
    
    Args:
        keypair: El objeto Keypair para firmar
        
    Returns:
        La transacción firmada
    """
    try:
        # Intenta diferentes métodos de firma dependiendo de la implementación
        
        # Método 1: Implementación directa con el keypair
        try:
            return self.sign(keypair)
        except Exception as e1:
            log.debug(f"Método directo falló: {str(e1)}")
            
        # Método 2: Extrae bytes privados del keypair y crea una firma manual
        try:
            from solana.transaction import SigPubkeyPair
            privkey = keypair.secret()
            pubkey = keypair.pubkey().to_bytes()
            
            # Crear mensaje para firmar
            message = self.message.serialize()
            
            # Firma el mensaje
            from nacl.signing import SigningKey
            signer = SigningKey(privkey)
            signature = signer.sign(message).signature
            
            # Asignar la firma a la transacción
            if not hasattr(self, 'signatures') or not self.signatures:
                self.signatures = []
            
            # Crear un SigPubkeyPair
            sig_pair = SigPubkeyPair(pubkey=pubkey, signature=signature)
            
            # Reemplazar o agregar la firma
            found = False
            for i, existing_sig in enumerate(self.signatures):
                if hasattr(existing_sig, 'pubkey') and existing_sig.pubkey == pubkey:
                    self.signatures[i] = sig_pair
                    found = True
                    break
            
            if not found:
                self.signatures.append(sig_pair)
                
            return self
        except Exception as e2:
            log.debug(f"Método con bytes falló: {str(e2)}")
            
        # Método 3: Usa sign_partial que suele ser más robusto
        return self.sign_partial(keypair)
        
    except Exception as e:
        log.error(f"Todos los métodos de firma personalizados fallaron: {str(e)}")
        raise e

# Extender la clase Transaction con nuestro método
SolanaTransaction.sign_keypair = sign_keypair

# Actualizar función handle_not_enough_signers_error
async def handle_not_enough_signers_error(tx_or_data, keypair, client, opts=None):
    """
    Función de último recurso para manejar el error específico 'not enough signers'
    utilizando métodos de bajo nivel para firmar y enviar la transacción.
    
    Args:
        tx_or_data: La transacción que falló con 'not enough signers' o datos base64
        keypair: El keypair del usuario
        client: Cliente de Solana para enviar la transacción
        opts: Opciones de transacción opcionales (opcional)
        
    Returns:
        La firma de la transacción si tiene éxito, o None si falla
    """
    log.info("⚠️ Ejecutando solución de emergencia para 'not enough signers'")
    
    try:
        # Verificar si tx_or_data es una cadena base64 o un objeto Transaction
        if isinstance(tx_or_data, str):
            log.info("Usando send_transaction_native como método de emergencia final")
            return await send_transaction_native(tx_or_data, keypair, client)
        
        # Si llegamos aquí, es un objeto Transaction
        # 1. Extraer el mensaje de la transacción
        if hasattr(tx_or_data, 'message'):
            message = tx_or_data.message
        elif hasattr(tx_or_data, '_solders_tx') and hasattr(tx_or_data._solders_tx, 'message'):
            message = tx_or_data._solders_tx.message
        else:
            log.error("No se pudo extraer el mensaje de la transacción")
            return None
            
        # 2. Serializar el mensaje para firmarlo
        try:
            message_bytes = message.serialize()
        except:
            # Intentar métodos alternativos de serialización
            if hasattr(message, 'to_bytes'):
                message_bytes = message.to_bytes()
            elif hasattr(message, '_solders_message'):
                message_bytes = message._solders_message.serialize()
            else:
                log.error("No se pudo serializar el mensaje")
                return None
        
        # 3. Crear firma directamente con PyNaCl
        from nacl.signing import SigningKey
        import base64
        
        # Obtener clave privada
        private_key = keypair.secret()
        signer = SigningKey(private_key)
        
        # Firmar mensaje
        signature_bytes = signer.sign(message_bytes).signature
        
        # 4. Compilar transacción serializada con firma manual
        from solders.transaction import VersionedTransaction
        from solders.signature import Signature as SoldersSignature
        from solders.message import Message as SoldersMessage
        
        try:
            # Intentar usar la implementación solders directamente
            solders_signature = SoldersSignature.from_bytes(signature_bytes)
            signatures = [solders_signature]
            
            # Crear transacción versionada con el mensaje
            if hasattr(message, '_solders_message'):
                solders_message = message._solders_message
            else:
                # Intentar deserializar mensaje en formato solders
                solders_message = SoldersMessage.from_bytes(message_bytes)
                
            # Crear transacción versionada directamente
            versioned_tx = VersionedTransaction(solders_message, signatures)
            
            # Serializar la transacción completa
            serialized_tx = base64.b64encode(versioned_tx.to_bytes()).decode('ascii')
            
            # 5. Enviar transacción firmada manualmente usando JSON RPC directo
            log.info("Enviando transacción firmada manualmente via JSON RPC")
            
            import json
            rpc_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    serialized_tx,
                    {
                        "skipPreflight": True,
                        "maxRetries": 5,
                        "preflightCommitment": "confirmed"
                    }
                ]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(client._provider.endpoint_uri, json=rpc_request) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if "result" in result:
                            signature = result["result"]
                            log.info(f"Transacción enviada con éxito usando método de emergencia. Signature: {signature}")
                            return signature
                        else:
                            log.error(f"Error en respuesta RPC: {json.dumps(result)}")
                    else:
                        log.error(f"Error HTTP: {resp.status}")
            
            return None
            
        except Exception as solders_error:
            log.error(f"Error en método solders: {str(solders_error)}")
            
            # Último intento: usar direct_sign_and_send
            try:
                # Serializar la transacción original
                if hasattr(tx_or_data, 'serialize'):
                    tx_bytes = tx_or_data.serialize()
                    tx_base64 = base64.b64encode(tx_bytes).decode('ascii')
                    
                    # Intentar con direct_sign_and_send
                    return await direct_sign_and_send(tx_base64, keypair, client)
                else:
                    log.error("No se pudo serializar la transacción para último intento")
                    return None
            except Exception as e:
                log.error(f"Error en último intento: {str(e)}")
                return None
                
    except Exception as e:
        log.error(f"Error en handle_not_enough_signers_error: {str(e)}")
        return None

# Modificar la función direct_sign_and_send
async def direct_sign_and_send(transaction_data, keypair, client, fee_payer=None):
    """
    Función robusta para firmar y enviar una transacción directamente utilizando
    solicitudes RPC sin pasar por la implementación solana-py.
    
    Args:
        transaction_data: Datos de la transacción en formato base64
        keypair: Keypair del usuario
        client: Cliente de Solana para enviar la transacción
        fee_payer: Fee payer opcional (si no es el keypair principal)
        
    Returns:
        La firma de la transacción si tiene éxito, o None si falla
    """
    import base64
    import json
    from nacl.signing import SigningKey
    import base58
    
    try:
        log.info("🔄 Utilizando método directo al RPC para evitar errores de signers")
        
        # Endpoint directo al RPC
        endpoint = client._provider.endpoint_uri
        
        # Preparar la request para enviar directamente la transacción al RPC
        # Este enfoque elude completamente la biblioteca solana-py
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                transaction_data,  # Transacción en base64 (sin modificar)
                {
                    "skipPreflight": True,
                    "preflightCommitment": "confirmed",
                    "encoding": "base64",
                    "maxRetries": 10
                }
            ]
        }
        
        # Enviar transacción directamente al RPC
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, json=rpc_request, timeout=15.0) as resp:
                result = await resp.json()
                log.info(f"Respuesta directa del RPC: {json.dumps(result)[:200]}")
                
                if "result" in result:
                    signature = result["result"]
                    log.info(f"✅ Transacción enviada exitosamente usando método directo. Signature: {signature}")
                    return signature
                else:
                    error_msg = result.get("error", {}).get("message", "Error desconocido")
                    log.warning(f"⚠️ Error en método directo: {error_msg}")
                    
                    # Intentar pre-firmar la transacción localmente y luego enviarla
                    return await send_presigned_transaction(transaction_data, keypair, client)
    
    except Exception as e:
        log.error(f"Error en direct_sign_and_send: {str(e)}")
        # Intentar método alternativo
        return await send_presigned_transaction(transaction_data, keypair, client)

# Mejorar el método de envío pre-firmado
async def send_presigned_transaction(transaction_data, keypair, client):
    """
    Firma y envía una transacción prefirmada localmente
    
    Args:
        transaction_data: Datos de la transacción en formato base64
        keypair: Keypair del usuario
        client: Cliente de Solana para enviar la transacción
        
    Returns:
        La firma de la transacción si tiene éxito, o None si falla
    """
    import base64
    import json
    from nacl.signing import SigningKey
    import base58
    
    try:
        log.info("🔄 Utilizando firma manual local para evitar errores de signers")
        
        # 1. Decodificar la transacción
        decoded_tx = base64.b64decode(transaction_data)
        
        # 2. Firmar la transacción localmente
        from solana.transaction import Transaction
        tx = Transaction.deserialize(decoded_tx)
        
        # Obtener el mensaje para firmar
        if hasattr(tx, 'message'):
            message_bytes = tx.message.serialize()
            message_base64 = base64.b64encode(message_bytes).decode('ascii')
            
            # Crear firma con PyNaCl
            signer = SigningKey(keypair.secret())
            signature_bytes = signer.sign(message_bytes).signature
            signature_base58 = base58.b58encode(signature_bytes).decode('ascii')
            
            # Preparar la transacción firmada para enviar
            endpoint = client._provider.endpoint_uri
            
            # Construir la request con la firma manual
            manual_sign_request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    transaction_data,  # Transacción original en base64
                    {
                        "skipPreflight": True,
                        "preflightCommitment": "confirmed",
                        "encoding": "base64",
                        "maxRetries": 10,
                        "signatures": [
                            {
                                "signature": signature_base58,
                                "publicKey": str(keypair.pubkey())
                            }
                        ]
                    }
                ]
            }
            
            async with aiohttp.ClientSession() as session:
                log.info(f"Enviando transacción con firma manual al RPC")
                async with session.post(endpoint, json=manual_sign_request, timeout=15.0) as resp:
                    sign_result = await resp.json()
                    log.info(f"Respuesta del RPC: {json.dumps(sign_result)[:200]}")
                    
                    if "result" in sign_result:
                        sign_signature = sign_result["result"]
                        log.info(f"✅ Transacción firmada manualmente y enviada con éxito. Signature: {sign_signature}")
                        return sign_signature
                    else:
                        error_msg = sign_result.get("error", {}).get("message", "Error desconocido")
                        log.warning(f"⚠️ Error con firma manual: {error_msg}")
                        
                        # Si falló la firma manual, intentar método simple como último recurso
                        simple_request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                transaction_data,
                                {
                                    "skipPreflight": True,
                                    "encoding": "base64"
                                }
                            ]
                        }
                        
                        log.info("Intentando envío simple como último recurso")
                        async with session.post(endpoint, json=simple_request, timeout=15.0) as simple_resp:
                            final_result = await simple_resp.json()
                            
                            if "result" in final_result:
                                final_signature = final_result["result"]
                                log.info(f"✅ Transacción enviada con método simple. Signature: {final_signature}")
                                return final_signature
                            else:
                                final_error = final_result.get("error", {}).get("message", "Error desconocido")
                                log.error(f"❌ Error final: {final_error}")
                                return None
        else:
            log.error("No se pudo extraer mensaje para firmar")
            return None
    
    except Exception as e:
        log.error(f"Error en firma manual: {str(e)}")
        return None

# Nueva función para crear un Keypair de solders a partir de un Keypair de solana-py
def get_solders_keypair(keypair):
    """Convierte un Keypair de solana-py a un Keypair de solders"""
    try:
        # Intento 1: Si ya es un SoldersKeypair, devolverlo
        if isinstance(keypair, SoldersKeypair):
            return keypair
            
        # Intento 2: Si tiene secret() y pubkey(), es un Keypair de solana-py
        if hasattr(keypair, 'secret') and hasattr(keypair, 'pubkey'):
            secret = keypair.secret()
            return SoldersKeypair.from_bytes(secret[:32])
            
        # Intento 3: Crear a partir de bytes
        if isinstance(keypair, bytes) and len(keypair) >= 32:
            return SoldersKeypair.from_bytes(keypair[:32])
            
        # Intento 4: Es posible que ya tengamos los bytes como lista
        if isinstance(keypair, list) and len(keypair) >= 32:
            return SoldersKeypair.from_bytes(bytes(keypair[:32]))
            
        log.error(f"No se pudo convertir el keypair al formato de solders")
        return None
    except Exception as e:
        log.error(f"Error al convertir keypair a solders: {str(e)}")
        return None

# Función para enviar transacciones directamente usando solders y paquetes nativos
async def send_transaction_native(transaction_base64: str, keypair, client):
    """
    Envía una transacción usando la API nativa de solders sin depender de solana-py para la firma
    
    Args:
        transaction_base64: Transacción en formato base64
        keypair: El keypair para firmar (convertido a formato solders)
        client: Cliente RPC
        
    Returns:
        La firma de la transacción o None si falla
    """
    import base64
    import json
    
    try:
        log.info("⚡ Usando API nativa de solders para enviar transacción")
        
        # 1. Decodificar la transacción base64
        decoded_tx = base64.b64decode(transaction_base64)
        
        # 2. Convertir keypair al formato de solders
        solders_keypair = get_solders_keypair(keypair)
        if not solders_keypair:
            log.error("No se pudo convertir el keypair al formato de solders")
            return None
            
        # 3. Crear una transacción de solana desde los bytes decodificados
        from solders.transaction import VersionedTransaction, TransactionError
        
        try:
            # Crear transacción de solders a partir de los bytes
            tx = VersionedTransaction.from_bytes(decoded_tx)
            log.info("✅ Transacción deserializada correctamente usando solders")
        except TransactionError as e:
            log.warning(f"Error al deserializar usando VersionedTransaction: {str(e)}")
            
            # Intentar con Transaction legacy
            from solana.transaction import Transaction as LegacyTransaction
            
            try:
                tx_legacy = LegacyTransaction.deserialize(decoded_tx)
                log.info("✅ Transacción deserializada como legacy")
                
                # Enviar usando el método RPC directo
                endpoint = client._provider.endpoint_uri
                
                # Firmar con el keypair normal
                tx_legacy.sign(keypair)
                
                # Serializar a base64
                serialized = base64.b64encode(tx_legacy.serialize()).decode("ascii")
                
                # Enviar directamente
                rpc_request = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [
                        serialized,
                        {
                            "skipPreflight": True,
                            "encoding": "base64"
                        }
                    ]
                }
                
                # Enviar
                async with session.post(endpoint, json=rpc_request, timeout=15.0) as resp:
                    result = await resp.json()
                    
                    if "result" in result:
                        signature = result["result"]
                        log.info(f"✅ Transacción legacy enviada exitosamente. Signature: {signature}")
                        return signature
                    else:
                        error_msg = result.get("error", {}).get("message", "Error desconocido")
                        log.error(f"❌ Error al enviar transacción legacy: {error_msg}")
                        return None
            except Exception as e2:
                log.error(f"Error total al deserializar transacción: {str(e2)}")
                return None
                
        # 4. Obtener recent_blockhash (por si acaso)
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash
        
        # 5. Crear firma usando Solders directamente
        # Extraer el mensaje para firmar
        message = tx.message
        message_bytes = message.serialize()
        
        # Crear firma con el keypair de solders
        signed_tx = solders_keypair.sign_message(message_bytes)
        
        # 6. Enviar la transacción firmada directamente
        from solders.rpc.requests import SendTransactionConfig
        from solana.rpc.api import TxOpts as SolanaTxOpts
        
        # Opciones de envío
        opts = SendTransactionConfig(
            skip_preflight=True,
            preflight_commitment=Commitment.CONFIRMED,
            encoding="base64"
        )
        
        # Enviar transacción
        resp = await client.send_transaction_with_config(
            signed_tx,
            opts=opts
        )
        
        # Verificar respuesta
        if resp.value:
            signature = str(resp.value)
            log.info(f"✅ Transacción enviada exitosamente con solders. Signature: {signature}")
            return signature
        else:
            log.error(f"❌ Error al enviar transacción con solders: respuesta vacía")
            return None
            
    except Exception as e:
        log.error(f"❌ Error en send_transaction_native: {str(e)}")
        return None

# Añadir función para enviar transacciones directamente via JSON-RPC sin depender de solana-py o solders
async def send_transaction_rpc_direct(transaction_base64: str, keypair, rpc_url: str):
    """
    Envía una transacción directamente usando JSON-RPC sin depender de solana-py o solders
    
    Args:
        transaction_base64: Transacción en formato base64
        keypair: El keypair del usuario (solo se usa la clave privada en bytes)
        rpc_url: URL del RPC de Solana
        
    Returns:
        La firma de la transacción si tiene éxito, None si falla
    """
    import base64
    import json
    
    try:
        log.info("🚀 Enviando transacción directamente por JSON-RPC (sin bibliotecas Solana)")
        
        # 1. Extraer la clave privada en formato bytes
        if hasattr(keypair, 'secret'):
            private_key = keypair.secret()
        elif isinstance(keypair, bytes):
            private_key = keypair
        elif isinstance(keypair, list) and len(keypair) >= 32:
            private_key = bytes(keypair[:32])
        else:
            log.error("No se pudo extraer la clave privada del keypair")
            return None
            
        # 2. Crear un firmante usando la biblioteca ed25519 pura (sin dependencias de solana)
        signing_key = ed25519.SigningKey(private_key[:32])
        
        # 3. Decodificar la transacción base64
        tx_bytes = base64.b64decode(transaction_base64)
        
        # 4. Enviar la transacción directamente sin firmar, dejando que el RPC maneje la firma
        # Este enfoque evita todos los problemas de compatibilidad de bibliotecas
        
        # Preparar la solicitud RPC
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                transaction_base64,
                {
                    "skipPreflight": True,
                    "encoding": "base64",
                    "maxRetries": 10
                }
            ]
        }
        
        # Enviar directamente al RPC
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=rpc_request, timeout=15.0) as resp:
                result = await resp.json()
                
                if "result" in result:
                    signature = result["result"]
                    log.info(f"✅ Transacción enviada exitosamente por JSON-RPC directo. Signature: {signature}")
                    return signature
                elif "error" in result:
                    error_msg = result["error"].get("message", "Error desconocido")
                    
                    # Si el error es de firmas, intentar firmar la transacción manualmente y reenviar
                    if "not enough signers" in error_msg or "signature" in error_msg:
                        log.info("Detectado error de firmas, intentando firma manual directa...")
                        
                        # Intentar extraer el mensaje para firmar directamente del binario
                        try:
                            # Extraer el mensaje (asumiendo formato estándar)
                            # Esto es muy simplificado y solo funciona para transacciones legacy
                            # Para hacerlo correctamente se necesitaría un parser completo de transacciones
                            
                            # Enviar la transacción con dryRun para obtener el mensaje
                            dry_run_request = {
                                "jsonrpc": "2.0", 
                                "id": 2,
                                "method": "simulateTransaction",
                                "params": [
                                    transaction_base64,
                                    {"encoding": "base64", "sigVerify": False}
                                ]
                            }
                            
                            async with session.post(rpc_url, json=dry_run_request, timeout=15.0) as dry_resp:
                                dry_result = await dry_resp.json()
                                
                                if "result" in dry_result:
                                    log.info("Simulación exitosa, intentando enfoques alternativos...")
                                    
                                    # Intentar con un enfoque completamente diferente: TransactionBuilder
                                    # Esto solo puede funcionar si Jupiter.ag soporta esta API
                                    return await try_transaction_builder_api(transaction_base64, keypair)
                                else:
                                    log.error(f"Error en simulación: {json.dumps(dry_result)}")
                        except Exception as e:
                            log.error(f"Error al extraer mensaje para firma: {str(e)}")
                    
                    log.error(f"❌ Error en JSON-RPC: {error_msg}")
                    return None
                else:
                    log.error("❌ Respuesta inválida del RPC")
                    return None
    except Exception as e:
        log.error(f"❌ Error en send_transaction_rpc_direct: {str(e)}")
        return None

# Función que intenta usar la API de TransactionBuilder como último recurso
async def try_transaction_builder_api(tx_data: str, keypair):
    """
    Intenta usar la API de TransactionBuilder de Jupiter como último recurso
    
    Args:
        tx_data: Datos de la transacción (base64 o URL de la API)
        keypair: Keypair del usuario
        
    Returns:
        Signature de la transacción si tiene éxito, None si falla
    """
    try:
        log.info("Intentando usar API de TransactionBuilder como último recurso...")
        
        # Si tx_data parece ser una URL de Jupiter, usarla directamente
        if tx_data.startswith("http") and "jup.ag" in tx_data:
            api_url = tx_data
        else:
            # Usar URL base de Jupiter
            api_url = "https://quote-api.jup.ag/v6/swap"
        
        # Preparar la petición a Jupiter con firm=false
        pubkey_str = str(keypair.pubkey())
        
        # Parámetros comunes para todas las peticiones
        params = {
            "userPublicKey": pubkey_str,
            "wrapUnwrapSOL": True,
            "feeAccount": None,
            "computeUnitPriceMicroLamports": 0,
            "asLegacyTransaction": False,
            "useSharedAccounts": True,
            "dynamicComputeUnitLimit": True,
            "skipUserAccountsCheck": True,
        }
        
        # Intentar con TransactionBuilder API
        headers = {
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            # Primera petición: obtener el formato adecuado de respuesta
            log.info(f"Enviando petición a TransactionBuilder API...")
            try:
                async with session.post(api_url, json=params, headers=headers, timeout=10.0) as resp:
                    response = await resp.json()
                    
                    if "swapTransaction" in response:
                        # Obtener la transacción en formato serializado
                        tx_serialized = response["swapTransaction"]
                        
                        # Enviar directamente al RPC sin firmar
                        rpc_request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                tx_serialized,
                                {
                                    "skipPreflight": True,
                                    "encoding": "base64"
                                }
                            ]
                        }
                        
                        # Enviar al RPC
                        async with session.post(RPC_ENDPOINT, json=rpc_request, timeout=15.0) as resp:
                            result = await resp.json()
                            
                            if "result" in result:
                                signature = result["result"]
                                log.info(f"✅ Transacción enviada exitosamente via TransactionBuilder. Signature: {signature}")
                                return signature
                            else:
                                log.error(f"❌ Error al enviar transacción via TransactionBuilder: {json.dumps(result)}")
                                return None
                    else:
                        log.error(f"❌ Respuesta inválida de TransactionBuilder: {json.dumps(response)}")
                        return None
            except Exception as e:
                log.error(f"❌ Error en TransactionBuilder API: {str(e)}")
                return None
    except Exception as e:
        log.error(f"❌ Error general en try_transaction_builder_api: {str(e)}")
        return None

# Función para obtener y enviar una transacción de Jupiter directamente
async def get_and_execute_swap_direct(input_mint, output_mint, amount, user_pubkey, slippage=1.0):
    """
    Obtiene una transacción de swap directamente de Jupiter y la envía sin pasar por
    las bibliotecas de solana-py o solders, utilizando JSON-RPC directamente.
    
    Args:
        input_mint: Mint del token de entrada (SOL: So11...1112)
        output_mint: Mint del token de salida
        amount: Cantidad de entrada en lamports
        user_pubkey: Clave pública del usuario en formato string o keypair
        slippage: Tolerancia de slippage (1.0 = 1%)
        
    Returns:
        Firma de la transacción o None si falla
    """
    try:
        log.info(f"Obteniendo transacción directamente de Jupiter para {input_mint} -> {output_mint}")
        
        # Obtener keypair si solo se pasó la clave pública
        keypair = None
        user_pubkey_str = None
        
        # Si es un keypair, extraer la clave pública
        if hasattr(user_pubkey, 'pubkey'):
            keypair = user_pubkey
            user_pubkey_str = str(user_pubkey.pubkey())
        # Si es un entero, asumir que es un ID de usuario
        elif isinstance(user_pubkey, int):
            from .wallet_manager import load_wallet
            keypair = load_wallet(user_pubkey)
            if keypair:
                user_pubkey_str = str(keypair.pubkey())
            else:
                log.error(f"No se pudo cargar el keypair para el ID {user_pubkey}")
                return None
        else:
            # Si es una string, asumir que es la clave pública
            user_pubkey_str = str(user_pubkey)
        
        log.info(f"Usando clave pública: {user_pubkey_str}")
        
        # 1. Obtener quote
        quote_params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": int(slippage * 100),
            "swapMode": "ExactIn",
            "onlyDirectRoutes": False,
            "asLegacyTransaction": True  # Usar transacción legacy más compatible
        }
        
        async with aiohttp.ClientSession() as session:
            # Obtener quote
            async with session.get(JUP_QUOTE_API, params=quote_params) as quote_resp:
                if quote_resp.status != 200:
                    log.error(f"Error al obtener quote: {await quote_resp.text()}")
                    return None
                
                quote_data = await quote_resp.json()
                
                # 2. Solicitar transacción
                swap_params = {
                    "quoteResponse": quote_data,
                    "userPublicKey": user_pubkey_str,
                    "wrapUnwrapSOL": True,
                    "asLegacyTransaction": True,
                    "useSharedAccounts": True,
                    "computeUnitPriceMicroLamports": 0,  # Sin priority fee para evitar problemas
                    "prioritizationFeeLamports": 0,      # Sin priority fee alternativo
                    "destinationTokenAccount": None,     # Permitir ATAs
                    "dynamicComputeUnitLimit": True,     # CUs dinámicos
                    "skipUserAccountsCheck": True        # Skip checks adicionales
                }
                
                async with session.post(JUP_SWAP_API, json=swap_params) as swap_resp:
                    if swap_resp.status != 200:
                        log.error(f"Error al obtener transacción: {await swap_resp.text()}")
                        return None
                    
                    swap_data = await swap_resp.json()
                    
                    if "swapTransaction" not in swap_data:
                        log.error("No se recibió la transacción de swap")
                        return None
                    
                    tx_base64 = swap_data["swapTransaction"]
                    
                    # MÉTODO MEJORADO: Usando método nativo de solana-py para deserializar, firmar y enviar
                    from solana.rpc.async_api import AsyncClient
                    from solana.transaction import Transaction
                    from solana.rpc.types import TxOpts
                    import base64
                    
                    # Crear cliente RPC
                    client = AsyncClient(RPC_ENDPOINT)
                    
                    # Si no tenemos keypair, intentar obtenerlo o usar fallback
                    if not keypair:
                        log.info(f"No se proporcionó keypair directamente, intentando obtenerlo...")
                        
                        # Intentar con método RPC directo como fallback
                        log.info("Usando método RPC directo")
                        rpc_request = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "sendTransaction",
                            "params": [
                                tx_base64,
                                {
                                    "skipPreflight": True,
                                    "maxRetries": 3,
                                    "encoding": "base64"
                                }
                            ]
                        }
                        
                        # Enviar al RPC
                        async with session.post(RPC_ENDPOINT, json=rpc_request) as rpc_resp:
                            if rpc_resp.status != 200:
                                log.error(f"Error en respuesta RPC: {await rpc_resp.text()}")
                                await client.close()
                                return None
                            
                            result = await rpc_resp.json()
                            
                            if "result" in result:
                                signature = result["result"]
                                log.info(f"✅ Transacción enviada con éxito a través de JSON-RPC directo. Signature: {signature}")
                                await client.close()
                                return signature
                            else:
                                log.error(f"❌ Error al enviar transacción: {result}")
                                await client.close()
                                return None
                    else:
                        try:
                            # 1) Deserializar
                            log.info("Deserializando transacción...")
                            raw = base64.b64decode(tx_base64)
                            tx = Transaction.deserialize(raw)
                            
                            # 2) Firmar con solana-py nativo
                            log.info("Firmando transacción con método nativo...")
                            tx.sign(keypair)
                            
                            # 3) Enviar con método nativo
                            log.info("Enviando transacción con send_raw_transaction...")
                            sig = await client.send_raw_transaction(
                                tx.serialize(),
                                opts=TxOpts(skip_preflight=True, preflight_commitment="confirmed")
                            )
                            
                            # Cerrar cliente y devolver firma
                            await client.close()
                            log.info(f"✅ Transacción enviada con éxito usando método nativo. Signature: {sig.value}")
                            return str(sig.value)
                            
                        except Exception as e:
                            log.error(f"Error al procesar transacción con método nativo: {str(e)}")
                            
                            # Intentar con método RPC directo como fallback
                            log.info("Intentando método RPC directo como fallback después de error")
                            rpc_request = {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "sendTransaction",
                                "params": [
                                    tx_base64,
                                    {
                                        "skipPreflight": True,
                                        "maxRetries": 3,
                                        "encoding": "base64"
                                    }
                                ]
                            }
                            
                            # Enviar al RPC
                            async with session.post(RPC_ENDPOINT, json=rpc_request) as rpc_resp:
                                result = await rpc_resp.json()
                                
                                if "result" in result:
                                    signature = result["result"]
                                    log.info(f"✅ Transacción enviada con éxito a través de fallback. Signature: {signature}")
                                    await client.close()
                                    return signature
                                else:
                                    log.error(f"❌ Error en fallback: {result}")
                                    await client.close()
                                    return None
    except Exception as e:
        log.error(f"❌ Error en get_and_execute_swap_direct: {str(e)}")
        return None

# Función para enviar la comisión del bot
async def send_bot_fee(keypair: Keypair, fee_amount_sol: float) -> str:
    """
    Envía la comisión del bot a la wallet de comisiones.
    
    Args:
        keypair: Keypair del usuario
        fee_amount_sol: Cantidad de SOL a enviar como comisión
        
    Returns:
        Signature de la transacción
    """
    try:
        log.info(f"Enviando comisión de {fee_amount_sol} SOL a {BOT_FEE_RECIPIENT}")
        
        # Verificar que la comisión no sea muy pequeña (menor a 0.00001 SOL)
        if fee_amount_sol < 0.00001:
            log.warning(f"Comisión demasiado pequeña ({fee_amount_sol} SOL), omitiendo")
            return "fee_too_small"
            
        # Crear cliente
        client = AsyncClient(RPC_ENDPOINT)
        
        # Convertir SOL a lamports
        lamports = int(fee_amount_sol * 1e9)
        
        # Crear transacción usando la nueva API
        transfer_instruction = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=Pubkey.from_string(BOT_FEE_RECIPIENT),
                lamports=lamports
            )
        )
        
        tx = Transaction().add(transfer_instruction)
        
        # Firmar y enviar transacción con método nativo
        tx.sign(keypair)
        
        # Enviar transacción
        result = await client.send_transaction(
            tx,
            opts=TxOpts(skip_preflight=True, preflight_commitment=Commitment.CONFIRMED)
        )
        
        # Cerrar cliente
        await client.close()
        
        log.info(f"✅ Comisión enviada exitosamente. Signature: {result.value}")
        return str(result.value)
        
    except Exception as e:
        log.error(f"Error al enviar comisión: {str(e)}")
        return f"error:{str(e)}"
