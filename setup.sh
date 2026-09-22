#!/usr/bin/env bash
# Puts your Google Ads MCP server on Google Cloud Run so Claude can reach it.
#
# Run it in Google Cloud Shell, from this folder:
#   bash setup.sh
#
# Safe to run again. It skips what already exists and keeps your settings
# unless you type new ones.
set -euo pipefail
cd "$(dirname "$0")"

# Never let gcloud stop to ask a yes/no question.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

SERVICE=google-ads-mcp
REGION="${REGION:-us-central1}"
SECRET_CLIENT=google-ads-mcp-oauth-client-secret
SECRET_JWT=google-ads-mcp-jwt-signing-key
SECRET_STORAGE=google-ads-mcp-storage-encryption-key

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
trap 'printf "\n\033[31mSetup stopped because of the error above. Copy that error text and paste it to Claude for help.\033[0m\n"' ERR

# Asks a question. Pressing Enter keeps the value shown in [brackets].
ask() {
  local question="$1" default="${2:-}" reply
  if [[ -n $default ]]; then
    read -r -p "$question [$default]: " reply
    printf '%s' "${reply:-$default}"
  else
    read -r -p "$question: " reply
    printf '%s' "$reply"
  fi
}

# Runs a command until it works. New service accounts can take a minute to
# show up everywhere in Google Cloud.
retry() {
  local tries=0
  until "$@" >/dev/null 2>&1; do
    tries=$((tries + 1))
    if ((tries >= 6)); then
      "$@" # last try, with its error shown
      return
    fi
    sleep 10
  done
}

secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }

# Creates a secret holding a random value, once. Never replaced, so people
# stay signed in when you run this script again.
ensure_random_secret() {
  if ! secret_exists "$1"; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(48), end="")' |
      gcloud secrets create "$1" --data-file=- --replication-policy=automatic >/dev/null
  fi
}

# ---------------------------------------------------------------------------
bold "Step 1 of 6: Finding your Google Cloud project"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
PROJECT_ID="$(ask "Google Cloud project ID" "$PROJECT_ID")"
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
BASE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
REDIRECT_URI="${BASE_URL}/auth/callback"
echo "Using project $PROJECT_ID"

# ---------------------------------------------------------------------------
bold "Step 2 of 6: Turning on the Google Cloud services it needs (about a minute)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  googleads.googleapis.com

# Settings from an earlier run, so pressing Enter keeps them.
EXISTING_JSON="$(gcloud run services describe "$SERVICE" --region "$REGION" --format=json 2>/dev/null || true)"
existing_env() {
  printf '%s' "$EXISTING_JSON" | python3 -c '
import json, sys
try:
    containers = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"]
except Exception:
    sys.exit(0)
for env in containers[0].get("env", []):
    if env.get("name") == sys.argv[1] and "value" in env:
        print(env["value"], end="")
' "$1"
}

# ---------------------------------------------------------------------------
bold "Step 3 of 6: Your Google sign-in key (OAuth client)"
cat <<EOF
In another browser tab, open (or use the menu: Google Auth Platform > Clients):
  https://console.cloud.google.com/auth/clients?project=${PROJECT_ID}

Click "Create client", then:
  - Application type: Web application
  - Name: Claude Google Ads
  - Under "Authorized redirect URIs" click "Add URI" and paste exactly:

      ${REDIRECT_URI}

  - Click "Create". Copy the Client ID and Client secret it shows you.
EOF
echo
CLIENT_ID="$(ask "Paste the Client ID" "$(existing_env GOOGLE_ADS_MCP_OAUTH_CLIENT_ID)")"
CLIENT_ID="${CLIENT_ID//[[:space:]]/}"
if [[ $CLIENT_ID != *.apps.googleusercontent.com ]]; then
  echo "That does not look like a Client ID. It should end in .apps.googleusercontent.com" >&2
  false
fi
if secret_exists "$SECRET_CLIENT"; then
  read -r -s -p "Paste the Client secret (or press Enter to keep the one saved before): " CLIENT_SECRET
else
  read -r -s -p "Paste the Client secret (it stays hidden while you paste): " CLIENT_SECRET
fi
echo
CLIENT_SECRET="${CLIENT_SECRET//[[:space:]]/}"
if [[ -z $CLIENT_SECRET ]] && ! secret_exists "$SECRET_CLIENT"; then
  echo "The Client secret is required." >&2
  false
fi

# ---------------------------------------------------------------------------
bold "Step 4 of 6: Who is allowed to use this server"
echo "Only these Google accounts can use it. Use the email you log into Google Ads with."
echo "For more than one, separate them with commas."
DEFAULT_EMAILS="$(existing_env ALLOWED_EMAILS)"
DEFAULT_EMAILS="${DEFAULT_EMAILS:-$(gcloud config get-value account 2>/dev/null || true)}"
ALLOWED_EMAILS="$(ask "Allowed email(s)" "$DEFAULT_EMAILS")"
ALLOWED_EMAILS="${ALLOWED_EMAILS//[[:space:]]/}"
if [[ $ALLOWED_EMAILS != *@* ]]; then
  echo "Please enter at least one email address." >&2
  false
fi

echo
echo "Do you reach your ad accounts through a manager account (MCC)?"
echo "If yes, type its 10-digit customer ID. If no, just press Enter."
LOGIN_CUSTOMER_ID="$(ask "Manager account ID" "$(existing_env GOOGLE_ADS_LOGIN_CUSTOMER_ID)")"
LOGIN_CUSTOMER_ID="${LOGIN_CUSTOMER_ID//[^0-9]/}"
if [[ -n $LOGIN_CUSTOMER_ID && ${#LOGIN_CUSTOMER_ID} -ne 10 ]]; then
  echo "A manager account ID has 10 digits, like 123-456-7890." >&2
  false
fi

echo
echo "Thanks. The rest runs by itself and takes about 5 to 10 minutes."

# ---------------------------------------------------------------------------
bold "Step 5 of 6: Setting up storage, keys, and permissions"

# Firestore remembers who is signed in, so you stay connected in Claude.
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --database='(default)' \
    --location="$REGION" --type=firestore-native >/dev/null
fi

# One robot account runs the server, another builds it.
RUNNER_SA="google-ads-mcp-runner@${PROJECT_ID}.iam.gserviceaccount.com"
BUILDER_SA="google-ads-mcp-builder@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$RUNNER_SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create google-ads-mcp-runner \
    --display-name="Google Ads MCP server" >/dev/null
gcloud iam service-accounts describe "$BUILDER_SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create google-ads-mcp-builder \
    --display-name="Google Ads MCP builder" >/dev/null
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet --condition=None \
  --member="serviceAccount:${RUNNER_SA}" --role=roles/datastore.user
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet --condition=None \
  --member="serviceAccount:${BUILDER_SA}" --role=roles/run.builder

# Secrets live in Secret Manager, not in the code or settings.
if [[ -n $CLIENT_SECRET ]]; then
  if secret_exists "$SECRET_CLIENT"; then
    printf '%s' "$CLIENT_SECRET" | gcloud secrets versions add "$SECRET_CLIENT" --data-file=- >/dev/null
  else
    printf '%s' "$CLIENT_SECRET" |
      gcloud secrets create "$SECRET_CLIENT" --data-file=- --replication-policy=automatic >/dev/null
  fi
fi
unset CLIENT_SECRET
ensure_random_secret "$SECRET_JWT"
ensure_random_secret "$SECRET_STORAGE"
for secret in "$SECRET_CLIENT" "$SECRET_JWT" "$SECRET_STORAGE"; do
  retry gcloud secrets add-iam-policy-binding "$secret" --quiet \
    --member="serviceAccount:${RUNNER_SA}" --role=roles/secretmanager.secretAccessor
done

# ---------------------------------------------------------------------------
bold "Step 6 of 6: Building and starting the server (the slow part)"
ENV_FILE="$(mktemp)"
trap 'rm -f "$ENV_FILE"' EXIT
{
  echo "GOOGLE_PROJECT_ID: \"${PROJECT_ID}\""
  echo "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID: \"${CLIENT_ID}\""
  echo "GOOGLE_ADS_MCP_BASE_URL: \"${BASE_URL}\""
  echo "GOOGLE_ADS_MCP_STORAGE_TYPE: \"firestore\""
  echo "ALLOWED_EMAILS: \"${ALLOWED_EMAILS}\""
  if [[ -n $LOGIN_CUSTOMER_ID ]]; then
    echo "GOOGLE_ADS_LOGIN_CUSTOMER_ID: \"${LOGIN_CUSTOMER_ID}\""
  fi
} >"$ENV_FILE"

gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$RUNNER_SA" \
  --build-service-account "projects/${PROJECT_ID}/serviceAccounts/${BUILDER_SA}" \
  --allow-unauthenticated \
  --max-instances 1 \
  --cpu-boost \
  --env-vars-file "$ENV_FILE" \
  --set-secrets "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=${SECRET_CLIENT}:latest,GOOGLE_ADS_MCP_JWT_SIGNING_KEY=${SECRET_JWT}:latest,GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY=${SECRET_STORAGE}:latest" \
  --quiet

# Quick check that the server answers the way Claude expects.
STATUS="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/.well-known/oauth-protected-resource/mcp" || true)"
if [[ $STATUS != 200 ]]; then
  echo "The server was deployed but did not answer yet (status ${STATUS}). Wait a minute and run: curl ${BASE_URL}/.well-known/oauth-protected-resource/mcp" >&2
fi

bold "Done! Your server is live."
cat <<EOF

Paste this URL into Claude as a custom connector:

    ${BASE_URL}/mcp

Reminder: your Google OAuth client must list this redirect URI:
    ${REDIRECT_URI}
EOF
