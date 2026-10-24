# Design decisions (written by me)

Write each entry in your own words after you understand the code. Interviewers ask
"why", and this file is your answer sheet. Keep entries short.

Template:

```
## <date> · <decision>
What: ...
Why: ...
Alternative I considered: ... and why I did not pick it
What I'd change at 10x scale: ...
```

Suggested first entries (read docs/architecture.md, then write these yourself):

- [ ] Why the API returns 202 instead of running the investigation inline
- [ ] How FOR UPDATE SKIP LOCKED stops two workers from taking the same job
- [ ] What a lease and fencing protect against
- [ ] Why MCP servers instead of calling functions directly from the agent
- [ ] Why the agent loop has hard budgets
- [ ] How the grounding check prevents hallucinated evidence
- [ ] Why the agent is read-only
- [ ] Why GitHub OIDC + SSM instead of SSH keys for deploys
