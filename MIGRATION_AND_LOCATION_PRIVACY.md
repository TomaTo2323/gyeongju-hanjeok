# Production cleanup: GPS privacy + SQLite account migration

## Location policy

The production backend does **not** accept the user's live GPS coordinate.

- `/places` and `/api/v1/places` use a fixed Gyeongju service anchor.
- `/weather/current` and `/api/v1/weather/current` use a fixed Gyeongju service anchor.
- Legacy `/places/nearby` and `/places/nearest-tourist` endpoints were removed.
- Route recommendation request bodies no longer expose `start_latitude` / `start_longitude`.
- Core course request bodies no longer expose `latitude` / `longitude` or `current_latitude` / `current_longitude`.
- Journey visit completion accepts an on-device verification result, not a GPS coordinate.
- Etiquette lookup is place-based, not user-location-based.
- Public tourist-place coordinates remain in responses because they identify places, not the user.

The Flutter app should keep acquiring GPS locally for current-position UI, local distance calculation, sorting, and visit verification.

## SQLite -> PostgreSQL migration

Never upload the old SQLite database to GitHub. It contains account information and password hashes.

The migration script copies the existing password hash as-is. It never needs the plain-text password, so the same account password remains valid after migration.

### 1. Dry run

From the backend folder:

```powershell
python scripts/migrate_sqlite_accounts_to_postgres.py `
  --sqlite .\data\gyeongju_hanjeok.db `
  --dry-run
```

### 2. Copy Railway PostgreSQL public connection URL

Use Railway Postgres **Connect** / public TCP connection details. Do not paste it into chat or GitHub.

Set it only for the current PowerShell session:

```powershell
$env:TARGET_DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/railway"
```

### 3. Apply migration

```powershell
python scripts/migrate_sqlite_accounts_to_postgres.py `
  --sqlite .\data\gyeongju_hanjeok.db `
  --apply
```

The script migrates:

- users
- consents / public member code
- friends / invites
- shared routes / members / invites
- community posts / comments / recommendations / saved courses
- journeys

It intentionally does not migrate the `places` table because production tourist-place data is managed by the Railway sync process.

### 4. Verify test account

After migration, test the Tourism Organization review account from the production app:

1. Log in with the same email/password used in the old SQLite DB.
2. Confirm `/auth/me` succeeds after login.
3. Log out and log back in once more.
4. Restart the app and confirm the session/login flow is still normal.

Do this before the final release APK/AAB is uploaded.
