# SQLite companion

This optional CPython service stores the badge schema-v4 profile in SQLite. It
does not run on MonaOS/MicroPython and is not required for normal IR use.

## CLI

Run commands from the repository root with Python 3.9 or later:

```powershell
python -m companion --db .\badge-ir.sqlite3 serve
python -m companion --db .\badge-ir.sqlite3 list-devices
python -m companion --db .\badge-ir.sqlite3 list-discoveries
python -m companion --db .\badge-ir.sqlite3 import-profile .\profile.json
python -m companion --db .\badge-ir.sqlite3 export-profile .\profile.json
```

The API binds to `127.0.0.1:8765` by default. A non-loopback bind is refused
unless `--allow-remote` is explicitly supplied. The service has no remote-user
authentication, so keep it on loopback unless it is placed behind a properly
authenticated gateway and firewall.

## HTTP API

All write bodies use `Content-Type: application/json`. Responses wrap records
in `{"data": ...}`. Errors use
`{"error":{"code":"...","message":"..."}}`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service/database health |
| `GET`, `PUT` | `/api/v1/profile` | Export or atomically replace schema-v4 profile |
| `GET`, `POST` | `/api/v1/devices` | List or create devices |
| `GET`, `PATCH`, `DELETE` | `/api/v1/devices/{id}` | Read, update, or delete a device |
| `GET` | `/api/v1/devices/{id}/buttons` | List learned/preset commands |
| `PUT`, `DELETE` | `/api/v1/devices/{id}/buttons/{name}` | Upsert or delete a command |
| `GET`, `POST`, `DELETE` | `/api/v1/discoveries` | List, upsert, or clear scan results |
| `GET`, `DELETE` | `/api/v1/discoveries/{id}` | Read or delete one scan result |
| `POST` | `/api/v1/discoveries/save` | Save selected discoveries as devices |

Examples:

```powershell
curl.exe http://127.0.0.1:8765/api/v1/devices

curl.exe -X POST http://127.0.0.1:8765/api/v1/devices `
  -H "Content-Type: application/json" `
  -d '{"name":"Living Room TV","type":"tv","transport":"ir"}'

curl.exe -X POST http://127.0.0.1:8765/api/v1/discoveries/save `
  -H "Content-Type: application/json" `
  -d '{"ids":[1,3]}'

curl.exe -X POST http://127.0.0.1:8765/api/v1/discoveries/save `
  -H "Content-Type: application/json" `
  -d '{"all":true}'
```

Discovery records intentionally return `"controllable": false`: BLE/Wi-Fi
presence alone does not provide a compatible control protocol or credentials.
