import json
import random
import requests
import time
from concurrent.futures import ThreadPoolExecutor

BASE_URL = "https://hol0lfwc9llere-8188.proxy.runpod.net"

# Reuse one HTTP connection (keep-alive) instead of opening a new
# TCP+TLS connection for every upload/prompt/history/view call.
session = requests.Session()


def upload(path):
    with open(path, "rb") as f:
        r = session.post(
            f"{BASE_URL}/upload/image",
            files={"image": f}
        )
    r.raise_for_status()
    return r.json()["name"]


def upload_all(paths):
    """Upload multiple images concurrently (order preserved)."""
    with ThreadPoolExecutor(max_workers=len(paths)) as executor:
        return list(executor.map(upload, paths))


# -----------------------------
# INPUTS
# -----------------------------

person_image = "4.jpeg"

# Add as many product images as you want (1 to 6 supported by the grid merger).
product_images = [
    "pants.jpg","shirts.jpg","shoes.jpg","glasses.jpg",
]

prompt = (
    "Replace the cloth in image 1 with pant , shirt , shoes and glasses in image 2."
)

# -----------------------------
# Upload Images
# -----------------------------

all_names = upload_all([person_image] + product_images)
person_name = all_names[0]
product_names = all_names[1:]

# -----------------------------
# Load Workflow
# -----------------------------

with open("workflow.json") as f:
    workflow = json.load(f)

# -----------------------------
# Person Image
# -----------------------------

workflow["76"]["inputs"]["image"] = person_name

# -----------------------------
# Product Images (Grid Merger)
# -----------------------------

GRID_SLOTS = ["image_1", "image_2", "image_3", "image_4", "image_5", "image_6"]

if not product_names:
    raise ValueError("At least one product image is required.")

if len(product_names) > len(GRID_SLOTS):
    raise ValueError(f"Too many product images: max {len(GRID_SLOTS)} supported.")

for slot, filename in zip(GRID_SLOTS, product_names):
    workflow["134"]["inputs"][slot] = filename

# Clear unused grid slots
for slot in GRID_SLOTS[len(product_names):]:
    workflow["134"]["inputs"][slot] = "none"

# -----------------------------
# Prompt
# -----------------------------

workflow["107"]["inputs"]["text"] = prompt

# -----------------------------
# Randomize Seed
# -----------------------------
# Force a fresh execution: a fixed seed + identical inputs makes ComfyUI's
# cache skip the run entirely, returning success with no "94" output.

workflow["104"]["inputs"]["noise_seed"] = random.randint(0, 2**32 - 1)

# -----------------------------
# Queue Workflow
# -----------------------------

r = session.post(
    f"{BASE_URL}/prompt",
    json={"prompt": workflow},
)

r.raise_for_status()

prompt_id = r.json()["prompt_id"]

print("Prompt ID:", prompt_id)

# -----------------------------
# Wait for Completion
# -----------------------------

while True:
    history = session.get(
        f"{BASE_URL}/history/{prompt_id}"
    ).json()

    if prompt_id in history:
        break

    time.sleep(2)

# -----------------------------
# Download Result
# -----------------------------

image = history[prompt_id]["outputs"]["94"]["images"][0]

filename = image["filename"]

img = session.get(
    f"{BASE_URL}/view",
    params={
        "filename": filename,
        "type": "output",
    },
)

with open("result.png", "wb") as f:
    f.write(img.content)

print("Saved result.png")