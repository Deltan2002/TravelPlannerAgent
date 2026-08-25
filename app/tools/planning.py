from app.models import BudgetBreakdown, TravelRequest, WeatherSummary


class BudgetAllocatorTool:
    def allocate(self, request: TravelRequest) -> BudgetBreakdown:
        total = round((request.budget_min + request.budget_max) / 2, 2)
        return BudgetBreakdown(
            total_budget=total,
            lodging=round(total * 0.35, 2),
            food=round(total * 0.20, 2),
            activities=round(total * 0.25, 2),
            local_transport=round(total * 0.15, 2),
            contingency=round(total * 0.05, 2),
            per_person=round(total / request.travelers, 2),
            currency=request.currency,
        )


class PackingListTool:
    def generate(self, request: TravelRequest, weather: WeatherSummary) -> list[str]:
        items = [
            "Passport/ID and digital booking copies",
            "Comfortable walking shoes",
            "Reusable water bottle",
            "Phone charger and travel adapter",
            f"Clothing for {request.days} days (or a laundry plan)",
        ]
        summary = weather.summary.lower()
        if "rain" in summary or (weather.precipitation_probability_max or 0) >= 30:
            items.append("Compact umbrella or waterproof shell")
        if weather.average_high_c is not None and weather.average_high_c >= 27:
            items.extend(["Sun protection", "Lightweight breathable clothing"])
        if weather.average_low_c is not None and weather.average_low_c <= 10:
            items.append("Warm layers")
        interests = " ".join(request.interests).lower()
        if any(word in interests for word in ("hike", "outdoor", "nature")):
            items.append("Daypack and trail-appropriate footwear")
        if any(word in interests for word in ("photo", "photography")):
            items.append("Camera, spare battery, and memory card")
        return list(dict.fromkeys(items))
