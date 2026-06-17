# iNaturalist Voucher Sync

A self-contained desktop GUI for syncing physical voucher IDs to
[iNaturalist](https://www.inaturalist.org) observation fields.

If you photograph a printed label (with a QR code and/or printed ID) alongside
each specimen, this tool reads the voucher ID straight out of your observation
photos and writes it into a custom observation field — so you never have to type
voucher numbers in by hand. 

Note: You must have the photo that contains the specimen and voucher as the **last** image in your observation.

## Download (Windows)

The easiest way to run it: grab **`VoucherSync.exe`** from the
[Releases page](../../releases) and double-click it. No Python, no installs —
OCR is built in. (Windows may show a SmartScreen "unknown publisher" warning
the first time; choose **More info → Run anyway**.)

Prefer to run from source, or on macOS/Linux? See [Usage](#usage) below.

## How it works

1. **Fetch** your observations from the iNaturalist API (optionally filtered by
   date).
2. **Read the voucher** from each observation's last photo:
   - First it tries to **decode a QR code** (OpenCV `QRCodeDetector`, with
     `pyzbar` as a secondary decoder). The label is located, deskewed, and
     upscaled before decoding so angled field shots still work.
   - If no QR is found, an **OCR fallback** reads the printed ID off the label.
     It's **on by default** (QR codes aren't always angled to decode) and uses
     a bundled engine that needs no separate install. OCR-derived values are
     flagged blue for review, since OCR is less certain than a QR read.
3. **Match** the decoded text against a voucher pattern (regex).
4. **Decide** an action per observation by comparing the detected voucher to the
   field's current value:

   | Color | Action | Meaning |
   |-------|--------|---------|
   | 🟢 Green | **Update** | QR-confirmed voucher; field was empty or being filled |
   | 🔵 Blue | **Update (review)** | Voucher came from OCR, not QR — double-check it |
   | ⚪ Grey | **Skip** | Field already holds the correct value |
   | 🟡 Amber | **Flag** | Detected voucher conflicts with an existing value |

5. **Review** the color-coded preview queue — summary chips tally the
   update / skip / flag counts — then **Apply updates**, or **export the queue
   to CSV** for record-keeping. A long scan can be aborted mid-run with
   **Stop**.

Nothing is written to iNaturalist until you click **Apply updates** and confirm.

## Requirements

- Python 3.8+
- Required: `requests`, `opencv-python`, `numpy`

  ```bash
  pip install requests opencv-python numpy
  ```

- **OCR fallback (recommended, on by default):** the bundled `RapidOCR`
  engine — pip-installable, no separate program to download. The first time
  you run a preview with OCR on, the app offers to **install it for you**
  (into its own Python environment). To install it yourself instead:

  ```bash
  pip install rapidocr-onnxruntime
  ```

- Optional, for QR decoding robustness: `pyzbar`
- Optional, **alternative** OCR engine — Tesseract, for users who already
  have it. Selectable via the **Engine** picker in the OCR fallback section;
  needs both the Python wrapper and the engine itself:

  ```bash
  pip install pytesseract
  ```

  - Windows: https://github.com/UB-Mannheim/tesseract/wiki
  - macOS: `brew install tesseract`
  - Debian/Ubuntu: `sudo apt-get install tesseract-ocr`

  If `tesseract` isn't on your PATH, point the app at the executable with
  **Browse…** (the path field appears only when the Tesseract engine is
  selected).

## Usage

```bash
python inat_voucher_sync.py
```

Then in the window:

1. **Paste your API token.** Get one at
   https://www.inaturalist.org/users/api_token. The token can also be loaded
   from a file or from the `INAT_API_TOKEN` environment variable. Once it
   verifies, the status pill in the top-right turns green and shows your login.
2. Enter your **username**, then choose the **observation field** to write to:
   start typing its name in the **Observation field** box and pick from the
   live suggestions — matching iNaturalist fields appear as you type, so you
   don't need to know the numeric ID.
3. Pick the **code format** that matches your label (see below), and optionally
   set a **date filter** (single day or range) or flip the **Overwrite existing
   values** toggle. The **OCR fallback** is on by default; you can turn it off
   or switch its **Engine** (built-in vs. Tesseract) in the OCR section.
4. Click **Preview run** to build the queue — hit **Stop** to abort a scan in
   progress — then review the color-coded results and **Apply updates**.
   **Export CSV** saves the queue, and the collapsible **Run log** at the bottom
   expands for a line-by-line trace.

### Configuration defaults

Every setting is editable in the GUI. The starting values live in a
`USER CONFIGURATION` block at the top of `inat_voucher_sync.py` — set them there
if you'd rather not retype them each session.

| Setting | Default | Notes |
|---------|---------|-------|
| Username | *(blank)* | Your iNaturalist login — required |
| Observation field | Personal voucher number (`#1907`) | The public field vouchers are written to |
| Code format | Prefix-Number | See the format picker below |

To target a different field, just start typing its name in the **Observation
field** box and pick from the live suggestions — the numeric ID is resolved for
you. You can also change the starting field via `DEFAULT_FIELD_NAME` /
`DEFAULT_FIELD_ID` in the `USER CONFIGURATION` block.

### Code format

The **Code format** control (under **Voucher matching**) chooses how a voucher
ID is recognized in the decoded QR/OCR text. Pick a preset, or **Custom** to
type your own regex.
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

## Building the Windows executable

The standalone `VoucherSync.exe` is produced with
[PyInstaller](https://pyinstaller.org/) from `inat_voucher_sync.spec`, which
bundles the OCR engine (RapidOCR models + the onnxruntime runtime) so the app
works with no install.

- **Automatically (recommended):** the
  [`Build Windows executable`](.github/workflows/build-windows.yml) GitHub
  Actions workflow builds it on a clean Windows runner. Run it manually from
  the **Actions** tab to download the exe as a build artifact, or push a tag
  (`git tag v1.0.0 && git push --tags`) to build it **and** attach it to the
  matching GitHub Release.
- **Locally on Windows:**

  ```bash
  pip install -r requirements.txt pyinstaller
  pyinstaller --noconfirm inat_voucher_sync.spec
  # → dist/VoucherSync.exe
  ```

The build is a single windowed `.exe` (no console). It's large (~hundreds of
MB) because it bundles opencv, numpy, and the onnxruntime OCR models, and the
first launch is a little slow as it unpacks. It is unsigned, so Windows
SmartScreen may warn on first run.

## Notes

- The tool reads tokens from input, a file, or `INAT_API_TOKEN` — **no
  credentials are stored in the source.** Don't commit your token.
- API requests are paced (rate-limited) to be a good iNaturalist API citizen.
- This is personal tooling shared as-is; the defaults are tailored to one
  workflow but every field is configurable in the GUI.
