import sys
sys.path.insert(0, '.')
from ops.db import Db
from ops import money
d = Db('data/ops.db', 'ops/migrations')
print('orders in hand', money.format(d.scalar(
    'SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand')))
print('forecast', money.format(d.scalar(
    "SELECT SUM(amount_cents) FROM claim_line WHERE status='forecast'")))
print('invoiced', money.format(d.scalar(
    "SELECT SUM(amount_cents) FROM claim_line WHERE status='invoiced'")))
d.close()
