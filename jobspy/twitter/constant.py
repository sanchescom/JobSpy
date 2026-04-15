"""Hashtag groups and regex patterns for parsing job tweets."""

# Hashtags appended to search queries by tech stack / category
HASHTAG_GROUPS = {
    "general": ["#hiring", "#jobs", "#jobalert", "#nowhiring"],
    "tech": ["#techjobs", "#devjobs", "#programmingjobs"],
    "php": ["#PHPjobs", "#LaravelJobs"],
    "python": ["#PythonJobs", "#DjangoJobs"],
    "js": ["#JavaScriptJobs", "#ReactJobs", "#NodeJobs"],
    "java": ["#JavaJobs"],
    "ruby": ["#RubyJobs", "#RailsJobs"],
    "go": ["#GoJobs", "#GolangJobs"],
    "rust": ["#RustJobs"],
    "devops": ["#DevOpsJobs", "#CloudJobs", "#AWSJobs"],
    "data": ["#DataJobs", "#DataScience", "#MLJobs"],
    "design": ["#DesignJobs", "#UXJobs", "#UIJobs"],
}

# Regex patterns for extracting a job title from tweet text
TITLE_PATTERNS = [
    r"(?:hiring|looking for|we need|open position|job opening|now hiring)[:\s]+(.+?)(?:\n|$)",
    r"^(.+?)\s+(?:needed|wanted|required)",
    r"(?:role|position)[:\s]+(.+?)(?:\n|$)",
    r"#hiring\s+(.+?)(?:\n|$|#)",
]

# Regex patterns for extracting location
LOCATION_PATTERNS = [
    r"(?:location|based in|located in|office in)[:\s]+(.+?)(?:\n|$|\|)",
    r"\U0001F4CD\s*(.+?)(?:\n|$)",  # 📍 emoji
    r"\U0001F30D\s*(.+?)(?:\n|$)",  # 🌍 emoji
]

# Regex patterns for extracting company name
COMPANY_PATTERNS = [
    r"(?:at|@|company)[:\s]+(.+?)(?:\n|$|\||#)",
    r"(?:join|work (?:at|for))\s+(.+?)(?:\n|$|\||#|!)",
]

# Keywords indicating remote work
REMOTE_KEYWORDS = [
    "remote",
    "wfh",
    "work from home",
    "work-from-home",
    "fully remote",
    "100% remote",
    "anywhere",
]

# Minimum tweet length to consider as a job posting
MIN_TWEET_LENGTH = 50
