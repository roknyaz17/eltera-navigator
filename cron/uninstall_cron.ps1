# Удаляет все EltreaBot-задачи из Task Scheduler.

$old = @(
    "EltreaBot-Morning", "EltreaBot-Update",
    "EltreaBot-Morning-Telegram", "EltreaBot-Noon-Tables", "EltreaBot-Afternoon-Vahtapro"
)
foreach ($t in $old) {
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "Все задачи EltreaBot-* удалены." -ForegroundColor Green
