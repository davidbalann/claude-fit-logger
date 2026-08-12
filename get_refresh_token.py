from google_auth_oauthlib.flow import InstalledAppFlow

# Scopes must match exactly what you added in the Data Access tab
SCOPES = [
    "https://www.googleapis.com/auth/fitness.nutrition.write",
    "https://www.googleapis.com/auth/fitness.nutrition.read",
]

CLIENT_SECRET_FILE = "client_secret.json"

def main():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    # Opens your default browser, spins up a local server to catch the redirect
    creds = flow.run_local_server(port=0)

    print("\n--- SAVE THESE ---")
    print("Refresh token:", creds.refresh_token)
    print("Client ID:", creds.client_id)
    print("Client secret:", creds.client_secret)
    print("------------------\n")

if __name__ == "__main__":
    main()