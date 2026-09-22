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
trap 'printf "\n\nSetup was stopped before it finished. Your answers so far are saved. Run bash setup.sh to pick up where you left off.\n"; exit 130' INT

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

# Asks Google whether a Client ID and secret belong together. It sends a
# made-up sign-in code on purpose: Google answers "invalid_client" only when
# the ID or secret is wrong. Prints what Google said and fails on a bad pair.
check_client() {
  local reply
  reply="$(printf '%s' "$2" | curl -s --max-time 15 https://oauth2.googleapis.com/token \
    --data-urlencode "client_id=$1" --data-urlencode "client_secret@-" \
    -d grant_type=authorization_code -d code=setup-check \
    --data-urlencode "redirect_uri=${REDIRECT_URI}" || true)"
  if [[ $reply == *'"invalid_client"'* ]]; then
    echo "Google says this Client ID and Client secret don't work together:"
    printf '%s\n' "$reply" | grep -o '"error_description"[^,}]*' || true
    return 1
  fi
}

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
DEFAULT_PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z $DEFAULT_PROJECT ]]; then
  PROJECTS="$(gcloud projects list --format='value(projectId)' 2>/dev/null || true)"
  if [[ -n $PROJECTS && $PROJECTS != *$'\n'* ]]; then
    DEFAULT_PROJECT="$PROJECTS" # only one project, so offer it
  elif [[ -n $PROJECTS ]]; then
    echo "Your project IDs:"
    printf '%s\n' "$PROJECTS" | sed 's/^/  /'
  fi
fi
echo "This is the project ID (like google-ads-mcp or google-ads-mcp-123456), not the Client ID."
for attempt in 1 2 3 4 5; do
  PROJECT_ID="$(ask "Google Cloud project ID" "$DEFAULT_PROJECT")"
  PROJECT_ID="${PROJECT_ID//[[:space:]]/}"
  if [[ $PROJECT_ID == *.apps.googleusercontent.com ]]; then
    echo "That's your Client ID. Keep it handy for step 3. Here I need the project ID."
  elif [[ -n $PROJECT_ID ]] &&
    PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)' 2>/dev/null)" &&
    [[ -n $PROJECT_NUMBER ]]; then
    break
  else
    echo "Couldn't find a project with the ID \"$PROJECT_ID\". Check the spelling and try again."
  fi
  if ((attempt == 5)); then
    echo "Run gcloud projects list to see your project IDs, then run bash setup.sh again." >&2
    false
  fi
done
gcloud config set project "$PROJECT_ID" >/dev/null 2>&1
BASE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"
REDIRECT_URI="${BASE_URL}/auth/callback"
echo "Using project $PROJECT_ID"

# Answers from earlier runs (never the secret), so pressing Enter keeps them.
SETTINGS_FILE="$HOME/.google-ads-mcp-setup-${PROJECT_ID}"
if [[ -f $SETTINGS_FILE ]]; then
  # shellcheck source=/dev/null
  source "$SETTINGS_FILE"
fi
save_setting() {
  local tmp
  tmp="$(mktemp)"
  { grep -v "^$1=" "$SETTINGS_FILE" 2>/dev/null || true; printf '%s=%q\n' "$1" "$2"; } >"$tmp"
  mv "$tmp" "$SETTINGS_FILE"
}

# Google Cloud won't turn on the services below without billing.
BILLING="$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [[ ${BILLING,,} == false ]]; then
  cat >&2 <<EOF

Billing isn't turned on for project ${PROJECT_ID} yet. Google Cloud needs it before it can run the server.
  1. Open https://console.cloud.google.com/billing/projects
  2. Find ${PROJECT_ID}, click the three dots, and pick "Change billing".
  3. Choose your billing account and click "Set account".
No billing account yet? Make one first at https://console.cloud.google.com/billing/create
Then run: bash setup.sh
EOF
  false
fi

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

To paste here, press Ctrl+V (Cmd+V on a Mac). Ctrl+C in this window stops the setup.
EOF
echo
DEFAULT_CLIENT_ID="${SAVED_CLIENT_ID:-$(existing_env GOOGLE_ADS_MCP_OAUTH_CLIENT_ID)}"
for attempt in 1 2 3 4 5; do
  CLIENT_ID="$(ask "Paste the Client ID" "$DEFAULT_CLIENT_ID")"
  CLIENT_ID="${CLIENT_ID//[[:space:]]/}"
  if [[ $CLIENT_ID =~ ^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$ ]]; then
    break
  fi
  echo "That isn't a full Client ID. Open your client and click the copy button next to Client ID."
  echo "A full one looks like 1234567890-abc123def456.apps.googleusercontent.com, with no ... in the middle."
  if ((attempt == 5)); then false; fi
done
save_setting SAVED_CLIENT_ID "$CLIENT_ID"

if secret_exists "$SECRET_CLIENT"; then
  SECRET_PROMPT="Paste the Client secret, or press Enter to keep the one you saved before: "
else
  SECRET_PROMPT="Paste the Client secret and press Enter (nothing shows while you paste, that's normal): "
fi
for attempt in 1 2 3 4 5; do
  read -r -s -p "$SECRET_PROMPT" CLIENT_SECRET
  echo
  CLIENT_SECRET="${CLIENT_SECRET//[[:space:]]/}"
  if [[ $CLIENT_SECRET == *.apps.googleusercontent.com ]]; then
    echo "That's the Client ID. The secret is the other value, and it often starts with GOCSPX-."
  elif [[ -n $CLIENT_SECRET ]]; then
    if check_client "$CLIENT_ID" "$CLIENT_SECRET"; then
      # Saved right away in Secret Manager, so a second run won't need it again.
      if secret_exists "$SECRET_CLIENT"; then
        printf '%s' "$CLIENT_SECRET" | gcloud secrets versions add "$SECRET_CLIENT" --data-file=- >/dev/null
      else
        printf '%s' "$CLIENT_SECRET" |
          gcloud secrets create "$SECRET_CLIENT" --data-file=- --replication-policy=automatic >/dev/null
      fi
      echo "Google accepted it. Saved the Client secret that ends in ${CLIENT_SECRET: -4}."
      break
    fi
    echo "Make sure the secret is turned on and comes from the same client as the Client ID above."
  elif secret_exists "$SECRET_CLIENT"; then
    SAVED_SECRET="$(gcloud secrets versions access latest --secret="$SECRET_CLIENT" 2>/dev/null || true)"
    if check_client "$CLIENT_ID" "$SAVED_SECRET"; then
      echo "Keeping the Client secret you saved before (ends in ${SAVED_SECRET: -4})."
      unset SAVED_SECRET
      break
    fi
    unset SAVED_SECRET
    echo "The saved secret doesn't work anymore. Paste the current one from your client's page."
  else
    echo "Nothing was pasted. Try again."
  fi
  if ((attempt == 5)); then false; fi
done
unset CLIENT_SECRET

# ---------------------------------------------------------------------------
bold "Step 4 of 6: Who is allowed to use this server"
echo "Only these Google accounts can use it. Use the email you log into Google Ads with."
echo "For more than one, separate them with commas."
DEFAULT_EMAILS="${SAVED_ALLOWED_EMAILS:-$(existing_env ALLOWED_EMAILS)}"
DEFAULT_EMAILS="${DEFAULT_EMAILS:-$(gcloud config get-value account 2>/dev/null || true)}"
ALLOWED_EMAILS="$(ask "Allowed email(s)" "$DEFAULT_EMAILS")"
ALLOWED_EMAILS="${ALLOWED_EMAILS//[[:space:]]/}"
if [[ $ALLOWED_EMAILS != *@* ]]; then
  echo "Please enter at least one email address." >&2
  false
fi
save_setting SAVED_ALLOWED_EMAILS "$ALLOWED_EMAILS"

echo
echo "Do you reach your ad accounts through a manager account (MCC)?"
echo "If yes, type its 10-digit customer ID. If no, just press Enter."
if [[ -n ${SAVED_LOGIN_CUSTOMER_ID+x} ]]; then
  DEFAULT_MCC="$SAVED_LOGIN_CUSTOMER_ID"
else
  DEFAULT_MCC="$(existing_env GOOGLE_ADS_LOGIN_CUSTOMER_ID)"
fi
if [[ -n $DEFAULT_MCC ]]; then
  echo "To stop using a manager account, type none."
fi
LOGIN_CUSTOMER_ID="$(ask "Manager account ID" "$DEFAULT_MCC")"
LOGIN_CUSTOMER_ID="${LOGIN_CUSTOMER_ID//[^0-9]/}"
if [[ -n $LOGIN_CUSTOMER_ID && ${#LOGIN_CUSTOMER_ID} -ne 10 ]]; then
  echo "A manager account ID has 10 digits, like 123-456-7890." >&2
  false
fi
save_setting SAVED_LOGIN_CUSTOMER_ID "$LOGIN_CUSTOMER_ID"

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

Only if you want to publish the Google sign-in app (no weekly reconnect):
open https://console.cloud.google.com/auth/branding?project=${PROJECT_ID}
  - Application home page:        ${BASE_URL}/
  - Application privacy policy:   ${BASE_URL}/privacy
  - Authorized domains, Add domain: ${BASE_URL#https://}
Click Save, then go to Audience and click Publish app.
EOF
