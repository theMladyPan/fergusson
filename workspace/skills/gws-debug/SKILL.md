---
name: gws-debug
version: 1.0.0
description: "Diagnose and repair gws authentication, scopes, token caches, and headless OAuth without confusing service-account ADC with personal Google user access."
metadata:
  openclaw:
    category: "utility"
    requires:
      bins: ["gws"]
      skills: []
---

# GWS Debug and Setup

Use this skill whenever `gws` returns 401/403 errors, credentials are missing or expired, scopes are wrong, or OAuth must be configured on a headless host.

## Identity rule

- A consumer `@gmail.com` mailbox must use interactive user OAuth.
- Domain-Wide Delegation applies only to users in a Google Workspace-managed domain and requires a Workspace Super Admin.
- `GOOGLE_APPLICATION_CREDENTIALS` is service-account ADC and must not be used to access or impersonate a consumer Gmail mailbox.
- A Cloud IAM role such as `roles/serviceusage.serviceUsageConsumer` fixes quota-project authorization only. It does not grant access to a user's Gmail, Drive, or Calendar.
- In Fergusson, Chirp uses `STT_CREDENTIALS_FILE`. The service environment must not define `GOOGLE_APPLICATION_CREDENTIALS`, because `gws` treats ADC as an authentication fallback.

## Golden rules

- Start with read-only diagnostics: `gws --version`, `gws auth status`, then one raw API read.
- If OAuth is missing or invalid, authenticate once with every required service. Calling login later with a smaller service set can replace the saved scopes.
- Never print credential files, refresh tokens, access tokens, or service-account private keys.
- Logout, cache removal, package upgrades, IAM changes, and service restarts require explicit user confirmation.
- After login, verify the account identity before any send, update, upload, or calendar write.
- Do not switch to another CLI when `gws` auth fails; diagnose the failing credential source.

## Helper

Use the tracked helper for repeatable diagnostics:

```bash
bash workspace/skills/gws-debug/scripts/gws-helper.sh status
bash workspace/skills/gws-debug/scripts/gws-helper.sh verify gmail drive calendar
bash workspace/skills/gws-debug/scripts/gws-helper.sh auth-combined
```

`auth-combined` starts the remote OAuth callback, prints the consent URL, and prints an SSH tunnel command for the callback port. The user must run the tunnel from their workstation before opening the URL.

## Triage

### No credentials / 401

```bash
gws auth status
```

If no user credential exists, run combined OAuth:

```bash
gws auth login --services gmail,chat,drive,calendar,docs,sheets,tasks
```

On a headless remote host, prefer the helper so the callback tunnel instructions are generated.

### Invalid grant

The user refresh token was revoked or expired. Run the combined OAuth flow again, then clear stale token caches and verify.

### 403 serviceUsageConsumer while ADC is set

This usually means `gws` fell back to a Google service account. First determine whether the requested data belongs to a user.

- Consumer Gmail: remove ADC from the service environment and use user OAuth. Do not grant IAM roles as a substitute.
- Workspace-domain user: Domain-Wide Delegation may be possible in custom code, but supported `gws` releases do not impersonate a Workspace user.
- Robot-owned Cloud resource: only then evaluate the requested Cloud IAM role.

### Scope failure

Inspect `gws auth status`. Re-run login once with the full combined service list. Do not authenticate one service at a time.

### Stale token cache

Only after explicit confirmation:

```bash
bash workspace/skills/gws-debug/scripts/gws-helper.sh heal-cache
```

The helper backs up and removes both user and service-account access-token caches before refreshing status.

## Verification

Use raw, read-only calls:

```bash
gws gmail users getProfile --params '{"userId":"me"}' --format json
gws drive files list --params '{"pageSize":1,"q":"trashed=false","fields":"files(id,name)"}' --format json
gws calendar calendarList list --params '{"maxResults":1}' --format json
```

Confirm the Gmail profile is the intended account before permitting writes.

## Command drift

For validation or syntax errors, inspect once and retry once:

```bash
gws gmail --help
gws schema gmail.users.messages.list
```

Do not guess command shapes repeatedly.
