# Clef build routing (2026-10-06)

`jbr route` used local Clef-flash through the System One endpoint on this Mac.
The work packages and detailed prompts are in this directory. The actual
implementation followed these assignments:

| Package | Router pick | Implemented by | Result |
|---|---|---|---|
| WP01 — backend seam | agy Gemini (0.88) | agy Gemini | accepted after one fix round |
| WP02 — wire production rungs | agy Gemini (0.53) | agy Gemini | accepted |
| WP03 — evaluation harnesses | Claude subagent (0.39; agy Gemini 0.35) | Codex Astra, by owner override | accepted after one fix round |
| WP04 — docs and disclosure | Claude subagent (0.90) | completed locally after Claude quota refusal | smoke suite passes |

Each package's code went through fresh review and adversarial verification in
the build workflow, except WP04 after the Claude weekly quota refusal. Its
documentation was reviewed locally against Cloudflare's official model and
announcement pages. The integrated smoke suite passes 6,228 checks.
