<#
.SYNOPSIS
    Automates the Google Cloud Platform (GCP) infrastructure setup for the LeadWise AI Sundries project.

.DESCRIPTION
    This script provisions all necessary GCP resources required for the GitHub Actions CI/CD 
    pipeline to deploy the Flask application to Cloud Run. 

    It performs the following steps:
    1. Validates that the Google Cloud CLI (gcloud) is installed and authenticated.
    2. Enables the required GCP APIs (Cloud Run, Artifact Registry, IAM).
    3. Creates a Docker repository in Artifact Registry to store the app images.
    4. Creates a dedicated Service Account for GitHub Actions to use during deployment.
    5. Grants the necessary IAM roles (Artifact Registry Writer, Cloud Run Admin, Service Account User).
    6. Sets up Workload Identity Federation (WIF) to allow secure, keyless authentication from GitHub.
    7. Binds the specific GitHub repository to the Service Account so it can impersonate it.
    8. Outputs the exact secret and variable values you need to paste into GitHub.

    The script is idempotent, meaning it checks if resources already exist before trying to 
    create them. You can safely run it multiple times without causing errors.

.HOW TO RUN
    1. Open PowerShell.
    2. Allow script execution for this session (bypasses Windows execution policy):
       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    3. Navigate to the project root:
       cd C:\Irfaan\leadwiseai\lead-wise-ai-sundries
    4. Run the script:
       .\setup-gcp.ps1
#>

$ErrorActionPreference = 'Stop'

Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " GCP Setup for Sundries" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan

# =====================================================================
# 1. CONFIGURATION VARIABLES
# Modify these if your GitHub username, repo name, or GCP region changes.
# =====================================================================
$env:PROJECT_ID="lead-wise-ai-sundries"
$env:REGION="africa-south1"
$env:REPO_NAME="sundries-repo"
$env:SA_NAME="github-deployer"
$env:POOL_ID="github-pool"
$env:PROVIDER_ID="github-provider"
$env:GITHUB_REPO="irfaans78602/lead-wise-ai-sundries"

Write-Host "Project: $env:PROJECT_ID"
Write-Host "Region:  $env:REGION"
Write-Host "Repo:    $env:GITHUB_REPO"
Write-Host ""

# =====================================================================
# 2. PRE-FLIGHT CHECKS
# Ensure gcloud is installed and the user is logged in.
# =====================================================================
if (-not (Get-Command "gcloud" -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: gcloud CLI is not installed or not in your PATH." -ForegroundColor Red
    Write-Host "Please install it from https://cloud.google.com/sdk/docs/install" -ForegroundColor Yellow
    exit 1
}

$authCheck = gcloud auth list --filter=status:ACTIVE --format="value(account)"
if ([string]::IsNullOrWhiteSpace($authCheck)) {
    Write-Host "ERROR: You are not logged into gcloud." -ForegroundColor Red
    Write-Host "Please run: gcloud auth login" -ForegroundColor Yellow
    exit 1
}

Write-Host "Authenticated as: $authCheck" -ForegroundColor Green
Write-Host ""

# Set active project
gcloud config set project $env:PROJECT_ID

# =====================================================================
# 3. ENABLE REQUIRED APIS
# We need Cloud Run, Artifact Registry, and IAM APIs enabled.
# =====================================================================
Write-Host "[1/6] Enabling APIs (this may take a minute)..." -ForegroundColor Cyan
gcloud services enable run.googleapis.com artifactregistry.googleapis.com iamcredentials.googleapis.com

# =====================================================================
# 4. CREATE ARTIFACT REGISTRY
# This is where Docker images will be stored.
# =====================================================================
Write-Host "[2/6] Creating Artifact Registry..." -ForegroundColor Cyan
$repoCheck = gcloud artifacts repositories describe $env:REPO_NAME --location=$env:REGION --format="value(name)" 2>$null
if ([string]::IsNullOrWhiteSpace($repoCheck)) {
    gcloud artifacts repositories create $env:REPO_NAME --repository-format=docker --location=$env:REGION --description="Docker repository for sundries"
    Write-Host "  Artifact Registry created." -ForegroundColor Green
} else {
    Write-Host "  Artifact Registry already exists, skipping." -ForegroundColor Yellow
}

# =====================================================================
# 5. CREATE SERVICE ACCOUNT
# This identity will be used by GitHub Actions to deploy to GCP.
# =====================================================================
Write-Host "[3/6] Creating Service Account..." -ForegroundColor Cyan
$env:SA_EMAIL="$env:SA_NAME@$env:PROJECT_ID.iam.gserviceaccount.com"

$saCheck = gcloud iam service-accounts describe $env:SA_EMAIL --format="value(email)" 2>$null
if ([string]::IsNullOrWhiteSpace($saCheck)) {
    gcloud iam service-accounts create $env:SA_NAME --display-name="GitHub Actions Deployer"
    Write-Host "  Service Account created." -ForegroundColor Green
} else {
    Write-Host "  Service Account already exists, skipping." -ForegroundColor Yellow
}

# =====================================================================
# 6. GRANT IAM ROLES TO SERVICE ACCOUNT
# The deployer needs to push images, deploy Cloud Run, and act as the runtime SA.
# =====================================================================
Write-Host "[4/6] Granting IAM Roles..." -ForegroundColor Cyan
gcloud projects add-iam-policy-binding $env:PROJECT_ID --member="serviceAccount:$env:SA_EMAIL" --role="roles/artifactregistry.writer" --quiet
gcloud projects add-iam-policy-binding $env:PROJECT_ID --member="serviceAccount:$env:SA_EMAIL" --role="roles/run.admin" --quiet
gcloud projects add-iam-policy-binding $env:PROJECT_ID --member="serviceAccount:$env:SA_EMAIL" --role="roles/iam.serviceAccountUser" --quiet
Write-Host "  Roles granted." -ForegroundColor Green

# =====================================================================
# 7. SETUP WORKLOAD IDENTITY FEDERATION (Keyless Auth)
# Allows GitHub to authenticate without storing long-lived JSON keys.
# =====================================================================
Write-Host "[5/6] Setting up Workload Identity Federation..." -ForegroundColor Cyan
$poolCheck = gcloud iam workload-identity-pools describe $env:POOL_ID --location="global" --format="value(name)" 2>$null
if ([string]::IsNullOrWhiteSpace($poolCheck)) {
    gcloud iam workload-identity-pools create $env:POOL_ID --location="global" --display-name="GitHub Actions Pool"
    Write-Host "  Pool created." -ForegroundColor Green
} else {
    Write-Host "  Pool already exists, skipping." -ForegroundColor Yellow
}

$providerCheck = gcloud iam workload-identity-pools providers describe $env:PROVIDER_ID --location="global" --workload-identity-pool=$env:POOL_ID --format="value(name)" 2>$null
if ([string]::IsNullOrWhiteSpace($providerCheck)) {
    gcloud iam workload-identity-pools providers create-oidc $env:PROVIDER_ID --location="global" --workload-identity-pool=$env:POOL_ID --display-name="GitHub Provider" --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" --issuer-uri="https://token.actions.githubusercontent.com" --attribute-condition="assertion.repository == '$env:GITHUB_REPO'"
    Write-Host "  Provider created." -ForegroundColor Green
} else {
    Write-Host "  Provider already exists, skipping." -ForegroundColor Yellow
}

# Retrieve the Project Number (required to construct the WIF provider string)
$env:PROJECT_NUMBER = (gcloud projects describe $env:PROJECT_ID --format="value(projectNumber)")
if ([string]::IsNullOrWhiteSpace($env:PROJECT_NUMBER)) {
    Write-Host "ERROR: Could not retrieve Project Number. Are you sure you have access to $env:PROJECT_ID?" -ForegroundColor Red
    exit 1
}

# Grant the GitHub repository permission to impersonate the Service Account
$bindingCheck = gcloud iam service-accounts get-iam-policy $env:SA_EMAIL --format="json" | ConvertFrom-Json
# Note: add-iam-policy-binding is idempotent. Running it again just updates the existing binding.
gcloud iam service-accounts add-iam-policy-binding $env:SA_EMAIL --role="roles/iam.workloadIdentityUser" --member="principalSet://iam.googleapis.com/projects/$env:PROJECT_NUMBER/locations/global/workloadIdentityPools/$env:POOL_ID/attribute.repository/$env:GITHUB_REPO" --quiet
Write-Host "  Impersonation permission granted." -ForegroundColor Green

# =====================================================================
# 8. OUTPUT THE VALUES FOR GITHUB
# Copy these directly into your GitHub Repository Secrets/Variables.
# =====================================================================
Write-Host "[6/6] Setup Complete!" -ForegroundColor Green
Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "COPY THESE VALUES INTO YOUR GITHUB SECRETS/VARIABLES" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host "WIF_SERVICE_ACCOUNT (Secret): $env:SA_EMAIL" -ForegroundColor Yellow
Write-Host "WIF_PROVIDER (Secret): projects/$env:PROJECT_NUMBER/locations/global/workloadIdentityPools/$env:POOL_ID/providers/$env:PROVIDER_ID" -ForegroundColor Yellow
Write-Host ""
Write-Host "GCP_PROJECT_ID (Variable): $env:PROJECT_ID"
Write-Host "GCP_REGION (Variable): $env:REGION"
Write-Host "GCP_REPO_NAME (Variable): $env:REPO_NAME"
Write-Host "GCP_SERVICE_NAME (Variable): sundries-api"
Write-Host "=====================================================" -ForegroundColor Cyan