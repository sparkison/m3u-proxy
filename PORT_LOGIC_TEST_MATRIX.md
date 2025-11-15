# Port Logic Test Matrix

## Test Cases for New Port Logic (Commit 2811b11)

### Scenario 1: Reverse Proxy with SSL (NGINX Proxy Manager)

**Environment:**

- `PUBLIC_URL=http://m3uhek.urztn.com/m3u-proxy` (no port, no scheme)
- `PORT=8085`
- `X-Forwarded-Scheme: https` (from NPM)

**Expected Behavior:**

- `url_port = None` (no port in PUBLIC_URL)
- `https_detected = True`
- `netloc = "m3uhek.urztn.com"` (no port)
- `base = "https://m3uhek.urztn.com/m3u-proxy"`
- `base_proxy_url = "https://m3uhek.urztn.com/m3u-proxy/hls/{stream_id}"`

**Result:** ✅ CORRECT - No internal port in URL

---

### Scenario 2: Direct Access (No Reverse Proxy)

**Environment:**

- `PUBLIC_URL=http://192.168.1.100/m3u-proxy` (no port)
- `PORT=8085`
- No `X-Forwarded-*` headers

**Expected Behavior:**

- `url_port = None`
- `https_detected = False`
- `port = 8085` and `port != 80`
- `netloc = "192.168.1.100:8085"` (include port)
- `base = "http://192.168.1.100:8085/m3u-proxy"`
- `base_proxy_url = "http://192.168.1.100:8085/m3u-proxy/hls/{stream_id}"`

**Result:** ✅ CORRECT - Internal port included for direct access

---

### Scenario 3: Explicit Port in PUBLIC_URL

**Environment:**

- `PUBLIC_URL=https://m3uhek.urztn.com:8443/m3u-proxy` (explicit port)
- `PORT=8085`
- `X-Forwarded-Scheme: https`

**Expected Behavior:**

- `url_port = 8443` (explicit port in PUBLIC_URL)
- `https_detected = True`
- `netloc = "m3uhek.urztn.com:8443"` (respect explicit port)
- `base = "https://m3uhek.urztn.com:8443/m3u-proxy"`
- `base_proxy_url = "https://m3uhek.urztn.com:8443/m3u-proxy/hls/{stream_id}"`

**Result:** ✅ CORRECT - Explicit port is respected

---

### Scenario 4: HTTP Reverse Proxy (Port 80)

**Environment:**

- `PUBLIC_URL=http://m3uhek.urztn.com/m3u-proxy`
- `PORT=8085`
- `X-Forwarded-Proto: http`

**Expected Behavior (AFTER FIX):**

- `url_port = None`
- `https_detected = False`
- `reverse_proxy_detected = True` (via X-Forwarded-Proto header)
- `netloc = "m3uhek.urztn.com"` (no port)
- `base = "http://m3uhek.urztn.com/m3u-proxy"`

**Result:** ✅ FIXED - No port included for HTTP reverse proxy

---

### Scenario 5: Localhost Development

**Environment:**

- `PUBLIC_URL=http://localhost/m3u-proxy`
- `PORT=8085`
- No reverse proxy headers

**Expected Behavior:**

- `url_port = None`
- `https_detected = False`
- `port = 8085` and `port != 80`
- `netloc = "localhost:8085"`
- `base = "http://localhost:8085/m3u-proxy"`

**Result:** ✅ CORRECT - Port included for localhost

---

### Scenario 6: No PUBLIC_URL Set

**Environment:**

- `PUBLIC_URL=None`
- `PORT=8085`

**Expected Behavior:**

- Falls to `else` block (line 794)
- `base = "http://localhost:8085"`
- `base_proxy_url = "http://localhost:8085/hls/{stream_id}"`

**Result:** ✅ CORRECT - Fallback to localhost with port

---

## IDENTIFIED BUG: Scenario 4 (FIXED)

**Problem:** When using HTTP reverse proxy (not HTTPS), the code still adds the internal port `:8085` because:

- `https_detected = False` (it's HTTP, not HTTPS)
- `port = 8085` and `port != 80`
- So it goes to line 783: `netloc = f"{host}:{port}"`

**This breaks HTTP reverse proxies!**

**Example:**

- User has HTTP reverse proxy on port 80
- PUBLIC_URL = `http://m3uhek.urztn.com/m3u-proxy`
- Generated URL: `http://m3uhek.urztn.com:8085/m3u-proxy/hls/{id}/segment`
- ❌ Port 8085 not accessible externally!

---

## SOLUTION (IMPLEMENTED)

Added `detect_reverse_proxy()` function that checks for **any** reverse proxy headers:

- `x-forwarded-for`
- `x-forwarded-proto`
- `x-forwarded-scheme`
- `x-forwarded-host`
- `x-forwarded-port`
- `x-forwarded-ssl`
- `x-real-ip`
- `forwarded`
- `front-end-https`
- `cf-connecting-ip` (Cloudflare)
- `true-client-ip` (Cloudflare Enterprise)
- `x-original-forwarded-for` (AWS ELB)

**New Port Logic:**

1. If `PUBLIC_URL` has explicit port → use it
2. If **any** reverse proxy detected → no port (proxy handles it)
3. If direct access with non-standard port → include port
4. Otherwise → no port

This now works correctly for:

- ✅ HTTPS reverse proxies (NGINX Proxy Manager, Caddy, Traefik, etc.)
- ✅ HTTP reverse proxies (NGINX, Apache, etc.)
- ✅ Direct access (localhost, IP address)
- ✅ Explicit ports in PUBLIC_URL
