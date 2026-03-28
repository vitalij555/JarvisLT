"""One-time Google OAuth flow for mcp-gsuite. Run once to store credentials."""
import http.server
import json
import os
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs

from oauth2client.client import flow_from_clientsecrets, OAuth2Credentials
from dotenv import load_dotenv

load_dotenv()

GAUTH_FILE = ".gauth.json"
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
]
REDIRECT_URI = "http://localhost:4100/code"
EMAIL = "vitalij555@gmail.com"

# Generate .gauth.json from env vars
client_id = os.environ.get("GOOGLE_OAUTH_ID")
client_secret = os.environ.get("GOOGLE_OAUTH_SECRET")
if client_id and client_secret:
    with open(GAUTH_FILE, "w") as f:
        json.dump({"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [REDIRECT_URI],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }}, f)

flow = flow_from_clientsecrets(GAUTH_FILE, " ".join(SCOPES), redirect_uri=REDIRECT_URI)
flow.params["access_type"] = "offline"
flow.params["approval_prompt"] = "force"
auth_url = flow.step1_get_authorize_url()

code_holder: dict = {}
done = threading.Event()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h1>No code received.</h1>")
        done.set()

    def log_message(self, *args):
        pass

server = http.server.HTTPServer(("localhost", 4100), Handler)
thread = threading.Thread(target=server.handle_request)
thread.start()

print(f"Opening browser for Google authorization...")
print(f"If browser doesn't open, visit:\n{auth_url}\n")
webbrowser.open(auth_url)

done.wait(timeout=120)
server.server_close()

if "code" not in code_holder:
    print("ERROR: No authorization code received within 2 minutes.")
    exit(1)

credentials: OAuth2Credentials = flow.step2_exchange(code_holder["code"])
cred_path = f".oauth2.{EMAIL}.json"
with open(cred_path, "w") as f:
    f.write(credentials.to_json())

print(f"Credentials saved to {cred_path}")
print("You can now run: pipenv run python main.py")
