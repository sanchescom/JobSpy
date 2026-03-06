import re
from datetime import datetime, timedelta

from jobspy.model import CompensationInterval, Compensation, JobType
from jobspy.util import create_logger

log = create_logger("IrishJobs")


def parse_salary(salary_text: str) -> Compensation | None:
    """Parse salary string from IrishJobs into a Compensation object.

    Common formats:
    - "€50,000 - €70,000 per annum"
    - "€30 - €45 per hour"
    - "€3,000 - €4,500 per month"
    - "€50,000 - €70,000"
    - "€50,000+"
    - "Negotiable" / "Competitive"
    """
    if not salary_text:
        return None

    salary_text = salary_text.strip()

    # Skip non-numeric salary descriptions
    if salary_text.lower() in ("negotiable", "competitive", "not disclosed", ""):
        return None

    # Determine interval
    text_lower = salary_text.lower()
    if "per hour" in text_lower or "p/h" in text_lower or "/hr" in text_lower:
        interval = CompensationInterval.HOURLY
    elif "per month" in text_lower or "p/m" in text_lower or "/month" in text_lower:
        interval = CompensationInterval.MONTHLY
    elif "per week" in text_lower or "p/w" in text_lower or "/week" in text_lower:
        interval = CompensationInterval.WEEKLY
    elif "per day" in text_lower or "p/d" in text_lower or "/day" in text_lower:
        interval = CompensationInterval.DAILY
    else:
        interval = CompensationInterval.YEARLY

    # Extract numbers
    amounts = re.findall(r"[\d,]+(?:\.\d+)?", salary_text.replace("€", "").replace("$", "").replace("£", ""))
    if not amounts:
        return None

    try:
        values = [float(a.replace(",", "")) for a in amounts]
    except ValueError:
        return None

    min_amount = values[0] if len(values) >= 1 else None
    max_amount = values[1] if len(values) >= 2 else min_amount

    if min_amount is None:
        return None

    return Compensation(
        interval=interval,
        min_amount=min_amount,
        max_amount=max_amount,
        currency="EUR",
    )


def parse_date(date_text: str) -> datetime | None:
    """Parse relative or absolute date strings from IrishJobs.

    Common formats:
    - "Posted 2 days ago"
    - "Posted today"
    - "Posted yesterday"
    - "Posted 1 hour ago"
    - "2024-01-15"
    - "15 Jan 2024"
    """
    if not date_text:
        return None

    text = date_text.strip().lower()

    if "today" in text or "just now" in text:
        return datetime.now().date()

    if "yesterday" in text:
        return (datetime.now() - timedelta(days=1)).date()

    # "X hours/minutes ago"
    hours_match = re.search(r"(\d+)\s*hours?\s*ago", text)
    if hours_match:
        return datetime.now().date()

    minutes_match = re.search(r"(\d+)\s*minutes?\s*ago", text)
    if minutes_match:
        return datetime.now().date()

    # "X days ago"
    days_match = re.search(r"(\d+)\s*days?\s*ago", text)
    if days_match:
        days = int(days_match.group(1))
        return (datetime.now() - timedelta(days=days)).date()

    # "X weeks ago"
    weeks_match = re.search(r"(\d+)\s*weeks?\s*ago", text)
    if weeks_match:
        weeks = int(weeks_match.group(1))
        return (datetime.now() - timedelta(weeks=weeks)).date()

    # "X months ago"
    months_match = re.search(r"(\d+)\s*months?\s*ago", text)
    if months_match:
        months = int(months_match.group(1))
        return (datetime.now() - timedelta(days=months * 30)).date()

    # Try ISO format
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass

    # Try common date formats
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def parse_location(location_text: str) -> tuple[str | None, str | None]:
    """Parse location string into (city, state/county).

    Examples:
    - "Dublin" -> ("Dublin", None)
    - "Dublin, Co. Dublin" -> ("Dublin", "Co. Dublin")
    - "Cork City, Co. Cork" -> ("Cork City", "Co. Cork")
    """
    if not location_text:
        return None, None

    parts = [p.strip() for p in location_text.split(",")]
    city = parts[0] if parts else None
    state = parts[1] if len(parts) > 1 else None

    return city, state


def map_job_type(job_type_text: str) -> list[JobType] | None:
    """Map IrishJobs job type strings to JobType enum values."""
    if not job_type_text:
        return None

    text = job_type_text.lower().strip()
    types = []

    if "full" in text and "time" in text:
        types.append(JobType.FULL_TIME)
    if "part" in text and "time" in text:
        types.append(JobType.PART_TIME)
    if "contract" in text:
        types.append(JobType.CONTRACT)
    if "temporary" in text or "temp" in text:
        types.append(JobType.TEMPORARY)
    if "internship" in text or "intern" in text:
        types.append(JobType.INTERNSHIP)
    if "permanent" in text:
        types.append(JobType.FULL_TIME)

    return types if types else None


def slugify(text: str) -> str:
    """Convert text to URL slug for IrishJobs search URLs.

    Example: "software engineer" -> "software-engineer"
    """
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return text
