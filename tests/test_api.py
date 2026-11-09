from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


def slot(slot_id="s1", minutes=60):
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {"id": slot_id, "provider": "Dr Ade", "start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}


def reset(client, headers, slots):
    response = client.post("/reset", headers=headers, json={"slots": slots})
    assert response.status_code == 200


def test_auth_slots_booking_lookup_and_cancel(client, headers):
    assert client.get("/slots").status_code == 401
    reset(client, headers, [slot()])
    slots = client.get("/slots", headers=headers).json()["slots"]
    assert slots[0]["available"] is True

    created = client.post("/bookings", headers=headers, json={"slot_id": "s1", "name": " Ada Obi ", "email": " ADA@example.com "})
    assert created.status_code == 201
    booking = created.json()
    assert booking["name"] == "Ada Obi"
    assert booking["email"] == "ada@example.com"
    assert client.get("/slots", headers=headers).json()["slots"][0]["available"] is False
    assert client.get(f"/bookings/{booking['id']}", headers=headers).json() == booking

    cancelled = client.delete(f"/bookings/{booking['id']}", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.delete(f"/bookings/{booking['id']}", headers=headers).status_code == 409
    assert client.get("/slots", headers=headers).json()["slots"][0]["available"] is True


def test_validation_and_past_slot(client, headers):
    reset(client, headers, [slot(minutes=-60)])
    assert client.post("/bookings", headers=headers, json={"slot_id": "s1", "name": "", "email": "bad"}).status_code == 400
    response = client.post("/bookings", headers=headers, json={"slot_id": "s1", "name": "Ada", "email": "ada@example.com"})
    assert response.status_code == 400
    assert response.json() == {"error": "slot has already started"}


def test_idempotency(client, headers):
    reset(client, headers, [slot()])
    body = {"slot_id": "s1", "name": "Ada", "email": "ada@example.com"}
    keyed = {**headers, "Idempotency-Key": "retry-1"}
    first = client.post("/bookings", headers=keyed, json=body)
    second = client.post("/bookings", headers=keyed, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    changed = client.post("/bookings", headers=keyed, json={**body, "name": "Other"})
    assert changed.status_code == 422


def test_only_one_concurrent_booking_wins(client, headers):
    reset(client, headers, [slot()])
    def book(number):
        return client.post("/bookings", headers=headers, json={"slot_id": "s1", "name": f"Patient {number}", "email": f"p{number}@example.com"}).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(book, range(8)))
    assert statuses.count(201) == 1
    assert statuses.count(409) == 7


def test_pagination(client, headers):
    slots = [slot(f"s{i}", 60 + i) for i in range(3)]
    reset(client, headers, slots)
    for item in slots:
        response = client.post("/bookings", headers=headers, json={"slot_id": item["id"], "name": "Ada", "email": "ADA@example.com"})
        assert response.status_code == 201
    first = client.get("/bookings?email=ada@example.com&limit=2", headers=headers).json()
    assert len(first["bookings"]) == 2 and first["next_cursor"]
    second = client.get(f"/bookings?email=ADA@example.com&limit=2&cursor={first['next_cursor']}", headers=headers).json()
    assert len(second["bookings"]) == 1 and second["next_cursor"] is None


def test_rate_limit_is_per_email_and_counts_failures(client, headers):
    reset(client, headers, [])
    body = {"slot_id": "missing", "name": "Ada", "email": "ada@example.com"}
    for _ in range(5):
        assert client.post("/bookings", headers=headers, json=body).status_code == 404
    limited = client.post("/bookings", headers=headers, json=body)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    other = client.post("/bookings", headers=headers, json={**body, "email": "other@example.com"})
    assert other.status_code == 404


def test_provider_filter_invalid_limit_and_error_replay(client, headers):
    reset(client, headers, [slot("s1"), {**slot("s2", 90), "provider": "Dr Bisi"}])
    filtered = client.get("/slots?provider=Dr%20Bisi", headers=headers).json()["slots"]
    assert [item["id"] for item in filtered] == ["s2"]
    assert client.get("/bookings?email=ada@example.com&limit=0", headers=headers).status_code == 400

    keyed = {**headers, "Idempotency-Key": "missing-slot"}
    body = {"slot_id": "missing", "name": "Ada", "email": "ada@example.com"}
    first = client.post("/bookings", headers=keyed, json=body)
    second = client.post("/bookings", headers=keyed, json=body)
    assert first.status_code == second.status_code == 404
    assert first.json() == second.json() == {"error": "slot not found"}


def test_invalid_booking_attempts_with_email_are_rate_limited(client, headers):
    reset(client, headers, [])
    invalid = {"slot_id": "missing", "name": "", "email": "Invalid-Email"}
    for _ in range(5):
        assert client.post("/bookings", headers=headers, json=invalid).status_code == 400
    limited = client.post("/bookings", headers=headers, json=invalid)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1


def test_patient_web_routes_work_without_exposing_api_key(client, headers):
    reset(client, headers, [slot()])
    listed = client.get("/web/slots")
    assert listed.status_code == 200
    assert listed.json()["slots"][0]["id"] == "s1"

    created = client.post(
        "/web/bookings",
        headers={"Idempotency-Key": "web-booking"},
        json={"slot_id": "s1", "name": "Ada Obi", "email": "ada@example.com"},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "confirmed"

