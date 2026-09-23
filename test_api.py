"""
Manual test script for the Virtual Try-On API.

Edit the CONFIG section below with your own image paths and labels,
then run:

    python test_api.py
"""

import pathlib
import time

import requests

# =========================================================
# CONFIG - edit these before running
# =========================================================

API_URL = "https://profits-selective-albums-pediatric.trycloudflare.com/virtual-tryon-image"

HUMAN_IMAGE_PATH = "images/5.png"

# Each entry is (label, image_path). Label is what gets put in the
# "Image N (label): ..." line the API expects in descriptionText.
GARMENTS = [
    (" shirt", "images/shirts.jpg"),
    (" pants", "images/pants.jpg"),
]

OUTPUT_PATH = "images/test_result.png"

# =========================================================


def build_description_text(garments):
    # Mirrors the Electron client's format: "Image {i+2} ({label}): {desc}"
    lines = []
    for i, (label, _) in enumerate(garments):
        lines.append(f"Image {i + 2} ({label}): {label}")
    return "\n".join(lines)


def main():
    human_path = pathlib.Path(HUMAN_IMAGE_PATH)
    if not human_path.exists():
        raise SystemExit(f"Human image not found: {human_path}")

    for _, path in GARMENTS:
        if not pathlib.Path(path).exists():
            raise SystemExit(f"Garment image not found: {path}")

    description_text = build_description_text(GARMENTS)
    print("descriptionText:")
    print(description_text)
    print()

    files = [("human_image", (human_path.name, open(human_path, "rb"), "image/png"))]
    for _, path in GARMENTS:
        p = pathlib.Path(path)
        files.append(("clothes", (p.name, open(p, "rb"), "image/jpeg")))

    data = {"descriptionText": description_text}

    print(f"POST {API_URL}")
    start = time.monotonic()
    try:
        response = requests.post(API_URL, files=files, data=data, timeout=300)
    finally:
        for _, (_, fh, _) in files:
            fh.close()

    elapsed = time.monotonic() - start
    print(f"Status: {response.status_code} ({elapsed:.2f}s)")

    if response.status_code != 200:
        print("Error response:", response.text)
        return

    out_path = pathlib.Path(OUTPUT_PATH)
    out_path.write_bytes(response.content)
    print(f"Saved result to {out_path} ({len(response.content)} bytes)")


if __name__ == "__main__":
    main()
