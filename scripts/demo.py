from datetime import date, timedelta

import httpx


def main() -> None:
    start = date.today() + timedelta(days=60)
    request = {
        "current_location": "Bengaluru, India",
        "destination": "Kyoto, Japan",
        "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=2)).isoformat(),
        "budget_min": 1800,
        "budget_max": 2600,
        "currency": "USD",
        "interests": ["temples", "food", "photography"],
        "travelers": 2,
        "transport_modes": ["flight", "train", "bus"],
        "allow_transport_connections": True,
    }
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=300) as client:
        created = client.post("/plan", json=request)
        created.raise_for_status()
        plan_id = created.json()["plan_id"]
        print(f"Draft ready: {plan_id}")

        draft = client.get(f"/plan/{plan_id}")
        draft.raise_for_status()
        print(f"Status: {draft.json()['status']}")
        print(f"Title: {draft.json()['draft_plan']['title']}")

        approved = client.post(f"/plan/{plan_id}/review", json={"action": "approve"})
        approved.raise_for_status()
        print(f"After approval: {approved.json()['status']}")

        final = client.get(f"/plan/{plan_id}/final")
        final.raise_for_status()
        print(f"Final days: {len(final.json()['days'])}")


if __name__ == "__main__":
    main()
