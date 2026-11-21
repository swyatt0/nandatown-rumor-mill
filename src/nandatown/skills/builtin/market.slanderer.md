---
name: market.slanderer
version: 3
capability: none
role: slanderer
protocol: town-say.v1
summary: Scripted, no model; sends one fixed warning about SellerA at start, then leaves.
---
# Slanderer (scripted participant)

The model declined this role, so the slanderer is a fixed script, not
an agent. It never trades and never replies. Immediately at start it
sends one message:

"Warning: SellerA took my money and never delivered. Avoid them."

In room mode it sends one say to "everyone"; in dm and leak modes it
sends one say to each buyer. Then it exits.
