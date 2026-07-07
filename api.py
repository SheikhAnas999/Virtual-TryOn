import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

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


@app.post("/virtual-tryon-image")
def virtual_tryon_image(
    prompt: str = Form(...),
    human_image: UploadFile = File(...),
    product_image_1: UploadFile = File(...),
    product_image_2: Optional[UploadFile] = File(None),
    product_image_3: Optional[UploadFile] = File(None),
    product_image_4: Optional[UploadFile] = File(None),
    product_image_5: Optional[UploadFile] = File(None),
    product_image_6: Optional[UploadFile] = File(None),
):
    product_images = [
        f
        for f in [
            product_image_1,
            product_image_2,
            product_image_3,
            product_image_4,
            product_image_5,
            product_image_6,
        ]
        if f is not None
    ]
    if len(product_images) > len(GRID_SLOTS):
        raise HTTPException(
            400, f"Too many product images: max {len(GRID_SLOTS)} supported."
        )

    # -----------------------------
    # Upload Images (concurrently)
    # -----------------------------

    all_names = upload_all([human_image] + product_images)
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
