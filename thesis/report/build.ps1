# Build the SmartLoad final report (XeLaTeX + biber, IEEE bibliography).
# Usage:  pwsh ./build.ps1
# Produces main.pdf. Uses the manual pass sequence so no Perl/latexmk is required.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$mik = "$env:LOCALAPPDATA\Programs\MiKTeX\miktex\bin\x64"
if (Test-Path $mik) { $env:Path = "$mik;$env:Path" }

Write-Host "Pass 1/4: xelatex"      ; xelatex -interaction=nonstopmode main.tex | Out-Null
Write-Host "Pass 2/4: biber"        ; biber main | Out-Null
Write-Host "Pass 3/4: xelatex"      ; xelatex -interaction=nonstopmode main.tex | Out-Null
Write-Host "Pass 4/4: xelatex"      ; xelatex -interaction=nonstopmode main.tex | Out-Null

if (Test-Path main.pdf) {
    $pages = (Select-String -Path main.log -Pattern "Output written on main.pdf \((\d+) page").Matches[-1].Groups[1].Value
    Write-Host ("OK -> main.pdf ({0} pages, {1:N0} bytes)" -f $pages, (Get-Item main.pdf).Length) -ForegroundColor Green
} else {
    Write-Error "Build failed: main.pdf was not produced. See main.log."
}
