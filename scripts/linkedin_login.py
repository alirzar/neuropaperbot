"""Log in to LinkedIn and save the token the bot posts with. LinkedIn's tokens last 60 days.

Once, first (at https://www.linkedin.com/developers/apps):
  1. Create an app. LinkedIn asks you to attach it to a LinkedIn Page you manage.
  2. On the app's Products tab, add "Share on LinkedIn" and "Sign In with LinkedIn using OpenID Connect".
  3. On its Auth tab, add this redirect URL: http://localhost:8765/callback
  4. Copy its Client ID and Client Secret into .env as LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET.

Then now, and again whenever the token runs out:
  python scripts/linkedin_login.py

It opens LinkedIn in your browser. Once you allow access, it saves LINKEDIN_ACCESS_TOKEN in .env
and offers to update the GitHub secret of the same name. To do only that last part:
  python scripts/linkedin_login.py --upload     copy the token in .env to GitHub, no new login
"""
import argparse
import html
import http.server
import re
import secrets
import shutil
import subprocess
import time
import urllib.parse
import webbrowser
from datetime import timedelta

import requests

import linkedin_api
from common import ROOT, now_utc, require_env

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
SCOPES = "openid profile w_member_social"  # who you are, and permission to post as you


def wait_for_code(state, timeout=300):
    """Serve one page on localhost until LinkedIn sends the browser back with ?code=…"""
    result = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(url.query)
            if query.get("state", [""])[0] != state:
                result["error"] = "the reply doesn't belong to this login attempt"
            elif "error" in query:
                result["error"] = query.get("error_description", query["error"])[0]
            else:
                result["code"] = query.get("code", [""])[0]
            message = "Logged in. You can close this tab." if result.get("code") else f"Login failed: {result['error']}"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<p style='font-family:sans-serif'>{html.escape(message)}</p>".encode())

        def log_message(self, *args):  # keep the terminal quiet
            pass

    deadline = time.monotonic() + timeout
    with http.server.HTTPServer(("localhost", PORT), Callback) as server:
        server.timeout = 2
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get("code"):
        raise SystemExit(f"LinkedIn login didn't finish: {result.get('error', 'timed out')}")
    return result["code"]


def exchange(code, client_id, client_secret):
    """Trade the one-time code for an access token. Returns (token, seconds until it expires)."""
    resp = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=60,
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if "access_token" not in body:
        raise SystemExit(f"LinkedIn didn't give a token: {body.get('error_description') or resp.status_code}")
    return body["access_token"], int(body.get("expires_in", 0))


def save_to_env(token):
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"LINKEDIN_ACCESS_TOKEN={token}"
    text, found = re.subn(r"(?m)^LINKEDIN_ACCESS_TOKEN=.*$", lambda _: line, text)
    if not found:
        text = text.rstrip("\n") + f"\n{line}\n"
    env.write_text(text, encoding="utf-8", newline="")


def github_repo():
    """owner/name of this folder's GitHub remote, if it has one."""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else None


def offer_github_update(token, ask=True):
    repo = github_repo()
    if not (repo and shutil.which("gh")):
        print("Now update the LINKEDIN_ACCESS_TOKEN secret on GitHub (Settings, Secrets and variables, Actions).")
        return
    if ask:
        try:
            answer = input(f"Update the LINKEDIN_ACCESS_TOKEN secret on GitHub ({repo}) too? [Y/n] ")
        except EOFError:  # not running in an interactive terminal
            answer = "n"
        if answer.strip().lower() not in ("", "y", "yes"):
            print("GitHub not updated. To do it later: python scripts/linkedin_login.py --upload")
            return
    done = subprocess.run(["gh", "secret", "set", "LINKEDIN_ACCESS_TOKEN", "--repo", repo], input=token, text=True)
    if done.returncode == 0:
        print(f"Updated the LINKEDIN_ACCESS_TOKEN secret on GitHub ({repo}).")
    else:
        print(f"That didn't work. Try: gh secret set LINKEDIN_ACCESS_TOKEN --repo {repo}")


def main():
    parser = argparse.ArgumentParser(description="Log in to LinkedIn and save the token the bot posts with.")
    parser.add_argument("--upload", action="store_true", help="copy the token in .env to the GitHub secret, without logging in again")
    if parser.parse_args().upload:
        (token,) = require_env("LINKEDIN_ACCESS_TOKEN")
        offer_github_update(token, ask=False)
        return

    client_id, client_secret = require_env("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state, "scope": SCOPES},
        quote_via=urllib.parse.quote,  # spaces as %20, as in LinkedIn's examples, not +
    )
    print(f"Opening LinkedIn in your browser. If nothing opens, go to:\n{url}\n")
    webbrowser.open(url)
    token, expires_in = exchange(wait_for_code(state), client_id, client_secret)
    who = linkedin_api.whoami(token).get("name", "you")
    save_to_env(token)
    expires = (now_utc() + timedelta(seconds=expires_in)).date()
    print(f"Logged in as {who}. Saved LINKEDIN_ACCESS_TOKEN in .env; LinkedIn says it lasts until {expires}.")
    offer_github_update(token)


if __name__ == "__main__":
    main()
