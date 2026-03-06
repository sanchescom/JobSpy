from datetime import datetime
from typing import Optional

from jobspy.model import (
    Location,
    Country,
    Compensation,
    CompensationInterval,
    JobType,
)


def parse_location(location_name: str) -> Location:
    """Parse Reed location string into a Location object."""
    if not location_name:
        return Location(country=Country.UK)

    parts = location_name.split(",", 1)
    city = parts[0].strip()
    state = parts[1].strip() if len(parts) > 1 else None

    return Location(city=city, state=state, country=Country.UK)


def parse_date(date_str: str) -> Optional[datetime]:
    """Parse date string from Reed API (e.g. '17/09/2024')."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d/%m/%Y")
    except (ValueError, TypeError):
        pass
    try:
        clean = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


SALARY_TYPE_MAP = {
    "per annum": CompensationInterval.YEARLY,
    "per month": CompensationInterval.MONTHLY,
    "per week": CompensationInterval.WEEKLY,
    "per day": CompensationInterval.DAILY,
    "per hour": CompensationInterval.HOURLY,
}


def parse_salary_from_details(details: dict) -> Optional[Compensation]:
    """Build Compensation from Reed detail endpoint fields.

    Uses yearlyMinimumSalary/yearlyMaximumSalary and salaryType
    from the details endpoint for accurate salary data.
    """
    # Prefer yearly salary fields from details (already annualized by Reed)
    yearly_min = _to_float(details.get("yearlyMinimumSalary"))
    yearly_max = _to_float(details.get("yearlyMaximumSalary"))

    # Raw salary fields
    min_val = _to_float(details.get("minimumSalary"))
    max_val = _to_float(details.get("maximumSalary"))

    if min_val is None and max_val is None:
        return None

    # Determine interval from salaryType field
    salary_type = (details.get("salaryType") or "").lower().strip()
    interval = SALARY_TYPE_MAP.get(salary_type, CompensationInterval.YEARLY)

    currency = details.get("currency") or "GBP"

    return Compensation(
        interval=interval,
        min_amount=min_val,
        max_amount=max_val,
        currency=currency,
    )


def parse_salary_from_search(min_salary, max_salary, currency: str | None = None) -> Optional[Compensation]:
    """Fallback: build Compensation from search endpoint fields (no salaryType available)."""
    min_val = _to_float(min_salary)
    max_val = _to_float(max_salary)

    if min_val is None and max_val is None:
        return None

    # Without salaryType, guess interval from magnitude
    ref = min_val or max_val
    if ref < 350:
        interval = CompensationInterval.HOURLY
    elif ref < 1000:
        interval = CompensationInterval.DAILY
    else:
        interval = CompensationInterval.YEARLY

    return Compensation(
        interval=interval,
        min_amount=min_val,
        max_amount=max_val,
        currency=currency or "GBP",
    )


CONTRACT_TYPE_MAP = {
    "permanent": JobType.FULL_TIME,
    "contract": JobType.CONTRACT,
    "temporary": JobType.TEMPORARY,
}

JOB_TYPE_MAP = {
    "full time": JobType.FULL_TIME,
    "part time": JobType.PART_TIME,
}


def map_job_type_from_details(details: dict) -> list[JobType]:
    """Extract job types from Reed detail endpoint fields (jobType, contractType)."""
    types = []

    job_type_str = (details.get("jobType") or "").lower().strip()
    if job_type_str in JOB_TYPE_MAP:
        types.append(JOB_TYPE_MAP[job_type_str])

    contract_type_str = (details.get("contractType") or "").lower().strip()
    if contract_type_str in CONTRACT_TYPE_MAP:
        mapped = CONTRACT_TYPE_MAP[contract_type_str]
        if mapped not in types:
            types.append(mapped)

    return types


def map_job_type_from_search(job_data: dict) -> list[JobType]:
    """Fallback: extract job types from Reed search boolean fields."""
    types = []
    if job_data.get("fullTime"):
        types.append(JobType.FULL_TIME)
    if job_data.get("partTime"):
        types.append(JobType.PART_TIME)
    if job_data.get("contract"):
        types.append(JobType.CONTRACT)
    if job_data.get("temp"):
        types.append(JobType.TEMPORARY)
    return types


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None
