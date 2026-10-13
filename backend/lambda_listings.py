"""Lambda: oilloop-listings
Routes:
  GET  /listings        recent listings (slim fields; addresses are never returned)
  POST /pickup          collector batch  {"listing_ids": [...], "area": "...", "truck_litres": 100}
  POST /request-pickup  vendor request   {"listing_id", "date": "YYYY-MM-DD", "slot": "08-10|10-12|16-18",
                                          "address", "vendor_name" (optional), "quantity_litres" (optional)}
  POST /lab-result      processor check  {"listing_id", "lab_grade": "A|B|C|Reject"}
Env (optional): TABLE=listings, PICKUPS=pickups
"""
import datetime
import json
import os
import uuid
from decimal import Decimal

import boto3

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
db = boto3.resource("dynamodb", region_name=REGION)
listings = db.Table(os.environ.get("TABLE", "listings"))
pickups = db.Table(os.environ.get("PICKUPS", "pickups"))

FIELDS = ("id", "created_at", "grade", "volume_litres", "area", "retire_check", "method",
          "oil_type", "fry_count", "pickup_id", "pickup_status", "request_date", "request_slot",
          "vendor_name", "lab_grade")  # address is stored but deliberately not returned (API has no auth)
SLOTS = {"08-10", "10-12", "16-18"}
GRADES = {"A", "B", "C", "Reject"}


def _resp(code, payload):
    conv = lambda o: float(o) if isinstance(o, Decimal) else str(o)
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload, default=conv)}


def _update(listing_id, sets):
    """SET several attributes on an existing listing; raises KeyError if it does not exist."""
    names = {f"#k{i}": k for i, k in enumerate(sets)}
    vals = {f":v{i}": v for i, v in enumerate(sets.values())}
    expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets)))
    try:
        listings.update_item(Key={"id": listing_id}, UpdateExpression=expr,
                             ExpressionAttributeNames=names, ExpressionAttributeValues=vals,
                             ConditionExpression="attribute_exists(id)")
    except listings.meta.client.exceptions.ConditionalCheckFailedException:
        raise KeyError(listing_id)


def _india_today():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()


def lambda_handler(event, context):
    try:
        route = event.get("routeKey", "")
        body = json.loads(event.get("body") or "{}") if route.startswith("POST") else {}

        if route == "GET /listings":
            items, kw = [], {}
            while len(items) < 500:
                r = listings.scan(**kw)
                items += r["Items"]
                if "LastEvaluatedKey" not in r:
                    break
                kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
            items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
            return _resp(200, {"items": [{k: i.get(k) for k in FIELDS} for i in items]})

        if route == "POST /pickup":
            ids = [i for i in body.get("listing_ids", []) if isinstance(i, str)][:100]
            if not ids:
                return _resp(400, {"error": "listing_ids is required"})
            pid = uuid.uuid4().hex
            pickups.put_item(Item={
                "id": pid, "created_at": datetime.datetime.utcnow().isoformat() + "Z",
                "area": str(body.get("area", ""))[:60], "listing_ids": ids,
                "truck_litres": Decimal(str(body.get("truck_litres", 0))),
                "destination": str(body.get("destination", ""))[:80],
                "trips": int(body.get("trips", 1))})
            for i in ids:
                _update(i, {"pickup_id": pid, "pickup_status": "scheduled"})
            return _resp(200, {"pickup_id": pid, "count": len(ids)})

        if route == "POST /request-pickup":
            lid = body.get("listing_id")
            address = str(body.get("address", "")).strip()[:200]
            slot = body.get("slot")
            if not isinstance(lid, str) or not address or slot not in SLOTS:
                return _resp(400, {"error": "listing_id, address and a valid slot are required"})
            try:
                day = datetime.date.fromisoformat(str(body.get("date")))
            except ValueError:
                return _resp(400, {"error": "date must be YYYY-MM-DD"})
            today = _india_today()
            if not today <= day <= today + datetime.timedelta(days=7):
                return _resp(400, {"error": "date must be within the next 7 days"})
            sets = {"pickup_status": "requested", "request_date": day.isoformat(),
                    "request_slot": slot, "address": address}
            if body.get("vendor_name"):
                sets["vendor_name"] = str(body["vendor_name"]).strip()[:60]
            if body.get("quantity_litres") is not None:
                q = float(body["quantity_litres"])
                if not 0 < q <= 1000:
                    return _resp(400, {"error": "quantity_litres must be between 0 and 1000"})
                sets["volume_litres"] = Decimal(str(q))
            _update(lid, sets)
            return _resp(200, {"ok": True, "listing_id": lid, "status": "requested"})

        if route == "POST /lab-result":
            lid, grade = body.get("listing_id"), body.get("lab_grade")
            if not isinstance(lid, str) or grade not in GRADES:
                return _resp(400, {"error": "listing_id and lab_grade (A|B|C|Reject) are required"})
            _update(lid, {"lab_grade": grade,
                          "lab_checked_at": datetime.datetime.utcnow().isoformat() + "Z"})
            return _resp(200, {"ok": True, "listing_id": lid, "lab_grade": grade})

        return _resp(404, {"error": "unknown route"})
    except KeyError:
        return _resp(404, {"error": "listing not found"})
    except Exception as exc:
        return _resp(500, {"error": type(exc).__name__, "detail": str(exc)[:300]})
