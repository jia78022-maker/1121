param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$repo = "jia78022-maker/1121"
$root = $PSScriptRoot
Set-Location $root

if (-not $env:GITHUB_TOKEN) {
    $env:GITHUB_TOKEN = [Environment]::GetEnvironmentVariable("GITHUB_TOKEN", "User")
}
if (-not $env:GITHUB_TOKEN) {
    throw "未检测到 GITHUB_TOKEN。请先在 Windows 用户环境变量中设置一个具有仓库 Contents: Read and write 权限的 GitHub Fine-grained token，然后重新打开 PowerShell。"
}
if (-not $Version) {
    $match = Select-String -Path "$root\app.py" -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $match) { throw "无法从 app.py 读取 APP_VERSION。" }
    $Version = $match.Matches[0].Groups[1].Value
}

$icon = Join-Path $root "客户管理器.ico"
if (-not (Test-Path $icon)) { python "$root\create_icon.py" }
python -m py_compile app.py server\account_server.py
python -m PyInstaller --noconfirm --clean --onefile --windowed --icon $icon --name "客户管理器" app.py
Copy-Item -LiteralPath "$root\dist\客户管理器.exe" -Destination "$root\客户管理器.exe" -Force
$asset = Join-Path $root "客户管理器.exe"
$hashAsset = "$asset.sha256"
$hash = (Get-FileHash -LiteralPath $asset -Algorithm SHA256).Hash.ToLower()
Set-Content -LiteralPath $hashAsset -Value "$hash  客户管理器.exe" -Encoding ascii

# 只提交源代码和图标；本地账号、订单数据库、EXE 与令牌均由 .gitignore 排除。
git add app.py create_icon.py 客户管理器.ico 客户管理器图标.png server\account_server.py .gitignore 发布到GitHub.ps1
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git commit -m "release: 客户管理器 v$Version"
    git push origin main
}

$headers = @{ Authorization = "Bearer $env:GITHUB_TOKEN"; Accept = "application/vnd.github+json"; "X-GitHub-Api-Version" = "2022-11-28" }
$tag = "v$Version"
try {
    $release = Invoke-RestMethod -Method Get -Headers $headers -Uri "https://api.github.com/repos/$repo/releases/tags/$tag"
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    $body = @{ tag_name = $tag; target_commitish = "main"; name = "客户管理器 v$Version"; draft = $false; prerelease = $false } | ConvertTo-Json
    $release = Invoke-RestMethod -Method Post -Headers $headers -ContentType "application/json" -Body $body -Uri "https://api.github.com/repos/$repo/releases"
}

foreach ($upload in @(
    @{ file = $asset; name = "CustomerManager.exe" },
    @{ file = $hashAsset; name = "CustomerManager.exe.sha256" }
)) {
    $file = $upload.file
    $name = $upload.name
    $old = @($release.assets | Where-Object { $_.name -eq $name }) | Select-Object -First 1
    if ($old) { Invoke-RestMethod -Method Delete -Headers $headers -Uri $old.url | Out-Null }
    $uploadUrl = ($release.upload_url -split '\{')[0]
    $uploadUri = [Uri]($uploadUrl + "?name=" + [Uri]::EscapeDataString($name))
    Invoke-WebRequest -Method Post -Headers $headers -ContentType "application/octet-stream" -InFile $file -Uri $uploadUri | Out-Null
}
Write-Host "发布完成：客户管理器 v$Version" -ForegroundColor Green

