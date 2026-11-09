"""Tool schemas. The first sentence of each description is at most 60 characters."""

RECENT = {
    "name": "us_filings_recent",
    "description": "List recent SEC EDGAR filings for a ticker or CIK. Each row has the form, such as a 10-K, 10-Q, or 8-K, the filing date, the accession number, and the document location on EDGAR. This tool is read only. It does not return financial figures.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ticker": {"type": "string", "description": "US ticker such as AAPL. Optional when cik is set."},
            "cik": {"type": "string", "description": "SEC CIK digits. Optional when ticker is set."},
            "limit": {"type": "integer", "description": "How many rows to return. Default 20. Maximum 40."},
            "forms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional SEC form names such as 10-K. Empty means every form.",
            },
        },
    },
}

LOCATE = {
    "name": "us_filings_locate",
    "description": "Find one SEC EDGAR filing location by accession. Pass a ticker or CIK with the accession number. The result is the EDGAR index page and the primary document. This tool is read only.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "accession": {"type": "string", "description": "Accession number, for example 0000320193-26-000026."},
            "ticker": {"type": "string", "description": "US ticker. Optional when cik or a saved watch already has the filing."},
            "cik": {"type": "string", "description": "SEC CIK digits. Optional when ticker is set."},
        },
        "required": ["accession"],
    },
}

WATCH = {
    "name": "us_filings_watch",
    "description": "Watch a company for new SEC EDGAR filings. Add or remove a ticker, or list the companies already watched each morning. Add and remove ask before they write the morning script or start hermes cron. The morning line names the form, such as a 10-K, and the accession number.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "ticker": {"type": "string", "description": "US ticker such as AAPL."},
            "cik": {"type": "string", "description": "SEC CIK digits."},
            "hour": {"type": "integer", "description": "Hour 0 through 23. Required for add. The schedule is that hour every day."},
            "deliver": {"type": "string", "description": "Required for add. local, origin, or a platform target you choose."},
            "forms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional form filter such as 10-K or 8-K. Empty means every form, including Form 4.",
            },
        },
        "required": ["action"],
    },
}

ALL_SCHEMAS = [RECENT, LOCATE, WATCH]
