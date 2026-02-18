@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo Сборка GoldSprint2 в портативную папку...
echo.

pip show pyinstaller >nul 2>&1 || (
  echo Установка PyInstaller...
  pip install pyinstaller
  if errorlevel 1 (
    echo Ошибка. Попробуйте: pip install pyinstaller
    echo Или запустите cmd от имени администратора.
    pause
    exit /b 1
  )
)

if exist "dist\GoldSprint2" rmdir /s /q "dist\GoldSprint2"
if exist "build" rmdir /s /q "build"

python -m PyInstaller --onedir --name GoldSprint2 --windowed --noconfirm --noupx --clean ^
  --hidden-import=bleak ^
  --hidden-import=bleak.backends.winrt ^
  --hidden-import=bleak.backends.winrt.scanner ^
  --hidden-import=bleak.backends.device ^
  --hidden-import=asyncio ^
  --collect-all bleak ^
  goldsprint2.py

if errorlevel 1 (
  echo Сборка завершилась с ошибкой.
  pause
  exit /b 1
)

if exist "cadence_config.json" (
  copy /y "cadence_config.json" "dist\GoldSprint2\cadence_config.json" >nul
  echo Скопирован cadence_config.json
)

echo.
echo Готово. Портативная папка: %CD%\dist\GoldSprint2
echo Запуск: dist\GoldSprint2\GoldSprint2.exe
echo Скопируйте всю папку dist\GoldSprint2 — всё необходимое внутри.
pause
