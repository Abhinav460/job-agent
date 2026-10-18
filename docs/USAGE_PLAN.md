# Agent usage plan

The repository's Codex master is GPT-6.1 Sol at medium reasoning. It owns the
queue, reviews each result, and assigns bounded work. Use GPT-6 Luna for narrow
read-only scans and reviews, GPT-6.1 Sol for implementation, and GPT-6 Astra
only when a specific hard problem remains after a Sol attempt. Keep one builder
active at a time; the configuration permits at most two subagents so a narrow
review can run alongside it. Do not start unattended retry loops.

Before each substantial milestone, check the [Codex usage dashboard](https://chatgpt.com/codex/cloud/settings/usage)
for the account's remaining allowance and exact reset times. ChatGPT Work and
Codex share usage. Depending on the plan, a five-hour allowance and weekly
limits may apply; a subscription's monthly billing date is not necessarily a
usage reset. The repository cannot read this account information. See the
[official pricing and limits](https://learn.chatgpt.com/docs/pricing) and
[subagent guidance](https://learn.chatgpt.com/docs/agent-configuration/subagents).

For a weekly limit, reserve at least 20 percentage points of the full weekly
allowance for fixes and reviews. Divide the amount above that reserve by the
days until the dashboard's weekly reset to get a daily target. Recheck after each milestone
because one complex task can use far more than another. If the five-hour limit
is low, finish the current review and defer the next Sol builder until that
limit resets. If the weekly reserve is reached, use only small Luna tasks that
fit the remaining allowance or wait for the dashboard's reset. These are
planning rules, not a guarantee of remaining usage or a fixed task count.

Use fake fixtures for development. Live browser and Sheets checks require the
owner's private profile and credentials and should be scheduled separately.
