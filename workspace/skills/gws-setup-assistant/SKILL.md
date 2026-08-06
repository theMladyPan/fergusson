---
name: gws-setup-assistant
version: 2.0.0
description: "Compatibility handoff for Google Workspace CLI setup; delegates authentication and permission troubleshooting to gws-debug."
metadata:
  openclaw:
    category: "persona"
    requires:
      bins: ["gws"]
      skills: ["gws-debug"]
---

# Google Workspace Setup Assistant

> **PREREQUISITE:** Load `gws-debug` before executing this workflow.

This skill remains as a compatibility entry point. Follow `gws-debug` for all authentication, scope, token-cache, headless OAuth, and service-account diagnosis.

## Readiness handoff

After `gws-debug` confirms the intended user identity, verify each service with one raw read:

```bash
gws gmail users getProfile --params '{"userId":"me"}' --format json
gws drive files list --params '{"pageSize":1,"q":"trashed=false","fields":"files(id,name)"}' --format json
gws calendar calendarList list --params '{"maxResults":1}' --format json
```

Do not proceed to email, Drive, or Calendar writes unless all required services pass and Gmail reports the intended account.
