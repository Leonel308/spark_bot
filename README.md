# Bot de Trading de Solana

Bot de trading para Solana con sistema integrado de datos en tiempo real, WebSockets y optimización de transacciones.

## Características principales

- 🚀 Trading automatizado de tokens en Solana
- 📊 Datos de mercado en tiempo real desde múltiples fuentes
- 💰 Soporte para compra/venta de tokens con visualización de slippage
- 🔄 Sistema WebSocket para actualizaciones de precio sin retardo
- 🧩 Integración con Jito para comisiones prioritarias
- 📦 Sistema de bundling de transacciones para mayor eficiencia

## Mejoras recientes en la obtención de datos

### Sistema optimizado de obtención de datos en tiempo real

- **Multi-fuente paralela**: Consulta simultánea a DexScreener, Pump.fun y QuickNode para obtener los datos más recientes.
- **Limpieza agresiva de caché**: Eliminación completa de caché al actualizar para garantizar datos frescos.
- **Sistema de respaldo inteligente**: Si una fuente falla, automáticamente usa fuentes alternativas.
- **Verificación de consistencia**: Comprobación de datos para asegurar que se están mostrando valores actualizados.

### Optimización del botón "Actualizar"

El botón "Actualizar" ahora implementa las siguientes mejoras:

```python
# Limpieza completa de caché
for key_prefix in ["pumpfun_", "jupiter_", "dex_", "price_", "token_stats_"]:
    cache_key = f"{key_prefix}{mint}"
    if cache_key in _token_cache:
        del _token_cache[cache_key]
```

- ⚡ **Timeout extendido**: Aumentado de 1s a 2s para garantizar la obtención de datos frescos
- 🔄 **Espera de todas las fuentes**: Espera a que todas las fuentes respondan para elegir la mejor
- 📊 **Transferencia completa de datos**: Asegura que todos los campos (precio, marketcap, cambio %) se actualicen correctamente
- 🛡️ **Método de respaldo**: Intenta métodos alternativos si la actualización principal falla

### Interfaz mejorada

- Presentación detallada de tokens con secciones claramente separadas
- Visualización de cambio de precio con indicadores visuales (🟢/🔴)
- Información de PnL para tokens que el usuario posee
- Feedback visual durante la carga y actualización de datos

## Integración con sistemas avanzados

- **Jito MEV**: Sistema de comisiones prioritarias para transacciones más rápidas
- **Transaction Bundler**: Agrupamiento de transacciones para mayor eficiencia
- **WebSockets en tiempo real**: Conexión constante a datos para minimizar latencia

## Ejemplos de uso

El bot responde a enlaces de tokens de Pump.fun y DexScreener, mostrando información detallada y opciones para comprar o vender.

```
🏦 SOLANA TRADING BOT 🏦

👛 Wallet
Hb14oLx7XFjEzxmhaW6AnnTsPG3hopZrcXFga6ZQvaF
Toca para copiar

───────────────────

💰 Balance: 0.002605 SOL ($0.45)
📈 SOL Price: $171.68

───────────────────

🪙 GORK - 1-833-YUR-GORK
BpwvxTDuXgJm5yq8Hg4Y7CNN3J9biB44EunEoRXUpump

📊 Token Stats
├USD:  $0.00000019 (🔴-43.53%)
├MC:   $939,685.00
├Vol:  $120,563.39
└LP:   $19,845.86

�� Posición actual: 2602571.2344 GORK ($506.14)

💵 Invirtiendo: 0.5 SOL ($85.84)
🪙 Recibirás: 421052.6316 GORK
📉 Slippage: 6.44%

🕒 Datos: DexScreener (api.dexscreener.com)
⏱️ Tiempo: 531ms | 04:42:03

_Selecciona cuánto SOL quieres invertir:_
```

## Tecnologías utilizadas

- Python 3.10+
- Solana Web3.js
- Telegram Bot API
- WebSockets para datos en tiempo real
- Solana Program Library (SPL)
- Jito Priority Fees API
