Set-Location -LiteralPath $PSScriptRoot
python -c "import fastapi, uvicorn"
if ($LASTEXITCODE -ne 0) {
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Không cài được các thư viện cần thiết.' }
}
python app.py @args
