"""Lambda: oilloop-grade-oil

Route:   POST /grade
Body:    {"key": "uploads/<id>.jpg", "volume_litres": 12, "fry_count": 3, "oil_type": "palm"}
Returns: grade, payout range, biodiesel / diesel / CO2e estimates, cues, flags.

Env vars:
  BUCKET            your S3 bucket name
  ENDPOINT_NAME     SageMaker endpoint name (optional; leave unset for Bedrock-only mode)
  BEDROCK_MODEL_ID  default amazon.nova-lite-v1:0
  TABLE             default listings
Set the Lambda timeout to 60 s and memory to 512 MB.
"""
import datetime
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
BUCKET = os.environ["BUCKET"]
ENDPOINT_NAME = os.environ.get("ENDPOINT_NAME", "")  # empty = Bedrock-only (cue rules) mode
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
TABLE = os.environ.get("TABLE", "listings")

s3 = boto3.client("s3", region_name=REGION)
smr = boto3.client("sagemaker-runtime", region_name=REGION)
bedrock = boto3.client("bedrock-runtime", region_name=REGION)
table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)

# ---- Constants (from uco_reference_data.json). All are estimates / assumptions. ----
PRICE_INR = {"A": (48, 60), "B": (35, 45), "C": (20, 28), "Reject": (0, 0)}
YIELD_L_PER_L = {"A": 0.86, "B": 0.78, "C": 0.65, "Reject": 0.0}
DIESEL_DISPLACED_PER_L = 0.88
CO2E_KG_PER_L = 2.3
LOW_CONFIDENCE = 0.55
FRY_RETIRE_AT = int(os.environ.get("FRY_RETIRE_AT", "5"))  # adjustable heuristic, not a measured limit

PROMPT = """You are inspecting a photo of used cooking oil in a clear cup placed beside a color reference card.
Respond with ONLY a JSON object, no other text, with exactly these keys:
{"photo_usable": true or false,
 "cup_visible": true or false,
 "color_card_visible": true or false,
 "water_layer_visible": true or false,
 "non_oil_mixture": true or false,
 "sediment": "none" or "light" or "heavy",
 "foam_visible": true or false,
 "darkness": "light" or "medium" or "dark" or "black",
 "notes": "at most 15 words"}
Rules: photo_usable is false if the image is blurry, too dark, or no oil is visible.
water_layer_visible is true only if you can see a separate watery layer.
non_oil_mixture is true only if you can see clear signs the liquid is not oil, such as a separate watery layer, many small fizzy bubbles like a soft drink, or large food pieces mixed in. A dark colour alone is NOT a sign, because used cooking oil can be very dark.
darkness describes the oil's own body colour, ignoring bright reflections, highlights and white backgrounds:
light = pale straw or golden and see-through; medium = clear amber to orange-brown; dark = deep brown or reddish brown and hard to see through; black = nearly opaque.
If the colour sits between two levels, choose the darker one.
sediment: none = clear; light = a few specks; heavy = many particles or debris visible.
Do not guess about things you cannot see."""


def read_cues(image_bytes, ext):
    """Ask a multimodal Bedrock model to read visible cues as JSON."""
    fmt = "png" if ext == "png" else "jpeg"
    resp = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                {"text": PROMPT},
            ],
        }],
        inferenceConfig={"maxTokens": 300, "temperature": 0},
    )
    text = resp["output"]["message"]["content"][0]["text"]
    match = re.search(r"\{.*\}", text, re.S)
    return json.loads(match.group(0))


def classify(image_bytes):
    """Call the SageMaker endpoint. Returns {"classes": [...], "probs": [...]}."""
    resp = smr.invoke_endpoint(
        EndpointName=ENDPOINT_NAME,
        ContentType="image/jpeg",
        Body=image_bytes,
    )
    return json.loads(resp["Body"].read())


DARKNESS_SCORE = {"light": 0, "medium": 2, "dark": 3, "black": 4}  # A needs a light colour
SEDIMENT_SCORE = {"none": 0, "light": 1, "heavy": 2}


def grade_from_cues(cues):
    """Transparent rule-based feedstock grade from visible cues (used with a Good/Replace model).

    This is a pilot heuristic, not a measurement; the processor confirms by lab test.
    """
    if not cues:
        return "B"  # neutral default when the cue check is unavailable
    score = DARKNESS_SCORE.get(cues.get("darkness"), 2) + SEDIMENT_SCORE.get(cues.get("sediment"), 1)
    if score <= 1:
        return "A"
    if score <= 3:
        return "B"
    return "C"


def _handle(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "Body must be JSON"})

    key = body.get("key", "")
    if not key.startswith("uploads/"):
        return _resp(400, {"error": "key is required and must start with uploads/"})
    try:
        volume = float(body.get("volume_litres", 1))
    except (TypeError, ValueError):
        return _resp(400, {"error": "volume_litres must be a number"})
    if not 0 < volume <= 1000:
        return _resp(400, {"error": "volume_litres must be between 0 and 1000"})

    fry_count = body.get("fry_count")
    oil_type = body.get("oil_type")
    area = (body.get("area") or "").strip()[:60] or None

    try:
        image_bytes = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return _resp(404, {
            "error": "upload not found",
            "detail": "The photo is not in S3. The PUT to the upload URL probably failed; it must return 200.",
        })
    ext = key.rsplit(".", 1)[-1].lower()

    cues, cue_error, result = None, None, None
    if ENDPOINT_NAME:
        # Run both AI calls in parallel to keep total latency low.
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_cues = pool.submit(read_cues, image_bytes, ext)
            f_cls = pool.submit(classify, image_bytes)
            try:
                cues = f_cues.result()
            except Exception as exc:  # keep going without cues
                cue_error = str(exc)[:200]
            try:
                result = f_cls.result()
            except Exception as exc:
                return _resp(502, {"error": "classifier unavailable", "detail": str(exc)[:200]})
    else:
        # Bedrock-only mode: no trained model, grade and retire check come from cue rules.
        try:
            cues = read_cues(image_bytes, ext)
        except Exception as exc:
            return _resp(502, {"error": "cue check unavailable", "detail": str(exc)[:200]})

    if cues and (cues.get("photo_usable") is False or cues.get("cup_visible") is False):
        return _resp(200, {
            "status": "retake",
            "reason": cues.get("notes") or "Retake the photo with the cup and color card in frame.",
        })

    reasons = []
    if result is None:
        method = "cue_rules"
        probs, top, confidence = {}, None, None
        binary_model = False
    else:
        method = "model_plus_cues"
        probs = dict(zip(result["classes"], result["probs"]))
        top = max(probs, key=probs.get)
        confidence = probs[top]
        binary_model = set(probs) <= {"Good", "Replace"}

    if result is None:
        # Pilot rules from visible cues only (no trained model).
        retire_probability = None
        grade = grade_from_cues(cues)
        retire_check = bool(
            cues.get("foam_visible")
            or cues.get("darkness") in ("dark", "black")
            or cues.get("sediment") == "heavy"
        )
        reasons.append("Grade and retire check are rule-based on visible cues (pilot, no trained model)")
    elif binary_model:
        # The photo model answers "reuse or replace?". The feedstock grade comes from visible cues.
        retire_probability = probs.get("Replace", 0.0)
        retire_check = retire_probability >= 0.5
        grade = grade_from_cues(cues)
        reasons.append("Grade is rule-based on visible cues (pilot, pending lab-labelled data)")
    else:
        # Original four-class grade model.
        retire_probability = None
        grade = top
        retire_check = bool(cues and (cues.get("foam_visible") or cues.get("darkness") in ("dark", "black")))

    # Fusion rule: visible water or non-oil mixture overrides everything.
    if cues and (cues.get("water_layer_visible") or cues.get("non_oil_mixture")):
        grade = "Reject"
        reasons.append("Water layer or non-oil mixture visible")

    # Vendor-reported reuse count: an adjustable heuristic, not a measurement.
    try:
        uses = int(fry_count) if fry_count is not None else None
    except (TypeError, ValueError):
        uses = None
    if uses is not None and uses >= FRY_RETIRE_AT:
        retire_check = True
        reasons.append(f"Reported {uses} uses (reuse limit {FRY_RETIRE_AT}, an adjustable assumption)")
        if cues and not (cues.get("foam_visible") or cues.get("darkness") in ("dark", "black") or cues.get("sediment") == "heavy"):
            reasons.append("The photo looks lighter than the reported use suggests, so confirm with a test strip")

    lo, hi = PRICE_INR[grade]
    biodiesel_l = round(volume * YIELD_L_PER_L[grade], 2)

    out = {
        "id": uuid.uuid4().hex,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "graded",
        "grade": grade,
        "method": method,
        "confidence": None if confidence is None else round(confidence, 3),
        "low_confidence": confidence is not None and confidence < LOW_CONFIDENCE,
        "probabilities": {k: round(v, 3) for k, v in probs.items()},
        "reasons": reasons,
        "cues": cues,
        "cue_check_error": cue_error,
        "retire_check": retire_check,
        "retire_probability": None if retire_probability is None else round(retire_probability, 3),
        "retire_message": (
            "This oil looks due for replacement. "
            "Confirm with a test strip before frying with it again."
        ) if retire_check else None,
        "volume_litres": volume,
        "fry_count": fry_count,
        "oil_type": oil_type,
        "area": area,
        "payout_inr_range": [round(lo * volume), round(hi * volume)],
        "biodiesel_litres_est": biodiesel_l,
        "diesel_displaced_litres_est": round(biodiesel_l * DIESEL_DISPLACED_PER_L, 2),
        "co2e_avoided_kg_est": round(biodiesel_l * CO2E_KG_PER_L, 1),
        "image_key": key,
        "disclaimer": "Screening grade from a photo. The processor confirms with a lab test. Energy and CO2 figures are estimates.",
    }

    # DynamoDB does not accept Python floats, so convert to Decimal.
    item = json.loads(json.dumps(out), parse_float=Decimal)
    table.put_item(Item=item)

    return _resp(200, out)


def lambda_handler(event, context):
    try:
        return _handle(event)
    except Exception as exc:  # return the real error instead of a bare 500
        import traceback
        traceback.print_exc()
        return _resp(500, {"error": type(exc).__name__, "detail": str(exc)[:300]})


def _resp(code, payload):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, default=str),
    }
