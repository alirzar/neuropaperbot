"""A minimal client for LinkedIn's Posts API: just enough to post a link card from your own profile.

Docs: https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api
"""
import re

import requests

API = "https://api.linkedin.com"
# The API is versioned by month (YYYYMM), and LinkedIn supports each version for at least a year.
# If posting starts failing with a version error, move this to a recent month.
VERSION = "202609"
TIMEOUT = 60
RESERVED = set("|{}@[]()<>#\\*_~")  # "little text" characters that need a backslash to show as text

session = requests.Session()


class LinkedInError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status} {message}" if status else message)
        self.status = status


def call(method, url, token, versioned=True, **kwargs):
    headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
    if versioned:
        headers.update({"LinkedIn-Version": VERSION, "X-Restli-Protocol-Version": "2.0.0"})
    try:
        resp = session.request(method, url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise LinkedInError(0, f"network error ({type(exc).__name__})") from None
    if resp.status_code == 401:
        raise LinkedInError(401, "login expired or revoked; run scripts/linkedin_login.py")
    if resp.status_code >= 400:
        try:
            message = resp.json().get("message") or resp.text[:200]
        except ValueError:
            message = resp.text[:200]
        raise LinkedInError(resp.status_code, message)
    return resp


def whoami(token):
    """The logged-in member's profile basics (needs the openid and profile scopes)."""
    return call("GET", f"{API}/v2/userinfo", token, versioned=False).json()


def person_urn(token):
    return f"urn:li:person:{whoami(token)['sub']}"


def upload_image(token, owner, png):
    """Upload a PNG and return its urn:li:image:… for use as a thumbnail."""
    body = {"initializeUploadRequest": {"owner": owner}}
    upload = call("POST", f"{API}/rest/images?action=initializeUpload", token, json=body).json()["value"]
    call("PUT", upload["uploadUrl"], token, versioned=False, data=png, headers={"Content-Type": "application/octet-stream"})
    return upload["image"]


def commentary(text):
    """Text in LinkedIn's "little text" format: reserved characters escaped, #hashtags kept clickable.

    Unescaped, a character like "(" can make LinkedIn cut the post off there.
    """
    escaped = "".join("\\" + c if c in RESERVED else c for c in text)
    return re.sub(r"\\#([^\W\d_]\w*)", lambda m: "{hashtag|\\#|" + m.group(1) + "}", escaped)


def create_post(token, author, text, link, title, description, thumbnail=None):
    """Post a link card (LinkedIn calls it an article) and return the new post's URN."""
    # LinkedIn doesn't fetch link previews for API posts, so the card's details are all given here.
    article = {"source": link, "title": title[:399], "description": description}
    if thumbnail:
        article["thumbnail"] = thumbnail
    body = {
        "author": author,
        "commentary": commentary(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"article": article},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    return call("POST", f"{API}/rest/posts", token, json=body).headers.get("x-restli-id")


def post_url(urn):
    return f"https://www.linkedin.com/feed/update/{urn}/"
