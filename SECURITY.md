# Security Policy

## Reporting Security Vulnerabilities

**Please do NOT open a public GitHub issue for security vulnerabilities.**

Instead, use [GitHub Security Advisories](https://github.com/bogware/bog-agents/security/advisories/new)
to report vulnerabilities privately.

Include in your report:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Affected versions
- A proposed fix (if any)

We aim to acknowledge reports within 48 hours and provide a resolution timeline
within 7 days of confirmation.

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.7.x   | ✅ Active support |
| 0.6.x   | ⚠️ Security patches only |
| < 0.6   | ❌ End of life |

## Security Design

- **API keys** are stored via `bog-agents vars set` (encrypted vault) or environment variables; never hardcoded.
- **Daemon API** binds to `127.0.0.1` (localhost only) by default and requires a token stored at `~/.bog-agents/daemon/token` (mode 0o600).
- **Webhook secrets** are validated with HMAC-SHA256 (`hmac.compare_digest` for timing-safe comparison).
- **File output paths** are restricted to the user's home directory and `/tmp` to prevent path traversal.
- **Git hook scripts** use `shlex.quote()` to prevent shell injection.
- **Auth token comparison** uses `hmac.compare_digest` to prevent timing attacks.
