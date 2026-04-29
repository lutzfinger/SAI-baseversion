---
prompt_id: people_of_interest_weekly_search_v1
version: "1"
description: Weekly OpenAI web search prompt for one monitored person of interest.
---
Research one public person of interest over roughly the last 7 days.

Return strict JSON only.

Use the provided identity details to avoid confusion with similarly named people.
Only include public, professional, safe-for-work developments that matter to a strategic operator, such as:
- major product or company announcements
- new articles, newsletters, interviews, podcasts, or talks
- funding, investing, research, policy, or leadership updates
- major role changes, launches, partnerships, or high-signal public commentary

Rules:
- If you are not confident you found the right person, set `identity_confidence` to `low` and return no findings.
- If there are no notable public updates in the period, set `no_notable_updates` to `true`.
- Keep `overall_summary` to at most 120 words.
- Keep each finding `summary` to at most 60 words.
- Prefer high-signal sources and avoid rumor or low-signal reposts.

The worker will provide the structured person identity and the current date window separately.
