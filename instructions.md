# Instrucciones para implementar la interfaz unificada

Para unificar la interfaz de usuario cuando se detecta un token (ya sea que el usuario lo tenga o no), sigue estos pasos:

## Paso 1: Mover el nuevo archivo

Mueve el archivo `unified_interface.py` recién creado a la carpeta `src`:

```
mv unified_interface.py src/
```

## Paso 2: Implementar la solución

Modifica el archivo `src/bot.py` para usar las nuevas funciones:

### Importar el nuevo módulo

Al inicio del archivo, en la sección de importaciones, añade:

```python
from .unified_interface import unified_keyboard, build_unified_message
```

### Modificar la función on_msg

En la función `on_msg`, ya está implementado correctamente el código para guardar siempre la información de token en el estado y llamar a `show_buy`. No es necesario modificar esta parte.

### Modificar la función show_buy

Ahora necesitas modificar la función `show_buy` para usar las nuevas funciones unificadas. Localiza la parte donde se construye el mensaje y se configura el teclado, y reemplázala con algo similar a esto:

```python
# Extraer datos necesarios
sol_value_usd = sel * sol_price
amount_out = await calculate_output_amount(mint, sel)
slippage_pct = ((sol_value_usd - token_value_usd) / sol_value_usd) * 100 if sol_value_usd > 0 else 0
current_value_usd = token_balance * price

# Construir mensaje unificado con toda la información relevante
msg = build_unified_message(
    token_data=token_data if token_balance > 0 else {},
    user_balance=token_balance,
    symbol=symbol,
    name=name,
    mint=mint,
    price=price,
    marketcap=marketcap,
    volume=volume,
    liquidity=liquidity,
    price_change=price_change,
    sol_balance=sol_balance,
    sol_price=sol_price,
    selected_sol=sel,
    estimated_tokens=amount_out,
    current_value_usd=current_value_usd,
    slippage_pct=slippage_pct,
    data_source=data_source,
    fetch_time=fetch_time
)

# Crear teclado unificado
keyboard = unified_keyboard(
    symbol=symbol,
    sel=sel,
    token_balance=token_balance,
    is_refreshing=is_refreshing
)

# Mostrar mensaje con teclado de opciones
if edit:
    await target.edit_message_text(
        msg,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
else:
    await target.message.reply_text(
        msg,
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
```

Esto reemplazará la lógica existente y mostrará siempre una interfaz unificada con información sobre el token y opciones tanto para comprar como para vender, independientemente de si el usuario tiene el token o no.

## Beneficios de esta solución

1. **Experiencia coherente**: El usuario siempre ve el mismo tipo de interfaz, independientemente de si ya tiene el token o no.
2. **Más opciones**: Se muestran siempre las opciones de compra y venta (aunque venta se desactiva si no tiene tokens).
3. **Información completa**: Se muestra toda la información relevante del token en un solo lugar.
4. **Mejor mantenibilidad**: La lógica de construcción del mensaje y teclado está separada en funciones específicas, facilitando cambios futuros.

## Nota importante

Si hay problemas para modificar el archivo directamente debido a la estructura actual, considera crear una función auxiliar temporal que llame a las nuevas funciones, hasta que puedas reorganizar el código completamente. 