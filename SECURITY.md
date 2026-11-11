# Security policy

ARGUS holds a detailed record of someone's working day, so security reports
are taken seriously and handled first.

## Reporting a vulnerability

**Please do not open a public issue.**

Report it privately through GitHub:
[**Report a vulnerability**](https://github.com/ansh0965/argus/security/advisories/new)
(the *Security* tab of the repository → *Report a vulnerability*).

Include what you can of:

- what the problem is and what an attacker could do with it,
- the steps or a proof of concept,
- the ARGUS version or commit, and how you run it (`Argus.exe` or from source).

You can expect an acknowledgement within a week. Once a fix is ready it is
released, and you are credited in the release notes unless you would rather
not be.

## What counts

Especially interesting:

- Anything that lets **another website or another machine** read from or write
  to the dashboard API (it should be reachable from `127.0.0.1` only, with
  Host and Origin checks).
- Anything that makes ARGUS **send data off the machine** that
  [docs/privacy.md](docs/privacy.md) says it does not.
- **Prompt injection that has an effect**: text in a mail subject, page title
  or window title that makes ARGUS take an action, rather than just appear in
  a summary.
- A way around the **"never send mail, never delete"** guard on Google
  accounts.
- Secrets or private titles that reach the database **unmasked**.
- Anything a toast click, a link or a file can do beyond opening an `https://`
  page or the local dashboard.

Out of scope: an attacker who already has your Windows account or can read
your disk. ARGUS's database is a local file protected by your account; turn on
Windows Device encryption to protect it at rest.

## Supported versions

Fixes land on the latest release and on `main`. Older versions are not
patched.
