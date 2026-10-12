# Security Policy

The ClassPilot AI team takes the security and privacy of student data, Google credentials, and AI communication seriously. This document describes our security policy, supported versions, and how to report vulnerabilities responsibly.

---

## 1. Supported Versions

Security updates are actively applied to the following versions:

| Version | Supported |
|---|---|
| 0.2.x | :white_check_mark: Active maintenance |
| < 0.2.0 | :x: End of Life |

---

## 2. Reporting a Vulnerability

If you discover a potential security vulnerability or credential leak within ClassPilot AI, please **do not open a public GitHub issue**.

### Preferred Disclosure Method
Please report security vulnerabilities privately via:
1. **GitHub Private Vulnerability Reporting**:
   Navigate to the [Security Advisories tab](https://github.com/Rama-chandra55/classpilot-ai/security/advisories) of the repository and click **Report a vulnerability**.
2. **Direct Security Contact**:
   If GitHub Private Vulnerability Reporting is unavailable, contact the project maintainer directly at:
   `rock6262712@gmail.com` with the subject line: `[SECURITY] ClassPilot AI Vulnerability Report`.

### What to Include in Your Report
Please provide:
- A clear description of the vulnerability and its potential impact.
- Step-by-step reproduction instructions or a minimal proof of concept (PoC).
- Affected versions, endpoints, or modules (e.g., `classpilot/mcp_auth.py`, `classpilot/google_oauth.py`).
- Any suggested fixes or remediations.

### Response Commitment
- **Initial Acknowledgment**: Within 48 hours of receipt.
- **Triage & Assessment**: Within 5 business days.
- **Fix & Public Advisory**: Coordinated release and CVE assignment (if applicable) following thorough verification.

---

## 3. Security Foundations & Built-In Defenses

For background on the technical security architecture implemented in ClassPilot AI, please review [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/PRIVACY.md`](docs/PRIVACY.md), specifically:
- **Dual-OAuth Domain Isolation**: Strict barrier between Google OAuth access tokens and MCP client tokens.
- **At-Rest Token Encryption**: All stored refresh tokens encrypted via AES-128-CBC + HMAC-SHA256 (Fernet).
- **Opaque Hashed MCP Tokens**: Bearer tokens are stored exclusively as SHA-256 hashes.
- **Memory-Only Processing**: No student attachments or files are persisted to disk.
- **Thread-Safe In-Flight Coalescing**: Mitigates speculative browser OAuth races.
