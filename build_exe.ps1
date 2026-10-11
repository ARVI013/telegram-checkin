# 一键打包「Telegram签到.exe」并放到桌面
# 用法：powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

Write-Host '== 1/4 检查依赖 =='
python -c "import PyInstaller, PIL, uiautomation" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host '缺少打包依赖，正在安装 pyinstaller / pillow / uiautomation ...'
  python -m pip install -U pyinstaller pillow uiautomation
}

Write-Host '== 2/4 生成图标（telegram.svg -> telegram.ico）=='
python telegram_checkin\icon_build.py

Write-Host '== 3/4 PyInstaller 打包 =='
python -m PyInstaller --noconfirm --onefile --windowed --name "Telegram签到" `
  --icon telegram_checkin\telegram.ico --add-data "telegram_checkin\telegram.ico;." `
  --hidden-import uiautomation --hidden-import comtypes --hidden-import comtypes.client `
  --collect-submodules comtypes `
  --distpath dist --workpath build --specpath . telegram_checkin\cli.py

Write-Host '== 4/4 复制到桌面 =='
$desk = [Environment]::GetFolderPath('Desktop')
Copy-Item (Join-Path $root 'dist\Telegram签到.exe') $desk -Force
Write-Host "已生成: $desk\Telegram签到.exe"
Write-Host '提示：如果之前用旧路径建过计划任务，请在程序里点一次「同步计划任务」。'
