#!/usr/bin/env python3
"""
inat_voucher_sync.py

Self-contained desktop GUI for syncing QR-decoded voucher IDs to iNaturalist
observation fields.  All backend logic is embedded — no other project files
are required.

Requirements:
  pip install requests opencv-python numpy

OCR fallback reads the printed voucher ID when the QR code can't be decoded.
It is ON by default and uses the bundled, pip-only RapidOCR engine (no system
install):
  pip install rapidocr-onnxruntime

Optionally, the OCR engine can be switched to Tesseract in the GUI (for users
who already have it). That path additionally needs the Tesseract engine itself:
  pip install pytesseract
  # Windows: https://github.com/UB-Mannheim/tesseract/wiki  (grab the installer)
  # macOS:   brew install tesseract
  # Debian:  sudo apt-get install tesseract-ocr

Run:
  python inat_voucher_sync.py
"""

import base64
import csv
import hashlib
import json
import os
import queue
import re
import secrets
import sys
import time
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import datetime
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Dependency check — friendly error before the window opens
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Missing dependency",
        "The 'requests' package is not installed.\n\n"
        "Run:  pip install requests opencv-python numpy",
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Backend constants
# ---------------------------------------------------------------------------
API        = "https://api.inaturalist.org/v1"
WEB        = "https://www.inaturalist.org"
USER_AGENT = ("inat-voucher-sync/1.0 "
              "(+https://github.com/bthorson1029/inat-voucher-sync)")

# ---------------------------------------------------------------------------
# OAuth sign-in  ("Sign in with iNaturalist")
#
# Lets the user authorize in their browser and have the app mint an API token
# for them, instead of copying one from the token page by hand.  The flow is
# OAuth2 Authorization Code with PKCE over a localhost loopback redirect
# (RFC 8252): the app opens the browser, catches the redirect on a short-lived
# local server, exchanges the code for an OAuth access token, then calls
# /users/api_token to get the same 24-hour JWT the rest of the app already uses.
#
# ONE-TIME DEVELOPER SETUP (not per user): register an application at
#   https://www.inaturalist.org/oauth/applications
# and paste its Client ID below.  Register the redirect URI EXACTLY as
#   http://127.0.0.1:8579/callback
# (matching OAUTH_REDIRECT_PORT below).  If iNaturalist registers the app as a
# confidential client that requires a secret at the token step, also fill
# OAUTH_CLIENT_SECRET; if it accepts a public client with PKCE, leave it blank.
# Until OAUTH_CLIENT_ID is filled in, the Sign-in button explains this and the
# manual token paste / INAT_API_TOKEN paths keep working unchanged.
# Registered application #1027 (public / non-confidential client). Because it's
# non-confidential, PKCE alone secures the flow and NO secret is sent or shipped
# — OAUTH_CLIENT_SECRET stays blank on purpose (never commit a secret to a
# public repo). The Client ID is not sensitive.
OAUTH_CLIENT_ID     = "UAXeDpCOFXTyUrdP3Eyr-bkioGE0iY0bes1niyaEzaI"
OAUTH_CLIENT_SECRET = ""   # intentionally blank: public client uses PKCE, no secret
OAUTH_REDIRECT_PORT = 8579
OAUTH_SCOPE         = "login write"   # write is needed to set observation fields

OAUTH_AUTHORIZE_URL = f"{WEB}/oauth/authorize"
OAUTH_TOKEN_URL     = f"{WEB}/oauth/token"
API_TOKEN_URL       = f"{WEB}/users/api_token"

# ---------------------------------------------------------------------------
# USER CONFIGURATION
# These are the starting values shown in the GUI. Every one of them can be
# changed in the window at runtime — edit them here only to set your own
# defaults so you don't have to retype them each session.
# ---------------------------------------------------------------------------
# Your iNaturalist login. Leave blank to be prompted for it in the GUI.
DEFAULT_USER       = ""
# The observation field to write vouchers into. "Personal voucher number"
# (ID 1907) is a public iNaturalist field; change the ID to target a different
# field (find its numeric ID on the field's page on inaturalist.org).
DEFAULT_FIELD_NAME = "Personal voucher number"
DEFAULT_FIELD_ID   = 1907
# Regex matching your label/voucher format. Matching is case-insensitive.
# The default accepts a 2–4 character alphanumeric prefix that must contain at
# least one letter, a hyphen, and a 3–5 digit numeric suffix, e.g. "BT-001",
# "ABC-1234", "AB12-34567". The leading (?=...) lookahead enforces the "at
# least one letter" rule, so a purely numeric prefix like "12-3456" (likely
# OCR noise or a date) is not mistaken for a voucher. The required hyphen,
# bounded lengths, and word boundaries further constrain matches; widen or
# narrow it to match your own scheme.
DEFAULT_VOUCHER_RE = r"\b(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,4}-\d{3,5}\b"
REQUEST_PAUSE      = 0.8
PER_PAGE           = 200
# Photos are fetched from iNaturalist's CDN/S3, not the rate-limited write API,
# so the preview scan can download and decode several observations at once.
# Each photo still runs through the identical decode path — only the wall-clock
# overlap changes, not the detection result.  Keep this modest to stay polite
# to the photo host and bounded in memory (this many originals in flight).
SCAN_WORKERS       = 6

# Voucher-format presets offered as radio options in the GUI.  Each maps a
# friendly name to a regex (matching is always case-insensitive); "Custom"
# is a sentinel of None that unlocks the regex box for a hand-written pattern.
# The patterns are word-bounded and require enough structure that stray OCR
# text from a photo with no label is unlikely to match.
VOUCHER_FORMATS = [
    ("Prefix-Number", DEFAULT_VOUCHER_RE),              # BT-001, AB12-34567
    ("Numbers only",  r"\b\d{3,6}\b"),                  # 00421, 123456
    # Alphanumeric: 4–10 chars containing at least one letter and one digit,
    # so it won't collapse into "any word" or "any number".
    ("Alphanumeric",
     r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10}\b"),
    ("Custom", None),
]
DEFAULT_VOUCHER_FORMAT = VOUCHER_FORMATS[0][0]

# Example codes shown as live hint text under the pattern field, one entry per
# format name above.  They illustrate what each preset matches so the user can
# tell at a glance whether it fits their labels; "Custom" points at the box.
VOUCHER_FORMAT_EXAMPLES = {
    "Prefix-Number": "BT-001, AB12-34567",
    "Numbers only":  "00421, 123456",
    "Alphanumeric":  "AB12, 4F9X, X7Y9Z2",
}

UPDATE = "update"
SKIP   = "skip"
FLAG   = "flag"

# OCR engines. "rapidocr" is the bundled default (pip-installable, no system
# binary); "tesseract" is the optional engine for users who already have it.
OCR_RAPIDOCR = "rapidocr"
OCR_TESSERACT = "tesseract"
DEFAULT_OCR_ENGINE = OCR_RAPIDOCR


# ---------------------------------------------------------------------------
# iNaturalist API client
# ---------------------------------------------------------------------------
class INatClient:
    def __init__(self, token=None):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.token = token

    def _auth(self):
        return {"Authorization": self.token} if self.token else {}

    def verify_token(self):
        r = self.session.get(f"{API}/users/me", headers=self._auth(), timeout=30)
        if r.status_code == 401:
            return None
        r.raise_for_status()
        results = r.json().get("results", [])
        return results[0].get("login") if results else None

    def fetch_observations(self, user_login, created_d1=None, created_d2=None):
        page = 1
        fetched = 0
        total = None
        while True:
            params = {
                "user_login": user_login,
                "per_page": PER_PAGE,
                "page": page,
                "order_by": "created_at",
                "order": "asc",
            }
            if created_d1:
                params["created_d1"] = created_d1
            if created_d2:
                params["created_d2"] = created_d2
            r = self.session.get(f"{API}/observations", params=params, timeout=30)
            r.raise_for_status()
            payload = r.json()
            if total is None:
                total = payload.get("total_results", 0)
            results = payload.get("results", [])
            if not results:
                break
            for obs in results:
                yield obs
                fetched += 1
            if fetched >= total or page * PER_PAGE >= total:
                break
            page += 1
            time.sleep(REQUEST_PAUSE)

    def create_ofv(self, observation_id, field_id, value):
        body = {"observation_field_value": {
            "observation_id": observation_id,
            "observation_field_id": field_id,
            "value": value,
        }}
        r = self.session.post(
            f"{WEB}/observation_field_values.json",
            json=body, headers=self._auth(), timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def update_ofv(self, ofv_id, observation_id, field_id, value):
        body = {"observation_field_value": {
            "observation_id": observation_id,
            "observation_field_id": field_id,
            "value": value,
        }}
        r = self.session.put(
            f"{WEB}/observation_field_values/{ofv_id}.json",
            json=body, headers=self._auth(), timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def download_image(self, url):
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.content

    def search_observation_fields(self, query):
        """Search iNaturalist observation fields by name.  Returns a list of
        {"id", "name", "datatype"} dicts.  No auth required."""
        r = self.session.get(
            f"{WEB}/observation_fields.json",
            params={"q": query}, timeout=30,
        )
        r.raise_for_status()
        fields = []
        for f in r.json() or []:
            fid = f.get("id")
            name = f.get("name")
            if fid and name:
                fields.append({"id": fid, "name": name,
                               "datatype": f.get("datatype", "")})
        return fields


# ---------------------------------------------------------------------------
# OAuth sign-in  (Authorization Code + PKCE over a localhost loopback redirect)
# ---------------------------------------------------------------------------
class OAuthError(Exception):
    """Raised when the browser sign-in flow can't produce an API token."""


def oauth_configured():
    """True once the developer has pasted a Client ID (see OAUTH_CLIENT_ID)."""
    return bool(OAUTH_CLIENT_ID)


def _pkce_pair():
    """Return (code_verifier, code_challenge) for PKCE S256 per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the single OAuth redirect, then shows a close-me page."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        # Only the real redirect carries code/error; ignore favicon etc.
        if "code" in params or "error" in params:
            self.server.oauth_response = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;padding:48px;"
            b"text-align:center'><h2>iNaturalist sign-in complete</h2>"
            b"<p>You can close this tab and return to Voucher Sync.</p>"
            b"</body></html>")

    def log_message(self, *_args):
        pass  # keep the local server silent


def oauth_login(open_browser=True, timeout=300, log=None):
    """Run the browser sign-in flow and return (api_token_jwt, access_token).

    Steps: open the iNaturalist authorize page in the user's browser, catch the
    redirect on a localhost server, exchange the code (with the PKCE verifier)
    for an OAuth access token, then call /users/api_token to mint the JWT the
    app uses.  Raises OAuthError with a user-facing message on any failure.

    `log` is an optional callable for progress lines.
    """
    if not oauth_configured():
        raise OAuthError(
            "Browser sign-in isn't set up in this build.\n\n"
            "The developer needs to register an iNaturalist application and "
            "paste its Client ID into the app (see OAUTH_CLIENT_ID).\n\n"
            "For now, use the token page and paste your token instead.")

    def _say(msg):
        if log:
            log(msg)

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"http://127.0.0.1:{OAUTH_REDIRECT_PORT}/callback"

    try:
        server = HTTPServer(("127.0.0.1", OAUTH_REDIRECT_PORT), _CallbackHandler)
    except OSError as exc:
        raise OAuthError(
            f"Couldn't start the local sign-in listener on port "
            f"{OAUTH_REDIRECT_PORT}.\n\nIt may be in use by another program. "
            f"Close it and try again.\n\n({exc})")
    server.timeout = 1
    server.oauth_response = None

    authorize_url = f"{OAUTH_AUTHORIZE_URL}?" + urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": OAUTH_SCOPE,
        "state": state,
    })

    try:
        _say("Opening your browser to sign in to iNaturalist…")
        if open_browser:
            webbrowser.open(authorize_url)

        deadline = time.time() + timeout
        while server.oauth_response is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    params = server.oauth_response
    if params is None:
        raise OAuthError("Sign-in timed out before it completed. Try again.")
    if params.get("state", [None])[0] != state:
        raise OAuthError("Sign-in failed a security check (state mismatch). "
                         "Try again.")
    if "error" in params:
        raise OAuthError(
            "iNaturalist didn't grant access "
            f"({params['error'][0]}). You can try again, or paste a token "
            "manually instead.")
    code = params.get("code", [None])[0]
    if not code:
        raise OAuthError("Sign-in returned no authorization code. Try again.")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    _say("Exchanging the authorization code for an access token…")
    token_body = {
        "client_id": OAUTH_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if OAUTH_CLIENT_SECRET:
        token_body["client_secret"] = OAUTH_CLIENT_SECRET
    creds = _oauth_token_request(token_body, session)

    _say("Fetching your API token…")
    api_token = fetch_api_token(creds["access_token"], session)
    return api_token, creds


def _oauth_token_request(body, session):
    """POST to the OAuth token endpoint and return a creds dict carrying the
    access token (and refresh token, if iNaturalist issues one)."""
    try:
        r = session.post(OAUTH_TOKEN_URL, data=body, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except requests.RequestException as exc:
        raise OAuthError(f"Couldn't exchange credentials for a token: {exc}")
    access_token = payload.get("access_token")
    if not access_token:
        raise OAuthError("iNaturalist returned no access token.")
    creds = {"access_token": access_token}
    if payload.get("refresh_token"):
        creds["refresh_token"] = payload["refresh_token"]
    return creds


def fetch_api_token(access_token, session=None):
    """Exchange an OAuth access token for a fresh 24-hour API JWT.

    This is what makes silent daily refresh possible: the access token is
    long-lived, so on each launch the app can mint a new JWT without the user
    signing in again.  Raises OAuthError on failure.
    """
    own = session is None
    if own:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
    try:
        r = session.get(
            API_TOKEN_URL,
            headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
        r.raise_for_status()
        api_token = r.json().get("api_token")
    except requests.RequestException as exc:
        raise OAuthError(f"Couldn't fetch your API token: {exc}")
    finally:
        if own:
            session.close()
    if not api_token:
        raise OAuthError("iNaturalist returned no API token.")
    return api_token


def refresh_access_token(refresh_token):
    """Trade a refresh token for a new access token (creds dict).

    Only used if iNaturalist's access tokens turn out to expire and it issued a
    refresh token; the common case is non-expiring access tokens, where this is
    never reached.
    """
    body = {
        "client_id": OAUTH_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if OAUTH_CLIENT_SECRET:
        body["client_secret"] = OAUTH_CLIENT_SECRET
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    try:
        return _oauth_token_request(body, session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Saved-credential storage  (enables silent token refresh between launches)
#
# We persist the long-lived OAuth credentials — NOT the 24-hour JWT — so the
# app can mint a fresh JWT on startup without prompting again.  This is an
# account credential: it lives in a per-user file with owner-only permissions
# where the OS supports it.  "Sign out" deletes it.  Users who'd rather not
# store anything can ignore browser sign-in and paste a token each session.
# ---------------------------------------------------------------------------
def _credentials_path():
    return os.path.join(
        os.path.expanduser("~"), ".inat_voucher_sync", "credentials.json")


def save_credentials(creds):
    """Persist the OAuth creds dict (access/refresh token) for next launch."""
    path = _credentials_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(creds, fh)
        try:
            os.chmod(path, 0o600)   # best-effort; no-op on some platforms
        except OSError:
            pass
    except OSError:
        pass  # persistence is a convenience; never block sign-in on it


def load_credentials():
    """Return the saved creds dict, or None if there isn't a usable one."""
    try:
        with open(_credentials_path(), encoding="utf-8") as fh:
            creds = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(creds, dict) and creds.get("access_token"):
        return creds
    return None


def clear_credentials():
    """Forget any saved sign-in (used by Sign out and on refresh failure)."""
    try:
        os.remove(_credentials_path())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Photo selection
# ---------------------------------------------------------------------------
def last_photo_url(obs, size="original"):
    ophotos = obs.get("observation_photos") or []
    if not ophotos:
        return None
    ophotos = sorted(ophotos, key=lambda p: p.get("position", 0))
    photo = ophotos[-1].get("photo") or {}
    url = photo.get("url")
    return url.replace("square", size) if url else None


# ---------------------------------------------------------------------------
# QR decoding
# ---------------------------------------------------------------------------
def _image_variants(img):
    import cv2
    yield img
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    yield gray
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield otsu
    yield cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)


def load_image(image_bytes):
    """Decode raw image bytes to a BGR ndarray.  Returns (img, error)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None, "cv2_not_installed"
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, "image_decode_failed"
    return img, None


def _get_candidates(img, cache):
    """
    Return the ranked label-region crops for `img`, computing them at most
    once per image.  `cache` is a per-observation dict shared between the QR
    and OCR passes so the expensive detection isn't repeated.
    """
    if "candidates" not in cache:
        cache["candidates"] = _label_candidates(img)
    return cache["candidates"]


def decode_qr(img, cache):
    import cv2

    detector = cv2.QRCodeDetector()
    for variant in _image_variants(img):
        try:
            ok, decoded, _, _ = detector.detectAndDecodeMulti(variant)
        except cv2.error:
            ok, decoded = False, []
        if ok:
            for text in decoded:
                if text:
                    return text, None
        try:
            text, _, _ = detector.detectAndDecode(variant)
        except cv2.error:
            text = ""
        if text:
            return text, None

    # Second QR attempt: run the detector on the deskewed, upscaled label
    # crops.  OpenCV often locates a QR in the full frame but fails to decode
    # it at that scale; the perspective-corrected crop is far more decodable.
    try:
        for crop in _get_candidates(img, cache):
            for variant in (crop,
                            cv2.threshold(crop, 0, 255,
                                          cv2.THRESH_BINARY
                                          + cv2.THRESH_OTSU)[1]):
                try:
                    ok, decoded, _, _ = detector.detectAndDecodeMulti(variant)
                except cv2.error:
                    ok, decoded = False, []
                if ok:
                    for text in decoded:
                        if text:
                            return text, None
                try:
                    text, _, _ = detector.detectAndDecode(variant)
                except cv2.error:
                    text = ""
                if text:
                    return text, None
    except Exception:
        pass

    try:
        from pyzbar.pyzbar import decode as zbar_decode
        for variant in _image_variants(img):
            for res in zbar_decode(variant):
                if res.data:
                    return res.data.decode("utf-8", "replace"), None
        # pyzbar on the label crops too.
        try:
            for crop in _get_candidates(img, cache):
                for res in zbar_decode(crop):
                    if res.data:
                        return res.data.decode("utf-8", "replace"), None
        except Exception:
            pass
    except ImportError:
        pass

    return None, "no_qr_detected"


def extract_voucher(text, voucher_re):
    if text is None:
        return None
    m = voucher_re.search(text)
    return m.group(0).upper() if m else None


# ---------------------------------------------------------------------------
# OCR fallback  (requires pytesseract + Tesseract engine)
# ---------------------------------------------------------------------------
# Common Windows install path; used when tesseract_cmd is not explicitly set.
_WIN_TESS_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _order_points(pts):
    """
    Order four box corners as: top-left, top-right, bottom-right, bottom-left.
    Required for a stable perspective transform regardless of rotation.
    """
    import numpy as np
    rect = np.zeros((4, 2), dtype=np.float32)
    s         = pts.sum(axis=1)
    diff      = np.diff(pts, axis=1)
    rect[0]   = pts[np.argmin(s)]     # top-left     (smallest x+y)
    rect[2]   = pts[np.argmax(s)]     # bottom-right (largest  x+y)
    rect[1]   = pts[np.argmin(diff)]  # top-right    (smallest x-y)
    rect[3]   = pts[np.argmax(diff)]  # bottom-left  (largest  x-y)
    return rect


def _label_candidates(img, max_candidates=4):
    """
    Find candidate voucher-label regions in a field photo, ranked by how
    label-like each one is, and return upscaled deskewed grayscale crops.

    Why ranking instead of "largest bright region": a pale mushroom cap, a
    sun-bleached leaf, or a patch of sky can all be brighter and bigger than
    the label.  Picking purely by area grabs the wrong object.  Instead each
    candidate is scored on:
      - rectangularity (contour area / its bounding-rect area) — a printed
        label is a crisp rectangle (~0.9); organic shapes score much lower.
      - aspect ratio closeness to the real label format (~2.5:1).
    Candidates are gathered across several brightness thresholds so the method
    adapts to ambient light, then the top N distinct regions are returned for
    OCR to try in order.

    Returns a list of grayscale crop arrays (possibly empty).
    """
    import cv2
    import numpy as np

    h, w     = img.shape[:2]
    img_area = h * w
    gray     = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    ksize  = max(5, min(w, h) // 80)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize))

    # Gather scored candidates across a range of percentile thresholds so the
    # method works in both bright and shaded photos.
    scored = []
    for p in (94, 92, 90, 88, 86, 83, 80):
        thresh_val = int(np.percentile(gray, p))
        _, bright = cv2.threshold(gray, thresh_val, 255, cv2.THRESH_BINARY)
        closed = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(
            closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if not (img_area * 0.002 <= area <= img_area * 0.25):
                continue
            rect   = cv2.minAreaRect(cnt)
            rw, rh = rect[1]
            if not (rw and rh):
                continue
            aspect = max(rw, rh) / min(rw, rh)
            if not (1.4 <= aspect <= 4.5):
                continue
            rect_area      = rw * rh
            rectangularity = area / rect_area if rect_area else 0
            aspect_score   = 1.0 - min(abs(aspect - 2.5) / 2.5, 1.0)
            score = rectangularity * 0.7 + aspect_score * 0.3
            scored.append((score, rect))

    # Sort best-first, then drop near-duplicate regions (same label found at
    # several thresholds) by comparing centers.
    scored.sort(key=lambda s: -s[0])
    chosen, seen_centers = [], []
    for score, rect in scored:
        cx, cy = rect[0]
        if any(abs(cx - sx) < 60 and abs(cy - sy) < 60
               for sx, sy in seen_centers):
            continue
        seen_centers.append((cx, cy))
        chosen.append(rect)
        if len(chosen) >= max_candidates:
            break

    # Perspective-correct and upscale each chosen region.
    crops = []
    for rect in chosen:
        box = cv2.boxPoints(rect).astype(np.float32)
        src = _order_points(box)
        rw  = int(max(rect[1]))
        rh  = int(min(rect[1]))
        if rw < 1 or rh < 1:
            continue
        dst = np.array([[0, 0], [rw - 1, 0],
                        [rw - 1, rh - 1], [0, rh - 1]], dtype=np.float32)
        M      = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(img, M, (rw, rh))
        scale  = max(3.0, 500 / rh)
        crop   = cv2.resize(warped, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_CUBIC)
        crops.append(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))

    return crops


_RAPIDOCR = None
_RAPIDOCR_LOCK = threading.Lock()


def _rapidocr_engine():
    """Lazily construct the bundled RapidOCR engine (loads ONNX models once,
    then reuses the instance for every subsequent observation).

    Guarded by a lock: the preview scan runs several workers concurrently, so
    without it the first QR failures could each try to build the engine at
    once."""
    global _RAPIDOCR
    if _RAPIDOCR is None:
        with _RAPIDOCR_LOCK:
            if _RAPIDOCR is None:
                from rapidocr_onnxruntime import RapidOCR
                _RAPIDOCR = RapidOCR()
    return _RAPIDOCR


def ocr_engine_available(engine, tesseract_cmd=None):
    """Check whether the selected OCR engine can actually run, *before* a scan
    starts, so a missing dependency is reported once with install guidance
    rather than stamped on every flagged row.

    Returns (ok, message); `message` is a user-facing hint when not ok.
    """
    import importlib.util

    if engine == OCR_TESSERACT:
        if importlib.util.find_spec("pytesseract") is None:
            return False, (
                "The Tesseract OCR engine isn't set up.\n\n"
                "Install the Python wrapper:\n"
                "    pip install pytesseract\n\n"
                "…and the Tesseract program itself (see the README).")
        try:
            import pytesseract
            cmd = tesseract_cmd or ""
            if not cmd and os.path.isfile(_WIN_TESS_DEFAULT):
                cmd = _WIN_TESS_DEFAULT
            if cmd:
                pytesseract.pytesseract.tesseract_cmd = cmd
            pytesseract.get_tesseract_version()
        except Exception:
            return False, (
                "The Tesseract program wasn't found.\n\n"
                "Install it (see the README), or point the app at "
                "tesseract.exe with Browse….")
        return True, ""

    # Bundled default engine.
    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return False, (
            "The built-in OCR engine isn't installed yet.\n\n"
            "Install it with:\n"
            "    pip install rapidocr-onnxruntime\n\n"
            "Or switch the OCR engine to Tesseract, or turn the OCR fallback "
            "off to scan QR codes only.")
    return True, ""


def ocr_fallback(img, cache, voucher_re, engine=DEFAULT_OCR_ENGINE,
                 tesseract_cmd=None):
    """Read the voucher ID from `img` when QR decoding has failed.

    Dispatches to the selected OCR engine.  `cache` is the per-observation
    candidate cache shared with decode_qr, so label detection is not repeated.
    Returns (voucher_id, raw_ocr_text, error_string).
    """
    if engine == OCR_TESSERACT:
        return _ocr_tesseract(img, cache, voucher_re, tesseract_cmd)
    return _ocr_rapidocr(img, cache, voucher_re)


def _ocr_rapidocr(img, cache, voucher_re):
    """Bundled OCR path (RapidOCR / ONNX runtime) — no system binary required.

    Runs the recognizer over the ranked, deskewed label crops first (most
    reliable), then the full frame as a last resort, matching the voucher
    pattern against each recognized text segment and their concatenation.
    Returns (voucher_id, raw_ocr_text, error_string).
    """
    try:
        engine = _rapidocr_engine()
    except ImportError:
        return None, None, "rapidocr_not_installed"
    except Exception as exc:
        return None, None, f"rapidocr_init_failed: {exc}"

    import cv2

    def _scan(image):
        """Run one RapidOCR pass; return (voucher_or_None, last_text)."""
        try:
            result, _ = engine(image)
        except Exception:
            return None, None
        if not result:
            return None, None
        texts = [seg[1] for seg in result if len(seg) >= 2 and seg[1]]
        for text in texts:
            voucher = extract_voucher(text, voucher_re)
            if voucher:
                return voucher, text
        # Try the joined line too, in case the ID was split across boxes.
        joined = " ".join(texts)
        voucher = extract_voucher(joined, voucher_re)
        if voucher:
            return voucher, joined
        return None, (texts[-1] if texts else None)

    last_raw = None
    # Pass 1 — ranked, deskewed label crops (grayscale → BGR for the engine).
    for crop in _get_candidates(img, cache):
        voucher, raw = _scan(cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR))
        last_raw = raw or last_raw
        if voucher:
            return voucher, last_raw, None

    # Pass 2 — the full frame, in case label detection missed the label.
    voucher, raw = _scan(img)
    last_raw = raw or last_raw
    if voucher:
        return voucher, last_raw, None

    return None, last_raw, "ocr_no_match"


def _ocr_tesseract(img, cache, voucher_re, tesseract_cmd=None):
    """
    Try to read the voucher ID from the image using Tesseract OCR.
    Called only when QR decoding has already failed.  `img` is the decoded
    BGR image and `cache` is the per-observation candidate cache shared with
    decode_qr, so label detection is not repeated here.

    Two-pass strategy:
      Pass 1 — detect and isolate the white label, perspective-correct the
               tilt, upscale the crop, and run OCR (PSM 6/7/3).
               This is far more reliable than running on the full photo.
      Pass 2 — full-image sparse-text fallback (PSM 11) without upscaling,
               as a last resort if label detection failed.

    Returns (voucher_id, raw_ocr_text, error_string).
    """
    try:
        import pytesseract
    except ImportError:
        return None, None, "pytesseract_not_installed"

    import cv2

    # Resolve Tesseract executable path.
    cmd = tesseract_cmd or ""
    if not cmd and os.path.isfile(_WIN_TESS_DEFAULT):
        cmd = _WIN_TESS_DEFAULT
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd

    # Whitelist limits OCR to characters that appear in a voucher (letters,
    # digits, hyphen), which sharply cuts misreads; the regex still decides
    # what counts as a valid voucher.  Dash last avoids range ambiguity.
    WL = ("-c tessedit_char_whitelist="
          "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
          "abcdefghijklmnopqrstuvwxyz0123456789-")
    last_raw = None

    # ------------------------------------------------------------------ #
    # Pass 1 — ranked, deskewed label-region candidates                  #
    # ------------------------------------------------------------------ #
    # Try each candidate region (best-scored first) until one yields a valid
    # voucher.  This is what lets the scanner pick the label over a bright
    # mushroom cap or leaf that might score on size alone.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    for crop in _get_candidates(img, cache):
        _, otsu_crop = cv2.threshold(
            crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        clahe_crop = clahe.apply(crop)

        for psm in (6, 7, 3):
            for variant in (crop, otsu_crop, clahe_crop):
                for wl in (WL, ""):
                    cfg = f"--psm {psm} --oem 3 {wl}".strip()
                    try:
                        raw = pytesseract.image_to_string(variant, config=cfg)
                    except pytesseract.TesseractNotFoundError:
                        return None, None, "tesseract_not_found"
                    except Exception:
                        continue
                    last_raw = raw.strip() or last_raw
                    voucher  = extract_voucher(raw, voucher_re)
                    if voucher:
                        return voucher, last_raw, None

    # ------------------------------------------------------------------ #
    # Pass 2 — full image, sparse mode, no upscaling                     #
    # ------------------------------------------------------------------ #
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    for psm in (11, 3):
        for variant in (gray, otsu):
            for wl in (WL, ""):
                cfg = f"--psm {psm} --oem 3 {wl}".strip()
                try:
                    raw = pytesseract.image_to_string(variant, config=cfg)
                except pytesseract.TesseractNotFoundError:
                    return None, None, "tesseract_not_found"
                except Exception:
                    continue
                last_raw = raw.strip() or last_raw
                voucher  = extract_voucher(raw, voucher_re)
                if voucher:
                    return voucher, last_raw, None

    return None, last_raw, "ocr_no_match"


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------
def existing_ofv(obs, field_id):
    for ofv in obs.get("ofvs") or []:
        if ofv.get("field_id") == field_id:
            return ofv.get("value"), ofv.get("id")
    return None, None


def taxon_label(obs):
    taxon = obs.get("taxon") or {}
    name = taxon.get("name") or "Unknown"
    common = taxon.get("preferred_common_name")
    return f"{name} ({common})" if common else name


def upload_date(obs):
    details = obs.get("created_at_details") or {}
    iso = details.get("date") or (obs.get("created_at") or "")[:10]
    # API returns YYYY-MM-DD; convert to DD/MM/YYYY for display.
    if iso and len(iso) == 10:
        y, m, d = iso.split("-")
        return f"{d}/{m}/{y}"
    return iso


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------
def build_row(client, obs, field_id, voucher_re, allow_overwrite,
              use_ocr=False, tesseract_cmd=None,
              ocr_engine=DEFAULT_OCR_ENGINE):
    obs_id = obs.get("id")
    row = {
        "observation_id":   obs_id,
        "url":              f"{WEB}/observations/{obs_id}",
        "taxon":            taxon_label(obs),
        "upload_date":      upload_date(obs),
        "detected_voucher": None,
        "current_value":    None,
        "field_state":      "empty",
        "action":           SKIP,
        "reason":           "",
        "ofv_id":           None,
        "raw_qr":           None,
        "raw_ocr":          None,   # populated when OCR fallback runs
    }

    current_value, ofv_id = existing_ofv(obs, field_id)
    row["current_value"] = current_value
    row["ofv_id"]        = ofv_id
    row["field_state"]   = "populated" if current_value else "empty"

    photo_url = last_photo_url(obs)
    if not photo_url:
        row["action"], row["reason"] = SKIP, "no_photos"
        return row

    try:
        image_bytes = client.download_image(photo_url)
    except requests.RequestException as exc:
        row["action"], row["reason"] = FLAG, f"photo_download_failed: {exc}"
        return row

    img, dec_err = load_image(image_bytes)
    if dec_err:
        row["action"], row["reason"] = FLAG, dec_err
        return row

    # Per-observation cache: label-region detection is computed at most once
    # and reused across the QR second pass and the OCR fallback.
    cache = {}
    text, qr_err = decode_qr(img, cache)

    if qr_err:
        # QR failed — try OCR if enabled, otherwise flag.
        if use_ocr:
            voucher, raw_ocr, ocr_err = ocr_fallback(
                img, cache, voucher_re, ocr_engine, tesseract_cmd)
            row["raw_ocr"] = raw_ocr
            if voucher:
                row["detected_voucher"] = voucher
                if not current_value:
                    row["action"], row["reason"] = UPDATE, "ocr_fallback"
                elif current_value.strip().upper() == voucher.upper():
                    row["action"], row["reason"] = SKIP, "already_correct"
                elif allow_overwrite:
                    row["action"], row["reason"] = UPDATE, "ocr_fallback_overwrite"
                else:
                    row["action"], row["reason"] = FLAG, "ocr_value_conflict"
            else:
                row["action"] = FLAG
                row["reason"] = ocr_err or qr_err
        else:
            row["action"], row["reason"] = FLAG, qr_err
        return row

    # QR succeeded.
    row["raw_qr"] = text
    voucher = extract_voucher(text, voucher_re)
    if not voucher:
        row["action"], row["reason"] = FLAG, "unexpected_qr_data"
        return row
    row["detected_voucher"] = voucher

    if not current_value:
        row["action"], row["reason"] = UPDATE, "field_empty"
    elif current_value.strip().upper() == voucher.upper():
        row["action"], row["reason"] = SKIP, "already_correct"
    elif allow_overwrite:
        row["action"], row["reason"] = UPDATE, "overwrite_existing"
    else:
        row["action"], row["reason"] = FLAG, "value_conflict"
    return row


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
def export_csv(rows, path):
    cols = ["observation_id", "url", "taxon", "upload_date", "detected_voucher",
            "field_state", "current_value", "action", "reason", "raw_qr", "raw_ocr"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


# ---------------------------------------------------------------------------
# Design system  —  "Voucher Sync" Direction B (Claude Design handoff)
#
# A modernized, desktop-shaped restyle: neutral cool-gray surfaces with a blue
# primary accent, and green reserved for "connected / success" status. Recreated
# in Tkinter from the HTML/CSS prototype. Tkinter can't do rounded corners or
# drop shadows, so cards use flat 1px borders; everything else (palette,
# hierarchy, segmented controls, toggles, status pills, chips) is matched.
# ---------------------------------------------------------------------------
COL = {
    "card_bg":      "#ffffff",
    "card_border":  "#e3e7ee",
    "header_bg":    "#fafbfd",
    "subtle":       "#f5f7fa",   # inset field / action-bar background
    "track":        "#eef1f5",   # segmented-control track
    "divider":      "#eef1f5",
    "primary":      "#2f6df0",   # primary action accent (blue)
    "primary_press":"#2a61d6",
    "text":         "#1b2530",
    "text_med":     "#4a5563",
    "text_soft":    "#647280",
    "text_soft2":   "#6b7785",
    "muted":        "#9aa3af",
    "muted2":       "#aab2bf",
    "green":        "#1f9d63",   # success / connected / progress
    "green_press":  "#1b8a57",
    "green_text":   "#16774a",
    "green_bg":     "#e6f5ec",
    "green_border": "#c3e8d4",
    "danger":       "#e0584a",   # Stop button
    "danger_press": "#c94436",
    "skip_bg":      "#eaeef3",
    "skip_fg":      "#5b6573",
    "flag_bg":      "#fdebcf",
    "flag_fg":      "#9a6712",
    "flag_row":     "#fff8f0",   # amber-tinted flag row
    "ocr_row":      "#cce5ff",   # OCR-derived update (review recommended)
    "zebra0":       "#ffffff",
    "zebra1":       "#fafbfd",
    "toggle_off":   "#d4dae2",
}

# Resolved against installed families in _init_fonts(); the design asks for
# Public Sans / JetBrains Mono, with the closest system fonts as fallbacks.
F = {}


def _init_fonts(root):
    fams = set(tkfont.families())
    ui   = "Public Sans"   if "Public Sans"   in fams else "Segoe UI"
    mono = "JetBrains Mono" if "JetBrains Mono" in fams else "Consolas"
    F.update({
        "title":     (ui, 16, "bold"),
        "subtitle":  (ui, 9),
        "eyebrow":   (ui, 8, "bold"),
        "label":     (ui, 9, "bold"),
        "help":      (ui, 8),
        "body":      (ui, 10),
        "btn":       (ui, 10, "bold"),
        "btn_sm":    (ui, 9, "bold"),
        "seg":       (ui, 9),
        "seg_sel":   (ui, 9, "bold"),
        "chip":      (ui, 9, "bold"),
        "pill_lbl":  (ui, 9, "bold"),
        "status":    (ui, 9, "bold"),
        "count":     (ui, 8),
        "tree":      (ui, 10),
        "tree_mono": (mono, 9),
        "heading":   (ui, 8, "bold"),
        "mono":      (mono, 9),
        "mono_sm":   (mono, 8),
        "log":       (mono, 9),
    })


# ---------------------------------------------------------------------------
# Custom widgets  —  Tkinter has no native toggle / segmented control, so the
# design's pieces are drawn by hand.
# ---------------------------------------------------------------------------
class Switch(tk.Frame):
    """A crisp Off/On toggle bound to a BooleanVar.

    Rendered as a two-segment pill rather than a canvas-drawn knob — Tkinter's
    canvas has no anti-aliasing, so a rounded knob comes out jagged. This reads
    as a toggle while staying pixel-clean and consistent with SegmentedControl.
    """

    def __init__(self, parent, variable, command=None, accent=None):
        super().__init__(parent, bg=COL["track"], highlightthickness=1,
                         highlightbackground=COL["card_border"], bd=0)
        self._var = variable
        self._cmd = command
        self._accent = accent or COL["primary"]
        self._off = tk.Label(self, text="Off", padx=12, pady=5,
                             cursor="hand2")
        self._off.pack(side="left", padx=2, pady=2)
        self._on = tk.Label(self, text="On", padx=12, pady=5, cursor="hand2")
        self._on.pack(side="left", padx=2, pady=2)
        self._off.bind("<Button-1>", lambda _e: self._set(False))
        self._on.bind("<Button-1>", lambda _e: self._set(True))
        self.refresh()

    def _set(self, value):
        self._var.set(value)
        self.refresh()
        if self._cmd:
            self._cmd()

    def refresh(self):
        if bool(self._var.get()):
            self._on.configure(bg=self._accent, fg="#ffffff", font=F["seg_sel"])
            self._off.configure(bg=COL["track"], fg=COL["text_soft2"],
                                font=F["seg"])
        else:
            self._off.configure(bg="#ffffff", fg=COL["text"], font=F["seg_sel"])
            self._on.configure(bg=COL["track"], fg=COL["text_soft2"],
                               font=F["seg"])


class SegmentedControl(tk.Frame):
    """A track of selectable segments bound to a StringVar (iOS-style)."""

    def __init__(self, parent, options, variable, command=None):
        super().__init__(parent, bg=COL["track"], highlightthickness=1,
                         highlightbackground=COL["card_border"], bd=0)
        self._var = variable
        self._cmd = command
        self._labels = {}
        for label, value in options:
            lbl = tk.Label(self, text=label, bg=COL["track"], padx=12, pady=5,
                           cursor="hand2")
            lbl.pack(side="left", padx=2, pady=2)
            lbl.bind("<Button-1>", lambda _e, v=value: self._select(v))
            self._labels[value] = lbl
        self.refresh()

    def _select(self, value):
        self._var.set(value)
        self.refresh()
        if self._cmd:
            self._cmd()

    def refresh(self):
        cur = self._var.get()
        for value, lbl in self._labels.items():
            if value == cur:
                lbl.configure(bg="#ffffff", fg=COL["text"], font=F["seg_sel"])
            else:
                lbl.configure(bg=COL["track"], fg=COL["text_soft2"],
                              font=F["seg"])


class FlatButton(tk.Button):
    """A flat, fully color-controlled button with explicit enabled/disabled
    palettes (ttk on Windows ignores most color options, so use classic tk)."""

    def __init__(self, parent, text, command, fg, bg, active,
                 disabled_fg, disabled_bg, font, border=None, padx=16):
        super().__init__(
            parent, text=text, command=command, font=font,
            fg=fg, bg=bg, activeforeground=fg, activebackground=active,
            relief="flat", bd=0, padx=padx, pady=8, cursor="hand2",
            highlightthickness=(1 if border else 0),
            highlightbackground=border or bg, takefocus=0,
        )
        self._enabled_palette  = (fg, bg, active)
        self._disabled_palette = (disabled_fg, disabled_bg)

    def set_enabled(self, on):
        if on:
            fg, bg, active = self._enabled_palette
            self.configure(state="normal", fg=fg, bg=bg,
                           activebackground=active, cursor="hand2")
        else:
            dfg, dbg = self._disabled_palette
            self.configure(state="disabled", bg=dbg, disabledforeground=dfg,
                           cursor="arrow")


class AutocompleteEntry(tk.Frame):
    """An entry with a live suggestion dropdown.

    Typing (debounced) calls `on_query(text)`; the owner runs the lookup off
    the UI thread and feeds matches back via `show_results(query, items)`,
    where `items` is a list of (label, value). Picking one fills the entry
    with the label and calls `on_select(value, label)`.
    """

    def __init__(self, parent, textvariable, on_query, on_select,
                 min_chars=2, delay=280, pad_left=0):
        super().__init__(parent, bg=COL["card_bg"])
        self._var = textvariable
        self._on_query = on_query
        self._on_select = on_select
        self._min = min_chars
        self._delay = delay
        self._after = None
        self._popup = None
        self._listbox = None
        self._items = []

        # Border lives on a wrapper frame so the entry text can be inset
        # `pad_left` px (tk.Entry has no internal padding); the frame's border
        # tracks focus for the usual blue focus ring.
        self._field = tk.Frame(self, bg=COL["card_bg"], highlightthickness=1,
                               highlightbackground=COL["card_border"])
        self._field.pack(fill="x")
        self._entry = tk.Entry(
            self._field, textvariable=self._var, bd=0, relief="flat",
            bg=COL["card_bg"], fg=COL["text"],
            insertbackground=COL["text"], font=F["body"])
        self._entry.pack(fill="x", expand=True, ipady=5, padx=(pad_left, 0))
        self._entry.bind(
            "<FocusIn>",
            lambda _e: self._field.configure(highlightbackground=COL["primary"]))
        self._entry.bind("<KeyRelease>", self._on_key)
        self._entry.bind("<Down>", self._focus_list)
        self._entry.bind("<Return>", self._on_return)
        self._entry.bind("<Escape>", lambda _e: self._hide())
        self._entry.bind("<FocusOut>", self._on_focus_out)

    # typing → debounced query
    def _on_key(self, evt):
        if evt.keysym in ("Up", "Down", "Return", "Escape", "Tab",
                          "Left", "Right", "Shift_L", "Shift_R"):
            return
        if self._after:
            self.after_cancel(self._after)
        self._after = self.after(self._delay, self._fire)

    def _fire(self):
        self._after = None
        text = self._var.get().strip()
        if len(text) < self._min:
            self._hide()
            return
        self._on_query(text)

    # owner feeds results back here (on the UI thread)
    def show_results(self, query, items):
        if query.strip() != self._var.get().strip():
            return  # stale: the user kept typing
        self._items = items
        if not items:
            self._hide()
            return
        self._ensure_popup()
        self._listbox.delete(0, "end")
        for label, _val in items:
            self._listbox.insert("end", label)
        self._position_popup(len(items))

    def _ensure_popup(self):
        if self._popup:
            return
        self._popup = tk.Toplevel(self)
        self._popup.wm_overrideredirect(True)
        self._listbox = tk.Listbox(
            self._popup, activestyle="none", bd=0, highlightthickness=1,
            highlightbackground=COL["card_border"], bg=COL["card_bg"],
            fg=COL["text"], font=F["body"], selectbackground="#dbe7ff",
            selectforeground=COL["text"], exportselection=False)
        self._listbox.pack(fill="both", expand=True)
        self._listbox.bind("<ButtonRelease-1>", self._choose)
        self._listbox.bind("<Return>", self._choose)
        self._listbox.bind("<Escape>",
                           lambda _e: (self._hide(), self._entry.focus_set()))

    def _position_popup(self, n):
        self._listbox.configure(height=min(n, 8))
        self._popup.update_idletasks()
        # Align the dropdown to the field's visible box, not the inset entry.
        x = self._field.winfo_rootx()
        y = self._field.winfo_rooty() + self._field.winfo_height() + 2
        w = self._field.winfo_width()
        h = self._listbox.winfo_reqheight()
        self._popup.wm_geometry(f"{w}x{h}+{x}+{y}")
        self._popup.lift()

    def _focus_list(self, _evt):
        if self._popup and self._listbox.size():
            self._listbox.focus_set()
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(0)
            self._listbox.activate(0)
            return "break"

    def _on_return(self, _evt):
        if self._popup and self._items:
            sel = self._listbox.curselection()
            self._pick(sel[0] if sel else 0)
            return "break"

    def _choose(self, _evt=None):
        if self._listbox:
            sel = self._listbox.curselection()
            if sel:
                self._pick(sel[0])

    def _pick(self, idx):
        label, value = self._items[idx]
        self._var.set(label)
        self._hide()
        self._entry.focus_set()
        self._entry.icursor("end")
        self._on_select(value, label)

    def _on_focus_out(self, _evt):
        self._field.configure(highlightbackground=COL["card_border"])
        # Defer so a click landing on the listbox is processed first.
        self.after(150, self._maybe_hide)

    def _maybe_hide(self):
        if self.focus_get() is not self._listbox:
            self._hide()

    def _hide(self):
        if self._after:
            self.after_cancel(self._after)
            self._after = None
        if self._popup:
            self._popup.destroy()
            self._popup = None
            self._listbox = None


class DatePicker(tk.Frame):
    """A DD/MM/YYYY text entry paired with a pop-up month calendar.

    The entry keeps the existing text contract (so validation and the API
    date conversion are unchanged), while the calendar button opens a
    point-and-click month grid.  Bound to a StringVar; selecting a day writes
    DD/MM/YYYY back into it.  Built by hand for the same reason as the other
    widgets here — Tkinter ships no date picker and the app stays dependency-
    light.
    """

    _WEEKDAYS = ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")

    def __init__(self, parent, variable, bg=None, width=11):
        bg = bg or COL["card_bg"]
        super().__init__(parent, bg=bg)
        self._var = variable
        self._bg = bg
        self._popup = None
        self._view = None          # first-of-month date currently shown
        self._click_bind = None

        self._entry = tk.Entry(
            self, textvariable=variable, bd=0, relief="flat",
            highlightthickness=1, highlightbackground=COL["card_border"],
            highlightcolor=COL["primary"], bg=bg, fg=COL["text"],
            insertbackground=COL["text"], font=F["mono"],
            width=width, justify="center")
        self._entry.pack(side="left", ipady=4)
        self._btn = tk.Label(self, text="📅", bg=bg, fg=COL["text_med"],
                             font=F["body"], cursor="hand2", padx=6)
        self._btn.pack(side="left")
        self._btn.bind("<Button-1>", lambda _e: self._toggle())
        self._entry.bind("<Escape>", lambda _e: self._close())

    # ---- value <-> date helpers ----------------------------------------- #
    def _parse(self):
        """Return the entry's current value as a date, or None if unparseable."""
        try:
            d, m, y = (self._var.get() or "").strip().split("/")
            return datetime.date(int(y), int(m), int(d))
        except (ValueError, AttributeError):
            return None

    # ---- popup lifecycle ------------------------------------------------- #
    def _toggle(self):
        self._close() if self._popup else self._open()

    def _open(self):
        self._view = (self._parse() or datetime.date.today()).replace(day=1)
        self._popup = tk.Toplevel(self)
        self._popup.wm_overrideredirect(True)
        self._popup.configure(bg=COL["card_bg"], highlightthickness=1,
                              highlightbackground=COL["card_border"])
        self._render()
        self._place()
        # Dismiss on a click anywhere outside the popup (and outside our button,
        # which toggles on its own).
        self._click_bind = self.winfo_toplevel().bind(
            "<Button-1>", self._maybe_close, add="+")

    def _place(self):
        self._popup.update_idletasks()
        x = self._entry.winfo_rootx()
        y = self._entry.winfo_rooty() + self._entry.winfo_height() + 3
        self._popup.wm_geometry(f"+{x}+{y}")
        self._popup.lift()

    def _maybe_close(self, evt):
        if not self._popup:
            return
        for w in (self._popup, self._btn):
            wx, wy = w.winfo_rootx(), w.winfo_rooty()
            if (wx <= evt.x_root <= wx + w.winfo_width() and
                    wy <= evt.y_root <= wy + w.winfo_height()):
                return
        self._close()

    def _close(self):
        if self._popup:
            self._popup.destroy()
            self._popup = None
        if self._click_bind:
            self.winfo_toplevel().unbind("<Button-1>", self._click_bind)
            self._click_bind = None

    # ---- rendering ------------------------------------------------------- #
    def _shift(self, months):
        m = self._view.month - 1 + months
        self._view = datetime.date(self._view.year + m // 12, m % 12 + 1, 1)
        self._render()

    def _select(self, d):
        self._var.set(d.strftime("%d/%m/%Y"))
        self._close()

    def _render(self):
        import calendar
        for w in self._popup.winfo_children():
            w.destroy()
        bg = COL["card_bg"]
        pad = tk.Frame(self._popup, bg=bg)
        pad.pack(padx=8, pady=8)

        head = tk.Frame(pad, bg=bg)
        head.pack(fill="x", pady=(0, 6))
        prev = tk.Label(head, text="‹", bg=bg, fg=COL["text_med"],
                        font=F["btn"], cursor="hand2", padx=8)
        prev.pack(side="left")
        prev.bind("<Button-1>", lambda _e: self._shift(-1))
        tk.Label(head, text=self._view.strftime("%B %Y"), bg=bg,
                 fg=COL["text"], font=F["label"]).pack(side="left", expand=True)
        nxt = tk.Label(head, text="›", bg=bg, fg=COL["text_med"],
                       font=F["btn"], cursor="hand2", padx=8)
        nxt.pack(side="right")
        nxt.bind("<Button-1>", lambda _e: self._shift(1))

        grid = tk.Frame(pad, bg=bg)
        grid.pack()
        for col, wd in enumerate(self._WEEKDAYS):
            tk.Label(grid, text=wd, bg=bg, fg=COL["muted"], font=F["count"],
                     width=3).grid(row=0, column=col, padx=1, pady=(0, 2))

        selected = self._parse()
        today = datetime.date.today()
        weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(
            self._view.year, self._view.month)
        for r, week in enumerate(weeks, start=1):
            for col, day in enumerate(week):
                if day == 0:
                    continue
                d = datetime.date(self._view.year, self._view.month, day)
                is_sel = selected == d
                rest_bg = COL["primary"] if is_sel else (
                    COL["track"] if d == today else bg)
                cell = tk.Label(
                    grid, text=str(day), width=3, cursor="hand2",
                    font=F["body"], bg=rest_bg,
                    fg="#ffffff" if is_sel else COL["text"])
                cell.grid(row=r, column=col, padx=1, pady=1)
                cell.bind("<Button-1>", lambda _e, dd=d: self._select(dd))
                if not is_sel:
                    cell.bind("<Enter>",
                              lambda _e, w=cell: w.configure(bg=COL["track"]))
                    cell.bind("<Leave>", lambda _e, w=cell, b=rest_bg:
                              w.configure(bg=b))


# ---------------------------------------------------------------------------
# GUI — preview-queue row colours (zebra rows, amber flags, tinted updates)
# ---------------------------------------------------------------------------
TAG_UPDATE = "tag_update"
TAG_OCR    = "tag_ocr"       # UPDATE row whose voucher came from OCR, not QR
TAG_FLAG   = "tag_flag"
TAG_ZEBRA0 = "tag_zebra0"    # even SKIP row
TAG_ZEBRA1 = "tag_zebra1"    # odd SKIP row

ROW_COLOR = {
    TAG_UPDATE: COL["green_bg"],   # soft green — QR-confirmed update
    TAG_OCR:    COL["ocr_row"],    # light blue — OCR-derived update (review)
    TAG_FLAG:   COL["flag_row"],   # amber-tinted — needs attention
    TAG_ZEBRA0: COL["zebra0"],     # zebra striping for no-action rows
    TAG_ZEBRA1: COL["zebra1"],
}


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class VoucherSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iNaturalist Voucher Sync")
        self.geometry("1220x900")
        self.minsize(1000, 720)
        self.configure(bg=COL["card_bg"])

        _init_fonts(self)
        self._init_style()

        self._rows   = []
        self._mq     = queue.Queue()
        self._worker = None
        self._cancel = threading.Event()
        self._log_open = False

        self._build_ui()
        self._poll()
        self._load_env_token()
        self._try_silent_refresh()

    def _init_style(self):
        """Theme ttk widgets (Treeview, Progressbar, Scrollbar) to the palette.
        'clam' is used because it honors background/foreground colors that the
        native Windows theme ignores."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Queue.Treeview",
            background=COL["card_bg"], fieldbackground=COL["card_bg"],
            foreground=COL["text"], font=F["tree"], rowheight=30,
            borderwidth=0, relief="flat",
        )
        style.configure(
            "Queue.Treeview.Heading",
            background=COL["subtle"], foreground=COL["muted"],
            font=F["heading"], relief="flat", borderwidth=0, padding=(10, 8),
        )
        style.map("Queue.Treeview.Heading",
                  background=[("active", COL["track"])])

        # Tk 8.6.9 regression: a ('!disabled', '!selected', ...) style-map entry
        # forces every normal row to the default background, overriding per-row
        # tag colours. Strip it so tag_configure backgrounds render. Harmless on
        # versions that don't carry the bad entry.
        def _fixed_map(option):
            return [e for e in style.map("Queue.Treeview", query_opt=option)
                    if e[:2] != ("!disabled", "!selected")]
        style.map("Queue.Treeview",
                  foreground=_fixed_map("foreground"),
                  background=[("selected", "#dbe7ff")])

        style.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor=COL["card_border"], background=COL["green"],
            bordercolor=COL["card_border"], lightcolor=COL["green"],
            darkcolor=COL["green"], thickness=7,
        )
        style.configure("Vertical.TScrollbar", background=COL["track"],
                        troughcolor=COL["card_bg"], bordercolor=COL["card_bg"],
                        arrowcolor=COL["muted"])
        style.configure("Horizontal.TScrollbar", background=COL["track"],
                        troughcolor=COL["card_bg"], bordercolor=COL["card_bg"],
                        arrowcolor=COL["muted"])

    # ----------------------------------------------------------------------- #
    # UI                                                                       #
    # ----------------------------------------------------------------------- #
    def _build_ui(self):
        self._build_header()

        body = tk.Frame(self, bg=COL["card_bg"])
        body.pack(fill="both", expand=True, padx=24, pady=(18, 22))

        self._build_config(body)
        self._build_action_bar(body)
        self._build_results(body)
        self._build_log(body)

    # ----- header band ----------------------------------------------------- #
    def _build_header(self):
        header = tk.Frame(self, bg=COL["header_bg"])
        header.pack(fill="x")
        inner = tk.Frame(header, bg=COL["header_bg"])
        inner.pack(fill="x", padx=24, pady=16)

        left = tk.Frame(inner, bg=COL["header_bg"])
        left.pack(side="left")
        tk.Label(left, text="Voucher Sync", bg=COL["header_bg"],
                 fg=COL["text"], font=F["title"]).pack(anchor="w")
        tk.Label(left,
                 text="Match specimen voucher labels in your photos to "
                      "iNaturalist observations.",
                 bg=COL["header_bg"], fg=COL["text_soft2"],
                 font=F["subtitle"]).pack(anchor="w", pady=(2, 0))

        # Connection state is shown in the Connection card (signed-in banner /
        # token status), so the header no longer carries a status pill.

        tk.Frame(self, bg=COL["card_border"], height=1).pack(fill="x")

    def _set_connected(self, login):
        # Connection state lives in the Connection card now: the manual-token
        # tab's inline "✓ valid" status…
        if hasattr(self, "_token_status"):
            if login:
                self._token_status.configure(text="✓ valid", fg=COL["green"])
            else:
                self._token_status.configure(text="")
        # …and the Sign-in tab, which swaps its call-to-action for the signed-in
        # banner (which also carries the Sign out link).
        self._refresh_signin_tab(login)

    # ----- small shared builders ------------------------------------------- #
    def _make_card(self, parent, bg=None):
        """A flat bordered card; returns (outer, padded_inner)."""
        bg = bg or COL["card_bg"]
        outer = tk.Frame(parent, bg=bg, highlightthickness=1,
                         highlightbackground=COL["card_border"], bd=0)
        inner = tk.Frame(outer, bg=bg)
        inner.pack(fill="both", expand=True, padx=16, pady=15)
        return outer, inner

    def _entry(self, parent, var, bg=None, mono=False, width=0, show=None):
        bg = bg or COL["card_bg"]
        return tk.Entry(
            parent, textvariable=var, bd=0, relief="flat",
            highlightthickness=1, highlightbackground=COL["card_border"],
            highlightcolor=COL["primary"], bg=bg, fg=COL["text"],
            insertbackground=COL["text"], disabledbackground=COL["subtle"],
            disabledforeground=COL["muted"], readonlybackground=COL["subtle"],
            font=F["mono"] if mono else F["body"],
            width=width or 0, show=show,
        )

    def _inset_entry(self, parent, var, pad_left=8, show=None):
        """A text entry whose content sits `pad_left` px in from the left edge.

        tk.Entry has no internal text padding, so the border lives on a wrapper
        frame and the entry is packed with left padding inside it; the frame's
        border tracks focus to keep the same blue focus ring as other fields.
        Returns (field_frame, entry) — pack the frame.
        """
        bg = COL["card_bg"]
        field = tk.Frame(parent, bg=bg, highlightthickness=1,
                         highlightbackground=COL["card_border"])
        entry = tk.Entry(field, textvariable=var, bd=0, relief="flat", bg=bg,
                         fg=COL["text"], insertbackground=COL["text"],
                         font=F["body"], show=show)
        entry.pack(fill="x", expand=True, padx=(pad_left, 0), ipady=5)
        entry.bind("<FocusIn>",
                   lambda _e: field.configure(highlightbackground=COL["primary"]))
        entry.bind("<FocusOut>",
                   lambda _e: field.configure(
                       highlightbackground=COL["card_border"]))
        return field, entry

    def _secondary_btn(self, parent, text, command, padx=14):
        return FlatButton(
            parent, text=text, command=command, font=F["btn_sm"],
            fg=COL["text_med"], bg=COL["card_bg"], active=COL["track"],
            disabled_fg=COL["muted2"], disabled_bg=COL["track"],
            border=COL["card_border"], padx=padx)

    # ----- configuration cards --------------------------------------------- #
    def _build_config(self, parent):
        grid = tk.Frame(parent, bg=COL["card_bg"])
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="cards")
        grid.columnconfigure(1, weight=1, uniform="cards")

        conn_o, conn_i = self._make_card(grid)
        conn_o.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_connection_card(conn_i)

        match_o, match_i = self._make_card(grid)
        match_o.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._build_matching_card(match_i)

        ocr_o, ocr_i = self._make_card(grid, bg=COL["header_bg"])
        ocr_o.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        self._build_ocr_card(ocr_i)

        # Auto-populate the common Windows Tesseract path if it exists.
        if os.path.isfile(_WIN_TESS_DEFAULT):
            self._tess_var.set(_WIN_TESS_DEFAULT)

        self._toggle_dates()
        self._on_format_change()
        self._toggle_ocr()

    def _eyebrow(self, parent, text, bg):
        tk.Label(parent, text=text, bg=bg, fg=COL["muted"],
                 font=F["eyebrow"]).pack(anchor="w", pady=(0, 13))

    def _field_label(self, parent, text, bg, pady=(0, 5)):
        tk.Label(parent, text=text, bg=bg, fg=COL["text_soft"],
                 font=F["label"]).pack(anchor="w", pady=pady)

    def _build_connection_card(self, c):
        bg = COL["card_bg"]
        self._eyebrow(c, "CONNECTION", bg)

        # Two ways to connect, as tabs: browser Sign in (the primary, default
        # path) and manual API token entry.
        self._conn_mode = tk.StringVar(value="signin")
        SegmentedControl(
            c, [("Sign in", "signin"), ("API token", "manual")],
            self._conn_mode, command=self._on_conn_mode).pack(
            anchor="w", pady=(0, 12))

        # One content area holding both tab panels; only the active one is
        # packed (swapped by _on_conn_mode), so its slot stays above Username.
        self._conn_tab_area = tk.Frame(c, bg=bg)
        self._conn_tab_area.pack(fill="x")
        self._tab_signin = tk.Frame(self._conn_tab_area, bg=bg)
        self._tab_manual = tk.Frame(self._conn_tab_area, bg=bg)

        self._build_signin_tab(self._tab_signin, bg)
        self._build_manual_token_tab(self._tab_manual, bg)
        self._on_conn_mode()  # show the active tab

        # Username
        self._field_label(c, "Username", bg, pady=(14, 5))
        self._user_var = tk.StringVar(value=DEFAULT_USER)
        user_field, _ = self._inset_entry(c, self._user_var)
        user_field.pack(fill="x")

        # Observation field — a predictive picker rather than a raw numeric ID.
        # Type to search iNaturalist's fields live; `_field_id_var` keeps the
        # numeric id the rest of the app uses, while the box shows a friendly
        # name. Seeded with the default field.
        self._field_label(c, "Observation field", bg, pady=(14, 5))
        self._field_id_var = tk.StringVar(value=str(DEFAULT_FIELD_ID))
        self._field_name_var = tk.StringVar(
            value=f"{DEFAULT_FIELD_NAME} (#{DEFAULT_FIELD_ID})")
        self._field_widget = AutocompleteEntry(
            c, self._field_name_var,
            on_query=self._field_query, on_select=self._field_chosen,
            pad_left=8)
        self._field_widget.pack(fill="x")

        tk.Label(c, text="Start typing to search the fields that store "
                         "voucher codes.",
                 bg=bg, fg=COL["muted"], font=F["help"]).pack(
            anchor="w", pady=(7, 0))

    def _on_conn_mode(self):
        """Show the panel for the selected connection tab."""
        self._tab_signin.pack_forget()
        self._tab_manual.pack_forget()
        if self._conn_mode.get() == "signin":
            self._tab_signin.pack(fill="x")
        else:
            self._tab_manual.pack(fill="x")

    def _build_signin_tab(self, parent, bg):
        """Browser sign-in.  Two states, swapped by _refresh_signin_tab:
        the big call-to-action when signed out, and a signed-in banner naming
        the account (with a Sign out link) once connected."""
        # ---- Signed-out: the primary call to action -----------------------
        self._signin_prompt = tk.Frame(parent, bg=bg)
        self._btn_signin = FlatButton(
            self._signin_prompt, text="Sign in with iNaturalist",
            command=self._oauth_sign_in,
            fg="#ffffff", bg=COL["primary"], active=COL["primary_press"],
            disabled_fg="#ffffff", disabled_bg=COL["muted2"],
            font=F["btn"], padx=22)
        self._btn_signin.pack(fill="x", pady=(0, 4))
        tk.Label(self._signin_prompt,
                 text="Opens your browser to authorize — no token to copy. "
                      "You stay signed in, so it's usually one click, and the "
                      "app remembers you for next time.",
                 bg=bg, fg=COL["muted"], font=F["help"], justify="left",
                 wraplength=360).pack(anchor="w", pady=(0, 4))

        # ---- Signed-in: a green banner naming the account, plus Sign out ----
        self._signin_banner = tk.Frame(parent, bg=bg)
        card = tk.Frame(self._signin_banner, bg=COL["green_bg"],
                        highlightthickness=1,
                        highlightbackground=COL["green_border"])
        card.pack(fill="x")
        row = tk.Frame(card, bg=COL["green_bg"])
        row.pack(fill="x", padx=12, pady=10)
        tk.Label(row, text="✓", bg=COL["green_bg"], fg=COL["green"],
                 font=F["btn"]).pack(side="left", padx=(0, 9))
        self._signin_banner_lbl = tk.Label(
            row, text="", bg=COL["green_bg"], fg=COL["green_text"],
            font=F["label"], justify="left", anchor="w")
        self._signin_banner_lbl.pack(side="left")
        # Sign out sits at the opposite (right) end of the banner.
        signout = tk.Label(
            row, text="Sign out", bg=COL["green_bg"], fg=COL["green_text"],
            cursor="hand2", font=(F["help"][0], F["help"][1], "underline"))
        signout.pack(side="right")
        signout.bind("<Button-1>", lambda _e: self._sign_out())

        self._refresh_signin_tab(None)

    def _refresh_signin_tab(self, login):
        """Show the CTA when signed out, the account banner when signed in."""
        if not hasattr(self, "_signin_prompt"):
            return  # header called _set_connected before the tab was built
        if login:
            self._signin_prompt.pack_forget()
            self._signin_banner_lbl.configure(text=f"Signed in as {login}")
            self._signin_banner.pack(fill="x", pady=(0, 4))
        else:
            self._signin_banner.pack_forget()
            self._signin_prompt.pack(fill="x", pady=(0, 4))

    def _build_manual_token_tab(self, parent, bg):
        """Manual entry: paste a token from the iNaturalist token page."""
        self._field_label(parent, "API token", bg)
        tok_row = tk.Frame(parent, bg=bg)
        tok_row.pack(fill="x")
        tok_field = tk.Frame(tok_row, bg=COL["subtle"], highlightthickness=1,
                             highlightbackground=COL["card_border"])
        tok_field.pack(side="left", fill="x", expand=True)
        self._token_var = tk.StringVar()
        tk.Entry(tok_field, textvariable=self._token_var, show="•", bd=0,
                 relief="flat", bg=COL["subtle"], fg=COL["text"],
                 font=F["mono"], insertbackground=COL["text"]).pack(
            side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        self._token_status = tk.Label(tok_field, text="", bg=COL["subtle"],
                                      fg=COL["green"], font=F["help"])
        self._token_status.pack(side="left", padx=(0, 10))
        tok_link = tk.Label(
            tok_row, text="Get a token ↗", bg=bg, fg=COL["primary"],
            cursor="hand2", font=(F["btn_sm"][0], F["btn_sm"][1],
                                  "bold underline"))
        tok_link.pack(side="left", padx=(12, 4))
        tok_link.bind("<Button-1>",
                      lambda _e: webbrowser.open(f"{WEB}/users/api_token"))

        tk.Label(parent,
                 text="Sign in to iNaturalist, then copy your token from the "
                      "token page. Tokens expire after about 24 hours.",
                 bg=bg, fg=COL["muted"], font=F["help"], justify="left",
                 wraplength=360).pack(anchor="w", pady=(7, 0))

    def _build_matching_card(self, c):
        bg = COL["card_bg"]
        self._eyebrow(c, "VOUCHER MATCHING", bg)

        # Code format — segmented control over the preset names.
        self._field_label(c, "Code format", bg, pady=(0, 6))
        self._format_var = tk.StringVar(value=DEFAULT_VOUCHER_FORMAT)
        fmt_opts = [(name, name) for name, _pat in VOUCHER_FORMATS]
        self._fmt_seg = SegmentedControl(c, fmt_opts, self._format_var,
                                         command=self._on_format_change)
        self._fmt_seg.pack(anchor="w")

        self._field_label(c, "Pattern", bg, pady=(11, 5))
        self._regex_var = tk.StringVar(value=DEFAULT_VOUCHER_RE)
        self._regex_entry = tk.Entry(
            c, textvariable=self._regex_var, bd=0, relief="flat",
            highlightthickness=1, highlightbackground=COL["divider"],
            highlightcolor=COL["primary"], bg=COL["subtle"],
            fg=COL["text_soft"], font=F["mono_sm"],
            insertbackground=COL["text"], readonlybackground=COL["subtle"])
        self._regex_entry.pack(fill="x", ipady=4)
        # Live hint: example codes for the selected format (set by
        # _on_format_change, which runs once at startup and on every switch).
        self._pattern_hint = tk.Label(c, text="", bg=bg, fg=COL["muted"],
                                      font=F["help"], anchor="w",
                                      justify="left")
        self._pattern_hint.pack(anchor="w", pady=(5, 0))

        # Date filter — segmented Single day / Range + date input(s).
        self._field_label(c, "Date filter", bg, pady=(16, 6))
        drow = tk.Frame(c, bg=bg)
        drow.pack(fill="x")
        self._date_mode = tk.StringVar(value="single")
        SegmentedControl(drow, [("Single day", "single"), ("Range", "range")],
                         self._date_mode, command=self._toggle_dates).pack(
            side="left")

        today = datetime.date.today().strftime("%d/%m/%Y")
        self._date_var = tk.StringVar(value=today)
        self._date_entry = DatePicker(drow, self._date_var, bg=bg, width=11)
        self._date_start_var = tk.StringVar()
        self._date_start_entry = DatePicker(drow, self._date_start_var, bg=bg,
                                            width=10)
        self._lbl_to = tk.Label(drow, text="to", bg=bg, fg=COL["muted"],
                                font=F["help"])
        self._date_end_var = tk.StringVar()
        self._date_end_entry = DatePicker(drow, self._date_end_var, bg=bg,
                                          width=10)
        tk.Label(c, text="DD / MM / YYYY", bg=bg, fg=COL["muted"],
                 font=F["help"]).pack(anchor="w", pady=(6, 0))

        # Overwrite toggle
        tk.Frame(c, bg=COL["divider"], height=1).pack(fill="x", pady=(16, 0))
        ow = tk.Frame(c, bg=bg)
        ow.pack(fill="x", pady=(13, 0))
        self._overwrite_var = tk.BooleanVar(value=False)
        ow_text = tk.Frame(ow, bg=bg)
        ow_sub = tk.Label(ow_text, text="Off — only fills blank fields.",
                          bg=bg, fg=COL["muted"], font=F["help"])

        def _ow_cmd():
            ow_sub.configure(
                text="On — replaces conflicting values."
                if self._overwrite_var.get()
                else "Off — only fills blank fields.")

        Switch(ow, self._overwrite_var, command=_ow_cmd).pack(
            side="left", padx=(0, 11))
        ow_text.pack(side="left")
        tk.Label(ow_text, text="Overwrite existing values", bg=bg,
                 fg=COL["text"], font=F["label"]).pack(anchor="w")
        ow_sub.pack(anchor="w")

    def _build_ocr_card(self, c):
        bg = COL["header_bg"]
        top = tk.Frame(c, bg=bg)
        top.pack(fill="x")

        left = tk.Frame(top, bg=bg)
        left.pack(side="left")
        # On by default: QR codes aren't always positioned to decode, so OCR
        # quietly fills the gaps. OCR-derived rows stay flagged for review.
        self._ocr_var = tk.BooleanVar(value=True)
        self._ocr_toggle = Switch(left, self._ocr_var,
                                  command=self._toggle_ocr)
        self._ocr_toggle.pack(side="left", padx=(0, 11))
        ltxt = tk.Frame(left, bg=bg)
        ltxt.pack(side="left")
        tk.Label(ltxt, text="OCR fallback", bg=bg, fg=COL["text"],
                 font=F["label"]).pack(anchor="w")
        tk.Label(ltxt, text="Reads the printed ID when a QR scan fails · "
                            "results flagged for review",
                 bg=bg, fg=COL["muted"], font=F["help"]).pack(anchor="w")

        # Engine picker — bundled (no install) vs. a system Tesseract.
        engine_box = tk.Frame(top, bg=bg)
        engine_box.pack(side="right")
        tk.Label(engine_box, text="ENGINE", bg=bg, fg=COL["muted"],
                 font=F["eyebrow"]).pack(anchor="e", pady=(0, 5))
        self._ocr_engine_var = tk.StringVar(value=DEFAULT_OCR_ENGINE)
        SegmentedControl(
            engine_box,
            [("Built-in", OCR_RAPIDOCR), ("Tesseract", OCR_TESSERACT)],
            self._ocr_engine_var,
            command=self._on_ocr_engine_change).pack(anchor="e")

        # Tesseract path — only relevant (and shown) for the Tesseract engine.
        self._tess_row = tk.Frame(c, bg=bg)
        tk.Frame(self._tess_row, bg=COL["card_border"], height=1).pack(
            fill="x", pady=(14, 11))
        tk.Label(self._tess_row, text="TESSERACT PATH", bg=bg,
                 fg=COL["muted"], font=F["eyebrow"]).pack(anchor="w",
                                                          pady=(0, 5))
        prow = tk.Frame(self._tess_row, bg=bg)
        prow.pack(fill="x")
        self._tess_var = tk.StringVar()
        self._tess_entry = tk.Entry(
            prow, textvariable=self._tess_var, bd=0, relief="flat",
            highlightthickness=1, highlightbackground=COL["card_border"],
            highlightcolor=COL["primary"], bg=COL["card_bg"],
            fg=COL["text_soft"], font=F["mono_sm"],
            insertbackground=COL["text"], disabledbackground=COL["subtle"],
            disabledforeground=COL["muted"])
        self._tess_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self._tess_browse_btn = self._secondary_btn(
            prow, "Browse…", self._browse_tesseract, padx=14)
        self._tess_browse_btn.pack(side="left", padx=(8, 0))
        # _tess_row is packed/hidden by _toggle_ocr based on the engine.

    def _on_ocr_engine_change(self):
        self._toggle_ocr()

    def _on_format_change(self):
        """Apply the selected voucher-format preset, or unlock the regex box
        for the Custom option, and refresh the example hint."""
        name = self._format_var.get()
        pattern = dict(VOUCHER_FORMATS).get(name)
        if pattern is None:                      # Custom
            self._regex_entry.configure(state="normal", fg=COL["text"])
            self._pattern_hint.configure(
                text="Enter your own regular-expression pattern above.")
        else:
            self._regex_var.set(pattern)
            self._regex_entry.configure(state="readonly", fg=COL["text_soft"])
            self._pattern_hint.configure(
                text=f"Examples: {VOUCHER_FORMAT_EXAMPLES.get(name, '')}")

    def _toggle_dates(self):
        """Single-day shows one date box; Range swaps in start/to/end."""
        single = self._date_mode.get() == "single"
        for w in (self._date_entry, self._date_start_entry,
                  self._lbl_to, self._date_end_entry):
            w.pack_forget()
        if single:
            self._date_entry.pack(side="left", padx=(10, 0), ipady=4)
        else:
            self._date_start_entry.pack(side="left", padx=(10, 0), ipady=4)
            self._lbl_to.pack(side="left", padx=7)
            self._date_end_entry.pack(side="left", ipady=4)

    def _toggle_ocr(self):
        on = self._ocr_var.get()
        tess = on and self._ocr_engine_var.get() == OCR_TESSERACT
        if tess:
            self._tess_row.pack(fill="x")
        else:
            self._tess_row.pack_forget()
        self._tess_entry.configure(state="normal" if tess else "disabled")
        self._tess_browse_btn.set_enabled(tess)

    def _browse_tesseract(self):
        path = filedialog.askopenfilename(
            title="Select tesseract executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._tess_var.set(path)

    # ----- predictive observation-field lookup ----------------------------- #
    def _field_query(self, query):
        """Fired (debounced) by the autocomplete entry as the user types."""
        threading.Thread(target=self._field_search_worker,
                         args=(query,), daemon=True).start()

    def _field_search_worker(self, query):
        try:
            fields = INatClient().search_observation_fields(query)
            self._mq.put({"kind": "field_results",
                          "query": query, "fields": fields})
        except Exception as exc:
            # Stay quiet on transient lookup errors (one per keystroke burst);
            # just note it in the run log rather than popping a dialog.
            self._mq.put({"kind": "field_results",
                          "query": query, "error": str(exc)})

    def _on_field_results(self, msg):
        if msg.get("error"):
            self._log_write(f"Field lookup failed: {msg['error']}")
            return
        items = []
        for f in msg["fields"]:
            dt = f" · {f['datatype']}" if f.get("datatype") else ""
            items.append((f"{f['name']} (#{f['id']}){dt}", f["id"]))
        self._field_widget.show_results(msg["query"], items)

    def _field_chosen(self, value, _label):
        self._field_id_var.set(str(value))

    # ----- action bar ------------------------------------------------------ #
    def _build_action_bar(self, parent):
        bar = tk.Frame(parent, bg=COL["subtle"], highlightthickness=1,
                       highlightbackground=COL["divider"])
        bar.pack(fill="x", pady=(18, 0))

        # Reminder shown above the run controls.
        tk.Label(bar,
                 text="Please note: You must have the photo that contains the "
                      "specimen and voucher as the last image in your "
                      "observation.",
                 bg=COL["subtle"], fg=COL["text_med"], font=F["help"],
                 anchor="w", justify="left").pack(
            anchor="w", fill="x", padx=16, pady=(12, 10))

        inner = tk.Frame(bar, bg=COL["subtle"])
        inner.pack(fill="x", padx=16, pady=(0, 12))

        self._btn_preview = FlatButton(
            inner, text="Preview run", command=self._start_preview,
            fg="#ffffff", bg=COL["primary"], active=COL["primary_press"],
            disabled_fg="#ffffff", disabled_bg=COL["muted2"],
            font=F["btn"], padx=22)
        self._btn_preview.pack(side="left")

        # Apply turns green when there are updates to commit (success accent).
        self._btn_apply = FlatButton(
            inner, text="Apply updates", command=self._start_apply,
            fg="#ffffff", bg=COL["green"], active=COL["green_press"],
            disabled_fg=COL["muted2"], disabled_bg=COL["track"],
            font=F["btn"], padx=18)
        self._btn_apply.set_enabled(False)
        self._btn_apply.pack(side="left", padx=(10, 0))

        self._secondary_btn(inner, "Export CSV", self._export_csv,
                            padx=16).pack(side="left", padx=(10, 0))

        FlatButton(
            inner, text="Clear", command=self._clear,
            fg=COL["muted"], bg=COL["subtle"], active=COL["track"],
            disabled_fg=COL["muted2"], disabled_bg=COL["subtle"],
            font=F["btn"], padx=14).pack(side="left", padx=(6, 0))

        status = tk.Frame(inner, bg=COL["subtle"])
        status.pack(side="right")
        self._status_lbl = tk.Label(status, text="Ready", bg=COL["subtle"],
                                    fg=COL["muted"], font=F["status"],
                                    anchor="e")
        self._status_lbl.pack(fill="x")
        self._prog_bar = ttk.Progressbar(
            status, length=190, mode="determinate",
            style="Green.Horizontal.TProgressbar")
        self._prog_bar.pack(fill="x", pady=(5, 0))
        self._count_lbl = tk.Label(status, text="", bg=COL["subtle"],
                                   fg=COL["muted"], font=F["count"],
                                   anchor="e")
        self._count_lbl.pack(fill="x", pady=(4, 0))

    # ----- results: chips + preview queue ---------------------------------- #
    def _build_results(self, parent):
        head = tk.Frame(parent, bg=COL["card_bg"])
        head.pack(fill="x", pady=(22, 11))
        tk.Label(head, text="Preview queue", bg=COL["card_bg"],
                 fg=COL["text"], font=(F["title"][0], 11, "bold")).pack(
            side="left", padx=(0, 10))
        self._chip_update = self._make_chip(head)
        self._chip_skip = self._make_chip(head)
        self._chip_flag = self._make_chip(head)
        tk.Label(head, text="Double-click any row to open in browser →",
                 bg=COL["card_bg"], fg=COL["muted"], font=F["help"]).pack(
            side="right")
        self._reset_chips()

        tree_card = tk.Frame(parent, bg=COL["card_bg"], highlightthickness=1,
                             highlightbackground=COL["card_border"])
        tree_card.pack(fill="both", expand=True)
        self._build_tree(tree_card)

    def _make_chip(self, parent):
        lbl = tk.Label(parent, font=F["chip"], padx=10, pady=3)
        lbl.pack(side="left", padx=(0, 6))
        return lbl

    def _style_chip(self, lbl, count, word, on_bg, on_fg):
        if count:
            lbl.configure(text=f"{count} {word}", bg=on_bg, fg=on_fg)
        else:
            lbl.configure(text=f"0 {word}", bg=COL["subtle"], fg=COL["muted"])

    def _reset_chips(self):
        self._style_chip(self._chip_update, 0, "update",
                         COL["green_bg"], COL["green_text"])
        self._style_chip(self._chip_skip, 0, "skip",
                         COL["skip_bg"], COL["skip_fg"])
        self._style_chip(self._chip_flag, 0, "flag",
                         COL["flag_bg"], COL["flag_fg"])

    def _build_tree(self, parent):
        cols = ("obs_id", "taxon", "uploaded", "detected",
                "current", "action", "reason")
        self._tree = ttk.Treeview(parent, columns=cols, show="headings",
                                  selectmode="browse", style="Queue.Treeview")
        headings = {
            "obs_id":   "OBS ID",
            "taxon":    "TAXON",
            "uploaded": "UPLOADED",
            "detected": "DETECTED",
            "current":  "CURRENT",
            "action":   "ACTION",
            "reason":   "REASON",
        }
        widths = {
            "obs_id": 92, "taxon": 280, "uploaded": 90,
            "detected": 108, "current": 108,
            "action": 84, "reason": 150,
        }
        for col in cols:
            self._tree.heading(
                col, text=headings[col],
                command=lambda c=col: self._sort_tree(c))
            self._tree.column(col, width=widths[col], minwidth=50,
                              stretch=(col == "taxon"))

        for tag, bg in ROW_COLOR.items():
            self._tree.tag_configure(tag, background=bg)

        vsb = ttk.Scrollbar(parent, orient="vertical",
                            command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self._tree.bind("<Double-1>", self._open_url)
        self._tree.bind("<Return>",   self._open_url)
        self._sort_reverse = False
        self._sort_col     = None

    # ----- collapsible run log --------------------------------------------- #
    def _build_log(self, parent):
        card = tk.Frame(parent, bg=COL["card_bg"], highlightthickness=1,
                        highlightbackground=COL["card_border"])
        card.pack(fill="x", pady=(16, 0))

        header = tk.Frame(card, bg=COL["header_bg"], cursor="hand2")
        header.pack(fill="x")
        self._log_arrow = tk.Label(header, text="▸", bg=COL["header_bg"],
                                   fg=COL["muted"], font=F["help"], width=2)
        self._log_arrow.pack(side="left", padx=(12, 4), pady=11)
        tk.Label(header, text="Run log", bg=COL["header_bg"],
                 fg=COL["text_med"], font=F["label"]).pack(side="left")
        self._log_lines = tk.Label(header, text="0 lines", bg=COL["header_bg"],
                                   fg=COL["muted"], font=F["help"])
        self._log_lines.pack(side="left", padx=(9, 0))
        self._log_hint = tk.Label(header, text="click to expand",
                                  bg=COL["header_bg"], fg=COL["muted2"],
                                  font=F["help"])
        self._log_hint.pack(side="right", padx=(0, 14))
        header.bind("<Button-1>", self._toggle_log)
        for child in header.winfo_children():
            child.bind("<Button-1>", self._toggle_log)

        self._log_body = tk.Frame(card, bg=COL["subtle"])
        tk.Frame(self._log_body, bg=COL["divider"], height=1).pack(fill="x")
        self._log = scrolledtext.ScrolledText(
            self._log_body, height=9, state="disabled", font=F["log"],
            wrap="word", bd=0, relief="flat", bg=COL["subtle"],
            fg=COL["text_soft"], padx=12, pady=10, highlightthickness=0)
        self._log.pack(fill="both", expand=True)
        # Collapsed by default — body is packed only when toggled open.

    def _toggle_log(self, _evt=None):
        self._log_open = not self._log_open
        if self._log_open:
            self._log_body.pack(fill="x")
            self._log_arrow.configure(text="▾")
            self._log_hint.configure(text="click to collapse")
        else:
            self._log_body.pack_forget()
            self._log_arrow.configure(text="▸")
            self._log_hint.configure(text="click to expand")

    @staticmethod
    def _action_tag(row, index=0):
        """Return the colour tag for a row: amber for flags, green/blue for
        updates (QR vs OCR), and zebra striping for no-action rows."""
        action = row["action"]
        if action == FLAG:
            return TAG_FLAG
        if action == UPDATE:
            return TAG_OCR if "ocr" in row.get("reason", "") else TAG_UPDATE
        return TAG_ZEBRA1 if index % 2 else TAG_ZEBRA0

    # ----------------------------------------------------------------------- #
    # Helpers                                                                  #
    # ----------------------------------------------------------------------- #
    def _load_env_token(self):
        t = os.environ.get("INAT_API_TOKEN", "").strip()
        if t:
            self._token_var.set(t)
            self._log_write("Token loaded from INAT_API_TOKEN environment variable.")

    # ----- browser sign-in (OAuth) ----------------------------------------- #
    def _oauth_sign_in(self):
        """Run the browser sign-in flow off the UI thread and feed the
        resulting token back through the message queue."""
        if getattr(self, "_signin_busy", False):
            return
        if not oauth_configured():
            messagebox.showinfo(
                "Browser sign-in not set up",
                "This build doesn't have an iNaturalist Client ID yet, so "
                "browser sign-in is unavailable.\n\nUse the token page and "
                "paste your token into the API token field instead.")
            return

        self._signin_busy = True
        self._btn_signin.configure(text="Signing in…")
        self._btn_signin.set_enabled(False)
        self._log_write("Starting iNaturalist browser sign-in…")

        def work():
            try:
                token, creds = oauth_login(
                    log=lambda m: self._mq.put({"kind": "log", "text": m}))
                # Confirm the token works and resolve the login name to show.
                login = INatClient(token=token).verify_token()
                self._mq.put({"kind": "oauth_ok", "token": token,
                              "creds": creds, "login": login})
            except OAuthError as exc:
                self._mq.put({"kind": "oauth_err", "text": str(exc)})
            except Exception as exc:  # network, JSON, anything unexpected
                self._mq.put({"kind": "oauth_err",
                              "text": f"Sign-in failed: {exc}"})

        threading.Thread(target=work, daemon=True).start()

    def _try_silent_refresh(self):
        """On launch, if we have saved OAuth creds and no token already set,
        mint a fresh JWT in the background — no prompt.  Falls back quietly to
        the Sign-in button if the saved session is gone."""
        if self._token_var.get().strip():
            return  # an env var or pasted token already takes precedence
        creds = load_credentials()
        if not creds:
            return
        self._log_write("Restoring your saved iNaturalist session…")

        def work():
            try:
                token = self._mint_from_creds(creds)
                login = INatClient(token=token).verify_token()
                if not login:
                    raise OAuthError("saved session is no longer valid")
                self._mq.put({"kind": "oauth_ok", "token": token,
                              "creds": creds, "login": login})
            except Exception as exc:
                self._mq.put({"kind": "oauth_refresh_failed",
                              "text": str(exc)})

        threading.Thread(target=work, daemon=True).start()

    def _mint_from_creds(self, creds):
        """Get a JWT from saved creds, refreshing the access token if needed.
        Mutates `creds` in place when a refresh yields a new access token so
        the caller can re-persist it."""
        try:
            return fetch_api_token(creds["access_token"])
        except OAuthError:
            if not creds.get("refresh_token"):
                raise
            new = refresh_access_token(creds["refresh_token"])
            creds.update(new)
            return fetch_api_token(creds["access_token"])

    def _signin_reset(self):
        self._signin_busy = False
        self._btn_signin.configure(text="Sign in with iNaturalist")
        self._btn_signin.set_enabled(True)

    def _on_oauth_ok(self, msg):
        self._signin_reset()
        self._token_var.set(msg["token"])
        creds = msg.get("creds")
        if creds:
            save_credentials(creds)   # remember for silent refresh next launch
        login = msg.get("login")
        if login:
            # The sync targets your own observations, so fill in the username.
            self._user_var.set(login)
            self._set_connected(login)
            self._log_write(f"Signed in as {login}; API token loaded.")
        else:
            # Token minted but verification came back empty — keep it, but
            # don't claim a connection we couldn't confirm.
            self._log_write("Signed in; API token loaded.")

    def _on_oauth_err(self, text):
        self._signin_reset()
        self._log_write(f"Sign-in failed: {text}")
        messagebox.showerror("Sign-in failed", text)

    def _on_refresh_failed(self, text):
        # Startup refresh only: stay quiet (no dialog), drop the stale creds,
        # and leave the Sign-in button for the user.
        clear_credentials()
        self._log_write(
            f"Saved session expired — please sign in again. ({text})")

    def _sign_out(self):
        clear_credentials()
        self._token_var.set("")
        self._set_connected(None)   # clears the inline status and shows the CTA
        self._log_write("Signed out; saved session forgotten.")

    def _get_dates(self):
        def to_api(s):
            """Convert DD/MM/YYYY user input to YYYY-MM-DD for the iNat API."""
            if not s:
                return None
            parts = s.strip().split("/")
            if len(parts) == 3:
                d, m, y = parts
                return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
            return s  # pass through if format is unexpected

        if self._date_mode.get() == "single":
            d = self._date_var.get().strip()
            api_d = to_api(d) if d else None
            return api_d, api_d
        return (
            to_api(self._date_start_var.get().strip()),
            to_api(self._date_end_var.get().strip()),
        )

    def _validate(self):
        if not self._token_var.get().strip():
            messagebox.showwarning(
                "Token required",
                "Paste your API token (or load from a file) before proceeding.\n\n"
                f"Get one at: {WEB}/users/api_token",
            )
            return False
        if not self._user_var.get().strip():
            messagebox.showwarning(
                "Username required",
                "Enter your iNaturalist username before proceeding.",
            )
            return False
        try:
            int(self._field_id_var.get())
        except ValueError:
            messagebox.showwarning("Invalid field ID",
                                   "Field ID must be a whole number.")
            return False
        try:
            re.compile(self._regex_var.get())
        except re.error as exc:
            messagebox.showwarning("Invalid regex",
                                   f"Voucher regex error:\n{exc}")
            return False
        _date_re = re.compile(r"^\d{2}/\d{2}/\d{4}$")
        if self._date_mode.get() == "single":
            date_inputs = [self._date_var.get().strip()]
        else:
            date_inputs = [self._date_start_var.get().strip(),
                           self._date_end_var.get().strip()]
        for d in date_inputs:
            if d and not _date_re.match(d):
                messagebox.showwarning("Invalid date format",
                                       f"'{d}' is not a valid date.\n\n"
                                       "Please use DD/MM/YYYY, e.g. 04/10/2025")
                return False
        d1, d2 = self._get_dates()
        if not d1 and not d2:
            messagebox.showwarning("Date required",
                                   "Enter a date or date range.")
            return False
        return True

    def _log_write(self, text):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")
        self._update_log_count()

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._update_log_count()

    def _update_log_count(self):
        # Text widgets always carry a trailing empty line; subtract it.
        n = max(0, int(self._log.index("end-1c").split(".")[0]) - 1)
        self._log_lines.configure(text=f"{n} line{'' if n == 1 else 's'}")

    def _tree_insert(self, row):
        index = len(self._tree.get_children(""))
        self._tree.insert(
            "", "end",
            iid=str(row["observation_id"]),
            values=(
                row["observation_id"],
                row["taxon"],
                row["upload_date"],
                row["detected_voucher"] or "—",
                row["current_value"] or "—",
                row["action"].upper(),
                row["reason"],
            ),
            tags=(self._action_tag(row, index),),
        )

    def _tree_refresh_row(self, row):
        iid = str(row["observation_id"])
        if not self._tree.exists(iid):
            return
        index = self._tree.index(iid)
        self._tree.item(
            iid,
            tags=(self._action_tag(row, index),),
            values=(
                row["observation_id"],
                row["taxon"],
                row["upload_date"],
                row["detected_voucher"] or "—",
                row["current_value"] or "—",
                row["action"].upper(),
                row["reason"],
            ),
        )

    def _sort_tree(self, col):
        reverse = (self._sort_col == col) and (not self._sort_reverse)
        self._sort_col = col
        self._sort_reverse = reverse
        data = [(self._tree.set(k, col), k)
                for k in self._tree.get_children("")]
        data.sort(reverse=reverse)
        for idx, (_, k) in enumerate(data):
            self._tree.move(k, "", idx)

    def _open_url(self, _=None):
        sel = self._tree.focus()
        if sel:
            webbrowser.open(f"{WEB}/observations/{sel}")

    def _set_busy(self, busy):
        if busy:
            self._btn_apply.set_enabled(False)
        else:
            n_update = sum(1 for r in self._rows if r["action"] == UPDATE)
            self._btn_apply.set_enabled(bool(n_update))

    def _set_preview_mode(self, mode):
        """Toggle the primary button between Preview run, Stop, and Stopping."""
        btn = self._btn_preview
        if mode == "stop":
            btn._enabled_palette = ("#ffffff", COL["danger"],
                                    COL["danger_press"])
            btn.configure(state="normal", text="Stop",
                          command=self._stop_preview, fg="#ffffff",
                          bg=COL["danger"], activebackground=COL["danger_press"],
                          cursor="hand2")
        elif mode == "stopping":
            btn.configure(state="disabled", text="Stopping…",
                          bg=COL["muted2"], disabledforeground="#ffffff",
                          cursor="arrow")
        else:  # "preview"
            btn._enabled_palette = ("#ffffff", COL["primary"],
                                    COL["primary_press"])
            btn.configure(state="normal", text="Preview run",
                          command=self._start_preview, fg="#ffffff",
                          bg=COL["primary"], activebackground=COL["primary_press"],
                          cursor="hand2")

    def _stop_preview(self):
        self._cancel.set()
        self._set_preview_mode("stopping")
        self._status_lbl.configure(text="Stopping…", fg=COL["text_med"])

    def _update_summary(self):
        counts = {UPDATE: 0, SKIP: 0, FLAG: 0}
        for r in self._rows:
            counts[r["action"]] += 1
        self._style_chip(self._chip_update, counts[UPDATE], "update",
                         COL["green_bg"], COL["green_text"])
        self._style_chip(self._chip_skip, counts[SKIP], "skip",
                         COL["skip_bg"], COL["skip_fg"])
        self._style_chip(self._chip_flag, counts[FLAG], "flag",
                         COL["flag_bg"], COL["flag_fg"])

    # ----------------------------------------------------------------------- #
    # Queue polling                                                             #
    # ----------------------------------------------------------------------- #
    def _poll(self):
        try:
            while True:
                msg = self._mq.get_nowait()
                kind = msg["kind"]
                if kind == "log":
                    self._log_write(msg["text"])
                elif kind == "connected":
                    self._set_connected(msg["login"])
                elif kind == "oauth_ok":
                    self._on_oauth_ok(msg)
                elif kind == "oauth_err":
                    self._on_oauth_err(msg["text"])
                elif kind == "oauth_refresh_failed":
                    self._on_refresh_failed(msg["text"])
                elif kind == "field_results":
                    self._on_field_results(msg)
                elif kind == "progress":
                    self._prog_bar.configure(
                        mode="determinate",
                        maximum=msg["total"],
                        value=msg["value"],
                    )
                    self._status_lbl.configure(text="Scanning…",
                                               fg=COL["text_med"])
                    self._count_lbl.configure(
                        text=f"{msg['value']} of {msg['total']} "
                             "observations scanned")
                elif kind == "spin_start":
                    self._prog_bar.configure(mode="indeterminate")
                    self._prog_bar.start(12)
                    self._status_lbl.configure(text=msg["text"],
                                               fg=COL["text_med"])
                    self._count_lbl.configure(text="")
                elif kind == "spin_stop":
                    self._prog_bar.stop()
                    self._prog_bar.configure(
                        mode="determinate", maximum=100, value=0)
                elif kind == "row":
                    self._tree_insert(msg["row"])
                elif kind == "row_refresh":
                    self._tree_refresh_row(msg["row"])
                elif kind == "preview_done":
                    self._on_preview_done(msg["rows"],
                                          msg.get("cancelled", False))
                elif kind == "apply_done":
                    self._on_apply_done(msg["applied"], msg["failed"])
                elif kind == "error":
                    messagebox.showerror("Error", msg["text"])
                    self._set_busy(False)
                    self._set_preview_mode("preview")
                    self._prog_bar.stop()
                    self._prog_bar.configure(
                        mode="determinate", maximum=100, value=0)
                    self._status_lbl.configure(text="Error", fg=COL["flag_fg"])
                    self._count_lbl.configure(text="")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ----------------------------------------------------------------------- #
    # OCR engine availability / one-click install                             #
    # ----------------------------------------------------------------------- #
    def _prompt_missing_ocr(self, engine, message):
        """Ask what to do when the OCR engine isn't available.

        Returns "install", "qr" (scan QR-only this run), or "cancel".
        The bundled engine can be pip-installed in-app; Tesseract needs a
        system install, so there we just offer QR-only or cancel.
        """
        import importlib.util
        # In a frozen build (PyInstaller) the OCR engine is bundled, so this
        # branch shouldn't be reached — but never offer pip there: there's no
        # pip, and the bundle's site-packages is read-only.
        can_install = (engine == OCR_RAPIDOCR
                       and not getattr(sys, "frozen", False)
                       and importlib.util.find_spec("pip") is not None)
        if not can_install:
            if messagebox.askyesno(
                    "OCR engine unavailable",
                    message + "\n\nScan QR codes only for this run?"):
                return "qr"
            return "cancel"

        bg = COL["card_bg"]
        dlg = tk.Toplevel(self)
        dlg.title("OCR engine not installed")
        dlg.configure(bg=bg)
        dlg.transient(self)
        dlg.resizable(False, False)
        wrap = tk.Frame(dlg, bg=bg)
        wrap.pack(fill="both", expand=True, padx=22, pady=20)
        tk.Label(wrap, text="Built-in OCR isn't installed yet", bg=bg,
                 fg=COL["text"], font=(F["title"][0], 12, "bold")).pack(
            anchor="w")
        tk.Label(wrap, text="The OCR fallback needs a one-time download "
                            "(~tens of MB). I can install it for you now, or "
                            "you can scan QR codes only for this run.",
                 bg=bg, fg=COL["text_soft"], font=F["body"], justify="left",
                 wraplength=380).pack(anchor="w", pady=(8, 16))
        btns = tk.Frame(wrap, bg=bg)
        btns.pack(fill="x")

        choice = {"value": "cancel"}

        def pick(value):
            choice["value"] = value
            dlg.destroy()

        FlatButton(btns, text="Install now", command=lambda: pick("install"),
                   fg="#ffffff", bg=COL["primary"], active=COL["primary_press"],
                   disabled_fg="#ffffff", disabled_bg=COL["muted2"],
                   font=F["btn"], padx=18).pack(side="left")
        self._secondary_btn(btns, "Scan QR only",
                            lambda: pick("qr")).pack(side="left", padx=(10, 0))
        FlatButton(btns, text="Cancel", command=lambda: pick("cancel"),
                   fg=COL["muted"], bg=bg, active=COL["track"],
                   disabled_fg=COL["muted2"], disabled_bg=bg,
                   font=F["btn"], padx=14).pack(side="right")

        self._center_over_parent(dlg)
        dlg.grab_set()
        self.wait_window(dlg)
        return choice["value"]

    def _install_ocr_engine_blocking(self):
        """Pip-install the bundled OCR engine into this interpreter, showing a
        modal progress dialog. Returns True on success.

        Using `sys.executable -m pip` guarantees the package lands in the same
        environment that's running the app — the user never has to find the
        right venv or open a terminal.
        """
        import importlib

        bg = COL["card_bg"]
        dlg = tk.Toplevel(self)
        dlg.title("Installing OCR engine")
        dlg.configure(bg=bg)
        dlg.transient(self)
        dlg.resizable(False, False)
        wrap = tk.Frame(dlg, bg=bg)
        wrap.pack(fill="both", expand=True, padx=22, pady=20)
        tk.Label(wrap, text="Installing the built-in OCR engine…", bg=bg,
                 fg=COL["text"], font=F["label"]).pack(anchor="w")
        tk.Label(wrap, text="Downloading rapidocr-onnxruntime (one time). "
                            "This can take a minute.",
                 bg=bg, fg=COL["muted"], font=F["help"], justify="left",
                 wraplength=360).pack(anchor="w", pady=(6, 12))
        bar = ttk.Progressbar(wrap, length=340, mode="indeterminate",
                              style="Green.Horizontal.TProgressbar")
        bar.pack(fill="x")
        bar.start(12)

        result = {"ok": False, "out": ""}

        def worker():
            import subprocess
            base = [sys.executable, "-m", "pip", "install",
                    "rapidocr-onnxruntime"]
            # If a plain install fails (commonly a permissions error on a
            # system Python), retry with --user, which writes to the user's
            # own site-packages without admin. --user is invalid inside a
            # virtualenv, so only add it when we're not in one.
            in_venv = sys.prefix != sys.base_prefix
            attempts = [base] if in_venv else [base, base[:4] + ["--user"]
                                               + base[4:]]
            out = ""
            ok = False
            try:
                for cmd in attempts:
                    proc = subprocess.run(cmd, capture_output=True, text=True)
                    out += (proc.stdout or "") + (proc.stderr or "")
                    if proc.returncode == 0:
                        ok = True
                        break
            except Exception as exc:
                out += str(exc)
            result["ok"], result["out"] = ok, out
            self.after(0, dlg.destroy)

        self._center_over_parent(dlg)
        dlg.grab_set()
        threading.Thread(target=worker, daemon=True).start()
        self.wait_window(dlg)

        importlib.invalidate_caches()   # so the fresh install is importable
        if not result["ok"]:
            tail = (result["out"].strip().splitlines() or ["(no output)"])[-1]
            messagebox.showerror(
                "Install failed",
                "Couldn't install the OCR engine automatically.\n\n"
                f"{tail}\n\nYou can install it yourself with:\n"
                "    pip install rapidocr-onnxruntime")
        return result["ok"]

    def _center_over_parent(self, dlg):
        dlg.update_idletasks()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        pw, ph = self.winfo_width(), self.winfo_height()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        dlg.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")

    # ----------------------------------------------------------------------- #
    # Preview                                                                  #
    # ----------------------------------------------------------------------- #
    def _start_preview(self):
        if not self._validate():
            return

        use_ocr      = self._ocr_var.get()
        ocr_engine   = self._ocr_engine_var.get()
        tess_cmd     = (self._tess_var.get().strip()
                        if use_ocr and ocr_engine == OCR_TESSERACT else None)

        # Pre-flight: if OCR is on but its engine isn't available, handle it
        # once up front — offer a one-click install (bundled engine) or a
        # QR-only run — instead of flagging every QR failure with a cryptic
        # "engine not installed".
        ocr_downgraded = False
        if use_ocr:
            ok, msg = ocr_engine_available(ocr_engine, tess_cmd)
            if not ok:
                choice = self._prompt_missing_ocr(ocr_engine, msg)
                if choice == "cancel":
                    return
                if choice == "install":
                    installed = (self._install_ocr_engine_blocking()
                                 and ocr_engine_available(ocr_engine,
                                                          tess_cmd)[0])
                    if not installed:
                        if not messagebox.askyesno(
                                "OCR still unavailable",
                                "The OCR engine isn't ready. Scan QR codes "
                                "only for this run?"):
                            return
                        use_ocr = False
                        ocr_downgraded = True
                else:  # "qr"
                    use_ocr = False
                    ocr_downgraded = True

        self._clear()
        self._cancel.clear()
        self._set_busy(True)
        self._set_preview_mode("stop")
        if ocr_downgraded:
            self._log_write("OCR engine unavailable — scanning QR codes only "
                            "for this run.")

        token        = self._token_var.get().strip()
        user         = self._user_var.get().strip()
        field_id     = int(self._field_id_var.get())
        voucher_re   = re.compile(self._regex_var.get(), re.IGNORECASE)
        allow_ow     = self._overwrite_var.get()
        d1, d2       = self._get_dates()

        threading.Thread(
            target=self._preview_worker,
            args=(token, user, field_id, voucher_re, allow_ow,
                  use_ocr, ocr_engine, tess_cmd, d1, d2),
            daemon=True,
        ).start()

    def _preview_worker(self, token, user, field_id, voucher_re,
                        allow_overwrite, use_ocr, ocr_engine, tess_cmd,
                        d1, d2):
        q = self._mq
        try:
            client = INatClient(token=token)

            login = client.verify_token()
            if not login:
                q.put({"kind": "error",
                       "text": "Token is invalid or expired.\n\n"
                               f"Get a fresh one at:\n{WEB}/users/api_token"})
                return
            q.put({"kind": "log", "text": f"Authenticated as {login}"})
            q.put({"kind": "connected", "login": login})
            if use_ocr:
                engine_name = ("Tesseract" if ocr_engine == OCR_TESSERACT
                               else "built-in")
                q.put({"kind": "log",
                       "text": f"OCR fallback enabled ({engine_name})."})

            window = d1 if d1 == d2 else f"{d1} to {d2}"
            q.put({"kind": "log",
                   "text": f"Fetching observations for {user}  "
                           f"(uploaded {window})..."})
            q.put({"kind": "spin_start", "text": "Fetching..."})

            obs_list = list(client.fetch_observations(user, d1, d2))
            total = len(obs_list)
            q.put({"kind": "spin_stop"})

            if self._cancel.is_set():
                q.put({"kind": "log", "text": "Preview stopped before scanning."})
                q.put({"kind": "preview_done", "rows": [], "cancelled": True})
                return

            if not total:
                q.put({"kind": "log", "text": "No matching observations found."})
                q.put({"kind": "preview_done", "rows": []})
                return

            q.put({"kind": "log",
                   "text": f"Found {total} observation(s). Scanning photos...\n"})

            # Scan observations concurrently: photo downloads (I/O) overlap with
            # QR/OCR decoding (CPU/subprocess) instead of running one-at-a-time.
            # No REQUEST_PAUSE here — these are CDN photo fetches, not the
            # rate-limited write API.  Results are stored by original index so
            # `rows` stays in observation order even though they finish out of
            # order; the decode itself is unchanged, so detection is identical.
            #
            # Cancellation: when the user hits Stop we break out of the
            # completion loop, cancel any not-yet-started futures, and return
            # whatever finished.  In-flight decodes run to completion (one per
            # worker) but their results are simply ignored.
            rows = [None] * total
            done = 0
            cancelled = False
            pool = ThreadPoolExecutor(max_workers=SCAN_WORKERS)
            future_to_idx = {
                pool.submit(build_row, client, obs, field_id, voucher_re,
                            allow_overwrite, use_ocr, tess_cmd,
                            ocr_engine): idx
                for idx, obs in enumerate(obs_list)
            }
            try:
                for fut in as_completed(future_to_idx):
                    if self._cancel.is_set():
                        cancelled = True
                        break
                    idx = future_to_idx[fut]
                    obs = obs_list[idx]
                    try:
                        row = fut.result()
                    except Exception as exc:
                        row = {
                            "observation_id": obs.get("id"),
                            "url": f"{WEB}/observations/{obs.get('id')}",
                            "taxon": taxon_label(obs),
                            "upload_date": upload_date(obs),
                            "detected_voucher": None, "current_value": None,
                            "field_state": "empty", "action": FLAG,
                            "reason": f"scan_error: {exc}",
                            "ofv_id": None, "raw_qr": None, "raw_ocr": None,
                        }
                    rows[idx] = row
                    done += 1
                    q.put({"kind": "progress", "value": done, "total": total})
                    q.put({"kind": "row", "row": row})
                    ocr_note = " [OCR]" if "ocr" in row.get("reason", "") else ""
                    q.put({
                        "kind": "log",
                        "text": (
                            f"  [{done:>3}/{total}]  #{obs.get('id')}  "
                            f"{row['taxon'][:36]}  ->  "
                            f"{row['action'].upper()} ({row['reason']}){ocr_note}"
                            + (f"  |  {row['detected_voucher']}"
                               if row["detected_voucher"] else "")
                        ),
                    })
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            if cancelled:
                q.put({"kind": "log",
                       "text": f"\nPreview stopped — {done} of {total} "
                               "observation(s) scanned."})
            scanned = [r for r in rows if r is not None]
            q.put({"kind": "preview_done", "rows": scanned,
                   "cancelled": cancelled})

        except Exception as exc:
            q.put({"kind": "error", "text": str(exc)})

    def _on_preview_done(self, rows, cancelled=False):
        self._rows = rows
        self._update_summary()
        counts = {UPDATE: 0, SKIP: 0, FLAG: 0}
        ocr_count = 0
        for r in rows:
            counts[r["action"]] += 1
            if r["action"] == UPDATE and "ocr" in r.get("reason", ""):
                ocr_count += 1
        ocr_note = f" ({ocr_count} via OCR)" if ocr_count else ""
        verb = "Preview stopped" if cancelled else "Preview complete"
        self._log_write(
            f"\n{verb} — "
            f"{counts[UPDATE]} update{ocr_note}, "
            f"{counts[SKIP]} skip, "
            f"{counts[FLAG]} flag."
        )
        n = len(rows)
        if cancelled:
            self._status_lbl.configure(text="Preview stopped",
                                       fg=COL["text_med"])
            self._count_lbl.configure(text=f"{n} scanned before stop")
        else:
            self._status_lbl.configure(text="✓ Preview complete",
                                       fg=COL["green_text"])
            self._prog_bar.configure(mode="determinate", maximum=max(n, 1),
                                     value=n)
            self._count_lbl.configure(text=f"{n} of {n} observations scanned")
        self._set_preview_mode("preview")
        self._set_busy(False)

    # ----------------------------------------------------------------------- #
    # Apply                                                                    #
    # ----------------------------------------------------------------------- #
    def _start_apply(self):
        to_apply = [r for r in self._rows if r["action"] == UPDATE]
        if not to_apply:
            messagebox.showinfo("Nothing to apply",
                                "No rows are marked for update.")
            return

        ow_count  = sum(1 for r in to_apply if r["current_value"])
        ocr_count = sum(1 for r in to_apply if "ocr" in r.get("reason", ""))
        prompt = f"Apply {len(to_apply)} update(s) to iNaturalist?"
        if ocr_count:
            prompt += (f"\n\n{ocr_count} row(s) were identified via OCR "
                       "(light blue). Verify these before applying if accuracy "
                       "is critical.")
        if ow_count:
            prompt += f"\n\n{ow_count} row(s) will overwrite an existing value."
        if not messagebox.askyesno("Confirm", prompt):
            return

        self._set_busy(True)
        self._btn_preview.set_enabled(False)   # no preview while applying
        self._log_write("\nApplying updates...")

        token    = self._token_var.get().strip()
        field_id = int(self._field_id_var.get())
        allow_ow = self._overwrite_var.get()

        threading.Thread(
            target=self._apply_worker,
            args=(token, field_id, allow_ow, to_apply),
            daemon=True,
        ).start()

    def _apply_worker(self, token, field_id, allow_overwrite, to_apply):
        q = self._mq
        client  = INatClient(token=token)
        total   = len(to_apply)
        applied = failed = 0

        for i, r in enumerate(to_apply, 1):
            q.put({"kind": "progress", "value": i, "total": total})
            obs_id  = r["observation_id"]
            voucher = r["detected_voucher"]
            try:
                if r["ofv_id"] and allow_overwrite:
                    client.update_ofv(r["ofv_id"], obs_id, field_id, voucher)
                else:
                    client.create_ofv(obs_id, field_id, voucher)
                applied += 1
                r["action"]        = SKIP
                r["reason"]        = "applied"
                r["current_value"] = voucher
                q.put({"kind": "log",
                       "text": f"  OK    #{obs_id}  {voucher}"})
                q.put({"kind": "row_refresh", "row": r})
            except requests.RequestException as exc:
                failed += 1
                q.put({"kind": "log",
                       "text": f"  FAIL  #{obs_id}  {voucher}  —  {exc}"})
            time.sleep(REQUEST_PAUSE)

        q.put({"kind": "apply_done",
               "applied": applied, "failed": failed})

    def _on_apply_done(self, applied, failed):
        self._status_lbl.configure(
            text="✓ Apply complete" if not failed else "Apply finished",
            fg=COL["green_text"] if not failed else COL["flag_fg"])
        self._count_lbl.configure(
            text=f"{applied} written"
                 + (f", {failed} failed" if failed else ""))
        self._log_write(
            f"\nApply complete — {applied} written, {failed} failed.")
        self._update_summary()
        self._set_busy(False)
        self._set_preview_mode("preview")

    # ----------------------------------------------------------------------- #
    # Export                                                                   #
    # ----------------------------------------------------------------------- #
    def _export_csv(self):
        if not self._rows:
            messagebox.showinfo("No data", "Run a preview first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            title="Save queue as CSV",
        )
        if path:
            export_csv(self._rows, path)
            self._log_write(f"Exported to: {path}")

    # ----------------------------------------------------------------------- #
    # Clear                                                                    #
    # ----------------------------------------------------------------------- #
    def _clear(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._rows = []
        self._log_clear()
        self._reset_chips()
        self._btn_apply.set_enabled(False)
        self._prog_bar.stop()
        self._prog_bar.configure(mode="determinate", maximum=100, value=0)
        self._status_lbl.configure(text="Ready", fg=COL["muted"])
        self._count_lbl.configure(text="")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = VoucherSyncApp()
    app.mainloop()