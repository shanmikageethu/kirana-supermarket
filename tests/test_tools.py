import json
from tools.store_tools import *

def p(label, result):
    print(f"\n--- {label} ---")
    print(json.dumps(result, indent=2))

# New product
p("add_new_product Maggi", add_new_product(
    name="Maggi 70g", unit="packet", is_loose=False,
    cost_price=10, sell_price=14, mrp=14,
    hsn_code="19023090", gst_rate=12, initial_stock=0, reorder_level=10
))

# Receive stock
p("add_stock Maggi +50", add_stock("Maggi", 50))

# New loose product (0% GST) for oversell test
p("add_new_product Sugar", add_new_product(
    name="Sugar", unit="kg", is_loose=True,
    cost_price=40, sell_price=45, mrp=45,
    hsn_code="17019900", gst_rate=0, initial_stock=6, reorder_level=5
))

# Start a bill and add items
bill = start_bill()
p("start_bill", bill)
bill_id = bill["bill_id"]

p("add_bill_item 4 Maggi", add_bill_item(bill_id, "Maggi", 4))
item2 = add_bill_item(bill_id, "Sugar", 2)
p("add_bill_item 2kg sugar", item2)

# Edit mid-build: change Maggi qty to 6
maggi_item = get_bill_summary(bill_id)["items"][0]["bill_item_id"]
p("update Maggi qty to 6", update_bill_item_quantity(maggi_item, 6))

# Oversell guard: only 6kg sugar in stock, try to push total to 10kg
p("OVERSELL ATTEMPT: add 8 more kg sugar", add_bill_item(bill_id, "Sugar", 8))

p("bill summary before finalize", get_bill_summary(bill_id))

# Finalize
p("finalize_bill", finalize_bill(bill_id, "UPI", idempotency_key="update-1001"))

# Retry with same idempotency key (simulating Telegram redelivery)
p("RETRY finalize (same key)", finalize_bill(bill_id, "UPI", idempotency_key="update-1001"))

p("stock check after finalize", check_stock("Sugar"))

# Khata
p("add_khata_charge Ramesh 500", add_khata_charge("Ramesh", 500, "groceries on credit"))
p("record_khata_payment Ramesh 300", record_khata_payment("Ramesh", 300))
p("get_khata_balance Ramesh", get_khata_balance("Ramesh"))
p("payment for nonexistent customer", record_khata_payment("GhostCustomer", 100))

# Preferences
p("set_preference default_payment", set_preference("default_payment", "UPI"))
p("get_all_preferences", get_all_preferences())

# Daily summary
from datetime import date
p("daily summary", get_daily_summary(date.today().isoformat()))

# Low stock
p("low stock", get_low_stock())
