# GPS API cleanup verification

After Railway redeploy, open `/docs` and verify:

- `GET /api/v1/places`: only `radius_km`, `limit` are shown. No latitude/longitude.
- `GET /places`: only `query`, `category`, `radius_km`, `limit` are shown. No latitude/longitude.
- `/places/nearby` and `/places/nearest-tourist` no longer exist.
- `GET /api/v1/weather/current` and `GET /weather/current`: no latitude/longitude parameters.
- `POST /routes/recommend`: request body has no `start_latitude`/`start_longitude`.
- `POST /api/v1/courses/recommend`: request body has no `latitude`/`longitude`.
- `POST /api/v1/courses/recalculate`: request body has no `current_latitude`/`current_longitude`.
- `POST /api/v1/journeys/{journey_id}/visits`: request body contains `place_id` and `verified_on_device`, not GPS.
- Etiquette is place-based: `/api/v1/etiquette/place/{place_id}` and `/etiquette/place/{place_id}`.

Public tourist-place latitude/longitude values may still appear in responses. Those identify the tourist place, not the user's live location.
