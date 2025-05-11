# Script para iniciar el bot de trading
Write-Host "Deteniendo instancias previas del bot..." -ForegroundColor Yellow
taskkill /f /im python.exe 2>$null

Write-Host "Instalando/Actualizando dependencias..." -ForegroundColor Cyan
pip install -r requirements.txt

Write-Host "Iniciando bot de trading..." -ForegroundColor Green
python -m src.bot 