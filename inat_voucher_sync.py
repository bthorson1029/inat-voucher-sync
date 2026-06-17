#!/usr/bin/env python3
"""
inat_voucher_sync.py

Self-contained desktop GUI for syncing QR-decoded voucher IDs to iNaturalist
observation fields.  All backend logic is embedded — no other project files
are required.

Requirements:
  pip install requests opencv-python numpy

Optional OCR fallback (reads the printed voucher ID when the QR code fails):
  pip install pytesseract
  # Also install the Tesseract engine itself:
  # Windows: https://github.com/UB-Mannheim/tesseract/wiki  (grab the installer)
  # macOS:   brew install tesseract
  # Debian:  sudo apt-get install tesseract-ocr

Run:
  python inat_voucher_sync.py
"""

import csv
import os
import queue
import re
import sys
import time
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

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
USER_AGENT = "inat-voucher-sync/1.0 (personal voucher tooling)"

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
# The default accepts a 2–3 letter prefix, a hyphen, and 3–4 digits, e.g.
# "BT-001", "ABC-1234". The required hyphen, fixed digit count, and word
# boundaries keep OCR noise from being mistaken for a voucher; widen or
# narrow it to match your own scheme.
DEFAULT_VOUCHER_RE = r"\b[A-Za-z]{2,3}-\d{3,4}\b"
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
    ("Prefix-Number", DEFAULT_VOUCHER_RE),              # BT-001, ABC-1234
    ("Numbers only",  r"\b\d{3,6}\b"),                  # 00421, 123456
    # Alphanumeric: 4–10 chars containing at least one letter and one digit,
    # so it won't collapse into "any word" or "any number".
    ("Alphanumeric",
     r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10}\b"),
    ("Custom", None),
]
DEFAULT_VOUCHER_FORMAT = VOUCHER_FORMATS[0][0]

UPDATE = "update"
SKIP   = "skip"
FLAG   = "flag"


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


def ocr_fallback(img, cache, voucher_re, tesseract_cmd=None):
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
              use_ocr=False, tesseract_cmd=None):
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
                img, cache, voucher_re, tesseract_cmd)
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
# GUI — colour tags
# ---------------------------------------------------------------------------
TAG_UPDATE = "tag_update"
TAG_SKIP   = "tag_skip"
TAG_FLAG   = "tag_flag"
TAG_OCR    = "tag_ocr"      # UPDATE row whose voucher came from OCR, not QR

ROW_COLOR = {
    TAG_UPDATE: "#d4edda",   # soft green  — QR-confirmed update
    TAG_OCR:    "#cce5ff",   # light blue  — OCR-derived update (review recommended)
    TAG_SKIP:   "#f0f0f0",   # light grey  — no action needed
    TAG_FLAG:   "#fff3cd",   # amber       — needs attention
}

ACTION_TAG = {UPDATE: TAG_UPDATE, SKIP: TAG_SKIP, FLAG: TAG_FLAG}
# OCR rows use TAG_OCR; resolved per-row by _action_tag() rather than this map.


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class VoucherSyncApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("iNaturalist Voucher Sync")
        self.geometry("1120x820")
        self.minsize(860, 620)

        self._rows   = []
        self._mq     = queue.Queue()
        self._worker = None

        self._build_ui()
        self._poll()
        self._load_env_token()

    # ----------------------------------------------------------------------- #
    # UI                                                                       #
    # ----------------------------------------------------------------------- #
    def _build_ui(self):
        cfg = ttk.LabelFrame(self, text="Configuration", padding=10)
        cfg.pack(fill="x", padx=12, pady=(10, 4))
        self._build_config(cfg)

        btn = ttk.Frame(self, padding=(12, 0))
        btn.pack(fill="x")
        self._build_buttons(btn)

        prog = ttk.Frame(self, padding=(12, 4))
        prog.pack(fill="x")
        self._prog_lbl = ttk.Label(prog, text="", width=24)
        self._prog_lbl.pack(side="left")
        self._prog_bar = ttk.Progressbar(prog, length=360, mode="determinate")
        self._prog_bar.pack(side="left", padx=(6, 0))

        self._summary_var = tk.StringVar()
        ttk.Label(self, textvariable=self._summary_var,
                  font=("TkDefaultFont", 9, "bold"),
                  foreground="#333").pack(anchor="w", padx=14, pady=(0, 4))

        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=12, pady=(0, 10))

        tree_frame = ttk.LabelFrame(paned, text="Preview queue", padding=4)
        paned.add(tree_frame, weight=3)
        self._build_tree(tree_frame)

        log_frame = ttk.LabelFrame(paned, text="Log", padding=4)
        paned.add(log_frame, weight=1)
        self._log = scrolledtext.ScrolledText(
            log_frame, height=8, state="disabled",
            font=("Consolas", 9), wrap="word",
        )
        self._log.pack(fill="both", expand=True)

    def _build_config(self, parent):
        for col in (1, 3, 5):
            parent.columnconfigure(col, weight=1)

        # Token row
        ttk.Label(parent, text="API Token:").grid(
            row=0, column=0, sticky="e", padx=(0, 6), pady=4)
        self._token_var = tk.StringVar()
        ttk.Entry(parent, textvariable=self._token_var, show="*",
                  width=56).grid(row=0, column=1, columnspan=4,
                                 sticky="ew", pady=4)
        ttk.Button(parent, text="Load from file",
                   command=self._load_token_file).grid(
            row=0, column=5, padx=(8, 0), pady=4, sticky="w")

        # Username / field id / regex row
        ttk.Label(parent, text="Username:").grid(
            row=1, column=0, sticky="e", padx=(0, 6), pady=4)
        self._user_var = tk.StringVar(value=DEFAULT_USER)
        ttk.Entry(parent, textvariable=self._user_var, width=20).grid(
            row=1, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="Field ID:").grid(
            row=1, column=2, sticky="e", padx=(16, 6), pady=4)
        self._field_id_var = tk.StringVar(value=str(DEFAULT_FIELD_ID))
        ttk.Entry(parent, textvariable=self._field_id_var, width=8).grid(
            row=1, column=3, sticky="w", pady=4)

        # Voucher format row — pick a preset or "Custom" to type a regex.
        ttk.Label(parent, text="Voucher format:").grid(
            row=2, column=0, sticky="e", padx=(0, 6), pady=4)
        vf = ttk.Frame(parent)
        vf.grid(row=2, column=1, columnspan=5, sticky="w", pady=4)

        self._format_var = tk.StringVar(value=DEFAULT_VOUCHER_FORMAT)
        for name, _pat in VOUCHER_FORMATS:
            ttk.Radiobutton(vf, text=name, variable=self._format_var,
                            value=name,
                            command=self._on_format_change).pack(
                side="left", padx=(0, 10))

        self._regex_var = tk.StringVar(value=DEFAULT_VOUCHER_RE)
        self._regex_entry = ttk.Entry(vf, textvariable=self._regex_var,
                                      width=26)
        self._regex_entry.pack(side="left", padx=(6, 0))

        # Date row
        ttk.Label(parent, text="Date filter:").grid(
            row=3, column=0, sticky="e", padx=(0, 6), pady=4)
        df = ttk.Frame(parent)
        df.grid(row=3, column=1, columnspan=5, sticky="w", pady=4)

        self._date_mode = tk.StringVar(value="single")
        ttk.Radiobutton(df, text="Single date", variable=self._date_mode,
                        value="single",
                        command=self._toggle_dates).pack(side="left")
        ttk.Radiobutton(df, text="Date range", variable=self._date_mode,
                        value="range",
                        command=self._toggle_dates).pack(
            side="left", padx=(12, 0))

        ttk.Label(df, text="   Date:").pack(side="left")
        self._date_var = tk.StringVar()
        self._date_entry = ttk.Entry(df, textvariable=self._date_var, width=12)
        self._date_entry.pack(side="left", padx=(4, 0))

        self._lbl_start = ttk.Label(df, text="   Start:")
        self._lbl_start.pack(side="left")
        self._date_start_var = tk.StringVar()
        self._date_start_entry = ttk.Entry(
            df, textvariable=self._date_start_var, width=12)
        self._date_start_entry.pack(side="left", padx=(4, 0))

        ttk.Label(df, text="   End:").pack(side="left")
        self._date_end_var = tk.StringVar()
        self._date_end_entry = ttk.Entry(
            df, textvariable=self._date_end_var, width=12)
        self._date_end_entry.pack(side="left", padx=(4, 0))

        ttk.Label(df, text="  (DD/MM/YYYY)",
                  foreground="#888").pack(side="left")

        # Options row
        self._overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(parent, text="Allow overwrite of existing values",
                        variable=self._overwrite_var).grid(
            row=4, column=1, columnspan=4, sticky="w", pady=(2, 4))

        # OCR fallback row
        ttk.Label(parent, text="OCR fallback:").grid(
            row=5, column=0, sticky="e", padx=(0, 6), pady=4)
        ocr_frame = ttk.Frame(parent)
        ocr_frame.grid(row=5, column=1, columnspan=5, sticky="w", pady=4)

        self._ocr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ocr_frame,
                        text="Enable (pytesseract)  —  used when QR fails",
                        variable=self._ocr_var,
                        command=self._toggle_ocr).pack(side="left")

        ttk.Label(ocr_frame, text="    Tesseract path:").pack(side="left")
        self._tess_var = tk.StringVar()
        self._tess_entry = ttk.Entry(ocr_frame, textvariable=self._tess_var,
                                     width=36, state="disabled")
        self._tess_entry.pack(side="left", padx=(4, 0))
        self._tess_browse_btn = ttk.Button(ocr_frame, text="Browse",
                                           command=self._browse_tesseract,
                                           state="disabled")
        self._tess_browse_btn.pack(side="left", padx=(4, 0))

        # Auto-populate the common Windows path if it exists.
        if os.path.isfile(_WIN_TESS_DEFAULT):
            self._tess_var.set(_WIN_TESS_DEFAULT)

        ttk.Label(ocr_frame,
                  text="  (leave blank to use PATH)",
                  foreground="#888").pack(side="left")

        self._toggle_dates()
        self._on_format_change()

    def _on_format_change(self):
        """Apply the selected voucher-format preset, or unlock the regex box
        for the Custom option."""
        pattern = dict(VOUCHER_FORMATS).get(self._format_var.get())
        if pattern is None:                      # Custom
            self._regex_entry.configure(state="normal")
        else:
            self._regex_var.set(pattern)
            self._regex_entry.configure(state="readonly")

    def _toggle_dates(self):
        single = self._date_mode.get() == "single"
        self._date_entry.configure(state="normal" if single else "disabled")
        rs = "disabled" if single else "normal"
        self._date_start_entry.configure(state=rs)
        self._date_end_entry.configure(state=rs)

    def _toggle_ocr(self):
        state = "normal" if self._ocr_var.get() else "disabled"
        self._tess_entry.configure(state=state)
        self._tess_browse_btn.configure(state=state)

    def _browse_tesseract(self):
        path = filedialog.askopenfilename(
            title="Select tesseract executable",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if path:
            self._tess_var.set(path)

    def _build_buttons(self, parent):
        self._btn_preview = ttk.Button(parent, text="Preview", width=16,
                                       command=self._start_preview)
        self._btn_preview.pack(side="left", padx=(0, 8), pady=6)

        self._btn_apply = ttk.Button(parent, text="Apply Updates", width=16,
                                     command=self._start_apply,
                                     state="disabled")
        self._btn_apply.pack(side="left", padx=(0, 8), pady=6)

        ttk.Button(parent, text="Export CSV", width=12,
                   command=self._export_csv).pack(
            side="left", padx=(0, 8), pady=6)

        ttk.Button(parent, text="Clear", width=8,
                   command=self._clear).pack(side="left", pady=6)

    def _build_tree(self, parent):
        cols = ("obs_id", "taxon", "uploaded", "detected",
                "current", "action", "reason")
        self._tree = ttk.Treeview(parent, columns=cols, show="headings",
                                  selectmode="browse")
        headings = {
            "obs_id":   "Obs ID",
            "taxon":    "Taxon",
            "uploaded": "Uploaded",
            "detected": "Detected Voucher",
            "current":  "Current Value",
            "action":   "Action",
            "reason":   "Reason",
        }
        widths = {
            "obs_id": 90, "taxon": 250, "uploaded": 90,
            "detected": 120, "current": 110,
            "action": 72, "reason": 190,
        }
        for col in cols:
            self._tree.heading(
                col, text=headings[col],
                command=lambda c=col: self._sort_tree(c))
            self._tree.column(col, width=widths[col], minwidth=50,
                              stretch=(col == "taxon"))

        # Tk 8.6.9 regression: the Treeview style map contains a
        # ('!disabled', '!selected', ...) entry that forces every normal row
        # to the default background, overriding per-row tag colours. Strip it
        # so tag_configure backgrounds render. Fixed upstream in Tk 8.6.10;
        # this filter is harmless on versions that don't have the bad entry.
        style = ttk.Style()

        def _fixed_map(option):
            return [e for e in style.map("Treeview", query_opt=option)
                    if e[:2] != ("!disabled", "!selected")]

        style.map("Treeview",
                  foreground=_fixed_map("foreground"),
                  background=_fixed_map("background"))

        for tag, bg in ROW_COLOR.items():
            self._tree.tag_configure(tag, background=bg)

        vsb = ttk.Scrollbar(parent, orient="vertical",
                            command=self._tree.yview)
        hsb = ttk.Scrollbar(parent, orient="horizontal",
                            command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set,
                             xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        self._tree.bind("<Double-1>", self._open_url)
        self._tree.bind("<Return>",   self._open_url)
        self._sort_reverse = False
        self._sort_col     = None

    @staticmethod
    def _action_tag(row):
        """Return the colour tag for a row, distinguishing OCR updates."""
        if row["action"] == UPDATE and "ocr" in row.get("reason", ""):
            return TAG_OCR
        return ACTION_TAG.get(row["action"], TAG_SKIP)

    # ----------------------------------------------------------------------- #
    # Helpers                                                                  #
    # ----------------------------------------------------------------------- #
    def _load_env_token(self):
        t = os.environ.get("INAT_API_TOKEN", "").strip()
        if t:
            self._token_var.set(t)
            self._log_write("Token loaded from INAT_API_TOKEN environment variable.")

    def _load_token_file(self):
        path = filedialog.askopenfilename(
            title="Select token file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            with open(path, encoding="utf-8") as fh:
                self._token_var.set(fh.read().strip())
            self._log_write(f"Token loaded from: {path}")

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

    def _log_clear(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _tree_insert(self, row):
        self._tree.insert(
            "", "end",
            iid=str(row["observation_id"]),
            values=(
                row["observation_id"],
                row["taxon"],
                row["upload_date"],
                row["detected_voucher"] or "",
                row["current_value"] or "",
                row["action"].upper(),
                row["reason"],
            ),
            tags=(self._action_tag(row),),
        )

    def _tree_refresh_row(self, row):
        iid = str(row["observation_id"])
        if not self._tree.exists(iid):
            return
        self._tree.item(
            iid,
            tags=(self._action_tag(row),),
            values=(
                row["observation_id"],
                row["taxon"],
                row["upload_date"],
                row["detected_voucher"] or "",
                row["current_value"] or "",
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
        state = "disabled" if busy else "normal"
        self._btn_preview.configure(state=state)
        if busy:
            self._btn_apply.configure(state="disabled")
        else:
            n_update = sum(1 for r in self._rows if r["action"] == UPDATE)
            self._btn_apply.configure(
                state="normal" if n_update else "disabled")

    def _update_summary(self):
        counts  = {UPDATE: 0, SKIP: 0, FLAG: 0}
        ocr_count = 0
        for r in self._rows:
            counts[r["action"]] += 1
            if r["action"] == UPDATE and "ocr" in r.get("reason", ""):
                ocr_count += 1
        ocr_note = f" ({ocr_count} via OCR)" if ocr_count else ""
        self._summary_var.set(
            f"  {counts[UPDATE]} update{ocr_note}   "
            f"{counts[SKIP]} skip   "
            f"{counts[FLAG]} flag   "
            f"({len(self._rows)} total)    "
            "Double-click a row to open in browser."
        )

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
                elif kind == "progress":
                    self._prog_bar.configure(
                        mode="determinate",
                        maximum=msg["total"],
                        value=msg["value"],
                    )
                    self._prog_lbl.configure(
                        text=f"{msg['value']}/{msg['total']}")
                elif kind == "spin_start":
                    self._prog_bar.configure(mode="indeterminate")
                    self._prog_bar.start(12)
                    self._prog_lbl.configure(text=msg["text"])
                elif kind == "spin_stop":
                    self._prog_bar.stop()
                    self._prog_bar.configure(
                        mode="determinate", maximum=100, value=0)
                    self._prog_lbl.configure(text=msg.get("text", ""))
                elif kind == "row":
                    self._tree_insert(msg["row"])
                elif kind == "row_refresh":
                    self._tree_refresh_row(msg["row"])
                elif kind == "preview_done":
                    self._on_preview_done(msg["rows"])
                elif kind == "apply_done":
                    self._on_apply_done(msg["applied"], msg["failed"])
                elif kind == "error":
                    messagebox.showerror("Error", msg["text"])
                    self._set_busy(False)
                    self._prog_bar.stop()
                    self._prog_bar.configure(
                        mode="determinate", maximum=100, value=0)
                    self._prog_lbl.configure(text="")
        except queue.Empty:
            pass
        self.after(80, self._poll)

    # ----------------------------------------------------------------------- #
    # Preview                                                                  #
    # ----------------------------------------------------------------------- #
    def _start_preview(self):
        if not self._validate():
            return
        self._clear()
        self._set_busy(True)

        token        = self._token_var.get().strip()
        user         = self._user_var.get().strip()
        field_id     = int(self._field_id_var.get())
        voucher_re   = re.compile(self._regex_var.get(), re.IGNORECASE)
        allow_ow     = self._overwrite_var.get()
        use_ocr      = self._ocr_var.get()
        tess_cmd     = self._tess_var.get().strip() if use_ocr else None
        d1, d2       = self._get_dates()

        threading.Thread(
            target=self._preview_worker,
            args=(token, user, field_id, voucher_re, allow_ow,
                  use_ocr, tess_cmd, d1, d2),
            daemon=True,
        ).start()

    def _preview_worker(self, token, user, field_id, voucher_re,
                        allow_overwrite, use_ocr, tess_cmd, d1, d2):
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
            if use_ocr:
                q.put({"kind": "log",
                       "text": "OCR fallback enabled (pytesseract)."})

            window = d1 if d1 == d2 else f"{d1} to {d2}"
            q.put({"kind": "log",
                   "text": f"Fetching observations for {user}  "
                           f"(uploaded {window})..."})
            q.put({"kind": "spin_start", "text": "Fetching..."})

            obs_list = list(client.fetch_observations(user, d1, d2))
            total = len(obs_list)
            q.put({"kind": "spin_stop"})

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
            rows = [None] * total
            done = 0
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                future_to_idx = {
                    pool.submit(build_row, client, obs, field_id, voucher_re,
                                allow_overwrite, use_ocr, tess_cmd): idx
                    for idx, obs in enumerate(obs_list)
                }
                for fut in as_completed(future_to_idx):
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

            q.put({"kind": "preview_done", "rows": rows})

        except Exception as exc:
            q.put({"kind": "error", "text": str(exc)})

    def _on_preview_done(self, rows):
        self._rows = rows
        self._update_summary()
        counts = {UPDATE: 0, SKIP: 0, FLAG: 0}
        ocr_count = 0
        for r in rows:
            counts[r["action"]] += 1
            if r["action"] == UPDATE and "ocr" in r.get("reason", ""):
                ocr_count += 1
        ocr_note = f" ({ocr_count} via OCR)" if ocr_count else ""
        self._log_write(
            f"\nPreview complete — "
            f"{counts[UPDATE]} update{ocr_note}, "
            f"{counts[SKIP]} skip, "
            f"{counts[FLAG]} flag."
        )
        self._prog_lbl.configure(text="Done")
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
        self._prog_lbl.configure(text="Done")
        self._log_write(
            f"\nApply complete — {applied} written, {failed} failed.")
        self._update_summary()
        self._set_busy(False)

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
        self._summary_var.set("")
        self._btn_apply.configure(state="disabled")
        self._prog_bar.stop()
        self._prog_bar.configure(mode="determinate", maximum=100, value=0)
        self._prog_lbl.configure(text="")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = VoucherSyncApp()
    app.mainloop()