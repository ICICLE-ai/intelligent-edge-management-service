============================================================
ADDING TAPIS OAUTH (OAuth2 Authorization Code) TO A WEB APP
============================================================

This is the exact approach used in the ICICLE Edge Control Plane
(FastAPI + Starlette sessions). The pattern is framework-agnostic;
the code samples are FastAPI but the flow applies to any web app.

It uses the standard OAuth2 "authorization code" flow against a
Tapis tenant (e.g. https://icicleai.tapis.io). No Tapis SDK required —
just plain HTTPS calls and a signed cookie session.

------------------------------------------------------------
0. HOW THE FLOW WORKS (mental model)
------------------------------------------------------------

  Browser                Your App                 Tapis
    |  GET /              |                          |
    |  (not logged in)    |                          |
    | <-- 303 /auth/start |                          |
    |  GET /auth/start    |                          |
    | <-- 302 redirect to Tapis /authorize?...       |
    |---------------------------------------------->  |
    |        user logs in with Tapis credentials      |
    | <----- 302 redirect to /auth/callback?code=...  |
    |  GET /auth/callback?code=...                     |
    |                     | POST /v3/oauth2/tokens --> |
    |                     | <-- access_token (JWT)     |
    |                     | store username in session  |
    | <-- 303 to original page (now logged in)        |

Key points:
  - You register ONE callback URL with Tapis: https://YOUR_APP/auth/callback
  - The "redirect_uri" your app sends MUST match that registered URL
    EXACTLY (scheme, host, path, no trailing slash differences).
  - The access token is a JWT; the username is in the "tapis/username" claim.


------------------------------------------------------------
1. REGISTER AN OAUTH CLIENT IN TAPIS (one-time, per app)
------------------------------------------------------------

You need a client_id and client_key (secret) from the Tapis tenant,
plus a registered callback_url.

Option A — Tapis CLI / API (create a client):

  curl -X POST https://<TENANT>.tapis.io/v3/oauth2/clients \
    -H "X-Tapis-Token: $JWT" \
    -H "Content-Type: application/json" \
    -d '{
      "client_id": "my-app-client",
      "callback_url": "https://my-app.pods.<tenant>.tapis.io/auth/callback",
      "description": "My App OAuth client"
    }'

  The response includes "client_id" and "client_key" (the secret).
  SAVE THE client_key — it is shown once.

Option B — ask your tenant admin (for ICICLE: contact the Tapis/ICICLE
admin, e.g. via GitHub issue or Slack) to create the client and add the
callback URL.

IMPORTANT:
  - callback_url MUST equal exactly:  https://YOUR_PUBLIC_HOST/auth/callback
  - If your app moves (new domain / ngrok URL), update the client's
    callback_url, or you get:
       "redirect_uri query parameter does not match the registered
        callback_url for the client."
  - You can update an existing client's callback:
       PUT https://<TENANT>.tapis.io/v3/oauth2/clients/<client_id>
       body: {"callback_url": "https://NEW_HOST/auth/callback"}


------------------------------------------------------------
2. ENVIRONMENT VARIABLES THE APP READS
------------------------------------------------------------

  TAPIS_BASE_URL       = https://icicleai.tapis.io      (tenant base URL)
  TAPIS_CLIENT_ID      = my-app-client
  TAPIS_CLIENT_KEY     = <the client secret from step 1>
  APP_BASE_URL         = https://my-app.pods.icicleai.tapis.io
  APP_SECRET           = <random 32+ byte string; signs the session cookie>
  TAPIS_ADMIN_USERNAMES= alice,bob   (optional: who gets the admin role)

  # Optional override. If unset, the app derives:
  #   callback_url = APP_BASE_URL + "/auth/callback"
  # TAPIS_CALLBACK_URL = https://my-app.../auth/callback

Generate APP_SECRET:
  openssl rand -hex 32

NOTE on the callback: we deliberately DERIVE it from APP_BASE_URL so we
only have to set one URL. Just make sure APP_BASE_URL has NO trailing
slash and matches what you registered with Tapis.


------------------------------------------------------------
3. CONFIG OBJECT (load + validate env)
------------------------------------------------------------

# config.py
import os
from dataclasses import dataclass, field
from typing import List

@dataclass(frozen=True)
class TapisConfig:
    base_url: str
    client_id: str
    client_key: str
    callback_url: str
    admin_usernames: List[str] = field(default_factory=list)

    @property
    def oauth_base(self) -> str:
        return f"{self.base_url.rstrip('/')}/v3/oauth2"

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.client_id
                    and self.client_key and self.callback_url)

def load_tapis_config() -> TapisConfig:
    base_url = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
    tapis_base = os.getenv("TAPIS_BASE_URL", "https://icicleai.tapis.io").rstrip("/")
    callback = os.getenv("TAPIS_CALLBACK_URL", f"{base_url}/auth/callback").rstrip("/")
    admins = [u.strip() for u in os.getenv("TAPIS_ADMIN_USERNAMES", "").split(",") if u.strip()]
    return TapisConfig(
        base_url=tapis_base,
        client_id=os.getenv("TAPIS_CLIENT_ID", "").strip(),
        client_key=os.getenv("TAPIS_CLIENT_KEY", "").strip(),
        callback_url=callback,
        admin_usernames=admins,
    )


------------------------------------------------------------
4. THE OAUTH SERVICE (the core logic)
------------------------------------------------------------

# tapis_auth_service.py
import base64, json, secrets
from urllib.parse import urlencode
import httpx
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

_STATE_SALT = "my-app-oauth-state"
_STATE_MAX_AGE = 600   # seconds the "state" token stays valid

# --- "state" is a signed token that survives the round-trip to Tapis. ---
# It lets us (a) prevent CSRF and (b) remember where to send the user
# after login, WITHOUT needing a session cookie before login.

def _serializer(app_secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(app_secret, salt=_STATE_SALT)

def build_oauth_state(app_secret: str, next_path: str) -> str:
    return _serializer(app_secret).dumps({
        "n": secrets.token_urlsafe(12),
        "next": next_path if next_path.startswith("/") else "/",
    })

def parse_oauth_state(app_secret: str, state: str) -> str:
    try:
        data = _serializer(app_secret).loads(state, max_age=_STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise ValueError("Invalid or expired OAuth state — try again.")
    nxt = data.get("next", "/")
    return nxt if isinstance(nxt, str) and nxt.startswith("/") else "/"

# --- Step 1: build the URL we redirect the user to. ---
def authorize_url(cfg, state: str) -> str:
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.callback_url,   # MUST match registered callback
        "response_type": "code",
        "state": state,
    }
    return f"{cfg.oauth_base}/authorize?{urlencode(params)}"

# --- Step 2: exchange the ?code=... for tokens. ---
def exchange_code_for_token(cfg, code: str) -> dict:
    body = {
        "code": code,
        "redirect_uri": cfg.callback_url,   # MUST match again
        "grant_type": "authorization_code",
    }
    auth = base64.b64encode(f"{cfg.client_id}:{cfg.client_key}".encode()).decode()
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(f"{cfg.oauth_base}/tokens", json=body, headers=headers)
    if resp.status_code >= 400:
        raise ValueError(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    # Tapis wraps the payload in {"result": {...}} — unwrap it.
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    return data

# --- Helpers to read the JWT (no verification needed; Tapis already
#     authenticated. If you want, verify the signature with the tenant
#     public key, but reading the claim is enough for login). ---
def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}

def extract_access_token(data) -> str:
    # Tapis token responses vary in shape; handle the common ones.
    if isinstance(data, str) and data.count(".") >= 2:
        return data
    for key in ("access_token", "accessToken"):
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, str) and val.count(".") >= 2:
            return val
        if isinstance(val, dict):
            nested = val.get("access_token") or val.get("accessToken")
            if isinstance(nested, str) and nested.count(".") >= 2:
                return nested
    raise ValueError("Tapis token response missing access_token")

def username_from_token(access_token: str) -> str:
    claims = _decode_jwt_payload(access_token)
    return str(claims.get("tapis/username") or claims.get("sub", "").split("@")[0])


------------------------------------------------------------
5. THE ROUTES (/auth/start, /auth/callback, /auth/logout)
------------------------------------------------------------

# auth_routes.py  (FastAPI)
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

@router.get("/auth/start")
def start_oauth(request: Request, next: str = "/"):
    cfg = request.app.state.tapis
    if not cfg.configured:
        return RedirectResponse("/auth/login", status_code=303)
    state = build_oauth_state(request.app.state.app_secret, next)
    return RedirectResponse(authorize_url(cfg, state), status_code=302)

@router.get("/auth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    cfg = request.app.state.tapis
    if error or not code:
        return RedirectResponse("/auth/login", status_code=303)
    target = parse_oauth_state(request.app.state.app_secret, state)
    token_data = exchange_code_for_token(cfg, code)
    access_token = extract_access_token(token_data)
    username = username_from_token(access_token)
    # Persist who the user is in the signed session cookie.
    request.session["username"] = username
    request.session["access_token"] = access_token
    return RedirectResponse(target, status_code=303)

@router.get("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=303)


------------------------------------------------------------
6. SESSION + "REQUIRE LOGIN" MIDDLEWARE
------------------------------------------------------------

# main.py  (FastAPI app wiring)
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import RedirectResponse, JSONResponse
from urllib.parse import quote

PUBLIC_PREFIXES = ("/auth/", "/static/", "/api/health", "/docs", "/openapi.json")

def create_app():
    app = FastAPI()
    cfg = load_tapis_config()
    app.state.tapis = cfg
    app.state.app_secret = os.getenv("APP_SECRET", "dev-change-me")

    # Gate: redirect un-authenticated browser users to login.
    @app.middleware("http")
    async def require_login(request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if request.session.get("username"):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/auth/start?next={quote(nxt)}", status_code=303)

    # Session cookie MUST be added AFTER the gate so it runs FIRST
    # (Starlette runs the last-added middleware first).
    app.add_middleware(
        SessionMiddleware,
        secret_key=app.state.app_secret,
        https_only=cfg.base_url.startswith("https://") or
                   os.getenv("APP_BASE_URL", "").startswith("https://"),
        same_site="lax",
        max_age=14 * 24 * 3600,
    )

    app.include_router(auth_router)   # /auth/* must be registered
    # ... your other routers ...
    return app

Dependencies:
  pip install fastapi uvicorn httpx itsdangerous python-multipart


------------------------------------------------------------
7. COMMON PITFALLS (these cost us real time)
------------------------------------------------------------

1) "redirect_uri ... does not match the registered callback_url"
   -> The redirect_uri your app sends != what's registered in Tapis.
      Fix APP_BASE_URL (no trailing slash) OR update the client's
      callback_url. They must be byte-for-byte identical, including
      https:// and /auth/callback with no trailing slash.

2) Works locally, breaks in prod (or vice versa)
   -> You have ONE callback per environment. Either register both
      (local ngrok URL AND prod URL) on the client, or update the
      callback when you switch.

3) Session not persisting / login loop
   -> https_only=True on a cookie but serving over http (or behind a
      proxy that strips X-Forwarded-Proto). Run uvicorn with
      --proxy-headers --forwarded-allow-ips "*" behind Tapis/Traefik,
      and set https_only based on the PUBLIC scheme.

4) Cookie too big / token storage
   -> Storing the full JWT in the cookie can blow the 4KB cookie limit
      if the token is large. If so, store only the username and keep
      tokens server-side (or skip storing the token at all if you only
      need identity).

5) Don't expose /auth/* or /static or /health behind the login gate,
   or the redirect to Tapis can loop.


------------------------------------------------------------
8. QUICK CHECKLIST FOR YOUR FRIEND'S APP
------------------------------------------------------------

[ ] Get client_id + client_key from Tapis (register callback = APP/auth/callback)
[ ] Set env: TAPIS_BASE_URL, TAPIS_CLIENT_ID, TAPIS_CLIENT_KEY,
            APP_BASE_URL, APP_SECRET
[ ] Add /auth/start, /auth/callback, /auth/logout routes
[ ] Add SessionMiddleware (after the require-login middleware)
[ ] Add require-login gate; whitelist /auth/, /static/, /health
[ ] Confirm APP_BASE_URL has no trailing slash and matches registered callback
[ ] Open the site -> Tapis login -> lands back authenticated
