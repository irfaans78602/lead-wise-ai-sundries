# tools/sign_ask_ai.ps1

# 1. Set your production key (Paste the exact string from your Render env vars)
$env:LINK_SIGNING_KEYS="v1:ec86a2e39fb8d2fc1635d80d3a4cf87fc8a6d569d29b2360cac5045cf3957358"

# 2. The client's instance_id
$INSTANCE_ID = "123e4567-e89b-12d3-a456-426614174000"

# 3. Generate the script tag
Write-Host "`n--- GENERATING SCRIPT TAG ---" -ForegroundColor Cyan
python sign_ask_ai.py $INSTANCE_ID