---
name: us-filings
description: "SEC EDGAR filings for a watched US company. Before web_search, use tool_search and tool_call us_filings_recent, us_filings_locate, or us_filings_watch. Copy next_step. Do not invent a filing or a figure."
---

For this kind of request, use tool_search before web_search, then call the tool with tool_call.

Tools: `us_filings_recent`, `us_filings_locate`, `us_filings_watch`.

`us_filings_recent` lists filings for one ticker or one CIK. `us_filings_locate` needs an accession number. `us_filings_watch` adds or removes a company, or lists the companies already watched.

Before the user approves an add or a remove, the approval message must show the ticker or CIK, the hour, the deliver target, the forms when any were passed, and the full `hermes cron` command when a cron command will run. The card command field shows the tool name `us_filings_watch`. The handler runs the same cron arguments that the message shows.

If `next_step` is present, copy it. Do not invent a filing, an accession number, a document URL, or a figure. This plugin does not read financial amounts.

The archive directory is the subject company CIK. Do not build a URL from the accession prefix.
