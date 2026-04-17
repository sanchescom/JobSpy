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


# Country detection — signals ordered loosely by reliability.
# Returned codes are ISO 3166-1 alpha-2 (2-letter uppercase).

# Flag emojis → ISO-2
FLAG_EMOJI_TO_CC = {
    "\U0001F1FA\U0001F1F8": "US",  # 🇺🇸
    "\U0001F1EC\U0001F1E7": "GB",  # 🇬🇧
    "\U0001F1EE\U0001F1EA": "IE",  # 🇮🇪
    "\U0001F1E8\U0001F1E6": "CA",  # 🇨🇦
    "\U0001F1E6\U0001F1FA": "AU",  # 🇦🇺
    "\U0001F1F3\U0001F1FF": "NZ",  # 🇳🇿
    "\U0001F1E9\U0001F1EA": "DE",  # 🇩🇪
    "\U0001F1EB\U0001F1F7": "FR",  # 🇫🇷
    "\U0001F1F3\U0001F1F1": "NL",  # 🇳🇱
    "\U0001F1EA\U0001F1F8": "ES",  # 🇪🇸
    "\U0001F1EE\U0001F1F9": "IT",  # 🇮🇹
    "\U0001F1F5\U0001F1F1": "PL",  # 🇵🇱
    "\U0001F1F5\U0001F1F9": "PT",  # 🇵🇹
    "\U0001F1E7\U0001F1EA": "BE",  # 🇧🇪
    "\U0001F1E8\U0001F1ED": "CH",  # 🇨🇭
    "\U0001F1E6\U0001F1F9": "AT",  # 🇦🇹
    "\U0001F1F8\U0001F1EA": "SE",  # 🇸🇪
    "\U0001F1F3\U0001F1F4": "NO",  # 🇳🇴
    "\U0001F1E9\U0001F1F0": "DK",  # 🇩🇰
    "\U0001F1EB\U0001F1EE": "FI",  # 🇫🇮
    "\U0001F1EE\U0001F1F3": "IN",  # 🇮🇳
    "\U0001F1F3\U0001F1F5": "NP",  # 🇳🇵
    "\U0001F1F5\U0001F1F0": "PK",  # 🇵🇰
    "\U0001F1E7\U0001F1E9": "BD",  # 🇧🇩
    "\U0001F1F1\U0001F1F0": "LK",  # 🇱🇰
    "\U0001F1F8\U0001F1EC": "SG",  # 🇸🇬
    "\U0001F1F2\U0001F1FE": "MY",  # 🇲🇾
    "\U0001F1EE\U0001F1E9": "ID",  # 🇮🇩
    "\U0001F1F5\U0001F1ED": "PH",  # 🇵🇭
    "\U0001F1F9\U0001F1ED": "TH",  # 🇹🇭
    "\U0001F1FB\U0001F1F3": "VN",  # 🇻🇳
    "\U0001F1EF\U0001F1F5": "JP",  # 🇯🇵
    "\U0001F1F0\U0001F1F7": "KR",  # 🇰🇷
    "\U0001F1E8\U0001F1F3": "CN",  # 🇨🇳
    "\U0001F1ED\U0001F1F0": "HK",  # 🇭🇰
    "\U0001F1F9\U0001F1FC": "TW",  # 🇹🇼
    "\U0001F1E6\U0001F1EA": "AE",  # 🇦🇪
    "\U0001F1F8\U0001F1E6": "SA",  # 🇸🇦
    "\U0001F1EE\U0001F1F1": "IL",  # 🇮🇱
    "\U0001F1F9\U0001F1F7": "TR",  # 🇹🇷
    "\U0001F1F7\U0001F1FA": "RU",  # 🇷🇺
    "\U0001F1FA\U0001F1E6": "UA",  # 🇺🇦
    "\U0001F1E7\U0001F1F7": "BR",  # 🇧🇷
    "\U0001F1E6\U0001F1F7": "AR",  # 🇦🇷
    "\U0001F1F2\U0001F1FD": "MX",  # 🇲🇽
    "\U0001F1E8\U0001F1F1": "CL",  # 🇨🇱
    "\U0001F1E8\U0001F1F4": "CO",  # 🇨🇴
    "\U0001F1FF\U0001F1E6": "ZA",  # 🇿🇦
    "\U0001F1EA\U0001F1EC": "EG",  # 🇪🇬
    "\U0001F1F3\U0001F1EC": "NG",  # 🇳🇬
    "\U0001F1F0\U0001F1EA": "KE",  # 🇰🇪
}

# Case-insensitive country name / alias → ISO-2 (word boundaries matched at call site)
COUNTRY_NAME_TO_CC = {
    "usa": "US", "u.s.a": "US", "u.s.": "US", "united states": "US", "america": "US",
    "uk": "GB", "u.k.": "GB", "united kingdom": "GB", "britain": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "ireland": "IE", "republic of ireland": "IE",
    "canada": "CA",
    "australia": "AU",
    "new zealand": "NZ",
    "germany": "DE", "deutschland": "DE",
    "france": "FR",
    "netherlands": "NL", "holland": "NL",
    "spain": "ES", "españa": "ES",
    "italy": "IT", "italia": "IT",
    "poland": "PL", "polska": "PL",
    "portugal": "PT",
    "belgium": "BE",
    "switzerland": "CH",
    "austria": "AT",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "india": "IN", "bharat": "IN",
    "pakistan": "PK",
    "nepal": "NP",
    "bangladesh": "BD",
    "sri lanka": "LK",
    "singapore": "SG",
    "malaysia": "MY",
    "indonesia": "ID",
    "philippines": "PH",
    "thailand": "TH",
    "vietnam": "VN",
    "japan": "JP",
    "south korea": "KR", "korea": "KR",
    "china": "CN",
    "hong kong": "HK",
    "taiwan": "TW",
    "uae": "AE", "united arab emirates": "AE", "dubai": "AE",
    "saudi arabia": "SA",
    "israel": "IL",
    "turkey": "TR", "türkiye": "TR",
    "russia": "RU",
    "ukraine": "UA",
    "brazil": "BR", "brasil": "BR",
    "argentina": "AR",
    "mexico": "MX", "méxico": "MX",
    "chile": "CL",
    "colombia": "CO",
    "south africa": "ZA",
    "egypt": "EG",
    "nigeria": "NG",
    "kenya": "KE",
}

# Major city → ISO-2 (unambiguous: avoid Birmingham, Manchester, Portland, etc. that exist in multiple countries)
CITY_TO_CC = {
    # US
    "new york": "US", "nyc": "US", "san francisco": "US", "sf": "US", "los angeles": "US", "la": "US",
    "seattle": "US", "austin": "US", "boston": "US", "chicago": "US", "denver": "US", "atlanta": "US",
    "miami": "US", "washington dc": "US", "d.c.": "US", "silicon valley": "US", "palo alto": "US",
    # UK
    "london": "GB", "edinburgh": "GB", "glasgow": "GB", "bristol": "GB", "cardiff": "GB", "belfast": "GB",
    # Ireland
    "dublin": "IE", "cork": "IE", "galway": "IE", "limerick": "IE",
    # Canada
    "toronto": "CA", "montreal": "CA", "vancouver": "CA", "ottawa": "CA", "calgary": "CA", "edmonton": "CA",
    # Australia
    "sydney": "AU", "melbourne": "AU", "brisbane": "AU", "perth": "AU", "adelaide": "AU", "canberra": "AU",
    # New Zealand
    "auckland": "NZ", "wellington": "NZ", "christchurch": "NZ",
    # Germany
    "berlin": "DE", "munich": "DE", "münchen": "DE", "hamburg": "DE", "frankfurt": "DE", "cologne": "DE", "köln": "DE", "stuttgart": "DE",
    # France
    "paris": "FR", "lyon": "FR", "marseille": "FR", "toulouse": "FR", "nice": "FR",
    # Netherlands
    "amsterdam": "NL", "rotterdam": "NL", "utrecht": "NL", "eindhoven": "NL", "the hague": "NL",
    # Spain
    "madrid": "ES", "barcelona": "ES", "valencia": "ES", "seville": "ES",
    # Italy
    "rome": "IT", "milan": "IT", "milano": "IT", "florence": "IT", "turin": "IT",
    # Poland
    "warsaw": "PL", "kraków": "PL", "krakow": "PL", "wrocław": "PL", "wroclaw": "PL", "gdańsk": "PL", "gdansk": "PL",
    # Others EU
    "lisbon": "PT", "porto": "PT",
    "brussels": "BE", "antwerp": "BE",
    "zurich": "CH", "geneva": "CH",
    "vienna": "AT", "wien": "AT",
    "stockholm": "SE", "gothenburg": "SE",
    "oslo": "NO",
    "copenhagen": "DK", "københavn": "DK",
    "helsinki": "FI",
    # India
    "mumbai": "IN", "bombay": "IN", "bangalore": "IN", "bengaluru": "IN", "delhi": "IN", "new delhi": "IN",
    "hyderabad": "IN", "chennai": "IN", "madras": "IN", "kolkata": "IN", "pune": "IN", "ahmedabad": "IN",
    "noida": "IN", "gurgaon": "IN", "gurugram": "IN",
    # SE Asia
    "singapore city": "SG",
    "kuala lumpur": "MY",
    "jakarta": "ID", "bandung": "ID",
    "manila": "PH", "cebu": "PH",
    "bangkok": "TH",
    "ho chi minh": "VN", "hanoi": "VN", "saigon": "VN",
    # East Asia
    "tokyo": "JP", "osaka": "JP", "kyoto": "JP",
    "seoul": "KR", "busan": "KR",
    "beijing": "CN", "shanghai": "CN", "shenzhen": "CN", "guangzhou": "CN",
    "hong kong": "HK",
    "taipei": "TW",
    # Middle East
    "dubai": "AE", "abu dhabi": "AE",
    "riyadh": "SA", "jeddah": "SA",
    "tel aviv": "IL", "jerusalem": "IL",
    "istanbul": "TR", "ankara": "TR",
    # LatAm
    "são paulo": "BR", "sao paulo": "BR", "rio de janeiro": "BR", "rio": "BR",
    "buenos aires": "AR",
    "mexico city": "MX", "ciudad de méxico": "MX", "cdmx": "MX",
    "santiago": "CL",
    "bogotá": "CO", "bogota": "CO",
    # Others
    "cape town": "ZA", "johannesburg": "ZA",
    "cairo": "EG",
    "lagos": "NG",
    "nairobi": "KE",
}

# Currency symbols/codes → ISO-2 where unambiguous (skip USD/CAD/AUD ambiguity)
CURRENCY_TO_CC = {
    "£": "GB", "gbp": "GB",
    "€": "EU",  # ambiguous — resolved only if combined with city/country
    "₹": "IN", "inr": "IN",
    "npr": "NP",
    # "rs." / "rs " intentionally dropped — too aggressive. Matches "engineers ",
    # "Mrs.", "developers ", etc. Nepal still detected via flag / country name.
    "₩": "KR", "krw": "KR",
    "¥": "JP",  # could be CNY too — weak
    "jpy": "JP", "cny": "CN", "rmb": "CN",
    "chf": "CH", "sek": "SE", "nok": "NO", "dkk": "DK",
    "pln": "PL", "aed": "AE", "sar": "SA", "ils": "IL",
    "try": "TR", "brl": "BR", "mxn": "MX", "ars": "AR",
    "zar": "ZA", "idr": "ID", "thb": "TH", "vnd": "VN", "sgd": "SG", "myr": "MY",
}

# Phone country calling-code prefix → ISO-2 (only unambiguous codes)
PHONE_PREFIX_TO_CC = {
    "+1": None,  # US/CA — ambiguous
    "+44": "GB",
    "+353": "IE",
    "+49": "DE",
    "+33": "FR",
    "+34": "ES",
    "+39": "IT",
    "+31": "NL",
    "+32": "BE",
    "+41": "CH",
    "+43": "AT",
    "+46": "SE",
    "+47": "NO",
    "+45": "DK",
    "+358": "FI",
    "+48": "PL",
    "+351": "PT",
    "+61": "AU",
    "+64": "NZ",
    "+91": "IN",
    "+92": "PK",
    "+977": "NP",
    "+880": "BD",
    "+94": "LK",
    "+65": "SG",
    "+60": "MY",
    "+62": "ID",
    "+63": "PH",
    "+66": "TH",
    "+84": "VN",
    "+81": "JP",
    "+82": "KR",
    "+86": "CN",
    "+852": "HK",
    "+886": "TW",
    "+971": "AE",
    "+966": "SA",
    "+972": "IL",
    "+90": "TR",
    "+7": "RU",
    "+380": "UA",
    "+55": "BR",
    "+54": "AR",
    "+52": "MX",
    "+56": "CL",
    "+57": "CO",
    "+27": "ZA",
    "+20": "EG",
    "+234": "NG",
    "+254": "KE",
}

# URL / hashtag TLD / domain token → ISO-2
TLD_TO_CC = {
    ".co.uk": "GB", ".uk": "GB",
    ".ie": "IE",
    ".ca": "CA",
    ".com.au": "AU", ".au": "AU",
    ".co.nz": "NZ", ".nz": "NZ",
    ".de": "DE",
    ".fr": "FR",
    ".nl": "NL",
    ".es": "ES",
    ".it": "IT",
    ".pt": "PT",
    ".be": "BE",
    ".ch": "CH",
    ".at": "AT",
    ".se": "SE",
    ".no": "NO",
    ".dk": "DK",
    ".fi": "FI",
    ".pl": "PL",
    ".in": "IN", ".co.in": "IN",
    ".pk": "PK",
    ".np": "NP",
    ".bd": "BD",
    ".lk": "LK",
    ".sg": "SG", ".com.sg": "SG",
    ".my": "MY", ".com.my": "MY",
    ".id": "ID",
    ".ph": "PH",
    ".th": "TH", ".co.th": "TH",
    ".vn": "VN",
    ".jp": "JP", ".co.jp": "JP",
    ".kr": "KR", ".co.kr": "KR",
    ".cn": "CN",
    ".hk": "HK",
    ".tw": "TW",
    ".ae": "AE",
    ".sa": "SA",
    ".il": "IL",
    ".tr": "TR", ".com.tr": "TR",
    ".ru": "RU",
    ".ua": "UA",
    ".br": "BR", ".com.br": "BR",
    ".ar": "AR",
    ".mx": "MX",
    ".cl": "CL",
    # ".co" intentionally omitted — Twitter's t.co shortener gives too many
    # false positives. Colombia is still detected via city/country-name scans.
    ".za": "ZA", ".co.za": "ZA",
    ".ng": "NG",
    ".eg": "EG",
    ".ke": "KE",
}
