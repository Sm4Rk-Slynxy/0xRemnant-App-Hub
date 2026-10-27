# Example screenshots

These are captures of the actual 0xRemnant App Hub UI using disposable synthetic
app records. The Docker engine is modeled. The Little Notes frame is fulfilled
by the browser from the bundled HTML example. The pictured Focus Timer and Habit
Board cards are illustrative names, not additional bundled applications.

- `dashboard.png`: desktop app shelf with running/stopped states.
- `upload.png`: direct HTML upload dialog.
- `app-tab.png`: Little Notes inside a dashboard tab, with synthetic text.
- `mobile.png`: 390px responsive dashboard.

No real credentials, LAN addresses, hostnames, production records, or personal
notes were used. Loopback addresses and default example ports are intentional.
These images demonstrate the interface and do not verify Docker deployment.

Recreate with Playwright/Chromium installed:

```sh
node tests/screenshots.cjs
```

`APPHUB_PLAYWRIGHT` can point to an existing Playwright module. The script starts
and stops its own loopback fixture and deletes its temporary registry afterward.
Screenshots are original project UI captures, distributed under the project MIT
license along with the bundled examples.
