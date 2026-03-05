from datetime import datetime
from typing import Optional

from jobspy.model import Location, Country, Compensation, CompensationInterval
from .constant import WORK_TYPE_MAP


def parse_location(location_label: str, country: Country) -> Location:
    """Parse Seek location label like 'Sydney CBD, Inner West & Eastern Suburbs' into Location."""
    if not location_label:
        return Location(country=country)

    parts = location_label.split(",", 1)
    city = parts[0].strip()
    state = parts[1].strip() if len(parts) > 1 else None

    return Location(city=city, state=state, country=country)


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 date string from Seek API (e.g. '2024-01-15T10:30:00Z')."""
    if not date_str:
        return None
    try:
        # Handle ISO format with timezone
        clean = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def parse_salary(salary_label: str, country: Country = None) -> Optional[Compensation]:
    """Parse Seek salary label like '$80,000 - $100,000 per year' into Compensation."""
    if not salary_label:
        return None

    text = salary_label.lower().strip()

    # Determine interval
    interval = CompensationInterval.YEARLY
    if "per hour" in text or "hourly" in text or "/hr" in text:
        interval = CompensationInterval.HOURLY
    elif "per day" in text or "daily" in text:
        interval = CompensationInterval.DAILY
    elif "per month" in text or "monthly" in text:
        interval = CompensationInterval.MONTHLY
    elif "per week" in text or "weekly" in text:
        interval = CompensationInterval.WEEKLY

    # Determine currency: explicit in text > country-based default
    currency = "NZD" if country == Country.NEWZEALAND else "AUD"
    if "nz$" in text or "nzd" in text:
        currency = "NZD"
    elif "a$" in text or "aud" in text:
        currency = "AUD"

    # Extract numbers
    import re
    numbers = re.findall(r'[\d,]+(?:\.\d+)?', text)
    amounts = []
    for n in numbers:
        try:
            amounts.append(float(n.replace(",", "")))
        except ValueError:
            continue

    if not amounts:
        return None

    min_amount = amounts[0]
    max_amount = amounts[1] if len(amounts) > 1 else amounts[0]

    return Compensation(
        interval=interval,
        min_amount=min_amount,
        max_amount=max_amount,
        currency=currency,
    )


def map_work_type(work_type: str) -> Optional[str]:
    """Map Seek work type to JobType enum value."""
    if not work_type:
        return None
    return WORK_TYPE_MAP.get(work_type.lower().strip())
