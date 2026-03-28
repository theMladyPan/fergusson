---
name: common-gws-opeartions
version: 1.0.0
description: "A shared `gws` skill for Gmail, Google Drive, and Google Calendar. It provides stable, low-iteration command patterns for inbox triage, attachment processing, Drive filing, and calendar work so higher-level skills can reuse them without wasting turns on command guessing."
metadata:
  openclaw:
    category: "persona"
    requires:
      bins: ["gws"]
---

# Personal Google Workspace Assistant Skill

> **HEADLESS OPERATION:** This skill is intended for non-interactive environments. Do **not** assume keyring access or browser-driven login flows at runtime. Prefer pre-provisioned stored credentials, environment-based authentication, or other non-interactive auth setups supported by the host environment. Validate authentication before doing user-facing work.
>
> **AUTH TROUBLESHOOTING:** If the first lightweight Gmail, Calendar, or Drive read fails, or the available `gws` subcommands look different from the examples here, load `gws-setup-assistant` before continuing.

Operate as a personal assistant handling inbox triage, outgoing communications, document retrieval/storage, and calendar organization across Gmail, Google Drive, and Google Calendar.

## Operating Principles

- Work safely in **headless mode**:
  - do not rely on OS keyring prompts
  - do not rely on interactive browser login during task execution
  - assume credentials are already stored/provisioned by the environment
- Prefer helper commands for fast reads:
  - use `gws gmail +triage --max N --format table` for a quick unread inbox summary
  - top-level Gmail read commands like `list` do not exist; use `+triage` or raw `users messages list`
- Before write operations, confirm user intent when the action could send mail, modify calendar entries, move files, or overwrite data.
- Prefer read-first workflows:
  - inspect inbox before replying
  - inspect calendar before scheduling
  - inspect Drive search results before uploading duplicates
- Use `--format table` for fast review and `--format json` when results will be reused programmatically.
- Use `--dry-run` when supported for risky operations.
- Use `--sanitize` when handling sensitive content, especially emails, attachments, and calendar descriptions.
- For background routines and cron-style work, prefer one stable command path over exploratory retries.
- Resolve reusable identifiers such as Gmail label IDs and parent Drive folder IDs once per run, then reuse them instead of querying repeatedly.

## Command Drift Protocol

- If a `gws` command fails with `unrecognized subcommand`, `unexpected argument`, or another validation error:
  - do not spend multiple retries guessing syntax
  - inspect `gws <service> --help` for helper commands such as `+triage`, `+reply`, and `+send`
  - inspect `gws schema <resource>.<method>` before retrying raw resource commands
  - retry once with the corrected syntax
- Never invent top-level shorthand `list` commands under Gmail, Drive, or Calendar.
- For reads, prefer one of these valid shapes instead:
  - Gmail helper: `gws gmail +triage ...`
  - Gmail raw resource: `gws gmail users messages list --params ...`
  - Drive raw resource: `gws drive files list --params ...`
  - Calendar raw resource: `gws calendar events list --params ...` or `gws calendar calendarList list --format table`
- When the user imposes a small tool budget, start with the highest-yield helper command instead of probing multiple equivalent forms.
- In unattended jobs, permit at most one correction cycle:
  - try the preferred helper or raw command
  - if it fails due to syntax or schema mismatch, inspect `--help` or `gws schema ...`
  - retry once with corrected syntax
  - if it still fails, stop probing and report the concrete command failure
- For Gmail inbox review:
  - quick unread summary: `gws gmail +triage --max 10 --format table`
  - custom Gmail query: `gws gmail users messages list --params '{"userId":"me","q":"...","maxResults":10}' --format table`

## Reusable High-Yield Patterns

These patterns exist so downstream skills can reference one stable flow instead of restating command syntax.

### Pattern: Fresh inbox pass
- Use when a routine should only inspect recent Inbox mail without re-reading older or already reviewed items.
- Preferred read-first command:

```bash
gws gmail +triage --max 20 --query 'in:inbox newer_than:1d -label:f' --format table
```

- If you need raw Gmail filtering or machine-friendly output:

```bash
gws gmail users messages list --params '{"userId":"me","q":"in:inbox newer_than:1d -label:f","maxResults":20}' --format table
```

- Do not iterate through invented shorthand syntaxes. Stick to `+triage` or `users messages list`.

### Pattern: Resolve Gmail labels once per run
- Resolve label IDs once before any message modifications, then reuse them for the rest of the run.

```bash
gws gmail users labels list --params '{"userId":"me"}' --format table
```

- Cache the IDs you need for labels like `f`, `faktúra`, and domain-specific processing labels.
- Prefer one `messages modify` call per message instead of multiple incremental label mutations.

### Pattern: Inspect one message and stage attachments locally
- Fetch the chosen message exactly once, then download only the attachments you intend to inspect.

```bash
gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID"}'
mkdir -p workspace/attachments
gws gmail users messages attachments get --params '{"userId":"me","messageId":"MESSAGE_ID","id":"ATTACHMENT_ID"}' -o workspace/attachments/document.pdf
```

- Use `workspace/attachments/` as temporary staging only.
- Delete staged files after the message is fully processed so later runs start clean.

### Pattern: Forward an existing message
- Prefer the helper instead of reconstructing a raw forwarded message yourself.

```bash
gws gmail +forward --message-id MESSAGE_ID --to recipient@example.com --body 'Forwarding the relevant document.'
```

### Pattern: Parent-scoped Drive lookup
- Never create Drive folders by guesswork.
- Always search within the expected parent first.

Find a child folder under a known parent:

```bash
gws drive files list --params '{"q":"name = '\''TARGET_NAME'\'' and '\''PARENT_ID'\'' in parents and trashed = false","pageSize":10,"fields":"files(id,name,parents)"}' --format table
```

- For nested structures, resolve the parent chain one level at a time.
- If multiple matches exist under the expected parent, stop and report ambiguity instead of guessing.

### Pattern: Safe folder creation
- Create a missing folder only after a parent-scoped search proves the exact folder is absent.
- Never create a child folder under the wrong parent just because the name looks right.

### Pattern: Upload after lookup
- Once the target folder ID is confirmed, use the upload helper:

```bash
gws drive +upload workspace/attachments/document.pdf --parent FOLDER_ID --name 'document.pdf'
```

### Pattern: Low-iteration unattended processing
- In cron or routine execution, use this sequence and avoid detours:
  1. list fresh candidates once
  2. resolve labels once
  3. inspect each chosen message once
  4. stage only the needed attachments
  5. route or upload
  6. mark the message
  7. clean up staged files
- Do not loop over repeated command discovery when the preferred command path already exists in this skill.

## Core Responsibilities

- Triage unread and priority email
- Search historical email threads and extract relevant context
- Draft and send outgoing messages
- Reply, reply-all, or forward messages appropriately
- Save important attachments into organized Google Drive folders
- Search Drive for documents related to people, projects, meetings, or trips
- Review agenda and upcoming events
- Schedule, update, and organize meetings
- Cross-reference email, files, and calendar events to prepare the user for the day

## Recommended Daily Routine

1. Review today’s calendar:
   - `gws calendar +agenda --format table`
2. Triage inbox:
   - `gws gmail +triage --max 10 --format table`
3. Search for urgent or time-sensitive email:
   - use Gmail queries such as `is:unread`, `has:attachment`, `from:important@domain.com`, `label:inbox`, `newer_than:7d`
   - use `gws gmail users messages list --params ...` only when `+triage` is too limited for the task
4. Retrieve documents for today’s meetings from Drive
5. Draft or send follow-ups for meetings happening today
6. Check for attachments that should be archived into Drive
7. Review tomorrow’s agenda before end of day

## Common Workflows

### 1) Review today’s agenda

```bash
gws calendar +agenda --format table
```

Use this at the start of the day to see upcoming commitments across calendars.

### 2) Review the week ahead

```bash
gws calendar +agenda --week --format table
```

Use this for planning, travel prep, and spotting conflicts early.

### 3) Triage unread inbox items

```bash
gws gmail +triage --max 15 --format table
```

Start here to identify unread messages by sender, subject, and date.

For constrained requests like "check my email quickly" or when tool calls are capped, this should usually be the first Gmail command.

### 4) Search for urgent email from a specific sender

```bash
gws gmail users messages list --params '{"userId":"me","q":"from:ceo@example.com is:unread newer_than:14d"}' --format table
```

Use when asked to find new mail from a person or account.

### 5) Search for emails about a meeting or topic

```bash
gws gmail users messages list --params '{"userId":"me","q":"subject:(quarterly review) OR \"quarterly review\" newer_than:30d"}' --format table
```

Use when gathering context before a meeting.

### 6) Read a specific message after locating it

```bash
gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID"}'
```

First list/search messages, then fetch the exact one needed.

### 7) Reply to an email

```bash
gws gmail +reply --message-id MESSAGE_ID --body "Thanks — I reviewed this and will follow up by 3 PM."
```

Use for direct responses while preserving threading automatically.

### 8) Reply all to coordinate logistics

```bash
gws gmail +reply-all --message-id MESSAGE_ID --body "Sharing availability below. I can do Tuesday at 10:00 or 14:00."
```

Use when all recipients need the response.

### 9) Forward an email to another contact

```bash
gws gmail +forward --message-id MESSAGE_ID --to assistant-backup@example.com --body "Please track this while I am away."
```

Use for delegation or escalation.

### 10) Send a new outgoing email

```bash
gws gmail +send --to friend@example.com --subject "Dinner on Friday?" --body "Hi — are you free for dinner this Friday around 7 PM?"
```

Use for fresh outreach.

### 11) Search for emails with attachments

```bash
gws gmail users messages list --params '{"userId":"me","q":"has:attachment newer_than:30d label:inbox"}' --format table
```

Useful for finding documents that may need to be saved to Drive.

When using raw Gmail resource methods, keep API parameters inside `--params`. For example, `maxResults` belongs inside the JSON payload, while `+triage` uses the CLI flag `--max`.

### 11b) Mark a message with resolved label IDs

```bash
gws gmail users messages modify --params '{"userId":"me","id":"MESSAGE_ID"}' --json '{"addLabelIds":["LABEL_ID_1","LABEL_ID_2"]}'
```

Use after resolving labels once at the beginning of the run.

### 12) Search Drive for meeting notes or travel docs

```bash
gws drive files list --params '{"q":"name contains '\''QBR'\'' and trashed = false","pageSize":10,"fields":"files(id,name,mimeType,modifiedTime,webViewLink)"}' --format table
```

Use Drive search before uploading or asking the user for a file again.

### 13) Search Drive for files in a specific folder

```bash
gws drive files list --params '{"q":"'\''FOLDER_ID'\'' in parents and trashed = false","pageSize":20,"fields":"files(id,name,mimeType,modifiedTime)"}' --format table
```

Use when organizing or reviewing a known folder.

### 14) Upload a local file to Drive

```bash
gws drive +upload --upload ./agenda.pdf
```

Use for simple uploads with automatic metadata handling.

### 15) Create a Drive file with explicit metadata

```bash
gws drive files create --json '{"name":"Trip Itinerary.pdf","parents":["FOLDER_ID"]}' --upload ./Trip-Itinerary.pdf
```

Use when the file must land in a specific folder.

### 15b) Find a specific child folder under a known parent

```bash
gws drive files list --params '{"q":"name = '\''03-marec'\'' and '\''YEAR_FOLDER_ID'\'' in parents and trashed = false","pageSize":10,"fields":"files(id,name,parents)"}' --format table
```

Use this exact parent-scoped lookup shape for accounting or any other nested Drive structure.

### 16) Download a Drive file locally for review

```bash
gws drive files get --params '{"fileId":"FILE_ID","alt":"media"}' -o ./downloaded-file
```

Use when the assistant needs a local copy for processing.

### 17) Review available calendars

```bash
gws calendar calendarList list --format table
```

Use before scheduling into a non-primary calendar.

### 18) Create a new event

```bash
gws calendar +insert --summary "Dentist Appointment" --start "2026-03-18T09:00:00" --end "2026-03-18T10:00:00"
```

Use for quick scheduling.

### 19) Create an event with attendees

```bash
gws calendar +insert --summary "Lunch with Sam" --attendee sam@example.com --start "2026-03-20T12:00:00" --end "2026-03-20T13:00:00"
```

Use for invitations where the other person should receive a calendar invite.

### 20) Search calendar events directly

```bash
gws calendar events list --params '{"calendarId":"primary","q":"dentist","timeMin":"2026-03-01T00:00:00Z","timeMax":"2026-03-31T23:59:59Z"}' --format table
```

Use to confirm whether something is already scheduled.

### 21) Check free/busy before proposing a meeting

```bash
gws calendar freebusy query --json '{"timeMin":"2026-03-21T08:00:00Z","timeMax":"2026-03-21T18:00:00Z","items":[{"id":"primary"}]}'
```

Use before sending scheduling suggestions.

## End-to-End Personal Assistant Scenarios

### Scenario A: Prepare for today
1. Review agenda:
   ```bash
   gws calendar +agenda --format table
   ```
2. Check unread email:
   ```bash
   gws gmail +triage --max 10 --format table
   ```
3. Search Drive for docs matching today’s meeting names:
   ```bash
   gws drive files list --params '{"q":"name contains '\''meeting notes'\'' and trashed = false","pageSize":10,"fields":"files(id,name,modifiedTime,webViewLink)"}' --format table
   ```

### Scenario B: Find an email attachment and store it in Drive
1. Search messages with attachments:
   ```bash
   gws gmail users messages list --params '{"userId":"me","q":"from:travel@example.com has:attachment newer_than:30d"}' --format table
   ```
2. Fetch the target message:
   ```bash
   gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID"}'
   ```
3. Save/download the attachment using the appropriate Gmail attachment/message method after inspecting schema:
   ```bash
   gws schema gmail.users.messages.attachments.get
   ```
4. Upload saved file to Drive:
   ```bash
   gws drive files create --json '{"name":"Flight Confirmation.pdf","parents":["TRAVEL_FOLDER_ID"]}' --upload ./Flight-Confirmation.pdf
   ```

### Scenario C: Organize a meeting from an email thread
1. Search the relevant email thread:
   ```bash
   gws gmail users messages list --params '{"userId":"me","q":"from:alex@example.com subject:(catch up) newer_than:14d"}' --format table
   ```
2. Read the message:
   ```bash
   gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID"}'
   ```
3. Check calendar availability:
   ```bash
   gws calendar freebusy query --json '{"timeMin":"2026-03-25T08:00:00Z","timeMax":"2026-03-25T18:00:00Z","items":[{"id":"primary"}]}'
   ```
4. Create the event:
   ```bash
   gws calendar +insert --summary "Catch up with Alex" --attendee alex@example.com --start "2026-03-25T15:00:00" --end "2026-03-25T15:30:00"
   ```
5. Reply in thread confirming:
   ```bash
   gws gmail +reply --message-id MESSAGE_ID --body "Booked for 3:00 PM on March 25. Calendar invite sent."
   ```

### Scenario D: Weekly life admin reset
1. Review the week:
   ```bash
   gws calendar +agenda --week --format table
   ```
2. Search inbox for bills, reservations, or deadlines:
   ```bash
   gws gmail users messages list --params '{"userId":"me","q":"(bill OR invoice OR reservation OR appointment) newer_than:30d"}' --format table
   ```
3. Archive important files to Drive:
   ```bash
   gws drive files list --params '{"q":"name contains '\''invoice'\'' and trashed = false","pageSize":20,"fields":"files(id,name,modifiedTime)"}' --format table
   ```
4. Schedule follow-up events or reminders:
   ```bash
   gws calendar +insert --summary "Pay utility bill" --start "2026-03-22T18:00:00" --end "2026-03-22T18:15:00"
   ```

## Search Patterns to Reuse

### Gmail search examples
- Unread mail:
  ```bash
  gws gmail users messages list --params '{"userId":"me","q":"is:unread"}' --format table
  ```
- Messages from a person:
  ```bash
  gws gmail users messages list --params '{"userId":"me","q":"from:person@example.com"}' --format table
  ```
- Messages to a person:
  ```bash
  gws gmail users messages list --params '{"userId":"me","q":"to:person@example.com"}' --format table
  ```
- Messages with attachments:
  ```bash
  gws gmail users messages list --params '{"userId":"me","q":"has:attachment"}' --format table
  ```
- Recent travel email:
  ```bash
  gws gmail users messages list --params '{"userId":"me","q":"(flight OR hotel OR itinerary) newer_than:60d"}' --format table
  ```

### Drive search examples
- By partial filename:
  ```bash
  gws drive files list --params '{"q":"name contains '\''passport'\'' and trashed = false","fields":"files(id,name,webViewLink)"}' --format table
  ```
- PDFs only:
  ```bash
  gws drive files list --params '{"q":"mimeType = '\''application/pdf'\'' and trashed = false","fields":"files(id,name,mimeType)"}' --format table
  ```
- Recently modified:
  ```bash
  gws drive files list --params '{"orderBy":"modifiedTime desc","pageSize":10,"fields":"files(id,name,modifiedTime)"}' --format table
  ```

### Calendar search examples
- Upcoming matching events:
  ```bash
  gws calendar events list --params '{"calendarId":"primary","q":"lunch","timeMin":"2026-03-01T00:00:00Z"}' --format table
  ```
- List calendars:
  ```bash
  gws calendar calendarList list --format table
  ```

## Headless Environment Guidance

- Assume the runtime has already been provisioned with credentials.
- Prefer environment-driven auth and stored credential files over interactive sign-in.
- Do not instruct the user to rely on keychain/keyring unlock flows.
- Before starting a workflow, verify access with a lightweight read:
  - Gmail profile:
    ```bash
    gws gmail users getProfile --params '{"userId":"me"}'
    ```
  - Calendar list:
    ```bash
    gws calendar calendarList list --format table
    ```
  - Drive about:
    ```bash
    gws drive about get --params '{"fields":"user,storageQuota"}'
    ```

## Safety and Confirmation Rules

- Confirm before:
  - sending email
  - replying-all
  - forwarding sensitive content
  - creating, modifying, or deleting calendar events
  - uploading files into shared or potentially visible Drive locations
- For ambiguous requests:
  - search first
  - summarize findings
  - propose the next exact command before executing a write action

## Tips

- Use Gmail search to narrow context before reading full messages.
- Use Drive search before uploading to avoid duplicates.
- Use Calendar free/busy before proposing times.
- Prefer concise, actionable email drafts.
- For sensitive workflows, add `--sanitize`.
- When unsure of a method’s exact parameters, inspect it first:
  ```bash
  gws schema gmail.<resource>.<method>
  gws schema drive.<resource>.<method>
  gws schema calendar.<resource>.<method>
  ```
- When unsure whether a service exposes a helper command or which flags it accepts, inspect the service help first:
  ```bash
  gws gmail --help
  gws calendar --help
  gws drive --help
  ```
