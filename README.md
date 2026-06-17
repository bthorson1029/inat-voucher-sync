# iNaturalist Voucher Sync

A self-contained desktop GUI for syncing physical voucher IDs to
[iNaturalist](https://www.inaturalist.org) observation fields.

If you photograph a printed label (with a QR code and/or printed ID) alongside
each specimen, this tool reads the voucher ID straight out of your observation
photos and writes it into a custom observation field — so you never have to type
voucher numbers in by hand. 

Note: You must have the photo that contains the specimen and voucher as the **last** image in your observation.

## How it works

1. **Fetch** your observations from the iNaturalist API (optionally filtered by
   date).
2. **Read the voucher** from each observation's last photo:
   - First it tries to **decode a QR code** (OpenCV `QRCodeDetector`, with
     `pyzbar` as a secondary decoder). The label is located, deskewed, and
     upscaled before decoding so angled field shots still work.
   - If no QR is found, an optional **OCR fallback** (Tesseract) reads the
     printed ID off the label.
3. **Match** the decoded text against a voucher pattern (regex).
4. **Decide** an action per observation by comparing the detected voucher to the
   field's current value:

   | Color | Action | Meaning |
   |-------|--------|---------|
   | 🟢 Green | **Update** | QR-confirmed voucher; field was empty or being filled |
   | 🔵 Blue | **Update (review)** | Voucher came from OCR, not QR — double-check it |
   | ⚪ Grey | **Skip** | Field already holds the correct value |
   | 🟡 Amber | **Flag** | Detected voucher conflicts with an existing value |

5. **Review** the color-coded preview queue, then **Apply** the updates — or
   **export the queue to CSV** for record-keeping.

Nothing is written to iNaturalist until you click **Apply Updates** and confirm.

## Requirements

- Python 3.8+
- Required: `requests`, `opencv-python`, `numpy`

  ```bash
  pip install requests opencv-python numpy
  ```

- Optional, for QR decoding robustness: `pyzbar`
- Optional, for the OCR fallback: `pytesseract` **and** the Tesseract engine
  itself:

  ```bash
  pip install pytesseract
  ```

  - Windows: https://github.com/UB-Mannheim/tesseract/wiki
  - macOS: `brew install tesseract`
  - Debian/Ubuntu: `sudo apt-get install tesseract-ocr`

  If `tesseract` isn't on your PATH, you can point the app at the executable
  from the OCR fallback row in the GUI.

## Usage

```bash
python inat_voucher_sync.py
```

Then in the window:

1. **Paste your API token.** Get one at
   https://www.inaturalist.org/users/api_token. The token can also be loaded
   from a file or from the `INAT_API_TOKEN` environment variable.
2. Set your **username**, the **field ID** to write to, and pick the **voucher
   format** that matches your label (see below).
3. (Optional) Set a **date filter** and/or enable the **OCR fallback**.
4. Click **Preview** to build the queue, review it, then **Apply Updates**.

### Configuration defaults

Every setting is editable in the GUI. The starting values live in a
`USER CONFIGURATION` block at the top of `inat_voucher_sync.py` — set them there
if you'd rather not retype them each session.

| Setting | Default | Notes |
|---------|---------|-------|
| Username | *(blank)* | Your iNaturalist login — required |
| Field ID | `1907` | The public "Personal voucher number" observation field |
| Voucher format | Prefix-Number | See the format picker below |

To use a different observation field, find its numeric ID on iNaturalist and
update the **Field ID** value.

### Voucher format

The **Voucher format** picker chooses how a voucher ID is recognized in the
decoded QR/OCR text. Pick a preset, or **Custom** to type your own regex.
Matching is always case-insensitive, and the presets are word-bounded so stray
text from a photo with no label is unlikely to be mistaken for a voucher.

| Option | Pattern | Matches |
|--------|---------|---------|
| **Prefix-Number** (default) | `\b[A-Za-z]{2,3}-\d{3,4}\b` | 2–3 letters + hyphen + 3–4 digits — `BT-001`, `ABC-1234` |
| **Numbers only** | `\b\d{3,6}\b` | 3–6 digits — `00421`, `123456` |
| **Alphanumeric** | `\b…[A-Za-z0-9]{4,10}\b` | 4–10 chars with at least one letter *and* one digit — `AB12`, `A1B2C3` |
| **Custom** | *(your regex)* | Whatever you enter; the box unlocks when selected |

The default selection is set by `DEFAULT_VOUCHER_FORMAT` in the
`USER CONFIGURATION` block, and the preset patterns live in `VOUCHER_FORMATS`.

## Notes

- The tool reads tokens from input, a file, or `INAT_API_TOKEN` — **no
  credentials are stored in the source.** Don't commit your token.
- API requests are paced (rate-limited) to be a good iNaturalist API citizen.
- This is personal tooling shared as-is; the defaults are tailored to one
  workflow but every field is configurable in the GUI.
