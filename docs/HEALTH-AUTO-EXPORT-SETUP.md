# Apple Health Auto Export setup

Home Intelligence can ingest Apple HealthKit data from the iOS app **Health Auto Export** without sending health data outside your LAN.

## 1. Install the app

Install **Health Auto Export** from the iOS App Store and allow it to read the Health metrics you want Home Intelligence to use: sleep, wake time, workouts, weight, heart rate, mood, steps, and energy.

## 2. Configure the webhook automation

In Health Auto Export, create an automation:

- Type: **HTTP REST**
- URL: `http://<truenas-ip>:8080/admin/healthkit/sync`
- Method: `POST`
- Headers:
  - `Content-Type: application/json`
  - `X-Health-Token: <your token>`
- Payload: **full export**

Recommended schedule:

- Every hour for ambient metrics such as sleep, steps, heart rate, weight, and energy.
- Immediately on workout completion for workouts.

## 3. Set the TrueNAS token

Set `HEALTHKIT_WEBHOOK_TOKEN` in your TrueNAS app environment to a long random string. Generate one with:

```sh
openssl rand -hex 32
```

Use the same value in the iOS `X-Health-Token` header. The webhook refuses syncs if the token is unset or does not match.

## Privacy

Apple Health data is posted directly from your iPhone to Home Intelligence on your LAN. It is stored locally in Postgres and never leaves the LAN unless you explicitly export or share it.

## Test with curl

```sh
curl -X POST http://<truenas-ip>:8080/admin/healthkit/sync \
  -H 'Content-Type: application/json' \
  -H 'X-Health-Token: <your token>' \
  -d '{
    "data": {
      "metrics": [
        {
          "type": "HKQuantityTypeIdentifierStepCount",
          "unit": "count",
          "data": [{"date": "2026-05-13T08:00:00Z", "qty": 1234}]
        }
      ],
      "workouts": []
    }
  }'
```
