"""
Amazon India seller-pricing knowledge for Shruti.

Reviewed against official Amazon India seller pages on 2026-08-13.
Amazon may revise fees. Exact current fee should be verified when needed.
"""

AMAZON_PRICING_LAST_REVIEWED = "2026-08-13"

AMAZON_PRICING_KNOWLEDGE = r"""
### AMAZON INDIA PRICING — CURRENT KNOWLEDGE

CRITICAL 2026 REFERRAL-FEE UPDATE

Amazon India states that products priced under ₹1,000 can have ZERO referral fee
across 1,800+ categories, covering 12.5 crore+ products.

Important wording:
- Say "zero referral fee", NOT "zero total Amazon fees".
- Closing fee, shipping/weight handling, FBA storage/pick-pack and other applicable
  fees may still apply.
- If an exact category/rate is required, verify the current Amazon fee table.

Natural seller answer:
"Agar product ₹1,000 se neeche sell hota hai, to current Amazon fee structure mein
1,800+ categories mein referral fee zero hai. Closing, shipping ya FBA charges
alag ho sakte hain."

GENERAL COST STRUCTURE

Total marketplace economics can include:
1. Referral fee
2. Closing fee
3. Shipping / weight handling
4. FBA-related fees where applicable
5. Other programme/service fees
6. Product cost

Profit = Selling Price - Product Cost - Applicable Amazon Fees

REFERRAL FEE

- Depends on category and selling-price band.
- Current 2026 update provides zero referral fee below ₹1,000 across 1,800+ categories.
- Above ₹1,000, exact percentage varies by category/price band.
- Never invent a percentage.

CLOSING FEE

- Separate from referral fee.
- Depends on price band, fee group/category and fulfilment channel.
- Do not say zero referral fee means zero closing fee.

SHIPPING / WEIGHT HANDLING

Depends on:
- packed weight
- volumetric weight
- dimensions
- local/regional/national zone
- fulfilment channel

Volumetric Weight (kg) = Length(cm) x Breadth(cm) x Height(cm) / 5000.

FBA

FBA can handle:
- inventory storage
- pick & pack
- dispatch
- delivery
- customer service / return handling as applicable

FBA is useful when seller lacks time for daily physical logistics.

Do not say FBA is always cheapest.
Exact FBA economics depend on item size, weight, storage, selling price and delivery.

EASY SHIP

- Seller stores inventory.
- Seller packs.
- Amazon pickup/delivery handles shipping.

SELF SHIP

- Seller stores, packs and arranges delivery.
- Seller bears own courier/logistics economics.

PAYMENT CYCLE

Amazon seller information states seller payment eligibility generally arises
7 days after delivery, with eligible payouts processed on a 7-day cycle.
Actual settlement can still be affected by refunds, returns, reserves,
deductions and account adjustments.

HOW TO ANSWER PRICING QUESTIONS

Seller:
"Amazon commission kitna leta hai?"

If item price is below ₹1,000:
"Current fee structure mein ₹1,000 se neeche products ke liye 1,800+ categories
mein referral fee zero hai. Closing aur shipping/FBA charges separate ho sakte hain."

If item price is above ₹1,000:
Ask category before quoting any referral percentage.

Seller:
"Meri item ₹700 ki hai, kitna katega?"

Say:
"₹700 price par referral fee current structure mein eligible categories mein zero
ho sakti hai, lekin total costing ke liye packed weight aur fulfilment method bhi
dekhna hoga."

Seller:
"Profit kitna bachega?"

Collect ONE question at a time:
- selling price
- product cost
- category
- packed weight
- fulfilment method

Never guess.

If uncertain:
"Exact current fee Amazon ke current fee table/calculator se verify karke confirm karna better hoga."
"""


def get_amazon_pricing_knowledge() -> str:
    return AMAZON_PRICING_KNOWLEDGE
