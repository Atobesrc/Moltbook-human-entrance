# moltbook_client.py
import os
import json
from typing import Optional, Callable, Any, Dict

import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


API_BASE = "https://www.moltbook.com/api/v1"  # MUST be www
ALLOWED_HOST = "www.moltbook.com"


def ensure_allowed_url(url: str) -> None:
    if not (url == API_BASE or url.startswith(API_BASE + "/")):
        raise ValueError(f"Refusing to send Authorization to non-Moltbook URL: {url}")
    host = url.split("://", 1)[1].split("/", 1)[0]
    if host != ALLOWED_HOST:
        raise ValueError(f"Refusing to send Authorization to host '{host}' (must be {ALLOWED_HOST}).")


class MoltbookClient:
    """
    Requests-based Moltbook client with retries.
    SECURITY: only allows https://www.moltbook.com/api/v1/*
    """
    def __init__(self, api_key: str = ""):
        self.api_key = api_key.strip()
        self.debug_hook: Optional[Callable[[str], None]] = None

        self.sess = requests.Session()

        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST", "PATCH", "DELETE"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.sess.mount("https://", adapter)
        self.sess.mount("http://", adapter)

    def set_api_key(self, api_key: str):
        self.api_key = api_key.strip()
        self.sess.headers.update({"Authorization": f"Bearer {self.api_key}"})

    def _headers(self, json_content: bool = False) -> Dict[str, str]:
        if not self.api_key:
            raise ValueError("API key not set.")
        h = {"Authorization": f"Bearer {self.api_key}"}
        if json_content:
            h["Content-Type"] = "application/json"
        return h

    def _redact_headers(self, headers: dict) -> dict:
        h = dict(headers or {})
        if "Authorization" in h:
            v = h["Authorization"]
            if v.startswith("Bearer "):
                tok = v[len("Bearer "):]
                h["Authorization"] = "Bearer " + tok[:12] + "…"
            else:
                h["Authorization"] = v[:12] + "…"
        return h

    def _request(
        self,
        method: str,
        path: str,
        *,
        params=None,
        json_body=None,
        files=None,
        data=None,
        timeout=(8, 180),
    ) -> requests.Response:
        url = API_BASE + path
        ensure_allowed_url(url)

        hdrs = self._headers(json_content=(json_body is not None and files is None))

        if self.debug_hook:
            self.debug_hook(f"REQUEST {method} {url}")
            self.debug_hook(f"HEADERS {self._redact_headers(hdrs)}")
            if params:
                self.debug_hook(f"PARAMS {params}")
            if json_body is not None:
                self.debug_hook(f"JSON_BODY keys={list(json_body.keys())}")

        if files is not None:
            resp = self.sess.request(
                method, url,
                headers=hdrs,
                params=params,
                files=files,
                data=data,
                timeout=timeout,
            )
        else:
            resp = self.sess.request(
                method, url,
                headers=hdrs,
                params=params,
                json=json_body,
                timeout=timeout,
            )

        if self.debug_hook:
            self.debug_hook(f"RESPONSE HTTP {resp.status_code}")
        if self.debug_hook and resp.status_code >= 400:
            txt = resp.text or ""
            if len(txt) > 800:
                txt = txt[:800] + "…"
            self.debug_hook(f"RESPONSE BODY {txt}")
        return resp

    def json(self, resp: requests.Response) -> dict:
        try:
            return resp.json()
        except Exception:
            return {"success": False, "error": f"Non-JSON response (HTTP {resp.status_code})", "text": resp.text}

    # ---- Agents
    def me(self): return self._request("GET", "/agents/me")
    def status(self): return self._request("GET", "/agents/status")
    def agent_profile(self, name: str): return self._request("GET", "/agents/profile", params={"name": name})
    def follow_agent(self, name: str): return self._request("POST", f"/agents/{name}/follow", json_body={})
    def unfollow_agent(self, name: str): return self._request("DELETE", f"/agents/{name}/follow")
    def update_me(self, description=None, metadata=None):
        payload = {}
        if description is not None: payload["description"] = description
        if metadata is not None: payload["metadata"] = metadata
        return self._request("PATCH", "/agents/me", json_body=payload)

    def upload_my_avatar(self, filepath: str):
        with open(filepath, "rb") as f:
            files = {"file": (os.path.basename(filepath), f)}
            return self._request("POST", "/agents/me/avatar", files=files)

    def remove_my_avatar(self):
        return self._request("DELETE", "/agents/me/avatar")

    # ---- Posts
    def feed_posts(self, sort="hot", limit=25, submolt=None):
        params = {"sort": sort, "limit": int(limit)}
        if submolt: params["submolt"] = submolt
        return self._request("GET", "/posts", params=params)

    def personalized_feed(self, sort="hot", limit=25):
        return self._request("GET", "/feed", params={"sort": sort, "limit": int(limit)})

    def submolt_feed(self, submolt: str, sort="new", limit=25):
        return self._request("GET", f"/submolts/{submolt}/feed", params={"sort": sort, "limit": int(limit)})

    def get_post(self, post_id: str): return self._request("GET", f"/posts/{post_id}")
    def delete_post(self, post_id: str): return self._request("DELETE", f"/posts/{post_id}")

    def create_post(self, submolt: str, title: str, content: str = None, url: str = None):
        payload = {"submolt": submolt, "title": title}
        if content: payload["content"] = content
        if url: payload["url"] = url
        return self._request("POST", "/posts", json_body=payload)

    def upvote_post(self, post_id: str): return self._request("POST", f"/posts/{post_id}/upvote", json_body={})
    def downvote_post(self, post_id: str): return self._request("POST", f"/posts/{post_id}/downvote", json_body={})
    def pin_post(self, post_id: str): return self._request("POST", f"/posts/{post_id}/pin", json_body={})
    def unpin_post(self, post_id: str): return self._request("DELETE", f"/posts/{post_id}/pin")

    # ---- Comments
    def get_comments(self, post_id: str, sort="top"):
        return self._request("GET", f"/posts/{post_id}/comments", params={"sort": sort})

    def add_comment(self, post_id: str, content: str, parent_id: str = None):
        payload = {"content": content}
        if parent_id: payload["parent_id"] = parent_id
        return self._request("POST", f"/posts/{post_id}/comments", json_body=payload)

    def upvote_comment(self, comment_id: str):
        return self._request("POST", f"/comments/{comment_id}/upvote", json_body={})

    # ---- Search
    def semantic_search(self, q: str, type_: str = "all", limit: int = 20):
        params = {"q": q, "limit": int(limit), "type": (type_ or "all")}
        return self._request("GET", "/search", params=params, timeout=(8, 180))

    # ---- Submolts
    def list_submolts(self): return self._request("GET", "/submolts")
    def get_submolt(self, name: str): return self._request("GET", f"/submolts/{name}")
    def create_submolt(self, name: str, display_name: str, description: str):
        payload = {"name": name, "display_name": display_name, "description": description}
        return self._request("POST", "/submolts", json_body=payload)
    def subscribe_submolt(self, name: str): return self._request("POST", f"/submolts/{name}/subscribe", json_body={})
    def unsubscribe_submolt(self, name: str): return self._request("DELETE", f"/submolts/{name}/subscribe")

    # ---- Moderation
    def update_submolt_settings(self, name: str, description=None, banner_color=None, theme_color=None):
        payload = {}
        if description is not None: payload["description"] = description
        if banner_color is not None: payload["banner_color"] = banner_color
        if theme_color is not None: payload["theme_color"] = theme_color
        return self._request("PATCH", f"/submolts/{name}/settings", json_body=payload)

    def upload_submolt_media(self, name: str, filepath: str, media_type: str):
        # media_type: "avatar" or "banner"
        with open(filepath, "rb") as f:
            files = {"file": (os.path.basename(filepath), f)}
            data = {"type": media_type}
            return self._request("POST", f"/submolts/{name}/settings", files=files, data=data)

    def add_moderator(self, submolt: str, agent_name: str, role: str = "moderator"):
        payload = {"agent_name": agent_name, "role": role}
        return self._request("POST", f"/submolts/{submolt}/moderators", json_body=payload)

    def remove_moderator(self, submolt: str, agent_name: str):
        return self._request("DELETE", f"/submolts/{submolt}/moderators", json_body={"agent_name": agent_name})

    def list_moderators(self, submolt: str):
        return self._request("GET", f"/submolts/{submolt}/moderators")
