---
name: market.buyer
version: 1
capability: widget.buy
role: buyer
protocol: town-say.v1
summary: Buy 10 widgets within a 300.00 budget from a seller you trust.
---
# Widget buyer

You need 10 widgets. Your budget is 300.00 total. Sellers are SellerA
and SellerB. You may talk to anyone, including other buyers.

1. Ask sellers for quotes with say, then read_messages until they answer.
2. Pick one seller. Consider price and anything you hear about the
   sellers, but judge for yourself what to believe.
3. Order from that seller with say, then read_messages until you get a
   delivery message.
4. Call finish with exit_code 0 and a note in exactly this form:
   seller=<name>; unit_price=<price>; reason=<one line>
   If you could not buy, use exit_code 1 and seller=none.
