# Сборка GoldSprint2 в портативную папку (один exe + все зависимости в одной папке)
# Запуск: .\build_portable.ps1
# Требуется: Python 3, установить зависимости: pip install -r requirements.txt
# При ошибке установки PyInstaller попробуйте запустить PowerShell от имени администратора.

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$AppName = "GoldSprint2"
$EntryPoint = "goldsprint2.py"

# Проверка наличия скрипта
if (-not (Test-Path $EntryPoint)) {
    Write-Error "Не найден файл $EntryPoint в $ScriptDir"
}

# Установка PyInstaller при необходимости
$pi = pip show pyinstaller 2>$null
if (-not $pi) {
    Write-Host "Установка PyInstaller..."
    pip install pyinstaller
}

# Очистка предыдущей сборки
if (Test-Path "dist\$AppName") { Remove-Item -Recurse -Force "dist\$AppName" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

Write-Host "Сборка (режим one folder — портативная папка)..."

# Сборка: одна папка, без консоли (GUI)
# hidden-import для bleak и зависимостей на Windows
pyinstaller --onedir `
    --name $AppName `
    --windowed `
    --noconfirm `
    --noupx `
    --clean `
    --hidden-import=bleak `
    --hidden-import=bleak.backends.winrt `
    --hidden-import=bleak.backends.winrt.scanner `
    --hidden-import=bleak.backends.device `
    --hidden-import=asyncio `
    --hidden-import=winrt `
    --collect-all bleak `
    $EntryPoint

if ($LASTEXITCODE -ne 0) {
    Write-Error "Сборка завершилась с ошибкой."
}

# Копирование cadence_config.json в папку с exe (шаблон настроек)
$DestDir = "dist\$AppName"
if (Test-Path "cadence_config.json") {
    Copy-Item "cadence_config.json" "$DestDir\cadence_config.json" -Force
    Write-Host "Скопирован cadence_config.json в $DestDir"
}

Write-Host ""
Write-Host "Готово. Портативная папка: $ScriptDir\dist\$AppName"
Write-Host "Запуск: dist\$AppName\$AppName.exe"
Write-Host "Скопируйте всю папку dist\$AppName на любой компьютер — всё необходимое внутри."
