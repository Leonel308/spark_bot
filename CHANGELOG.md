# Changelog

## [2.1.0] - 2024-05-08

### Mejoras de Resiliencia y Conectividad
- Implementado sistema multi-fuente para obtención de datos de tokens
- Añadidos múltiples endpoints alternativos para todas las fuentes de datos:
  - PumpFun: 4 endpoints alternativos con fallback automático
  - DexScreener: 3 endpoints alternativos con fallback
  - Jupiter: 3 endpoints alternativos
- Implementado sistema de scraping como último recurso cuando las APIs fallan
- Mejorado el sistema de manejo de errores para evitar KeyErrors en la UI
- Reducidos tiempos de caché para mayor frescura de datos:
  - Tokens virales: actualización instantánea (0 segundos de caché)
  - PumpFun: reducido a 2 segundos para todos los tokens
- Añadidas fuentes adicionales para precio de SOL con 6 alternativas
- Optimizados los tiempos de timeout para adaptarse a condiciones de red variables
- Implementado sistema de valores predeterminados para evitar errores en la UI
- Añadida estrategia de combinación de datos de múltiples fuentes para maximizar disponibilidad

## [2.0.0] - 2024-03-07

### Mejoras en la obtención de datos de tokens
- Actualizado el sistema de obtención de datos para usar múltiples fuentes
- Agregado DexScreener como fuente principal de datos
- Mejorado el manejo de errores y fallbacks
- Implementadas consultas en paralelo para mejor rendimiento

#### Cambios en APIs
- Reemplazado `client-api.pump.fun` por endpoints públicos de `pump.fun`
- Agregado nuevo endpoint de DexScreener para tokens
- Mejorado el orden de prioridad de las fuentes de datos:
  1. DexScreener (datos primarios)
  2. PumpFun (datos secundarios)
  3. Birdeye/Jupiter (fallback)

#### Mejoras en la presentación
- Nuevo formato de visualización similar a Trojan on Sol
- Agregado formateo inteligente de números:
  - Valores en millones muestran "M"
  - Valores en miles muestran "K"
  - Decimales adaptivos según el tamaño del número
- Mejorada la presentación de precios y balances
- Agregado indicador de impacto de precio (placeholder)

### Características nuevas
- Visualización mejorada de datos del token:
  - Balance en SOL
  - Precio en USD
  - Liquidez (LP)
  - Market Cap (MC)
  - Estado de Renounced
  - Impacto de precio estimado
- Mejor formateo de números grandes y decimales
- Interfaz más limpia y profesional

### Correcciones
- Solucionado el problema de tokens que no mostraban datos
- Mejorado el manejo de errores en llamadas a APIs
- Optimizado el tiempo de respuesta con consultas paralelas

### Próximas mejoras planificadas
- Implementar cálculo real de impacto de precio
- Agregar más información sobre el token
- Mejorar el sistema de caché para datos frecuentemente consultados
- Agregar gráficos de precio y volumen

## [1.5.0] - 2023-07-12
### Optimizaciones Ultra-rápidas
- Reducción significativa de la latencia en actualización de precios para todos los tokens
- Optimización ultra-agresiva para tokens virales como INVENTORS con caché de solo 1 segundo
- Implementación de actualización directa a PumpFun con timeouts ultra-reducidos
- Mejora del sistema de actualización en segundo plano con prioridad para tokens virales
- Añadido token INVENTORS a la lista de tokens virales con su dirección correcta
- Añadidos más tokens populares a la lista de tokens virales: LUMBAGO, NOOT, JCHAN
- Reducción de todos los tiempos de caché para mayor frescura de datos
- Mejora de la detección y notificación de cambios significativos en precios
- Optimización de las solicitudes paralelas para reducir la latencia general
- Implementación de actualización en segundo plano más inteligente para minimizar latencia
- Solución para el problema de diferencia entre precios mostrados y precios reales en PumpFun

## [1.4.0] - 2023-06-28
### Características Nuevas
- Soporte completo para tokens de Solana con información detallada
- Visualización de market cap, liquidez y otros datos importantes
- Detección automática de tokens virales/populares para actualizaciones más frecuentes
- Sistema de caché inteligente con tiempos de expiración variables
- Interfaz mejorada para consultar precios y estadísticas de tokens
- Optimizaciones de rendimiento para respuestas más rápidas
- Soporte para múltiples fuentes de datos con fallback automático
- Sistema de alertas para movimientos significativos de precios

## [1.3.0] - 2023-06-15
### Optimizaciones y Mejoras
- Reducción de tiempos de respuesta para consultas de precios
- Mejora de la precisión de datos de market cap
- Implementación de sistema de caché adaptativa
- Optimización de consultas a PumpFun y otras APIs
- Corrección de errores relacionados con tokens sin liquidez
- Mejora de la visualización de datos en mensajes de Telegram
- Implementación de timeouts más inteligentes para APIs lentas
- Soporte para múltiples fuentes de datos con priorización

## [1.2.0] - 2023-05-20
### Funcionalidades
- Soporte inicial para tokens de Solana
- Integración con PumpFun para datos de mercado
- Sistema básico de caché para reducir llamadas API
- Comandos para consultar precios de tokens
- Visualización de datos básicos: precio, cambio 24h
- Soporte para múltiples wallets
- Interfaz básica para Telegram 