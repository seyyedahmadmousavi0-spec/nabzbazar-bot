@echo off
chcp 65001 > nul
title سامانه نبض بازار ۳۶۰
echo ========================================================
echo    در حال اجرای سامانه خودکار نبض بازار ۳۶۰
echo ========================================================
cd /d "%~dp0"

echo 1. بررسی و فعال‌سازی محیط پایتون...
if not exist ".venv\Scripts\python.exe" (
    echo محیط مجازی یافت نشد. در حال ساخت .venv...
    py -m venv .venv
    .\.venv\Scripts\pip install -r requirements.txt
)

echo 2. اجرای داشبورد مدیریت در پس‌زمینه (پورت 8585)...
start "داشبورد مدیریت نبض بازار" /min .\.venv\Scripts\python.exe -m marketintel serve

echo 3. اجرای ورکر خودکار استخراج و انتشار بازار...
echo پنل وب در آدرس زیر در دسترس است:
echo http://127.0.0.1:8585
echo نام کاربری: admin
echo گذرواژه: admin123456
echo ========================================================
echo برای توقف ورکر کلید Ctrl+C را فشار دهید.
.\.venv\Scripts\python.exe -m marketintel worker --interval 20
pause

