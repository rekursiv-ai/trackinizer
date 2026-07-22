# Security Policy

## Why this file exists

trackinizer is a Postgres-backed HTTP server with a typed client. It accepts
untrusted requests, builds SQL against a live database, holds a database
connection string, and can be pointed at external URLs. Some of these paths can
leak credentials, corrupt data, or execute unintended queries. Security reports
need a private path so exploit details are not published before review.

## Reporting a vulnerability

Please report suspected security vulnerabilities privately by emailing hello@rekursiv.ai.

Include:

- Affected version or commit.
- Steps to reproduce.
- Expected impact.
- Any suggested mitigation.

Please do not open public issues for vulnerabilities until we have investigated and coordinated disclosure.

## Scope

Security reports are especially useful for:

- SQL injection or unsafe query construction against the backing database.
- Authentication, authorization, or session-handling flaws in the HTTP server.
- Server-side request forgery (SSRF) or unsafe URL handling in server or client.
- Exposure of the database connection string, DSN, or other credentials in
  logs, errors, or responses.
- Dependency or packaging issues that affect installed users.
- Supply-chain concerns in the published wheel or its dependency set.
