# Security

## Reporting a vulnerability

Do not include tokens, photos, Telegram user IDs or database contents in public
issues. If the repository offers **Security → Report a vulnerability**, use that
private reporting channel. Otherwise open an issue asking for a private contact,
without exploit details or sensitive attachments.

## Deployment boundary

- Run one polling instance per bot token and state database.
- Keep the default user allowlist. Open access requires explicit configuration.
- Groups require a separate allowlist; use private chats for private photos.
- Use HTTPS for a remote backend. Plain HTTP is accepted only on loopback.
- The backend service key authorizes the bot to assert `telegram:*` principals.
  It must not be shared with untrusted clients or reused as an administrator token.
- Redirects are disabled; download sizes and read deadlines are bounded.
- The bot checks size and MIME. The backend must perform actual image decoding,
  workflow authorization, ownership, quotas and artifact validation.
- Keep `.env`, SQLite/WAL files, backups and user content outside Git. Database
  files use private permissions. Encrypt backups and restrict host access.

## Data and reliability

Pending comparisons store photo bytes in SQLite. The payload is cleared when a
comparison finishes, while identifiers and delivery metadata remain. SQLite/WAL
pages, backups, Telegram and the backend may retain earlier data. Logical deletion
is not a secure-erase guarantee. Configure a retention policy for each service.

Backend requests use idempotency keys. Telegram delivery and SQLite confirmation
are not one transaction: a crash between them can produce duplicate delivery.
Do not promise exactly-once delivery.

The project is not a sandbox for an untrusted backend and does not provide
network-level DDoS protection. Use a restricted service key and host-level controls.
