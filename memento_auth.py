"""memento_auth — Keycloak/OIDC team gate for shared memory (optional extra).

Turns the *self-asserted* ``namespace`` (= team) of the Postgres team backend
into a **token-derived** one: instead of trusting ``namespace=<arg>`` from the
caller, the team is read from a verified Keycloak access token. A user can only
read/write the team(s) they belong to (their Keycloak ``groups``).

This is **Option A** — enforcement lives in the per-developer MCP server, so it
stops accidental cross-team access and gives correct ``actor`` attribution. It
is *not* hard isolation: a developer who holds the Postgres credentials can
still bypass it. For that, put the DB behind a gateway that does this same check
server-side (see README → "Hardened team mode").

Login uses the OAuth2 **device-authorization grant** (no browser-redirect
handling — right for a CLI/dev tool):

    python memento_auth.py login      # one-time, opens a code to enter
    python memento_auth.py whoami      # show identity + teams
    python memento_auth.py logout

Off by default. Enable with ``MEMENTO_AUTH=keycloak`` and:

    MEMENTO_AUTH=keycloak
    MEMENTO_OIDC_ISSUER=https://kc.example.com/realms/<realm>
    MEMENTO_OIDC_CLIENT_ID=memento            # default: memento
    MEMENTO_OIDC_CLIENT_SECRET=...            # only for confidential clients
    MEMENTO_OIDC_TEAMS_CLAIM=groups           # default: groups
    MEMENTO_OIDC_AUDIENCE=...                 # optional, verified if set

Stdlib-only for the OAuth/HTTP flow (urllib). Signature verification needs
``pyjwt[crypto]`` — install the extra:  ``pip install devin-memento[team-auth]``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# ── config ────────────────────────────────────────────────────────────────────

MEMENTO_HOME = os.path.expanduser(os.environ.get("MEMENTO_HOME", "~/.memento"))
_TOKENS_PATH = os.path.join(MEMENTO_HOME, "auth.json")

# Refresh the access token this many seconds before it actually expires, so a
# call started just under the wire still lands with a valid token.
_EXP_SKEW = 30
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class AuthError(Exception):
    """Raised for any auth failure the caller should surface verbatim."""


def enabled() -> bool:
    return os.environ.get("MEMENTO_AUTH", "").lower() in ("keycloak", "oidc", "1", "on", "true")


def _cfg() -> dict:
    issuer = os.environ.get("MEMENTO_OIDC_ISSUER", "").rstrip("/")
    if not issuer:
        raise AuthError("MEMENTO_AUTH is on but MEMENTO_OIDC_ISSUER is not set "
                        "(e.g. https://kc.example.com/realms/myrealm).")
    return {
        "issuer": issuer,
        "client_id": os.environ.get("MEMENTO_OIDC_CLIENT_ID", "memento"),
        "client_secret": os.environ.get("MEMENTO_OIDC_CLIENT_SECRET", ""),
        "teams_claim": os.environ.get("MEMENTO_OIDC_TEAMS_CLAIM", "groups"),
        "audience": os.environ.get("MEMENTO_OIDC_AUDIENCE", ""),
    }


# ── OIDC discovery + small HTTP helpers (stdlib) ──────────────────────────────

_DISCO_CACHE: dict = {}


def _discover(issuer: str) -> dict:
    """Fetch (and cache for the process) the issuer's OIDC discovery doc."""
    if issuer not in _DISCO_CACHE:
        url = issuer + "/.well-known/openid-configuration"
        _DISCO_CACHE[issuer] = _get_json(url)
    return _DISCO_CACHE[issuer]


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as exc:
        raise AuthError(f"OIDC request to {url} failed: {exc}") from exc


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        # OAuth errors come back as JSON in the body (e.g. authorization_pending).
        try:
            return json.loads(exc.read().decode())
        except Exception:
            raise AuthError(f"POST {url} failed: {exc}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"POST {url} failed: {exc}") from exc


# ── token cache ───────────────────────────────────────────────────────────────

def _load_tokens() -> dict:
    try:
        with open(_TOKENS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_tokens(tok: dict) -> None:
    os.makedirs(MEMENTO_HOME, exist_ok=True)
    tmp = _TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tok, f, indent=2)
    os.replace(tmp, _TOKENS_PATH)
    try:
        os.chmod(_TOKENS_PATH, 0o600)  # tokens are secrets
    except OSError:
        pass


def _client_creds(cfg: dict) -> dict:
    d = {"client_id": cfg["client_id"]}
    if cfg["client_secret"]:
        d["client_secret"] = cfg["client_secret"]
    return d


# ── login (device-authorization grant) ────────────────────────────────────────

def login() -> dict:
    """Run the device flow interactively and persist the resulting tokens."""
    cfg = _cfg()
    disco = _discover(cfg["issuer"])
    dev_ep = disco.get("device_authorization_endpoint")
    tok_ep = disco["token_endpoint"]
    if not dev_ep:
        raise AuthError("Issuer does not advertise a device_authorization_endpoint; "
                        "enable the OAuth2 Device flow on the Keycloak client.")

    start = _post_form(dev_ep, {**_client_creds(cfg), "scope": "openid profile email"})
    if "device_code" not in start:
        raise AuthError(f"Device authorization failed: {start.get('error_description') or start}")

    uri = start.get("verification_uri_complete") or start.get("verification_uri")
    print(f"\n  To sign in, open:  {uri}")
    if not start.get("verification_uri_complete"):
        print(f"  and enter the code:  {start['user_code']}")
    print("\n  Waiting for you to finish in the browser ...", flush=True)

    interval = int(start.get("interval", 5))
    deadline = time.time() + int(start.get("expires_in", 600))
    while time.time() < deadline:
        time.sleep(interval)
        resp = _post_form(tok_ep, {**_client_creds(cfg),
                                   "grant_type": _DEVICE_GRANT,
                                   "device_code": start["device_code"]})
        err = resp.get("error")
        if not err:
            resp["_obtained_at"] = int(time.time())
            _save_tokens(resp)
            who = claims().get("preferred_username") or claims().get("sub")
            print(f"  ✓ signed in as {who}; teams: {', '.join(teams()) or '(none)'}")
            return resp
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        raise AuthError(f"Login failed: {resp.get('error_description') or err}")
    raise AuthError("Login timed out before you finished in the browser.")


def logout() -> None:
    try:
        os.remove(_TOKENS_PATH)
    except OSError:
        pass


# ── token lifecycle ───────────────────────────────────────────────────────────

def _refresh(cfg: dict, tok: dict) -> dict:
    rt = tok.get("refresh_token")
    if not rt:
        raise AuthError("Session expired and no refresh token; run: python memento_auth.py login")
    disco = _discover(cfg["issuer"])
    resp = _post_form(disco["token_endpoint"], {**_client_creds(cfg),
                                                "grant_type": "refresh_token",
                                                "refresh_token": rt})
    if resp.get("error"):
        raise AuthError("Session expired; run: python memento_auth.py login")
    resp["_obtained_at"] = int(time.time())
    _save_tokens(resp)
    return resp


def _valid_access_token() -> str:
    cfg = _cfg()
    tok = _load_tokens()
    if not tok.get("access_token"):
        raise AuthError("Not signed in. Run: python memento_auth.py login")
    age = int(time.time()) - tok.get("_obtained_at", 0)
    if age >= tok.get("expires_in", 300) - _EXP_SKEW:
        tok = _refresh(cfg, tok)
    return tok["access_token"]


# ── verification (PyJWT) ──────────────────────────────────────────────────────

_JWK_CLIENTS: dict = {}


def _verify(token: str) -> dict:
    try:
        import jwt  # PyJWT
        from jwt import PyJWKClient
    except ImportError as exc:
        raise AuthError("Team auth needs PyJWT. Install the extra: "
                        "pip install devin-memento[team-auth]") from exc
    cfg = _cfg()
    disco = _discover(cfg["issuer"])
    jwks_uri = disco["jwks_uri"]
    if jwks_uri not in _JWK_CLIENTS:
        _JWK_CLIENTS[jwks_uri] = PyJWKClient(jwks_uri)
    signing_key = _JWK_CLIENTS[jwks_uri].get_signing_key_from_jwt(token).key
    opts = {"verify_aud": bool(cfg["audience"])}
    return jwt.decode(token, signing_key, algorithms=["RS256", "ES256"],
                      audience=cfg["audience"] or None,
                      issuer=cfg["issuer"], options=opts)


def claims() -> dict:
    """Verified claims of the current access token."""
    return _verify(_valid_access_token())


def teams() -> list:
    """Teams the signed-in user belongs to (the configured groups claim,
    normalized — Keycloak emits group paths like ``/team-alpha``)."""
    cfg = _cfg()
    raw = claims().get(cfg["teams_claim"], [])
    if isinstance(raw, str):
        raw = [raw]
    return [g.lstrip("/") for g in raw if g]


def actor() -> str:
    """Stable identity string for attribution on saved memories."""
    c = claims()
    return c.get("preferred_username") or c.get("email") or c.get("sub", "")


# ── the one call the MCP server uses ──────────────────────────────────────────

def resolve_namespace(requested) -> str:
    """Map a *requested* namespace to the team the token actually authorizes.

    - requested in my teams  → use it
    - requested NOT in teams → denied (AuthError)
    - no request, one team   → that team
    - no request, many teams → ambiguous (AuthError — caller must pick)
    """
    mine = teams()
    if not mine:
        raise AuthError("Your account is in no Keycloak group, so no team memory "
                        "is accessible. Ask an admin to add you to a team group.")
    if requested:
        if requested in mine:
            return requested
        raise AuthError(f"Not authorized for team '{requested}'. "
                        f"Your teams: {', '.join(mine)}.")
    if len(mine) == 1:
        return mine[0]
    raise AuthError(f"You belong to several teams ({', '.join(mine)}); "
                    "pass namespace=<team> to choose one.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "whoami"
    try:
        if cmd == "login":
            login()
        elif cmd == "logout":
            logout()
            print("  signed out.")
        elif cmd in ("whoami", "teams"):
            print(f"  user:  {actor()}")
            print(f"  teams: {', '.join(teams()) or '(none)'}")
        else:
            print(__doc__)
            return 2
    except AuthError as exc:
        print(f"  auth error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
