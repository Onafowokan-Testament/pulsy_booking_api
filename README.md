# Pulsy Clinic Booking API

A FastAPI implementation of all five assessment levels, with the animated Pulsy patient booking experience served from the same application.

## Features

- API-key authentication on every assessment endpoint
- Slot listing and exact provider filtering
- Input validation and normalized patient details
- Database-enforced protection against concurrent double booking
- Cancellation that releases the slot
- Idempotent booking requests, including replayed error responses
- Cursor-paginated booking history
- Per-email booking-attempt rate limiting
- Persistent PostgreSQL storage in production
- Same-origin patient routes that keep the private API key out of browser code

## Run locally

Requires Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
copy .env.example .env
uvicorn app.main:app --reload
```

Set `API_KEY` and `DATABASE_URL` in your environment. If `DATABASE_URL` is omitted, the app uses `booking.db` for local development. Open `http://127.0.0.1:8000` for the Pulsy web page and `/docs` for interactive API documentation.

Run the test suite with:

```bash
pytest -q
```

## API contract

All assessment endpoints require `X-API-Key`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/reset` | Replace slots and clear bookings |
| GET | `/slots` | List slots and availability |
| POST | `/bookings` | Create a booking |
| GET | `/bookings` | List one patient's bookings |
| GET | `/bookings/{id}` | Look up a booking |
| DELETE | `/bookings/{id}` | Cancel a booking |

`GET /health` and the patient page at `/` are public so the host can monitor the service and patients can load the interface.

The patient page calls `/web/slots` and `/web/bookings`. These same-origin server routes reuse the booking service without exposing `API_KEY` in public JavaScript. All assessment endpoints remain protected by `X-API-Key`.

## Deploy

1. Create a Neon PostgreSQL project and copy its pooled connection string.
2. Push this repository to GitHub.
3. In Render, create a Blueprint from the repository using `render.yaml`.
4. Set `DATABASE_URL` to the Neon connection string. Render generates `API_KEY`; copy its value for the assessment submission.
5. Deploy and use the Render URL as both the API base URL and web page URL.

The application creates its tables at startup. For this assessment's single schema version, that keeps deployment small and repeatable.
