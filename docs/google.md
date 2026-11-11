# Connecting Google

Google is optional. Connected, ARGUS merges your calendars and mail into one
timeline, warns you before meetings, triages unread mail, and can put events
and tasks where you ask. Without it, everything else works and the Google
panels show these setup steps.

ARGUS uses **your own** Google Cloud OAuth client. There is no shared ARGUS
app and no server in between: your tokens stay in `data/secrets/` on your
machine.

## 1. Create the OAuth client (once)

`argus google login` prints these steps when no client exists yet.

1. Go to <https://console.cloud.google.com> and create a project.
   Use a personal account - company Workspaces often forbid project creation.
2. Enable these APIs: **Gmail, Google Calendar, Google Tasks, Google Drive,
   People**.
3. Configure the OAuth consent screen as **External**.
4. Add every Google account you plan to connect as a **Test user**, or its
   sign-in will be refused.
5. Create credentials → OAuth client ID → application type **Desktop app**.
6. Download the JSON. ARGUS finds it in your Downloads folder by itself.

> A **Web** client will not work - it fails with a redirect mismatch. It must
> be a Desktop-type client.

## 2. Connect accounts

One client covers any number of accounts. Each gets a short label.

```
argus google login --label personal
argus google login --label work
argus google list                    who is connected, and what each may read
argus google logout --label work
argus google relogin                 re-authorise every expired account in turn
```

Calendar and mail from all accounts are merged in the dashboard and the
briefings, each row tagged with its account. Toasts are prefixed with the
label so you know at once who a mail is for.

### A word on work accounts

A Workspace admin can block third-party apps outright, and whether to connect
a company account to a personal tool is a decision for you and your employer's
policy. If you do connect one, consider limiting it to calendar only (below).

## 3. Limit what an account may touch

```yaml
google:
  accounts:
    work:     {features: [calendar]}                     # meetings only, mail never read
    personal: {features: [calendar, mail, tasks, drive]}
```

`google.default_features` is what a newly connected account gets until you say
otherwise. `google.tasks_account` names the label that receives
`argus task add` (empty = the first connected account).

ARGUS may read, and may create events, drafts and tasks. It may **never delete
anything or send mail** on any account - `argus/gsuite/guard.py` enforces
that - unless you set `google.allow_destructive: true`.

## Tokens that expire every 7 days

While the consent screen is in **Testing** mode, Google expires every refresh
token 7 days after sign-in. ARGUS recognises this (`invalid_grant`), stops
retrying the dead token, raises one alert a day rather than one every five
minutes, warns you on day 6, and tells you the fix in `argus doctor` and
`argus google list`:

1. Open <https://console.cloud.google.com/auth/audience> in the project that
   owns your client → **Publish app**.
2. Run `argus google relogin` (or press **Log in again** on the dashboard's
   Google tab). Google warns that the app is unverified - expected for a
   personal app: *Advanced → Go to (your app name)*.

A token that fails for network reasons is reported as *unreachable* and simply
retried; only a genuinely dead token asks you to log in.

## Stale data says so

If calendar or mail has not synced for 12 hours, the briefings, the dashboard
("synced 7d ago", in amber) and meeting warnings say how old their copy is
rather than presenting it as current.
