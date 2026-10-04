# ===================== UNIRSE COMO WORKER (Windows / PowerShell) =====================
# Pegar todo en PowerShell. Servidor central: laptop de Kenny (Tailscale 100.118.70.69).
# Requisitos previos: Git, Python (comando "py") y Tailscale con sesion iniciada.

$Servidor = "http://100.118.70.69:8000"
$WorkerId = "worker-$env:COMPUTERNAME".ToLower()
$Dataset  = "C:\dataset"
$Repo     = "$HOME\distributed-processing-platform"
$FfDir    = "$HOME\ffmpeg"
$ProgressPreference = "SilentlyContinue"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# 1. Codigo: clonar la primera vez, o actualizar
if (-not (Test-Path "$Repo\.git")) { git clone https://github.com/nahum0804/distributed-processing-platform.git $Repo }
Set-Location $Repo
if (Test-Path .\tasks.db) { Rename-Item .\tasks.db ("tasks_viejo_" + (Get-Date -Format yyyyMMddHHmmss) + ".db") }
git checkout main
git pull origin main

# 2. Entorno de Python y dependencias
if (-not (Test-Path .\.venv\Scripts\python.exe)) { py -m venv .venv }
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3. FFmpeg (se descarga solo si no esta instalado)
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    if (-not (Get-ChildItem $FfDir -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue)) {
        Write-Host "Descargando FFmpeg (unos 115 MB)..."
        Invoke-WebRequest -Uri https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip -OutFile "$env:TEMP\ffmpeg.zip"
        Expand-Archive "$env:TEMP\ffmpeg.zip" -DestinationPath $FfDir -Force
    }
    $FfBin = (Get-ChildItem $FfDir -Recurse -Filter ffmpeg.exe | Select-Object -First 1).DirectoryName
    [Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";$FfBin", "User")
    $env:Path += ";$FfBin"
}
ffmpeg -version | Select-Object -First 1
ffprobe -version | Select-Object -First 1

# 4. Dataset local
if (Test-Path $Dataset) {
    Write-Host ("Archivos en el dataset: " + (Get-ChildItem $Dataset -Recurse -File -Include *.mp3, *.mp4, *.wav).Count)
} else {
    Write-Host "No existe $Dataset. Copiar ahi el dataset (mp3s, mp4, wav) o agregar --simulate al comando del worker." -ForegroundColor Red
}

# 5. Conexion con el servidor central
tailscale ping -c 3 100.118.70.69
Invoke-RestMethod "$Servidor/tasks/status"

# 6. Iniciar el worker (dejar esta ventana abierta; Ctrl + C para detenerlo)
python worker.py --server $Servidor --worker-id $WorkerId --dataset-dir $Dataset
