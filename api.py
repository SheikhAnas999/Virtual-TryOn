import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

BASE_URL = "https://isiqgm9p3nn82y-8188.proxy.runpod.net"
GRID_SLOTS = ["image_1", "image_2", "image_3", "image_4", "image_5", "image_6"]
POLL_INTERVAL = 2
POLL_TIMEOUT = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("virtual_tryon")

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
    logger.info("Uploading image to ComfyUI: filename=%s size=%d bytes", filename, len(content))
    start = time.monotonic()
    r = session.post(
        f"{BASE_URL}/upload/image",
        files={"image": (filename, content)},
    )
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        logger.error("ComfyUI upload failed for %s: status=%s body=%s", filename, r.status_code, r.text)
        raise HTTPException(502, f"ComfyUI upload failed for {filename}: {r.text}") from e
    uploaded_name = r.json()["name"]
    logger.info(
        "Upload succeeded: filename=%s -> comfy_name=%s (%.2fs)",
        filename, uploaded_name, time.monotonic() - start,
    )
    return uploaded_name


def upload_all(files: List[UploadFile]) -> List[str]:
    items = [(f.filename or "image.png", f.file.read()) for f in files]
    logger.info("Uploading %d image(s) concurrently: %s", len(items), [name for name, _ in items])
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        results = list(executor.map(lambda item: upload_image(*item), items))
    logger.info("All %d upload(s) complete in %.2fs: %s", len(items), time.monotonic() - start, results)
    return results


def build_prompt(labels: List[str]) -> str:
    if len(labels) > 1:
        joined = " , ".join(labels[:-1]) + " and " + labels[-1]
    else:
        joined = labels[0]
    return f"Replace the cloth in image 1 with {joined} in image 2.  "

# prompt for tuck in shirts : The shirt must be fully tucked into the pants exactly as a properly tucked dress shirt, with a visible waistband and no loose or hanging fabric.
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
    request_start = time.monotonic()
    logger.info("=" * 60)
    logger.info("Incoming request: POST /virtual-tryon-image")
    logger.info(
        "human_image: filename=%s content_type=%s",
        human_image.filename, human_image.content_type,
    )
    logger.info("clothes: received %d file(s)", len(clothes))
    for i, cloth in enumerate(clothes):
        logger.info(
            "  clothes[%d]: filename=%s content_type=%s",
            i, cloth.filename, cloth.content_type,
        )
    logger.info("descriptionText (raw): %s", descriptionText)

    if not clothes:
        logger.warning("Rejecting request: no garment images provided.")
        raise HTTPException(400, "At least one garment image (clothes) is required.")
    if len(clothes) > len(GRID_SLOTS):
        logger.warning(
            "Rejecting request: too many garment images (%d > %d).",
            len(clothes), len(GRID_SLOTS),
        )
        raise HTTPException(
            400, f"Too many product images: max {len(GRID_SLOTS)} supported."
        )

    labels = extract_labels(descriptionText)
    logger.info("Extracted labels from descriptionText: %s", labels)
    if not labels:
        logger.warning("Rejecting request: no garment labels found in descriptionText.")
        raise HTTPException(400, "Could not find any garment labels in descriptionText.")

    prompt = build_prompt(labels)
    print(prompt)
    logger.info("Built prompt for ComfyUI: %s", prompt)

    # -----------------------------
    # Upload Images (concurrently)
    # -----------------------------

    logger.info("Step 1/6: Uploading human + garment images to ComfyUI...")
    all_names = upload_all([human_image] + clothes)
    person_name = all_names[0]
    product_names = all_names[1:]
    logger.info("Person image uploaded as: %s", person_name)
    logger.info("Product images uploaded as: %s", product_names)

    # -----------------------------
    # Load Workflow (fresh copy per request)
    # -----------------------------

    logger.info("Step 2/6: Loading workflow.json...")
    with open("workflow.json") as f:
        workflow = json.load(f)
    logger.info("Workflow loaded with %d node(s).", len(workflow))

    # -----------------------------
    # Person Image
    # -----------------------------

    workflow["76"]["inputs"]["image"] = person_name
    logger.info("Set node 76 (person image) input.image = %s", person_name)

    # -----------------------------
    # Product Images (Grid Merger)
    # -----------------------------

    for slot, filename in zip(GRID_SLOTS, product_names):
        workflow["134"]["inputs"][slot] = filename

    for slot in GRID_SLOTS[len(product_names):]:
        workflow["134"]["inputs"][slot] = "none"

    logger.info("Set node 134 (grid merger) slots = %s", workflow["134"]["inputs"])

    # -----------------------------
    # Prompt
    # -----------------------------

    workflow["107"]["inputs"]["text"] = prompt
    logger.info("Set node 107 (prompt text) input.text = %s", prompt)

    # -----------------------------
    # Randomize Seed
    # -----------------------------
    # Force a fresh execution: a fixed seed + identical inputs makes ComfyUI's
    # cache skip the run entirely, returning success with no "94" output.

    seed = random.randint(0, 2**32 - 1)
    workflow["104"]["inputs"]["noise_seed"] = seed
    logger.info("Set node 104 (sampler) noise_seed = %d", seed)

    # -----------------------------
    # Queue Workflow
    # -----------------------------

    logger.info("Step 3/6: Queuing workflow with ComfyUI at %s/prompt", BASE_URL)
    r = session.post(f"{BASE_URL}/prompt", json={"prompt": workflow})
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        logger.error("ComfyUI rejected the workflow: status=%s body=%s", r.status_code, r.text)
        raise HTTPException(502, f"ComfyUI rejected the workflow: {r.text}") from e

    prompt_id = r.json()["prompt_id"]
    logger.info("Workflow queued successfully. prompt_id=%s", prompt_id)

    # -----------------------------
    # Wait for Completion
    # -----------------------------

    logger.info("Step 4/6: Polling ComfyUI history for completion (timeout=%ds)...", POLL_TIMEOUT)
    deadline = time.monotonic() + POLL_TIMEOUT
    history = {}
    poll_count = 0
    while prompt_id not in history:
        poll_count += 1
        history = session.get(f"{BASE_URL}/history/{prompt_id}").json()
        if prompt_id in history:
            logger.info("Job finished after %d poll(s).", poll_count)
            break
        if time.monotonic() > deadline:
            logger.error("Timed out waiting for ComfyUI to finish. prompt_id=%s", prompt_id)
            raise HTTPException(504, "Timed out waiting for ComfyUI to finish.")
        logger.info("  poll #%d: not ready yet, sleeping %ds...", poll_count, POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)

    outputs = history[prompt_id].get("outputs", {})
    logger.info("ComfyUI returned outputs for node ids: %s", list(outputs.keys()))
    if "94" not in outputs:
        logger.error("ComfyUI finished but node 94 produced no output. Full outputs: %s", outputs)
        raise HTTPException(
            502, "ComfyUI finished but produced no output for node 94."
        )

    # -----------------------------
    # Download Result
    # -----------------------------

    logger.info("Step 5/6: Downloading result image from ComfyUI...")
    image_info = outputs["94"]["images"][0]
    logger.info("Result image info: %s", image_info)

    img = session.get(
        f"{BASE_URL}/view",
        params={"filename": image_info["filename"], "type": "output"},
    )
    img.raise_for_status()
    logger.info("Downloaded result image: %d bytes", len(img.content))

    logger.info(
        "Step 6/6: Request complete. Total time: %.2fs",
        time.monotonic() - request_start,
    )
    logger.info("=" * 60)

    return Response(content=img.content, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
