@echo off
chcp 65001 >nul
echo ============================================================
echo   CameraBot - Cài đặt môi trường
echo ============================================================
echo.

REM --- Kiểm tra Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [LOI] Python chua duoc cai dat hoac chua co trong PATH.
    echo      Tai tai: https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python da duoc cai dat.

REM --- Cài thư viện Python ---
echo.
echo [*] Dang cai thu vien Python...
pip install -r requirements.txt
if errorlevel 1 (
    echo [LOI] Cai dat thu vien that bai. Kiem tra ket noi mang.
    pause
    exit /b 1
)
echo [OK] Thu vien Python da cai xong.

REM --- Kiểm tra FFmpeg ---
echo.
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [CANH BAO] FFmpeg chua duoc tim thay trong PATH.
    echo           Chuc nang NGHE se khong hoat dong neu thieu FFmpeg.
    echo.
    echo  Cach cai FFmpeg nhanh nhat tren Windows:
    echo    1. Mo PowerShell voi quyen Admin
    echo    2. Chay lenh:  winget install ffmpeg
    echo    Hoac tai thu cong tai:  https://ffmpeg.org/download.html
    echo.
) else (
    echo [OK] FFmpeg da co san.
)

echo.
echo ============================================================
echo   Cai dat hoan tat! Chay ung dung bang lenh: python main.py
echo ============================================================
pause
