from google_auth_oauthlib.flow import InstalledAppFlow

# Requesting the new Google Health API nutrition scopes.
# (Legacy Fit scopes are intentionally NOT included here — mixing legacy
# fitness.* scopes with googlehealth.* scopes in one token can cause the
# new API's auth layer to reject the request. Fit is being retired anyway.)
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth.nutrition.readonly",
    "https://www.googleapis.com/auth/googlehealth.nutrition.writeonly",
]

CLIENT_SECRET_FILE = "client_secret.json"

def main():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n--- SAVE THESE (Google Health API) ---")
    print("Refresh token:", creds.refresh_token)
    print("Client ID:", creds.client_id)
    print("Client secret:", creds.client_secret)
    print("---------------------------------------\n")

if __name__ == "__main__":
    main()
