# moltbook_util.py
import json
import os
from typing import Any, List, Optional, Dict

CRED_PATH = os.path.expanduser("~/.config/moltbook/credentials.json")


def load_creds() -> dict:
    if os.path.exists(CRED_PATH):
        try:
            with open(CRED_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_creds(api_key: str, agent_name: str = ""):
    os.makedirs(os.path.dirname(CRED_PATH), exist_ok=True)
    with open(CRED_PATH, "w", encoding="utf-8") as f:
        json.dump({"api_key": api_key, "agent_name": agent_name}, f, indent=2)


def normalize_submolt(name: str) -> str:
    name = (name or "").strip()
    if name.startswith("m/"):
        name = name[2:].strip()
    return name


def parse_json(client, resp) -> dict:
    data = client.json(resp)
    data["_http_status"] = resp.status_code
    if resp.status_code == 429 and isinstance(data, dict):
        hints = []
        for k in ["retry_after_minutes", "retry_after_seconds", "daily_remaining"]:
            if k in data:
                hints.append(f"{k}={data[k]}")
        if hints:
            data["_rate_limit"] = ", ".join(hints)
    return data


def extract_agent_name(me_payload: dict) -> Optional[str]:
    if not isinstance(me_payload, dict):
        return None
    a = me_payload.get("agent") or me_payload.get("data") or me_payload
    if isinstance(a, dict):
        return a.get("name") or a.get("agent_name")
    return None


def extract_posts_list(payload: Any) -> Optional[List[dict]]:
    if isinstance(payload, dict):
        for k in ["posts", "results"]:
            if isinstance(payload.get(k), list):
                return payload[k]
        if isinstance(payload.get("data"), dict):
            d = payload["data"]
            if isinstance(d.get("posts"), list):
                return d["posts"]
            if isinstance(d.get("results"), list):
                return d["results"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    if isinstance(payload, list):
        return payload
    return None


def extract_results_list(payload: Any) -> Optional[List[dict]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), list):
            return payload["results"]
        if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("results"), list):
            return payload["data"]["results"]
    return None


def extract_post_obj(payload: Any) -> Optional[dict]:
    if isinstance(payload, dict):
        for k in ["post", "data", "result"]:
            if isinstance(payload.get(k), dict):
                return payload[k]
        if "id" in payload and ("title" in payload or "content" in payload):
            return payload
    return None


def extract_comments_list(payload: Any) -> Optional[List[dict]]:
    if isinstance(payload, dict):
        for k in ["comments", "results"]:
            if isinstance(payload.get(k), list):
                return payload[k]
        if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("comments"), list):
            return payload["data"]["comments"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
    if isinstance(payload, list):
        return payload
    return None
