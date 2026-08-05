---
name: ai-notepad
description: "Manage Hugo's Notebook notes from terminal — list, read, create, update, delete, search, publish."
version: '1.2.13'
author: hugozhu
license: MIT
metadata:
    hermes:
        tags: [notes, notepad, writing, supabase, cli]
---

# AI Notepad CLI

Manage notes in Hugo's Notebook (hugozhu.site/notes) from the terminal.

## CLI Location

`skills/ai-notepad/cli` in this project, or install to `~/bin/ai-notepad`.

## Auth

Session cached at `~/.ai-notepad/session.json`. Expires after ~1 hour.

```bash
ai-notepad whoami                  # Check current user
ai-notepad register -e <email> -p *** # Register (domain-restricted, auto-confirm)
ai-notepad login --token <JWT>     # Paste JWT from browser DevTools (GitHub/Google SSO)
ai-notepad login -e <email> -p *** # Email/password auth
ai-notepad logout
```

If session expired, commands will print "Session expired" — re-run login.

### Auto-Auth Flow (for DingTalk users)

When using this skill, if no session exists, follow this auto-authentication flow:

1. **Get user info from DingTalk** (parallel calls):

    ```bash
    dws contact user get-self --format json    # Get jobNumber
    dws mail mailbox list --format json         # Get email
    ```

2. **Extract email and job number**:
    - `emailAccounts[0].email` from `mail mailbox list`: the user's email
    - `userId` from `contact user get-self`: the employee job number

3. **Determine account credentials**:
    - If email from `mail mailbox list` exists, use it as the email
    - If no email, use `auto_{jobNumber}@dingtalk.com` as the email
    - Password = `jobNumber` (if less than 8 digits, pad with leading zeros to 8 chars)

4. **Try to login**:

    ```bash
    ai-notepad login -e {email} -p '{password}'
    ```

5. **If login fails** (account doesn't exist), register and login again:

    ```bash
    ai-notepad register -e {email} -p '{password}'
    ```

6. **Proceed** with the requested functionality

Example: If jobNumber is `0420506555`, password is `0420506555`. If jobNumber is `12345`, password is `00012345`.

## Commands

### List notes

```bash
ai-notepad ls                    # Latest 10
ai-notepad ls -n 20              # More results
ai-notepad ls --offset 10        # Paginate
```

### Read a note

```bash
ai-notepad get <id>              # Full content (ID prefix OK, 8 chars)
ai-notepad get <full-uuid>       # Full UUID also works
```

### Create a note

```bash
ai-notepad create "Title" -c "Markdown content"
ai-notepad create "Title" -f notes.md   # From file
```

### Update a note

```bash
ai-notepad update <id> -t "New Title"
ai-notepad update <id> -c "New content"
ai-notepad update <id> -f notes.md      # Replace content from file
ai-notepad update <id> -t "Title" -c "Content"  # Both at once
```

### Delete a note

```bash
ai-notepad delete <id>
```

### Search notes

```bash
ai-notepad search "keyword"
```

### Publish / Unpublish (public page)

```bash
ai-notepad publish <id>          # Prints: https://hugozhu.site/notes/p/<slug>
ai-notepad unpublish <id>
```

### Annotations (reader feedback)

```bash
ai-notepad annotations <note-id>          # List annotations for a note
ai-notepad annotation-delete <ann-id>     # Delete a specific annotation
```

## Public Page URL

Published notes render at: `https://hugozhu.site/notes/p/{slug}`
Pages always show the latest DB content (not cached beyond 60s).

## Notes

- IDs support 8-char prefix matching (auto-resolves to full UUID)
- Content supports Markdown, max 100KB
- The Supabase project is `xryosrxuoqwohyqxszhs`
- Anon key stored at `~/.ai-notepad/config.json`
