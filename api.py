import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

BASE_URL = "https://jrh88py4eg2zzh-8188.proxy.runpod.net"
GRID_SLOTS = ["image_1", "image_2", "image_3", "image_4", "image_5", "image_6"]
POLL_INTERVAL = 2
POLL_TIMEOUT = 300

app = FastAPI(title="Virtual Try-On API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reuse one HTTP connection (keep-alive) instead of opening a new
# TCP+TLS connection for every upload/prompt/history/view call.
session = requests.Session()


def upload_image(filename: str, content: bytes) -> str:
    r = session.post(
        f"{BASE_URL}/upload/image",
        files={"image": (filename, content)},
    )
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise HTTPException(502, f"ComfyUI upload failed for {filename}: {r.text}") from e
    return r.json()["name"]


def upload_all(files: List[UploadFile]) -> List[str]:
    items = [(f.filename or "image.png", f.file.read()) for f in files]
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        return list(executor.map(lambda item: upload_image(*item), items))


def build_prompt(labels: List[str]) -> str:
    if len(labels) > 1:
        joined = " , ".join(labels[:-1]) + " and " + labels[-1]
    else:
        joined = labels[0]
    return f"Replace the cloth in image 1 with {joined} in image 2."


# The Electron client builds each line as f"Image {i+2} ({label}): {promptDesc}"
# and only sends this combined descriptionText, never the raw label by itself.
LABEL_LINE_RE = re.compile(r"^Image\s+\d+\s+\((.*?)\)\s*:")


def extract_labels(description_text: str) -> List[str]:
    labels = []
    for line in description_text.splitlines():
        match = LABEL_LINE_RE.match(line.strip())
        if match:
            labels.append(match.group(1).strip())
    return labels


@app.post("/virtual-tryon-image")
def virtual_tryon_image(
    human_image: UploadFile = File(...),
    descriptionText: str = Form(...),
    clothes: List[UploadFile] = File(...),
):
    if not clothes:
        raise HTTPException(400, "At least one garment image (clothes) is required.")
    if len(clothes) > len(GRID_SLOTS):
        raise HTTPException(
            400, f"Too many product images: max {len(GRID_SLOTS)} supported."
        )

    labels = extract_labels(descriptionText)
    if not labels:
        raise HTTPException(400, "Could not find any garment labels in descriptionText.")

    prompt = build_prompt(labels)
    print(prompt)
    # -----------------------------
    # Upload Images (concurrently)
    # -----------------------------

    all_names = upload_all([human_image] + clothes)
    person_name = all_names[0]
    product_names = all_names[1:]

    # -----------------------------
    # Load Workflow (fresh copy per request)
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

    for slot, filename in zip(GRID_SLOTS, product_names):
        workflow["134"]["inputs"][slot] = filename

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

    r = session.post(f"{BASE_URL}/prompt", json={"prompt": workflow})
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise HTTPException(502, f"ComfyUI rejected the workflow: {r.text}") from e

    prompt_id = r.json()["prompt_id"]

    # -----------------------------
    # Wait for Completion
    # -----------------------------

    deadline = time.monotonic() + POLL_TIMEOUT
    history = {}
    while prompt_id not in history:
        history = session.get(f"{BASE_URL}/history/{prompt_id}").json()
        if prompt_id in history:
            break
        if time.monotonic() > deadline:
            raise HTTPException(504, "Timed out waiting for ComfyUI to finish.")
        time.sleep(POLL_INTERVAL)

    outputs = history[prompt_id].get("outputs", {})
    if "94" not in outputs:
        raise HTTPException(
            502, "ComfyUI finished but produced no output for node 94."
        )

    # -----------------------------
    # Download Result
    # -----------------------------

    image_info = outputs["94"]["images"][0]

    img = session.get(
        f"{BASE_URL}/view",
        params={"filename": image_info["filename"], "type": "output"},
    )
    img.raise_for_status()

    return Response(content=img.content, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
