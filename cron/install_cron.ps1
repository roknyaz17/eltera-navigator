# Регистрация трёх задач в Windows Task Scheduler.
#
# Запуск:
#   powershell -ExecutionPolicy Bypass -File F:\eltera-bot\cron\install_cron.ps1
#
# Расписание:
#   09:30  →  EltreaBot-Morning-Telegram   ВахтаПро + AAA+ (со сбросом этих 2)
#   12:00  →  EltreaBot-Noon-Tables        КПК + Yappi + Маркетстафф (со сбросом этих 3)
#   13:00  →  EltreaBot-Afternoon-Vahtapro ВахтаПро (со сбросом только ВахтаПро)

$ErrorActionPreference = "Stop"

# Сносим старые задачи если есть
$old = @(
    "EltreaBot-Morning", "EltreaBot-Update",                       # старые из v1
    "EltreaBot-Morning-Telegram", "EltreaBot-Noon-Tables", "EltreaBot-Afternoon-Vahtapro"
)
foreach ($t in $old) {
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

function Register-Bot-Task {
    param([string]$Name, [string]$Bat, [DateTime]$At, [string]$Desc)
    $action = New-ScheduledTaskAction -Execute $Bat -WorkingDirectory "F:\eltera-bot"
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    Register-ScheduledTask -TaskName $Name `
        -Action $action -Trigger $trigger -Settings $settings -Description $Desc | Out-Null
    Write-Host "  + $Name ($($At.ToString('HH:mm')))" -ForegroundColor Green
}

Write-Host "Регистрирую задачи..." -ForegroundColor Cyan

Register-Bot-Task `
    -Name "EltreaBot-Morning-Telegram" `
    -Bat  "F:\eltera-bot\cron\morning_telegram.bat" `
    -At   "9:30am" `
    -Desc "ВахтаПро + AAA+ со сбросом is_active этих двух source."

Register-Bot-Task `
    -Name "EltreaBot-Noon-Tables" `
    -Bat  "F:\eltera-bot\cron\noon_tables.bat" `
    -At   "12:00pm" `
    -Desc "КПК + Yappi + Маркетстафф со сбросом is_active этих трёх source."

Register-Bot-Task `
    -Name "EltreaBot-Afternoon-Vahtapro" `
    -Bat  "F:\eltera-bot\cron\afternoon_vahtapro.bat" `
    -At   "1:00pm" `
    -Desc "ВахтаПро без сброса (деактивация только по snapshot_marker внутри сообщения)."

Write-Host ""
Write-Host "=== Зарегистрированные задачи ===" -ForegroundColor Green
Get-ScheduledTask -TaskName "EltreaBot-*" | Format-Table TaskName, State

Write-Host "Готово." -ForegroundColor Green
Write-Host ""
Write-Host "Запустить вручную для проверки:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName EltreaBot-Morning-Telegram"
Write-Host "Логи:" -ForegroundColor Yellow
Write-Host "  Get-Content F:\eltera-bot\logs\eltera.log -Tail 50"
