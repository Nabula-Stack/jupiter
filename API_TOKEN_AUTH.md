# API Token Authentication

The Jupiter API supports two authentication methods:

## 1. Session Auth (Browser/UI)
- Used by the admin dashboard
- Authenticated via Django session cookies
- No additional setup required

## 2. Token Auth (Programmatic)
- Used by external tools (Homepage, monitoring, scripts, etc.)
- Requires an API token in the `Authorization: Bearer` header
- Non-interactive, suitable for automation

---

## Creating an API Token

### Generate Token for a User

**Local Development:**
```bash
python manage.py generate_api_token admin
```

**Docker Compose:**
```bash
docker exec jupiter-web python manage.py generate_api_token admin
```

**k3s/Kubernetes:**
```bash
kubectl exec -n jupiter deployment/web -- python manage.py generate_api_token admin
```

This outputs:
```
Created token for user 'admin':

  Token: a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6

Usage in API calls:
  curl -H "Authorization: Bearer a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6" \
       https://jupiter.prod.home/api/v1/hosts/metrics
```

---

## Using the Token

### cURL
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  https://jupiter.prod.home/api/v1/hosts/metrics
```

### Python (requests)
```python
import requests

headers = {
    "Authorization": "Bearer YOUR_TOKEN"
}
response = requests.get(
    "https://jupiter.prod.home/api/v1/hosts/metrics",
    headers=headers
)
print(response.json())
```

### Homepage Widget (YAML)
```yaml
Infrastructure:
  - ESXi m920q:
      widget:
        type: customapi
        url: https://jupiter.prod.home/api/v1/hosts/metrics
        method: GET
        headers:
          Authorization: "Bearer YOUR_TOKEN"
        cache: 30
        mappings:
          - field: m920q.CPU
            label: CPU
            format: percent
          - field: m920q.memory_percent
            label: RAM
            format: percent
          - field: m920q.storage_percent
            label: Storage
            format: percent
```

---

## Managing Tokens

### List All Tokens
```bash
python manage.py generate_api_token --list
```

Output:
```
API Tokens:
------------------------------------------------------------
  User: admin               | Staff: True  | Token: a1b2c3d4e5f...
  User: viewer             | Staff: True  | Token: x9y8z7a6b5c...
------------------------------------------------------------
```

### Delete a Token
```bash
python manage.py generate_api_token --delete admin
```

---

## Security Notes

- **Treat tokens like passwords** — keep them secret
- **Use HTTPS only** (https://jupiter.prod.home) — tokens sent in headers
- **Rotate tokens regularly** — delete and regenerate if compromised
- **Staff privilege required** — tokens only work for users with `is_staff=True`
- **No token expiry** — delete to revoke access (manual management)

---

## Troubleshooting

**401 Unauthorized**
- Token is missing, invalid, or for an inactive user
- Check: `Authorization: Bearer` header format (space between Bearer and token)
- Verify token exists: `python manage.py generate_api_token --list`

**403 Forbidden**
- User is not staff
- Generate token for an admin user or promote user to staff in Django admin

**CORS / Origin errors**
- Ensure `DJANGO_CSRF_TRUSTED_ORIGINS` includes your API consumer origin
- Token auth bypasses CSRF (safe for non-browser clients)
