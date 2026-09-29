# Single sign-on between sibling sites

Elfak GIS Studio accepts the same accounts as Forestry PSC Preparation
(one username, one password, both products) and can additionally accept a
**signed handoff token** so a visitor who is already signed in on a
sibling site arrives here already signed in.

## How it works

1. The sibling site mints a short-lived token for the currently signed-in
   user and links to:

   ```
   https://<this-site>/sso/exchange?t=<token>
   ```

2. This site verifies the HMAC, checks the audience and the expiry, and
   signs the user straight in — no password prompt. It redirects to `/`.

3. A successful handoff also issues the normal 30-day "stay signed in"
   cookie, so later direct visits need no password either.

The token is **not** a session credential. It is a signed assertion that
the user is who the sibling site says they are, valid for a couple of
minutes, and only accepted by this site.

## Configuration

Both sites must share one secret. Set it on **both**:

| Variable        | Where       | Notes                                                        |
|-----------------|-------------|--------------------------------------------------------------|
| `SSO_SECRET`    | environment | 32+ random characters. Must be identical on both.            |
| `DATABASE_URL`  | environment | Neon Postgres, shared, so accounts and history match         |
| `SSO_MAX_TTL`   | environment | Optional. Rejects tokens claiming a longer life than this. `0` (default) = no cap. See [Token lifetime](#token-lifetime). |

Generate a secret with:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(32))"
```

If `SSO_SECRET` is unset the feature disables itself: `/sso/exchange`
sends the visitor to the normal login form and nothing breaks. A bad,
expired or forged token does the same — it is never an error page.

If the visitor is **already signed in**, a failed handoff does not show
that page at all: it redirects straight to `/`. Telling someone who is
already authenticated to "sign in with your username and password" and
then bouncing them back to the app reads as a broken product.

## Token format

`base64url(payload).base64url(hmac_sha256(secret, payload))`, no padding.

```json
{ "u": "student123", "aud": "elfakgisstudio", "exp": 1780000000 }
```

- `u` — the username, required
- `aud` — must be `elfakgisstudio`, so a token minted for another site
  is rejected. The pre-rename product name `elfakgisprostudio` is also
  accepted (see [Audience](#audience)); any other value is refused
- `exp` — Unix seconds, required to be in the future

The signature covers the encoded payload, so any edit to it invalidates
the token.

### Audience

Tokens minted before the product was renamed carry the old name
(`elfakgisprostudio`) in their `aud` claim. Those are signed with the same
shared secret and still assert "this user is signed in on the sibling site",
so they are accepted — otherwise every link already in circulation breaks at
once. `SSO_AUDIENCE_ALIASES` in `elfakgis/core/autshared.py` lists them.

New tokens should use the canonical `elfakgisstudio`. A token for any other
audience is still refused, so widening this has not turned the check into
accept-anything.

### Token lifetime

**A handoff token is a bearer credential carried in a URL.** Anyone who ever
sees that URL — browser history, a shared screenshot, a proxy or web-server
log, a `Referer` header, a chat message — can replay it until it expires,
from anywhere, without the password.

That makes a short `ttl` the whole point of the feature. The recommended
value is a few minutes; `180` is used throughout this document.

Do not mint long-lived handoff tokens. A 30-day handoff link is a 30-day
password bypass, for every copy of that link, forever until it is rotated.

To enforce this on this side, set `SSO_MAX_TTL` to the longest lifetime you
are willing to accept, in seconds:

```bash
SSO_MAX_TTL=600   # refuse any token claiming to live longer than 10 minutes
```

It defaults to `0` (no cap) for compatibility, because some siblings mint
long tokens. When it does reject a token it logs the lifetime that was
refused, so an unexpected rejection is visible in the logs rather than
silent:

```
SSO token rejected: lifetime 2592000s exceeds SSO_MAX_TTL 600s
```

### Minting (Python — sibling site)

```python
import base64, hmac, hashlib, json, os, time

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")

def sso_handoff_url(username: str, base: str, ttl: int = 180) -> str:
    secret = os.environ["SSO_SECRET"].encode()
    payload = {"u": username, "aud": "elfakgisstudio", "exp": int(time.time()) + ttl}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{base}/sso/exchange?t={body}.{sig}"
```

Use a short `ttl` — a couple of minutes is plenty for a page navigation,
and it keeps a leaked link from being useful for long. See
[Token lifetime](#token-lifetime) for why this matters more than it looks.

### Minting (JavaScript — sibling site)

```js
function ssoHandoffUrl(username, base) {
  const b64e = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
                        .replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  const enc = new TextEncoder().encode(
    JSON.stringify({ u: username, aud: 'elfakgisstudio',
                     exp: Math.floor(Date.now()/1000) + 180 }));
  const body = b64e(enc);
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(SSO_SECRET), { name:'HMAC', hash:'SHA-256' }, false, ['sign']);
  const sig = b64e(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(body)));
  return `${base}/sso/exchange?t=${body}.${sig}`;
}
```

Note this requires the same secret in the browser, which is only
appropriate if the sibling site is entirely yours. For anything else, mint
on the sibling's server and let the link point at the result.

## Verifying the wiring

With `SSO_SECRET` set on both sides, a valid token should land on the
studio already signed in, and a tampered one should fall back to the
login form:

```bash
# valid → 200 and ok:true
curl -i "https://<this-site>/sso/exchange?t=$(mint ...)&json=1"

# tampered → 401 and ok:false
curl -i "https://<this-site>/sso/exchange?t=deadbeef&json=1"
```

`?json=1` returns JSON instead of redirecting, which is easier to check
from a terminal.
