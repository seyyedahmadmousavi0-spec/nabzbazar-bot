# Run dashboard and worker together
$Host.UI.RawUI.WindowTitle = "سامانه نبض بازار ۳۶۰"
Set-Location $PSScriptRoot\..

Write-Host "اجرای داشبورد مدیریت در پورت 8585..." -ForegroundColor Cyan
Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m marketintel serve" -WindowStyle Hidden

Write-Host "پنل مدیریت در آدرس زیر آماده است: http://127.0.0.1:8585" -ForegroundColor Green
Write-Host "نام کاربری: admin | گذرواژه: admin123456" -ForegroundColor Yellow
Write-Host "شروع ورکر خودکار پایش و انتشار کانال‌ها..." -ForegroundColor Cyan

.\.venv\Scripts\python.exe -m marketintel worker --interval 20

